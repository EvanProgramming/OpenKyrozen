import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import tui_backend
from interaction import InteractionController
from event_store import EventStore


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

    def test_provider_shaped_plan_json_is_not_streamed_as_chat_text(self):
        callback = self.backend._stream_projection("turn-plan")
        callback({"event": "content", "chunk": '{"mode":"plan","plan_name":"Safe",'})
        callback({"event": "content", "chunk": '"overview":"Wait","steps":[]}'})
        callback({"event": "model_complete"})
        self.assertEqual(self.output.getvalue(), "")

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

    def test_start_launches_detached_learning_worker_when_provider_is_ready(self):
        context = type("Context", (), {
            "active_root": Path("/tmp/openkyrozen-tui-workspace"),
            "is_global": True,
        })()
        config = type("Config", (), {
            "provider": "deepseek",
            "model_simple": "deepseek-v4-flash",
        })()
        plugin = type("Plugin", (), {"load_once": lambda self: None})()
        with patch.object(tui_backend.agent, "configure_launch_context", return_value=context), \
                patch.object(tui_backend.agent, "detect_provider", return_value=config), \
                patch.object(tui_backend.agent, "_prompt_and_init_deepseek", return_value=True), \
                patch.object(tui_backend.agent, "_plugin_runtime_for_surface", return_value=plugin), \
                patch.object(tui_backend.agent, "_run_recovered_tasks", return_value=[]), \
                patch.object(tui_backend.agent, "_project_graph", None), \
                patch.object(tui_backend.agent, "_ensure_detached_learning_worker", return_value=True) as ensure_worker:
            self.backend.start({"command": "start", "global": True}, "start-1")

        ensure_worker.assert_called_once_with()
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual(events[-1]["event"], "status")
        self.assertEqual(events[-1]["state"], "ready")

    def test_approval_response_requires_a_correlated_request_id(self):
        payload, error = self.backend.validate({"command": "approval_response", "approved": True})
        self.assertIsNone(payload)
        self.assertIn("request_id", error)

    def test_interaction_envelope_and_structured_commands_are_correlated(self):
        with tempfile.TemporaryDirectory() as directory:
            original = tui_backend.agent._interaction_controller
            controller = InteractionController(
                EventStore(Path(directory) / "state.sqlite3"),
                workspace_id="tui", session_id="surface:tui",
            )
            tui_backend.agent._interaction_controller = controller
            try:
                question = controller.request_question({"questions": [{
                    "id": "scope", "header": "Scope", "prompt": "Which target?",
                    "choices": ["Core", "All"],
                }]})
                self.backend.interaction("state-1")
                event = json.loads(self.output.getvalue().splitlines()[-1])
                self.assertEqual(event["event"], "interaction")
                self.assertEqual(event["request_id"], "state-1")
                self.assertEqual(event["interaction"]["pending_question"]["request_id"], question["request_id"])
                payload, error = self.backend.validate({
                    "command": "question_response", "request_id": "turn-1",
                    "question_response": {"request_id": question["request_id"], "answers": {}, "action": "cancel"},
                })
                self.assertIsNone(error)
                self.assertIsNotNone(payload)
                self.backend.dispatch(payload)
                self.assertIsNone(controller.state()["pending_question"])
            finally:
                tui_backend.agent._interaction_controller = original

    def test_successful_update_requests_restart(self):
        with patch.object(
                tui_backend.agent, "_self_update",
                return_value="Updated OpenKyrozen from source revision abc123:\ndone",
        ):
            self.backend._command("/update", {}, "update-1")
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual(events[-1]["event"], "restart")
        self.assertEqual(events[-1]["request_id"], "update-1")

    def test_graph_requests_are_correlated_bounded_and_support_refresh(self):
        class Graph:
            def explore(self, **kwargs):
                return {"status": "ready", "nodes": 1, "edges": 0, "communities": 1,
                        "mini": {"nodes": [{"id": "n", "label": kwargs.get("query", "node")}], "edges": []}}

            def refresh_async(self, **kwargs):
                callback = kwargs.get("callback")
                if callback:
                    callback({"status": "ready"})
                return True

            def path(self, left, right):
                return f"{left} -> {right}"

        with patch.object(tui_backend.agent, "_project_graph", Graph()):
            self.backend.dispatch({"command": "graph_request", "request_id": "graph-1", "action": "search", "query": "main"})
            self.backend.dispatch({"command": "graph_request", "request_id": "graph-2", "action": "path", "left": "a", "right": "b"})
            self.backend.dispatch({"command": "graph_request", "request_id": "graph-3", "action": "refresh"})
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertTrue(all(event["event"] == "graph_state" for event in events))
        self.assertEqual(events[0]["request_id"], "graph-1")
        self.assertEqual(events[0]["graph"]["mini"]["nodes"][0]["label"], "main")
        self.assertIn("a -> b", events[1]["graph"]["detail"])
        self.assertTrue(all(len(line.encode("utf-8")) <= tui_backend.MAX_LINE_BYTES
                            for line in self.output.getvalue().splitlines()))

    def test_github_login_emits_terminal_safe_prompt(self):
        client = type("Client", (), {
            "binary": lambda self: "/managed/gh",
            "hostname": lambda self: "ghe.example",
        })()
        with patch.object(tui_backend.agent, "_github_cli", client):
            self.backend._command("/github login", {}, "gh-1")
        event = json.loads(self.output.getvalue().splitlines()[-1])
        self.assertEqual(event["event"], "prompt")
        self.assertEqual(event["kind"], "github_auth")
        self.assertEqual(event["binary"], "/managed/gh")
        self.assertEqual(event["hostname"], "ghe.example")


if __name__ == "__main__":
    unittest.main()
