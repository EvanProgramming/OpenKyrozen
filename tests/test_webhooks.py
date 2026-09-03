import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from fastapi.testclient import TestClient

import server


class _WebhookReceiver:
    def __init__(self):
        self.bodies = []
        self._lock = threading.Lock()
        self.httpd = None
        self.thread = None

    def __enter__(self):
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - stdlib HTTP server contract
                length = int(self.headers.get("content-length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                with receiver._lock:
                    receiver.bodies.append(payload)
                self.send_response(204)
                self.end_headers()

            def log_message(self, *_args):
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_port}/hook"

    def wait_for(self, count):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.bodies) >= count:
                    return
            time.sleep(0.01)

    def __exit__(self, *_args):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


class WebhookEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.original_webhooks = server._webhooks
        server._webhooks = []

    def tearDown(self):
        server._webhooks = self.original_webhooks

    def _use_receiver(self, receiver):
        server._webhooks.append({
            "url": receiver.url,
            "events": ["chat.completed"],
        })

    def test_non_stream_chat_emits_one_bounded_redacted_completion(self):
        with _WebhookReceiver() as receiver:
            self._use_receiver(receiver)
            reply = "secret=sk-live-should-not-leak " + ("usable reply " * 100)
            with patch.object(server, "_run_session_chat", return_value=reply):
                response = self.client.post("/api/chat", json={
                    "message": "hello",
                    "session_id": "webhook-non-stream",
                    "profile": "coder",
                })

            self.assertEqual(response.status_code, 200, response.text)
            receiver.wait_for(1)
            self.assertEqual(len(receiver.bodies), 1)
            event = receiver.bodies[0]
            self.assertEqual(event["event"], "chat.completed")
            data = event["data"]
            self.assertEqual(data["actor"], server._SERVER_ACTOR_ID)
            self.assertEqual(data["session_id"], "webhook-non-stream")
            self.assertEqual(data["profile"], "coder")
            self.assertFalse(data["streamed"])
            self.assertEqual(data["reply_length"], len(reply))
            self.assertLessEqual(len(data["reply_summary"]), 500)
            self.assertNotIn("sk-live-should-not-leak", data["reply_summary"])

    def test_non_stream_chat_error_emits_no_completion(self):
        with _WebhookReceiver() as receiver:
            self._use_receiver(receiver)
            with patch.object(server, "_run_session_chat", side_effect=RuntimeError("provider down")):
                response = self.client.post("/api/chat", json={
                    "message": "hello", "session_id": "webhook-error",
                })
            self.assertEqual(response.status_code, 500)
            self.assertEqual(receiver.bodies, [])

    def test_stream_completion_emits_exactly_one_event_after_done(self):
        with _WebhookReceiver() as receiver:
            self._use_receiver(receiver)
            with patch.object(server, "_run_session_chat", return_value="streamed reply"):
                response = self.client.post("/api/chat/stream", json={
                    "message": "hello", "session_id": "webhook-stream",
                    "profile": "researcher",
                })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("data: [DONE]", response.text)
            receiver.wait_for(1)
            self.assertEqual(len(receiver.bodies), 1)
            event = receiver.bodies[0]
            self.assertEqual(event["event"], "chat.completed")
            self.assertTrue(event["data"]["streamed"])
            self.assertEqual(event["data"]["reply_summary"], "streamed reply")

    def test_stream_error_emits_no_completion(self):
        with _WebhookReceiver() as receiver:
            self._use_receiver(receiver)
            with patch.object(server, "_run_session_chat", side_effect=RuntimeError("provider down")):
                response = self.client.post("/api/chat/stream", json={
                    "message": "hello", "session_id": "webhook-stream-error",
                })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn('"error": "provider down"', response.text)
            self.assertNotIn("data: [DONE]", response.text)
            self.assertEqual(receiver.bodies, [])

    def test_webhook_failure_is_audited_without_breaking_chat(self):
        server._webhooks.append({
            "url": "https://example.com/hook",
            "events": ["chat.completed"],
        })
        with patch("requests.post", side_effect=OSError("offline")), \
             patch.object(server, "_audit") as audit, \
             patch.object(server, "_run_session_chat", return_value="reply"):
            response = self.client.post("/api/chat", json={
                "message": "hello", "session_id": "webhook-failure",
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(any(call.args[0] == "WEBHOOK_FAILURE" for call in audit.call_args_list))


if __name__ == "__main__":
    unittest.main()
