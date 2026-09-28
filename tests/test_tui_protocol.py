import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tui_backend
from interaction import InteractionController
from event_store import EventStore
from workspace_context import resolve_launch_context


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

    def test_navigation_groups_global_project_and_legacy_chats_without_leakage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "state.sqlite3")
            memory = SimpleNamespace(store=store, user_id="local", workspace_id="global")
            project = root / "project"
            project.mkdir()
            context = resolve_launch_context(home=root / "home", project_path=project)
            store.append_event(
                "tui.chat_metadata", {"title": "Global chat"}, user_id="local",
                workspace_id="global", session_id="chat-global",
            )
            store.append_event(
                "tui.project_opened", {"path": str(project), "name": "project",
                                       "source_scope_id": context.source_scope_id},
                user_id="local", workspace_id="global",
            )
            store.append_event(
                "tui.chat_metadata", {"title": "Project chat"}, user_id="local",
                workspace_id=context.source_scope_id, session_id="chat-project",
            )
            store.append_event(
                "tui.chat_metadata", {"title": "Legacy"}, user_id="local",
                workspace_id=context.source_scope_id, session_id="surface:tui",
            )
            with patch.object(tui_backend.agent, "memory_bank", memory):
                groups = self.backend._navigation_groups()
            self.assertEqual(groups[0]["name"], "No Project")
            self.assertEqual([chat["session_id"] for chat in groups[0]["chats"]], ["chat-global"])
            project_group = next(group for group in groups if group["scope_id"] == context.source_scope_id)
            self.assertEqual(
                {chat["session_id"] for chat in project_group["chats"]},
                {"chat-project", "surface:tui"},
            )

    def test_switch_rejects_busy_and_unknown_targets(self):
        self.backend._busy = True
        self.backend._switch_chat("scope", "chat-missing", "busy")
        self.backend._busy = False
        with patch.object(self.backend, "_navigation_groups", return_value=[]):
            self.backend._switch_chat("scope", "chat-missing", "unknown")
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual([event["code"] for event in events], ["busy", "unknown_session"])

    def test_attach_stages_quoted_files_and_rejects_invalid_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            image = root / "image one.png"
            image.write_bytes(b"fake-png")
            notes = root / "notes.txt"
            notes.write_text("notes", encoding="utf-8")
            missing = root / "missing.txt"

            with patch.object(tui_backend.agent, "_get_workspace_root", return_value=workspace):
                self.backend._command(f'/attach "{image}" "{notes}"', {}, "attach-1")
                self.backend._command(f'/attach "{notes}" "{missing}"', {}, "attach-2")

            events = [json.loads(line) for line in self.output.getvalue().splitlines()]
            self.assertEqual(events[0]["event"], "response")
            self.assertIn("attachments/", events[0]["text"])
            self.assertEqual(events[-1]["code"], "attach_failed")
            self.assertEqual(len(self.backend._staged_attachments), 2)
            for item, content in zip(self.backend._staged_attachments, (b"fake-png", b"notes")):
                self.assertEqual((workspace / item["path"]).read_bytes(), content)

    def test_attach_rejects_symlinks_and_oversized_files_without_copying(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            regular = root / "regular.txt"
            regular.write_text("safe", encoding="utf-8")
            oversized = root / "oversized.bin"
            with oversized.open("wb") as handle:
                handle.truncate(tui_backend.MAX_ATTACHMENT_BYTES + 1)
            symlink = root / "link.txt"
            try:
                symlink.symlink_to(regular)
            except OSError:
                symlink = None

            with patch.object(tui_backend.agent, "_get_workspace_root", return_value=workspace):
                self.backend._command(f"/attach {oversized}", {}, "attach-large")
                if symlink is not None:
                    self.backend._command(f"/attach {symlink}", {}, "attach-link")

            self.assertEqual(self.backend._staged_attachments, [])
            self.assertFalse((workspace / "attachments").exists())
            errors = [json.loads(line) for line in self.output.getvalue().splitlines()]
            self.assertTrue(all(event["code"] == "attach_failed" for event in errors))

    def test_staged_attachments_are_sent_to_next_turn_and_retry_after_failure(self):
        self.backend._staged_attachments = [{"path": "attachments/batch/notes.txt", "bytes": 5}]
        tasks = SimpleNamespace(tasks=[], clear=lambda: None)
        memory = SimpleNamespace(add_log=lambda _text: None)
        with patch.object(tui_backend.agent, "llm_provider", object()), \
                patch.object(tui_backend.agent, "_sanitize_input", return_value=("question", False)), \
                patch.object(tui_backend.agent, "interaction_envelope", return_value={
                    "pending_question": None, "pending_plan": None,
                }), \
                patch.object(tui_backend.agent, "is_plan_acceptance", return_value=False), \
                patch.object(tui_backend.agent, "tasks", tasks), \
                patch.object(tui_backend.agent, "memory_bank", memory), \
                patch.object(tui_backend.agent, "_chat_turn", side_effect=tui_backend.agent.ProviderUnavailableError("offline")), \
                patch.object(self.backend, "interaction"), \
                patch.object(self.backend, "usage"):
            self.backend._run_submit("question", "turn-failed")
        self.assertEqual(len(self.backend._staged_attachments), 1)

        with patch.object(tui_backend.agent, "llm_provider", object()), \
                patch.object(tui_backend.agent, "_sanitize_input", return_value=("question", False)), \
                patch.object(tui_backend.agent, "interaction_envelope", return_value={
                    "pending_question": None, "pending_plan": None,
                }), \
                patch.object(tui_backend.agent, "is_plan_acceptance", return_value=False), \
                patch.object(tui_backend.agent, "tasks", tasks), \
                patch.object(tui_backend.agent, "memory_bank", memory), \
                patch.object(tui_backend.agent, "_chat_turn", return_value="answer") as chat, \
                patch.object(tui_backend.agent, "_clean_final_response", return_value="answer"), \
                patch.object(tui_backend.agent, "_split_reply", return_value=("", "answer")), \
                patch.object(self.backend, "interaction"), \
                patch.object(self.backend, "usage"):
            self.backend._run_submit("question", "turn-success")

        prompt = chat.call_args.args[0]
        self.assertIn("attachments/batch/notes.txt", prompt)
        self.assertIn("User request:\nquestion", prompt)
        self.assertEqual(self.backend._staged_attachments, [])

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

    def test_new_install_onboarding_defers_provider_prompt(self):
        context = type("Context", (), {
            "active_root": Path("/tmp/openkyrozen-onboarding-workspace"),
            "is_global": True,
        })()
        config = type("Config", (), {
            "provider": "deepseek",
            "model_simple": "deepseek-v4-flash",
        })()
        plugin = type("Plugin", (), {"load_once": lambda self: None})()
        with patch.object(tui_backend.agent, "configure_launch_context", return_value=context), \
                patch.object(tui_backend.agent, "detect_provider", return_value=config), \
                patch.object(tui_backend.agent, "_prompt_and_init_deepseek", return_value=False), \
                patch.object(tui_backend.agent, "_plugin_runtime_for_surface", return_value=plugin), \
                patch.object(tui_backend.agent, "_run_recovered_tasks", return_value=[]), \
                patch.object(tui_backend.agent, "_project_graph", None), \
                patch.object(tui_backend.agent, "_ensure_detached_learning_worker", return_value=True):
            self.backend.start({"command": "start", "global": True, "onboarding": "new"}, "new-1")

        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        prompts = [event for event in events if event["event"] == "prompt"]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["kind"], "onboarding")
        self.assertNotIn("api_key", [event.get("kind") for event in prompts])

    def test_onboarding_learning_choice_completes_setup(self):
        self.backend._onboarding_kind = "new"
        with patch.object(tui_backend.agent, "set_learning_policy", return_value="remote"), \
                patch.object(self.backend, "interaction"), \
                patch.object(self.backend, "status"):
            self.backend._command("self_learning", {"mode": "remote", "onboarding": True}, "learning-1")
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual([event["event"] for event in events], ["response", "onboarding_complete"])
        self.assertEqual(events[-1]["kind"], "new")
        self.assertEqual(self.backend._onboarding_kind, "")

    def test_usage_projection_reads_workspace_ledger(self):
        class Store:
            def usage_totals(self, **kwargs):
                self.kwargs = kwargs
                return {
                    "attempts": 2,
                    "authoritative_attempts": 2,
                    "estimated_attempts": 0,
                    "unknown_attempts": 0,
                    "prompt_tokens": 120,
                    "completion_tokens": 80,
                    "reasoning_tokens": 10,
                    "cost_picos": 2500000000,
                }

        memory = SimpleNamespace(user_id="local", workspace_id="workspace", store=Store())
        with patch.object(tui_backend.agent, "memory_bank", memory):
            self.backend.usage("usage-1")
        event = json.loads(self.output.getvalue().strip())
        self.assertEqual(event["event"], "usage")
        self.assertEqual(event["request_id"], "usage-1")
        self.assertEqual(event["scope"], "workspace")
        self.assertEqual(event["cost_picos"], 2500000000)
        self.assertNotIn("workspace", event.get("error", ""))

    def test_approval_response_requires_a_correlated_request_id(self):
        payload, error = self.backend.validate({"command": "approval_response", "approved": True})
        self.assertIsNone(payload)
        self.assertIn("request_id", error)

    def test_fast_command_prompts_for_jev_key_and_reports_kev_setup_failure(self):
        with patch.object(tui_backend.agent.fast_mode, "jev_key", return_value=""):
            self.backend._command("/fast jev", {}, "fast-key")
        with patch.object(tui_backend.agent, "set_fast_backend", side_effect=RuntimeError("unsupported")):
            self.backend._command("/fast kev", {}, "fast-kev")
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual(events[0]["kind"], "fast_key")
        self.assertEqual(events[-1]["code"], "fast_setup_failed")

    def test_decision_assist_command_reports_status_and_requires_explicit_kev_consent(self):
        with patch.object(tui_backend.agent, "decision_assist_state", return_value={
            "backend": "off", "kev_private_consent": False,
            "jev_configured": False, "kev_ready": False,
        }), patch.object(tui_backend.agent, "set_decision_assist", side_effect=ValueError("consent required")):
            self.backend._command("/decision-assist", {}, "assist-status")
            self.backend._command("/decision-assist kev", {}, "assist-kev")
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertIn("Decision Assist: off", events[0]["text"])
        self.assertEqual(events[-1]["code"], "decision_assist_setup_failed")

    def test_decision_assist_jev_prompts_for_missing_key(self):
        with patch.object(tui_backend.agent.fast_mode, "jev_key", return_value=""):
            self.backend._command("/decision-assist jev", {}, "assist-key")
        event = json.loads(self.output.getvalue().strip())
        self.assertEqual(event["event"], "prompt")
        self.assertEqual(event["kind"], "decision_assist_key")
        self.assertEqual(event["request_id"], "assist-key")

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

    def test_history_commands_require_explicit_rollback_confirmation(self):
        current = {"id": "hist_current"}
        with patch.object(tui_backend.agent, "history_text", return_value="* hist_current"), \
                patch.object(tui_backend.agent, "history_manager", return_value=SimpleNamespace(current=lambda: current)), \
                patch.object(tui_backend.agent, "restore_history", return_value={"recovery": {"id": "hist_recovery"}}) as restore:
            self.backend._command("history", "", "history-1")
            self.backend._command("rollback", "hist_current", "rollback-1")
            self.backend._command("rollback", "hist_current confirm", "rollback-2")
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual(events[0]["event"], "response")
        self.assertEqual(events[0]["text"], "* hist_current")
        self.assertEqual(events[1]["code"], "rollback_confirmation_required")
        self.assertEqual(events[2]["event"], "response")
        restore.assert_called_once_with("hist_current", confirm="rollback", expected_head="hist_current")

    def test_self_learning_prompt_includes_runtime_and_cost_source(self):
        with patch.object(tui_backend.agent, "learning_runtime", return_value={"mode": "local"}), \
                patch.object(tui_backend.agent, "learning_cost_source", return_value="Local CPU/RAM/disk; no API cost"):
            self.backend._command("/self-learning", {}, "learning-1")
        event = json.loads(self.output.getvalue().splitlines()[-1])
        self.assertEqual(event["event"], "prompt")
        self.assertEqual(event["runtime"]["mode"], "local")
        self.assertEqual(event["cost_source"], "Local CPU/RAM/disk; no API cost")

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
