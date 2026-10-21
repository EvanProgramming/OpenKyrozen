import json
import tempfile
import threading
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
        received = self.received
        remaining_at_forward = self.remaining_at_forward
        delimiter = self.stream_delimiter
        budget_ref = {}

        class Upstream(BaseHTTPRequestHandler):
            usage = {"input_tokens": 12, "output_tokens": 4,
                     "input_tokens_details": {"cached_tokens": 3},
                     "cache_creation_input_tokens": 2}

            def log_message(self, *_args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append(request)
                remaining_at_forward.append(budget_ref["ledger"].remaining_rmb)
                if request.get("stream"):
                    payload = ("event: response.completed\ndata: " + json.dumps({
                        "type": "response.completed", "response": {
                            "id": "response-fixture", "output": [], "usage": self.usage,
                        },
                    }) + delimiter[0]).encode()
                else:
                    payload = json.dumps({"id": "response-fixture", "output": [], "usage": self.usage}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if request.get("stream") else "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
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
        self.assertAlmostEqual(float(self.budget.spent_rmb), 0.0000204)
        self.assertEqual(self.relay.rows[0]["status"], "settled")
        self.assertEqual(self.relay.rows[0]["usage"]["cache_read_tokens"], 3)
        self.assertEqual(self.relay.rows[0]["usage"]["cache_write_tokens"], 2)

    def test_rejects_unauthenticated_local_requests_without_spending(self):
        response = requests.post(self.relay.url + "/responses", json={"input": []}, timeout=3)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.received, [])
        self.assertEqual(self.budget.spent_rmb, 0)

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
