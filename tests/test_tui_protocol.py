import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import openkyrozen.interfaces.tui.backend as tui_backend
from openkyrozen.agent.modes import InteractionController
from openkyrozen.persistence.store import EventStore
from openkyrozen.workspace.context import resolve_launch_context


class TUIProtocolTests(unittest.TestCase):
    def setUp(self):
        self.backend = tui_backend.Backend()
        update_check = patch.object(self.backend.agent, "_available_update", return_value=None)
        update_check.start()
        self.addCleanup(update_check.stop)
        self.output = io.StringIO()
        self.backend._output = self.output
        self.addCleanup(self.backend.stop)

    def test_agent_snapshots_and_large_inspection_stream_in_scoped_chunks(self):
        agents = [{"run_id": str(index), "name": "Evan" + str(index), "version": 1} for index in range(25)]
        detail = {**agents[0], "messages": [{"content": "é" * 1000} for _ in range(100)],
                  "metrics": {"prompt_tokens": 15, "cost_picos": 100}, "api_key": "sk-private"}
        self.backend.emit("agents", "inspect", agents=agents, detail=detail,
                          session_id="chat-a", source_scope_id="project-a")
        lines = self.output.getvalue().splitlines()
        self.assertTrue(all(len(line.encode()) < tui_backend.MAX_LINE_BYTES for line in lines))
        events = [json.loads(line) for line in lines]
        self.assertEqual(sum(event["event"] == "subagent" for event in events), 25)
        self.assertTrue(all(event["session_id"] == "chat-a" for event in events))
        reconstructed = json.loads("".join(event["text"] for event in events if event["event"] == "agent_detail"))
        self.assertEqual(len(reconstructed["messages"]), 100)
        self.assertEqual(reconstructed["metrics"]["prompt_tokens"], 15)
        self.assertEqual(reconstructed["api_key"], "<redacted>")

    def test_onboarding_provider_prompt_uses_the_complete_registry(self):
        from openkyrozen.providers.registry import PROVIDER_AUTO_SELECTION, PROVIDER_DEFAULT_MODELS
        self.backend._onboarding_kind = "new"
        self.backend.dispatch({"command": "command", "name": "onboarding_continue", "request_id": "setup"})
        event = json.loads(self.output.getvalue().splitlines()[-1])
        self.assertEqual(event["kind"], "provider")
        self.assertEqual({item["name"] for item in event["providers"]}, set(PROVIDER_DEFAULT_MODELS))
        self.assertEqual({item["name"] for item in event["providers"] if item["auto_selection"]}, set(PROVIDER_AUTO_SELECTION))

    def test_validation_rejects_malformed_shape_and_oversized_text(self):
        payload, error = self.backend.validate(["submit"])
        self.assertIsNone(payload)
        self.assertIn("object", error)
        payload, error = self.backend.validate({"command": "submit", "text": "x" * 12001})
        self.assertIsNone(payload)
        self.assertIn("too long", error)

    def test_permissions_jev_alias_is_normalized_before_persistence(self):
        with patch.object(self.backend.agent.fast_mode, "jev_key", return_value="configured"), \
                patch.object(self.backend.agent, "set_permission_mode", return_value={
                    "permission_mode": "full_jev",
                }) as set_mode, \
                patch.object(self.backend, "interaction"):
            self.backend._command("permissions", {"mode": "jev"}, "permission-jev")

        set_mode.assert_called_once_with("full_jev", jev_api_key=None)
        event = json.loads(self.output.getvalue().splitlines()[-1])
        self.assertEqual(event["text"], "Permissions set to full_jev.")

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
            with patch.object(self.backend.agent.current_session, "memory", memory):
                groups = self.backend._navigation_groups()
            self.assertEqual(groups[0]["name"], "No Project")
            self.assertEqual([chat["session_id"] for chat in groups[0]["chats"]], ["chat-global"])
            project_group = next(group for group in groups if group["scope_id"] == context.source_scope_id)
            self.assertEqual(
                {chat["session_id"] for chat in project_group["chats"]},
                {"chat-project", "surface:tui"},
            )

    def test_empty_chat_is_not_persisted_until_the_first_user_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "state.sqlite3")
            memory = SimpleNamespace(store=store, user_id="local", workspace_id="global")
            project = root / "project"
            project.mkdir()
            context = resolve_launch_context(home=root / "home", project_path=project)
            with patch.object(self.backend.agent.current_session, "memory", memory), \
                    patch.object(self.backend.agent, "get_launch_context", return_value=context), \
                    patch.object(self.backend.agent, "interaction_workspace_id",
                                 return_value=context.source_scope_id):
                self.backend._register_chat("chat-empty", title=None)
                self.assertEqual(self.backend._scope_chats(context.source_scope_id), [])
                self.assertEqual(
                    store.list_events(
                        "tui.chat_metadata", workspace_id=context.source_scope_id,
                        session_id="chat-empty", user_id="local",
                    ),
                    [],
                )
                self.assertEqual(len(store.list_events(
                    "tui.project_opened", workspace_id="global", user_id="local",
                )), 1)

                self.backend._register_chat("chat-empty", title="First message")
                self.assertEqual(
                    [chat["session_id"] for chat in self.backend._scope_chats(context.source_scope_id)],
                    ["chat-empty"],
                )

    def test_switch_rejects_busy_and_unknown_targets(self):
        self.backend._busy = True
        self.backend._switch_chat("scope", "chat-missing", "busy")
        self.backend._busy = False
        with patch.object(self.backend, "_navigation_groups", return_value=[]):
            self.backend._switch_chat("scope", "chat-missing", "unknown")
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual([event["code"] for event in events], ["busy", "unknown_session"])

    def test_project_command_creates_missing_directory_before_binding_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "new" / "project"
            with patch.object(self.backend, "_bind_chat") as bind, \
                    patch.object(self.backend, "_emit_bound_state"):
                self.backend._command("project", str(project), "create-project")
            self.assertTrue(project.is_dir())
            self.assertEqual(Path(bind.call_args.kwargs["project_path"]), project.resolve())
            self.assertTrue(bind.call_args.kwargs["create"])

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

            with patch.object(self.backend.agent, "_get_workspace_root", return_value=workspace):
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

            with patch.object(self.backend.agent, "_get_workspace_root", return_value=workspace):
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
        with patch.object(self.backend.agent, "llm_provider", object()), \
                patch.object(self.backend.agent, "_sanitize_input", return_value=("question", False)), \
                patch.object(self.backend.agent, "interaction_envelope", return_value={
                    "pending_question": None, "pending_plan": None,
                }), \
                patch.object(self.backend.agent, "is_plan_acceptance", return_value=False), \
                patch.object(self.backend.agent.current_session, "tasks", tasks), \
                patch.object(self.backend.agent.current_session, "memory", memory), \
                patch.object(self.backend.agent, "_chat_turn", side_effect=self.backend.agent.ProviderUnavailableError("offline")), \
                patch.object(self.backend, "interaction"), \
                patch.object(self.backend, "usage"):
            self.backend._run_submit("question", "turn-failed")
        self.assertEqual(len(self.backend._staged_attachments), 1)

        with patch.object(self.backend.agent, "llm_provider", object()), \
                patch.object(self.backend.agent, "_sanitize_input", return_value=("question", False)), \
                patch.object(self.backend.agent, "interaction_envelope", return_value={
                    "pending_question": None, "pending_plan": None,
                }), \
                patch.object(self.backend.agent, "is_plan_acceptance", return_value=False), \
                patch.object(self.backend.agent.current_session, "tasks", tasks), \
                patch.object(self.backend.agent.current_session, "memory", memory), \
                patch.object(self.backend.agent, "_chat_turn", return_value="answer") as chat, \
                patch.object(self.backend.agent, "_clean_final_response", return_value="answer"), \
                patch.object(self.backend.agent, "_split_reply", return_value=("", "answer")), \
                patch.object(self.backend, "interaction"), \
                patch.object(self.backend, "usage"):
            self.backend._run_submit("question", "turn-success")

        prompt = chat.call_args.args[0]
        self.assertIn("attachments/batch/notes.txt", prompt)
        self.assertIn("User request:\nquestion", prompt)
        self.assertEqual(self.backend._staged_attachments, [])

    def test_inline_command_is_applied_before_chat_and_removed_from_request(self):
        tasks = SimpleNamespace(tasks=[], clear=lambda: None)
        memory = SimpleNamespace(add_log=lambda _text: None)
        with patch.object(self.backend.agent, "llm_provider", object()), \
                patch.object(self.backend, "_command") as command, \
                patch.object(self.backend.agent, "_sanitize_input", side_effect=lambda text: (text, False)), \
                patch.object(self.backend.agent, "interaction_envelope", return_value={
                    "pending_question": None, "pending_plan": None,
                }), \
                patch.object(self.backend.agent, "is_plan_acceptance", return_value=False), \
                patch.object(self.backend.agent.current_session, "tasks", tasks), \
                patch.object(self.backend.agent.current_session, "memory", memory), \
                patch.object(self.backend.agent, "_chat_turn", return_value="answer") as chat, \
                patch.object(self.backend.agent, "_clean_final_response", return_value="answer"), \
                patch.object(self.backend.agent, "_split_reply", return_value=("", "answer")), \
                patch.object(self.backend, "interaction"), \
                patch.object(self.backend, "usage"):
            self.backend._run_submit("Build a todo list /mode plan", "inline-1")

        command.assert_called_once_with("/mode plan", {}, "inline-1")
        self.assertEqual(chat.call_args.args[0], "Build a todo list")

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

    def test_unwrapped_plan_json_is_not_streamed_as_chat_text(self):
        callback = self.backend._stream_projection("turn-unwrapped-plan")
        callback({"event": "content", "chunk": '{"title":"Todo list","summary":"Build it",'})
        callback({"event": "content", "chunk": '"steps":[{"description":"Model todos"}]}'})
        callback({"event": "model_complete"})
        self.assertEqual(self.output.getvalue(), "")

    def test_bare_json_tool_call_is_not_streamed_as_chat_text(self):
        callback = self.backend._stream_projection("turn-action")
        callback({"event": "content", "chunk": '{"action":"read_file","args":"README.md"}'})
        callback({"event": "model_complete"})
        self.assertEqual(self.output.getvalue(), "")

    def test_split_inline_json_tool_call_keeps_only_the_prose(self):
        callback = self.backend._stream_projection("turn-inline-action")
        callback({"event": "content", "chunk": "I will inspect. "})
        callback({"event": "content", "chunk": '{"action":"read_file",'})
        callback({"event": "content", "chunk": '"args":"README.md"}'})
        callback({"event": "model_complete"})
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual([event["text"] for event in events], ["I will inspect."])

    def test_provider_setup_emits_ready_and_masks_key_flow(self):
        config = self.backend.agent.ProviderConfig(provider="deepseek")
        with patch.object(self.backend.agent, "_provider_config", config), \
                patch.object(self.backend.agent, "_get_workspace_root", return_value=Path("/tmp/workspace")), \
                patch.object(self.backend.agent, "save_provider_config_encrypted"), \
                patch.object(self.backend.agent, "_prompt_and_init_deepseek", return_value=False):
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
        with patch.object(self.backend.agent, "configure_launch_context", return_value=context), \
                patch.object(self.backend.agent, "detect_provider", return_value=config), \
                patch.object(self.backend.agent, "_prompt_and_init_deepseek", return_value=True), \
                patch.object(self.backend.agent, "_plugin_runtime_for_surface", return_value=plugin), \
                patch.object(self.backend.agent, "_run_recovered_tasks", return_value=[]), \
                patch.object(self.backend.agent.current_session.workspace, "graph", None), \
                patch.object(self.backend.agent, "_ensure_detached_learning_worker", return_value=True) as ensure_worker, \
                patch.object(self.backend, "_register_chat") as register_chat, \
                patch("openkyrozen.interfaces.tui.onboarding.threading.Thread") as update_thread:
            self.backend.start({"command": "start", "global": True}, "start-1")

        ensure_worker.assert_called_once_with()
        update_thread.assert_called_once_with(target=self.backend._check_for_update, daemon=True)
        update_thread.return_value.start.assert_called_once_with()
        self.assertIsNone(register_chat.call_args.kwargs["title"])
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
        with patch.object(self.backend.agent, "configure_launch_context", return_value=context), \
                patch.object(self.backend.agent, "detect_provider", return_value=config), \
                patch.object(self.backend.agent, "_prompt_and_init_deepseek", return_value=False), \
                patch.object(self.backend.agent, "_plugin_runtime_for_surface", return_value=plugin), \
                patch.object(self.backend.agent, "_run_recovered_tasks", return_value=[]), \
                patch.object(self.backend.agent.current_session.workspace, "graph", None), \
                patch.object(self.backend.agent, "_ensure_detached_learning_worker", return_value=True):
            self.backend.start({"command": "start", "global": True, "onboarding": "new"}, "new-1")

        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        prompts = [event for event in events if event["event"] == "prompt"]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["kind"], "onboarding")
        self.assertNotIn("api_key", [event.get("kind") for event in prompts])

    def test_onboarding_learning_choice_completes_setup(self):
        self.backend._onboarding_kind = "new"
        with patch.object(self.backend.agent, "set_learning_policy", return_value="remote"), \
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
        with patch.object(self.backend.agent.current_session, "memory", memory):
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
        with patch.object(self.backend.agent.fast_mode, "jev_key", return_value=""):
            self.backend._command("/fast jev", {}, "fast-key")
        with patch.object(self.backend.agent, "set_fast_backend", side_effect=RuntimeError("unsupported")):
            self.backend._command("/fast kev", {}, "fast-kev")
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual(events[0]["kind"], "fast_key")
        self.assertEqual(events[-1]["code"], "fast_setup_failed")

    def test_decision_assist_command_reports_status_and_requires_explicit_kev_consent(self):
        with patch.object(self.backend.agent, "decision_assist_state", return_value={
            "backend": "off", "kev_private_consent": False,
            "jev_configured": False, "kev_ready": False,
        }), patch.object(self.backend.agent, "set_decision_assist", side_effect=ValueError("consent required")):
            self.backend._command("/decision-assist", {}, "assist-status")
            self.backend._command("/decision-assist kev", {}, "assist-kev")
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertIn("Decision Assist: off", events[0]["text"])
        self.assertEqual(events[-1]["code"], "decision_assist_setup_failed")

    def test_decision_assist_jev_prompts_for_missing_key(self):
        with patch.object(self.backend.agent.fast_mode, "jev_key", return_value=""):
            self.backend._command("/decision-assist jev", {}, "assist-key")
        event = json.loads(self.output.getvalue().strip())
        self.assertEqual(event["event"], "prompt")
        self.assertEqual(event["kind"], "decision_assist_key")
        self.assertEqual(event["request_id"], "assist-key")

    def test_interaction_envelope_and_structured_commands_are_correlated(self):
        with tempfile.TemporaryDirectory() as directory:
            original = self.backend.agent._interaction_controller
            controller = InteractionController(
                EventStore(Path(directory) / "state.sqlite3"),
                workspace_id="tui", session_id="surface:tui",
            )
            self.backend.agent._interaction_controller = controller
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
                self.backend.agent._interaction_controller = original

    def test_successful_update_requests_restart(self):
        with patch.object(
                self.backend.agent, "_self_update",
                return_value="Updated OpenKyrozen from source revision abc123:\ndone",
        ):
            self.backend._command("/update", {}, "update-1")
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual(events[-1]["event"], "restart")
        self.assertEqual(events[-1]["request_id"], "update-1")

    def test_update_notice_and_command(self):
        with patch.object(self.backend.agent, "_available_update", return_value="2.0.5"):
            self.backend._check_for_update()
        notice = json.loads(self.output.getvalue().splitlines()[-1])
        self.assertEqual(notice["event"], "update_available")
        self.assertEqual(notice["version"], "2.0.5")
        with patch.object(self.backend.agent, "_self_update", return_value="Updated OpenKyrozen from source revision abc123") as update:
            self.backend._command("/update", {}, "update-command")
        update.assert_called_once_with()
        self.assertEqual(json.loads(self.output.getvalue().splitlines()[-1])["event"], "restart")

    def test_history_commands_require_explicit_rollback_confirmation(self):
        current = {"id": "hist_current"}
        manager = SimpleNamespace(
            current=lambda: current,
            resolve_selector=lambda selector: {
                "id": "hist_current", "selector": 1, "summary": "Current turn",
                "file_summary": {"changes": {"added": 1, "changed": 2, "deleted": 0}},
            },
        )
        with patch.object(self.backend.agent, "history_text", return_value="Conversation: chat-current\n* [1]"), \
                patch.object(self.backend.agent, "history_manager", return_value=manager), \
                patch.object(self.backend.agent, "restore_history", return_value={"recovery": {"id": "hist_recovery"}}) as restore, \
                patch.object(self.backend, "navigation") as navigation, \
                patch.object(self.backend, "interaction") as interaction, \
                patch.object(self.backend, "graph_state") as graph_state, \
                patch.object(self.backend, "usage") as usage, \
                patch.object(self.backend, "status") as status:
            self.backend._command("history", "", "history-1")
            self.backend._command("rollback", "1", "rollback-1")
            self.backend._command("rollback", "1 confirm", "rollback-2")
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual(events[0]["event"], "response")
        self.assertIn("chat-current", events[0]["text"])
        self.assertIn("/rollback 1 confirm", events[1]["text"])
        restore.assert_called_once_with("hist_current", confirm="rollback", expected_head="hist_current")
        navigation.assert_called_once_with("rollback-2")
        interaction.assert_called_once_with("rollback-2")
        graph_state.assert_called_once_with("rollback-2")
        usage.assert_called_once_with("rollback-2")
        status.assert_called_once_with("ready", "Restored [1]. Recovery point saved.", "rollback-2")

    def test_self_learning_prompt_includes_runtime_and_cost_source(self):
        with patch.object(self.backend.agent, "learning_runtime", return_value={"mode": "local"}), \
                patch.object(self.backend.agent, "learning_cost_source", return_value="Local CPU/RAM/disk; no API cost"):
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

        with patch.object(self.backend.agent.current_session.workspace, "graph", Graph()):
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
        with patch.object(self.backend.agent.current_session.workspace, "github", client):
            self.backend._command("/github login", {}, "gh-1")
        event = json.loads(self.output.getvalue().splitlines()[-1])
        self.assertEqual(event["event"], "prompt")
        self.assertEqual(event["kind"], "github_auth")
        self.assertEqual(event["binary"], "/managed/gh")
        self.assertEqual(event["hostname"], "ghe.example")


if __name__ == "__main__":
    unittest.main()
