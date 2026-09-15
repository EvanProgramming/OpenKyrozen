import io
import json
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import tui_backend


class TUIProtocolTests(unittest.TestCase):
    def setUp(self):
        self.backend = tui_backend.Backend()
        self.output = io.StringIO()
        self.backend._output = self.output
        self.addCleanup(self.backend.stop)

    def test_validation_rejects_malformed_shape_and_oversized_text(self):
        payload, error = self.backend.validate(["submit"])
        self.assertIsNone(payload)
        self.assertIn("object", error)
        payload, error = self.backend.validate({"command": "submit", "text": "x" * 12001})
        self.assertIsNone(payload)
        self.assertIn("too long", error)

    def test_emit_is_one_json_object_per_line_and_redacts_secrets(self):
        self.backend.emit("response", "r1", text="token=should-not-leak")
        line = self.output.getvalue().strip()
        event = json.loads(line)
        self.assertEqual(event["event"], "response")
        self.assertEqual(event["request_id"], "r1")
        self.assertNotIn("should-not-leak", line)
        self.assertEqual(event["text"], "token=<redacted>")

        self.backend.emit("status", "r2", credentials={"api_key": "sk-live-secret"})
        self.assertNotIn("sk-live-secret", self.output.getvalue())

    def test_approval_handshake_correlates_request_and_denial(self):
        result = {}

        def wait_for_approval():
            result["approved"] = self.backend._approval("git_push", "origin main")

        thread = threading.Thread(target=wait_for_approval)
        thread.start()
        deadline = time.monotonic() + 1
        while "request_id" not in self.output.getvalue() and time.monotonic() < deadline:
            time.sleep(0.005)
            self.assertTrue(thread.is_alive())
        self.assertIn("request_id", self.output.getvalue())
        prompt = json.loads(self.output.getvalue().splitlines()[0])
        self.backend.approval_response({
            "command": "approval_response",
            "request_id": prompt["request_id"],
            "approved": False,
        })
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertFalse(result["approved"])

    def test_stream_projection_preserves_event_order_and_redacts_receipts(self):
        callback = self.backend._stream_projection("turn-1")
        callback({"event": "content", "chunk": "Answer"})
        callback({"event": "tool_receipt", "tool_receipt": {"action": "read_file", "result": "ok"}})
        callback({"event": "tasks", "tasks": [{"id": "task-1", "status": "succeeded"}]})
        callback({"event": "model_complete"})
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual([event["event"] for event in events], ["stream_delta", "tool_receipt", "tasks"])
        self.assertEqual(events[0]["request_id"], "turn-1")

    def test_provider_setup_emits_ready_and_masks_key_flow(self):
        config = tui_backend.agent.ProviderConfig(provider="deepseek")
        with patch.object(tui_backend.agent, "_provider_config", config), \
                patch.object(tui_backend.agent, "_get_workspace_root", return_value=Path("/tmp/workspace")), \
                patch.object(tui_backend.agent, "save_provider_config_encrypted"), \
                patch.object(tui_backend.agent, "_prompt_and_init_deepseek", return_value=False):
            self.backend.set_api_key("sk-test-secret", "key-1")
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual(events[0]["event"], "ready")
        self.assertEqual(events[0]["request_id"], "key-1")
        self.assertEqual(events[1]["event"], "status")
        self.assertNotIn("sk-test-secret", self.output.getvalue())

    def test_approval_response_requires_a_correlated_request_id(self):
        payload, error = self.backend.validate({"command": "approval_response", "approved": True})
        self.assertIsNone(payload)
        self.assertIn("request_id", error)


if __name__ == "__main__":
    unittest.main()
