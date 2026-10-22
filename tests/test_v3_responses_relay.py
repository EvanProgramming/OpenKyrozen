import json
import socket
import struct
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from benchmarks.v3_matched import BudgetLedger
from benchmarks.v3_responses_relay import MODEL, MAX_OUTPUT_TOKENS, ResponsesRelay
from openkyrozen.providers import ProviderConfig
from openkyrozen.providers.openai import OpenAIResponsesProvider


class V3ResponsesRelayTests(unittest.TestCase):
    def setUp(self):
        self.received = []
        self.remaining_at_forward = []
        self.stream_delimiter = ["\n\n"]
        self.stream_terminal_type = ["response.completed"]
        self.stream_control = {"enabled": False, "first_entered": threading.Event(),
                               "release_first": threading.Event(), "second_arrived": threading.Event()}
        received = self.received
        remaining_at_forward = self.remaining_at_forward
        delimiter = self.stream_delimiter
        stream_terminal_type = self.stream_terminal_type
        stream_control = self.stream_control
        budget_ref = {}

        class Upstream(BaseHTTPRequestHandler):
            usage = {"input_tokens": 12, "output_tokens": 4,
                     "input_tokens_details": {"cached_tokens": 3},
                     "cache_creation_input_tokens": 2}
            response_payload = None
            http_status = 200

            def log_message(self, *_args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append(request)
                if request.get("stream") and stream_control["enabled"]:
                    if len(received) == 1:
                        stream_control["first_entered"].set()
                    else:
                        stream_control["second_arrived"].set()
                remaining_at_forward.append(budget_ref["ledger"].remaining_rmb)
                if request.get("stream"):
                    payload = ("event: response.completed\ndata: " + json.dumps({
                        "type": stream_terminal_type[0], "response": {
                            "id": "response-fixture", "output": [], "usage": self.usage,
                        },
                    }) + delimiter[0]).encode()
                else:
                    payload = json.dumps({"id": "response-fixture", "output": [], "usage": self.usage}).encode()
                    if self.response_payload is not None:
                        payload = self.response_payload
                self.send_response(self.http_status)
                self.send_header("Content-Type", "text/event-stream" if request.get("stream") else "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if request.get("stream") and stream_control["enabled"] and len(received) == 1:
                    stream_control["release_first"].wait(3)
                self.wfile.write(payload)

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        self.tempdir = tempfile.TemporaryDirectory()
        self.budget = BudgetLedger(Path(self.tempdir.name) / "budget.sqlite3", limit_rmb="0.1")
        budget_ref["ledger"] = self.budget
        self.relay = ResponsesRelay(
            endpoint=f"http://127.0.0.1:{self.upstream.server_port}/v1",
            api_key="test-upstream-key", budget=self.budget,
        )
        self.relay.start()

    def tearDown(self):
        self.relay.close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join()
        self.tempdir.cleanup()

    def test_reserves_before_forwarding_and_forces_matched_responses_settings(self):
        response = requests.post(self.relay.url + "/responses", json={
            "model": "wrong-model", "input": [{"role": "user", "content": "fixture"}],
            "stream": True,
        }, headers={"Authorization": "Bearer " + self.relay.client_key}, timeout=3)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.received[0]["model"], MODEL)
        self.assertEqual(self.received[0]["max_output_tokens"], MAX_OUTPUT_TOKENS)
        self.assertTrue(self.received[0]["stream"])
        self.assertIn("event: response.completed", response.text)
        self.assertLess(self.remaining_at_forward[0], self.budget.limit_picormb / 1_000_000_000_000)
        self.assertAlmostEqual(float(self.budget.spent_rmb), 0.000001385)
        self.assertEqual(self.relay.rows[0]["status"], "settled")
        self.assertEqual(self.relay.rows[0]["usage"]["cache_read_tokens"], 3)
        self.assertEqual(self.relay.rows[0]["usage"]["cache_write_tokens"], 2)

    def test_rejects_unauthenticated_local_requests_without_spending(self):
        response = requests.post(self.relay.url + "/responses", json={"input": []}, timeout=3)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.received, [])
        self.assertEqual(self.budget.spent_rmb, 0)

    def test_upstream_error_holds_reservation_and_disables_further_forwarding(self):
        self.upstream.RequestHandlerClass.http_status = 400
        headers = {"Authorization": "Bearer " + self.relay.client_key}
        response = requests.post(self.relay.url + "/responses", json={
            "model": MODEL, "input": [{"role": "user", "content": "fixture"}],
        }, headers=headers, timeout=3)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.relay.provider_error, "upstream_http_400")
        self.assertEqual(self.relay.rows[0]["status"], "unknown")
        remaining = self.budget.remaining_rmb
        blocked = requests.post(self.relay.url + "/responses", json={
            "model": MODEL, "input": [{"role": "user", "content": "retry"}],
        }, headers=headers, timeout=3)
        self.assertEqual(blocked.status_code, 503)
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.budget.remaining_rmb, remaining)

    def test_upstream_transport_error_disables_further_forwarding(self):
        headers = {"Authorization": "Bearer " + self.relay.client_key}
        original_post = requests.post
        calls = []

        def fail_transport(*_args, **_kwargs):
            calls.append(True)
            raise requests.ConnectionError("private upstream detail")

        requests.post = fail_transport
        try:
            response = requests.Session().post(self.relay.url + "/responses", json={
                "input": [{"role": "user", "content": "fixture"}],
            }, headers=headers, timeout=3)
        finally:
            requests.post = original_post

        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.relay.provider_error, "upstream_transport_ConnectionError")
        self.assertNotIn("private upstream detail", self.relay.provider_error)
        self.assertEqual(self.relay.rows[0]["status"], "unknown")
        self.assertEqual(self.relay.rows[0]["transport_error"], "ConnectionError")
        blocked = requests.post(self.relay.url + "/responses", json={"input": []},
                                headers=headers, timeout=3)
        self.assertEqual(blocked.status_code, 503)
        self.assertEqual(len(calls), 1)

    def test_provider_failed_stream_event_disables_subsequent_requests(self):
        self.stream_terminal_type[0] = "response.failed"
        headers = {"Authorization": "Bearer " + self.relay.client_key}
        response = requests.post(self.relay.url + "/responses", json={
            "input": [{"role": "user", "content": "fixture"}], "stream": True,
        }, headers=headers, timeout=3)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.relay.provider_error, "upstream_response_failed")
        self.assertEqual(self.relay.rows[0]["status"], "settled")
        blocked = requests.post(self.relay.url + "/responses", json={"input": []},
                                headers=headers, timeout=3)
        self.assertEqual(blocked.status_code, 503)
        self.assertEqual(len(self.received), 1)

    def test_concurrent_requests_do_not_bypass_provider_fail_stop(self):
        entered = threading.Event()
        release = threading.Event()
        original = self.upstream.RequestHandlerClass.do_POST
        handler_class = self.upstream.RequestHandlerClass
        handler_class.http_status = 400

        def delayed_error(handler):
            entered.set()
            release.wait(3)
            original(handler)

        handler_class.do_POST = delayed_error
        headers = {"Authorization": "Bearer " + self.relay.client_key}
        responses = []

        def request():
            responses.append(requests.post(self.relay.url + "/responses", json={
                "input": [{"role": "user", "content": "fixture"}],
            }, headers=headers, timeout=5))

        try:
            first = threading.Thread(target=request)
            second = threading.Thread(target=request)
            first.start()
            self.assertTrue(entered.wait(2))
            second.start()
            time.sleep(0.1)
            release.set()
            first.join(5)
            second.join(5)
        finally:
            release.set()
            handler_class.do_POST = original

        self.assertEqual(sorted(response.status_code for response in responses), [400, 503])
        self.assertEqual(len(self.received), 1)

    def test_streaming_responses_do_not_serialize_independent_requests(self):
        control = self.stream_control
        control["enabled"] = True
        responses = []
        headers = {"Authorization": "Bearer " + self.relay.client_key}

        def request():
            response = requests.post(self.relay.url + "/responses", json={
                "input": [{"role": "user", "content": "fixture"}], "stream": True,
            }, headers=headers, timeout=5)
            responses.append((response.status_code, response.content))

        first = threading.Thread(target=request)
        second = threading.Thread(target=request)
        first.start()
        self.assertTrue(control["first_entered"].wait(2))
        second.start()
        second_arrived_before_release = control["second_arrived"].wait(2)
        control["release_first"].set()
        first.join(5)
        second.join(5)

        self.assertTrue(second_arrived_before_release)
        self.assertEqual(sorted(status for status, _ in responses), [200, 200])
        self.assertEqual(len(self.received), 2)

    def test_local_client_disconnect_does_not_latch_provider_failure(self):
        handler_class = self.upstream.RequestHandlerClass
        original_payload = handler_class.response_payload
        handler_class.response_payload = json.dumps({"output": "x" * 8_000_000,
                                                     "usage": handler_class.usage}).encode()
        body = json.dumps({"input": [{"role": "user", "content": "fixture"}]}).encode()
        client = socket.create_connection(("127.0.0.1", self.relay._server.server_port), timeout=3)
        try:
            client.sendall((f"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                            f"Authorization: Bearer {self.relay.client_key}\r\n"
                            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        finally:
            client.close()

        deadline = time.monotonic() + 5
        while (not self.relay.rows or "latency_ms" not in self.relay.rows[0]) and time.monotonic() < deadline:
            time.sleep(0.01)
        handler_class.response_payload = original_payload
        self.assertEqual(self.relay.rows[0]["status"], "settled")
        self.assertIsNone(self.relay.provider_error)

    def test_preserves_nonstreaming_native_tool_request_shape(self):
        response = requests.post(self.relay.url + "/responses", json={
            "model": "wrong-model", "input": [{"role": "user", "content": "fixture"}],
        }, headers={"Authorization": "Bearer " + self.relay.client_key}, timeout=3)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.received[0]["stream"])
        self.assertEqual(response.json()["id"], "response-fixture")
        self.assertEqual(self.relay.rows[0]["status"], "settled")

    def test_openai_responses_provider_uses_the_authenticated_relay(self):
        provider = OpenAIResponsesProvider(ProviderConfig(
            provider="openai", api_key=self.relay.client_key, base_url=self.relay.url,
            model_simple=MODEL, model_complex=MODEL,
        ))
        response = provider.chat_response([{"role": "user", "content": "fixture"}])

        self.assertEqual(response.text, "")
        self.assertEqual(response.usage["prompt_tokens"], 12)
        self.assertEqual(self.received[0]["model"], MODEL)
        self.assertFalse(self.received[0]["stream"])

    def test_stream_usage_is_settled_for_crlf_event_boundaries(self):
        self.stream_delimiter[0] = "\r\n\r\n"
        response = requests.post(self.relay.url + "/responses", json={
            "input": [{"role": "user", "content": "fixture"}], "stream": True,
        }, headers={"Authorization": "Bearer " + self.relay.client_key}, timeout=3)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.relay.rows[0]["status"], "settled")
        self.assertEqual(self.relay.rows[0]["usage"]["input_tokens"], 12)


if __name__ == "__main__":
    unittest.main()
