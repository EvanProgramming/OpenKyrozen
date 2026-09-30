import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openkyrozen.app.bootstrap import build_application

from openkyrozen.persistence.store import EventStore
from openkyrozen.agent.modes import (
    InteractionController,
    InteractionError,
    is_plan_acceptance,
    mode_capabilities,
    parse_control_block,
    route_mode,
    split_inline_command,
)
from openkyrozen.tasks.engine import TaskManager
from openkyrozen.security.capabilities import issue_capability_token
from openkyrozen.workspace.context import resolve_launch_context


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = EventStore(Path(self.directory.name) / "state.sqlite3")
        from openkyrozen.memory.service import MemoryBank
        self.application = build_application(surface="cli", memory=MemoryBank(self.store.path, store=self.store))
        self.agent = self.application.runtime
        self.addCleanup(self.application.close)
        self.controller = InteractionController(
            self.store, workspace_id="workspace", session_id="session",
        )

    def test_auto_routing_and_mode_capabilities_are_narrowing_only(self):
        self.assertEqual(route_mode("auto", "What does this function do?"), "ask")
        self.assertEqual(route_mode("auto", "How do I create a Python file?"), "ask")
        self.assertEqual(route_mode("auto", "Implement the fix and run tests"), "plan")
        self.assertEqual(route_mode("auto", "Please review this repository"), "plan")
        self.assertEqual(route_mode("agent", "What is this?"), "agent")
        base = frozenset({"read", "write", "shell", "network", "git", "browser", "dynamic"})
        self.assertEqual(mode_capabilities(base, "plan"), frozenset({"read", "network"}))
        self.assertEqual(mode_capabilities(frozenset({"read"}), "agent"), frozenset({"read"}))

    def test_inline_commands_are_trailing_and_conservative(self):
        self.assertEqual(
            split_inline_command("Build a todo list for me /mode plan"),
            ("Build a todo list for me", "/mode plan"),
        )
        self.assertEqual(
            split_inline_command("Review the workspace /plan"),
            ("Review the workspace", "/plan"),
        )
        self.assertIsNone(split_inline_command("Use /mode in the documentation"))
        self.assertIsNone(split_inline_command("Open https://example.com/a/mode"))
        self.assertIsNone(split_inline_command("Read /tmp/project"))
        self.assertIsNone(split_inline_command("Build it /mode invalid"))

    def test_cli_inline_command_dispatches_before_request(self):
        with patch.object(self.agent, "set_interaction_mode", return_value={"preference_mode": "plan"}) as set_mode, \
                patch.object(self.agent.console, "print"):
            self.assertTrue(self.agent._apply_inline_command("/mode plan"))
        set_mode.assert_called_once_with("plan")

    def test_project_interaction_state_is_isolated_from_other_projects(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as first, \
                tempfile.TemporaryDirectory() as second:
            first_context = resolve_launch_context(home=home, project_path=first)
            second_context = resolve_launch_context(home=home, project_path=second)
            first_scope = self.agent.interaction_workspace_id(first_context)
            second_scope = self.agent.interaction_workspace_id(second_context)
            self.assertNotEqual(first_scope, second_scope)

            first_controller = InteractionController(
                self.store, workspace_id=first_scope, session_id="surface:tui",
            )
            first_controller.propose_plan({
                "title": "First", "summary": "Only the first project.", "assumptions": [],
                "steps": [{"id": "one", "title": "One", "description": "Change first.",
                           "acceptance": ["First changed"]}],
            })
            second_controller = InteractionController(
                self.store, workspace_id=second_scope, session_id="surface:tui",
            )
            self.assertIsNotNone(first_controller.state()["pending_plan"])
            self.assertIsNone(second_controller.state()["pending_plan"])

            global_context = resolve_launch_context(home=home, global_mode=True)
            self.assertEqual(
                self.agent.interaction_workspace_id(global_context), self.agent.memory_bank.workspace_id,
            )

    def test_binding_empty_chat_clears_previous_short_term_memory(self):
        previous = (self.agent.short_term_memory, self.agent.tasks, self.agent._interaction_controller,
                    self.agent.memory_bank.session_id)
        def restore():
            self.agent.short_term_memory, self.agent.tasks, self.agent._interaction_controller = previous[:3]
            self.agent.memory_bank.session_id = previous[3]
        self.addCleanup(restore)
        self.agent.short_term_memory = [{"role": "user", "content": "previous project"}]
        empty_history = type("History", (), {"current": lambda self: None})()
        with patch.object(self.agent, "TaskManager", return_value=object()), \
                patch.object(self.agent, "InteractionController", return_value=object()), \
                patch.object(self.agent, "history_manager", return_value=empty_history), \
                patch.object(self.agent, "_restore_ponytail_level"):
            self.agent.bind_interaction_scope("chat-empty")
        self.assertEqual(self.agent.short_term_memory, [])

    def test_question_bounds_and_event_reconstruction(self):
        question = self.controller.request_question({"questions": [{
            "id": "scope", "header": "Scope", "prompt": "Which package?",
            "choices": ["Core", "All packages"],
        }]}, original_input="Implement the change")
        restored = InteractionController(
            self.store, workspace_id="workspace", session_id="session",
        )
        state = restored.state()
        self.assertEqual(state["pending_question"]["request_id"], question["request_id"])
        self.assertEqual(state["effective_mode"], "plan")
        restored.resolve_question(question["request_id"], {"scope": "Core"})
        self.assertIsNone(restored.state()["pending_question"])
        with self.assertRaises(InteractionError):
            restored.request_question({"questions": [{
                "header": "Secret", "prompt": "Send your API key",
                "choices": ["Yes", "No"],
            }]})
        with self.assertRaises(InteractionError):
            restored.request_question({"questions": [{
                "header": "Approval", "prompt": "May I run this command?",
                "choices": ["Approve", "Deny"],
            }]})
        with self.assertRaises(InteractionError):
            restored.request_question({"questions": [
                {"id": "duplicate", "header": "One", "prompt": "First?", "choices": ["A", "B"]},
                {"id": "duplicate", "header": "Two", "prompt": "Second?", "choices": ["A", "B"]},
            ]})

    def test_plan_revision_acceptance_is_versioned_and_idempotent(self):
        first = self.controller.propose_plan({
            "title": "Change", "summary": "Make the requested change.", "assumptions": [],
            "steps": [{"id": "inspect", "title": "Inspect", "description": "Read the code.",
                       "acceptance": ["Relevant path is identified"]}],
        })
        second = self.controller.propose_plan({
            "title": "Change safely", "summary": "Revise after feedback.", "assumptions": ["Tests exist"],
            "steps": [{"id": "inspect", "title": "Inspect", "description": "Read the code and tests.",
                       "acceptance": ["Relevant path and tests are identified"]}],
        })
        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertEqual(first["plan_id"], second["plan_id"])
        manager = TaskManager(self.store, workspace_id="workspace", session_id="session")
        accepted, created = self.controller.accept_plan(
            manager, plan_id=second["plan_id"], version=second["version"],
        )
        duplicate, created_again = self.controller.accept_plan(
            manager, plan_id=second["plan_id"], version=second["version"],
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(accepted, duplicate)
        self.assertEqual(len(manager.tasks), 1)
        self.assertEqual(manager.tasks[0]["acceptance"][0]["criterion"],
                         "Relevant path and tests are identified")
        self.assertEqual(self.controller.state()["effective_mode"], "agent")
        self.controller.complete_plan(accepted)
        self.assertEqual(self.controller.state()["effective_mode"], "ask")
        completed_duplicate, created_after_completion = self.controller.accept_plan(
            manager, plan_id=second["plan_id"], version=second["version"],
        )
        self.assertEqual(completed_duplicate, accepted)
        self.assertFalse(created_after_completion)
        self.assertEqual(len(manager.tasks), 1)

    def test_provider_plan_text_cannot_duplicate_an_accepted_checklist(self):
        previous_tasks = self.agent.tasks
        previous_controller = self.agent._interaction_controller
        manager = TaskManager(self.store, workspace_id="workspace", session_id="session")
        proposal = self.controller.propose_plan({
            "title": "Eight steps", "summary": "Execute once.", "assumptions": [],
            "steps": [
                {"id": f"step-{index}", "title": f"Step {index}",
                 "description": (
                     "Create index.html with the requested content."
                     if index == 0 else f"Perform step {index}."
                 ), "acceptance": [f"Step {index} done"]}
                for index in range(8)
            ],
        })
        self.controller.accept_plan(
            manager, plan_id=proposal["plan_id"], version=proposal["version"],
        )
        self.agent.tasks = manager
        self.agent._interaction_controller = self.controller
        mode_token = self.agent._active_interaction_mode.set("agent")
        try:
            repeated = "Plan:\n" + "\n".join(
                f"{index}. Perform step {index}." for index in range(1, 9)
            )
            self.agent._tasks_from_plan(repeated)
            self.agent._tasks_from_plan(repeated)
            self.agent._observe_model_response(
                "TaskList:\n```json\n"
                + json.dumps([f"Use run_cmd to perform step {index}" for index in range(1, 9)])
                + "\n```"
            )
            self.assertEqual(len(manager.tasks), 8)
            evidence = manager.record_evidence(
                action="write_file", args="index.html|ready", result="Wrote index.html", success=True,
            )
            self.assertEqual(evidence["task_id"], manager.tasks[0]["id"])
            self.assertEqual(manager.tasks[0]["status"], "succeeded")
        finally:
            self.agent.tasks = previous_tasks
            self.agent._interaction_controller = previous_controller
            self.agent._active_interaction_mode.reset(mode_token)

    def test_exact_acceptance_phrases_and_control_parser(self):
        self.assertTrue(is_plan_acceptance("Execute plan."))
        self.assertTrue(is_plan_acceptance("执行计划。"))
        self.assertFalse(is_plan_acceptance("run plan"))
        self.assertFalse(is_plan_acceptance("The plan looks acceptable"))
        value = parse_control_block(
            'AskUser:\n```json\n{"questions": []}\n```', "AskUser",
        )
        self.assertEqual(value, {"questions": []})
        self.assertEqual(parse_control_block(
            'PlanProposal\n```json\n{"steps": []}\n```', "PlanProposal",
        ), {"steps": []})

    def test_provider_shaped_plan_discards_executable_step_metadata(self):
        shaped = json.dumps({
            "plan_name": "Status page", "mode": "plan",
            "do_not_execute_until_approved": True,
            "overview": "Build after approval.", "assumptions": ["Python exists"],
            "steps": [{"step": 1, "title": "Create page", "details": "Write index.html.",
                       "acceptance_criteria": ["index.html exists"]}],
        })
        parsed = self.agent._observe_model_response(shaped)
        self.assertEqual(parsed["plan_proposal"]["title"], "Status page")
        self.assertEqual(parsed["plan_proposal"]["steps"][0]["id"], "step-1")
        self.assertEqual(parsed["tool_calls"], [])

        executable = json.dumps({
            "mode": "plan", "plan_name": "Safe proposal", "goal": "Write after acceptance.",
            "steps": [{"title": "Write", "details": "Write README.",
                       "action": "write_file", "path": "README.md", "content": "unsafe"}],
        })
        sanitized = self.agent._observe_model_response(executable)
        self.assertEqual(sanitized["tool_calls"], [])
        self.assertEqual(sanitized["plan_proposal"]["title"], "Safe proposal")
        self.assertEqual(
            set(sanitized["plan_proposal"]["steps"][0]),
            {"id", "title", "description", "acceptance"},
        )
        self.assertEqual(
            sanitized["plan_proposal"]["steps"][0]["acceptance"],
            ["Step completed with observable evidence"],
        )

    def test_plan_mode_converts_plain_plan_and_discards_early_actions(self):
        mode_token = self.agent._active_interaction_mode.set("plan")
        previous_controller = self.agent._interaction_controller
        self.agent._interaction_controller = self.controller
        try:
            parsed = self.agent._parse_model_response(
                "Plan:\n"
                "1. Create index.html with the requested heading.\n"
                "2. Serve it locally and verify HTTP 200.\n"
                'Action: {"action":"write_file","args":"index.html|unsafe"}'
            )
            self.assertEqual(parsed["tool_calls"], [])
            self.assertIsNone(parsed["protocol_error"])
            rendered = self.agent._interaction_gate(parsed, "Create a local page")
            self.assertIn("Create index.html", rendered)
            self.assertEqual(len(self.controller.state()["pending_plan"]["steps"]), 2)
            self.assertEqual(
                self.store.list_events(
                    "execution.receipt", workspace_id="workspace", session_id="session",
                ),
                [],
            )

            action_only = self.agent._parse_model_response(
                'Action: {"action":"run_cmd","args":"python -m http.server"}'
            )
            self.assertEqual(action_only["tool_calls"], [])
            self.assertIn("rejected an executable action", action_only["protocol_error"])

            markdown = self.agent._parse_model_response(
                "PlanProposal:\n\n**Goal:** Build locally.\n\n**Planned changes**\n\n"
                "1. **Create `index.html`** with no dependencies.\n"
                "   - Include a visible heading.\n"
                "2. **Serve on port 8765** with Python.\n"
                "3. **Verify HTTP 200** with curl.\n"
                "4. **Stop the server** and release the port.\n"
                "\n- This trailing note is not another step."
            )
            self.assertIsNone(markdown["protocol_error"])
            self.assertEqual(len(markdown["plan_proposal"]["steps"]), 4)
            self.assertEqual(markdown["plan_proposal"]["steps"][0]["id"], "step-1")
        finally:
            self.agent._interaction_controller = previous_controller
            self.agent._active_interaction_mode.reset(mode_token)

    def test_plan_mode_repairs_rejected_action_into_proposal(self):
        class LearningStub:
            def feedback_signal(self, _text): return None
            def route_profile(self, _text, _profile=None): return "coder"
            def begin_run(self, profile, _task, provider_model=None):
                return {"run_id": "plan-repair", "profile": profile, "provider_model": provider_model}
            def artifact_context(self, _run): return "", []

        class RuntimeStub:
            def turn_start(self, **_kwargs): return None
            def turn_end(self, **_kwargs): return None

        original = (
            self.agent.tasks, self.agent._interaction_controller, self.agent.learning_engine,
            self.agent._get_workspace_root(), self.agent._execution_capability_token,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "state.sqlite3")
            self.agent.tasks = TaskManager(store, workspace_id="repair", session_id="repair")
            self.agent._interaction_controller = InteractionController(
                store, workspace_id="repair", session_id="repair",
            )
            self.agent._interaction_controller.set_mode("plan")
            self.agent.learning_engine = LearningStub()
            self.agent._set_workspace_root(root)
            responses = [
                'Action: {"action":"write_file","args":"unsafe.txt|bad"}',
                'Action: {"action":"list_dir","args":"."}',
                'PlanProposal:\n```json\n{"title":"Safe plan","summary":"Write only after approval.",'
                '"assumptions":[],"steps":[{"id":"write","title":"Write file",'
                '"description":"Create the requested file.","acceptance":["File exists"]}]}\n```',
            ]
            try:
                with patch.object(self.agent, "_plugin_runtime_for_surface", return_value=RuntimeStub()), \
                        patch.object(self.agent, "_touch_detached_learning_heartbeat"), \
                        patch.object(self.agent, "dispatch_learning_cycle"), \
                        patch.object(self.agent, "_build_messages", return_value=[]), \
                        patch.object(self.agent, "_build_memory_context", return_value=""), \
                        patch.object(self.agent, "_classify_complexity", return_value="simple"), \
                        patch.object(self.agent, "_call_llm_with_spinner", side_effect=responses), \
                        patch.object(self.agent, "_finish_learning_run", side_effect=lambda run, receipts, task,
                                     result, records, tokens, started: result):
                    reply = self.agent._chat_turn("Create the file")
                self.assertIn("Safe plan (v1)", reply)
                self.assertFalse((root / "unsafe.txt").exists())
                self.assertEqual(
                    self.agent._interaction_controller.state()["pending_plan"]["title"], "Safe plan",
                )
            finally:
                (self.agent.tasks, self.agent._interaction_controller, self.agent.learning_engine,
                 previous_root, self.agent._execution_capability_token) = original
                self.agent._set_workspace_root(previous_root)

    def test_plan_prompt_advertises_only_read_and_network_tools(self):
        mode_token = self.agent._active_interaction_mode.set("plan")
        controls_token = self.agent._interaction_controls_enabled.set(True)
        previous_token = self.agent._execution_capability_token
        self.agent._execution_capability_token = issue_capability_token(
            "test:plan-prompt", frozenset({"read", "write", "shell", "network", "git", "dynamic"}),
        )
        try:
            prompt = self.agent._system_prompt("deliberately ignored")
        finally:
            self.agent._execution_capability_token = previous_token
            self.agent._interaction_controls_enabled.reset(controls_token)
            self.agent._active_interaction_mode.reset(mode_token)
        self.assertIn("PlanProposal", prompt)
        self.assertIn("read_file", prompt)
        self.assertIn("search_web", prompt)
        for forbidden in ("write_file", "run_cmd", "TaskList", "TaskDone", "DefineTool", "git_commit"):
            self.assertNotIn(forbidden, prompt)
        self.assertIn("The first Plan-mode response must be exactly one listed read-only Action", prompt)

    def test_plan_requires_inspection_and_normalizes_structured_prose(self):
        class LearningStub:
            def feedback_signal(self, _text): return None
            def route_profile(self, _text, _profile=None): return "coder"
            def begin_run(self, profile, _task, provider_model=None):
                return {"run_id": "plan-prose", "profile": profile, "provider_model": provider_model}
            def artifact_context(self, _run): return "", []

        class RuntimeStub:
            def turn_start(self, **_kwargs): return None
            def turn_end(self, **_kwargs): return None

        original = (self.agent.tasks, self.agent._interaction_controller, self.agent.learning_engine,
                    self.agent._get_workspace_root(), self.agent._execution_capability_token)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "state.sqlite3")
            self.agent.tasks = TaskManager(store, workspace_id="prose", session_id="prose")
            self.agent._interaction_controller = InteractionController(
                store, workspace_id="prose", session_id="prose",
            )
            self.agent._interaction_controller.set_mode("plan")
            self.agent.learning_engine = LearningStub()
            self.agent._set_workspace_root(root)
            records = []
            responses = [
                "1. Create the todo data model.\n2. Add reminders, priority, and status.\n3. Verify the workflow.",
                'Action: {"action":"list_dir","args":"."}',
                "1. Define the todo data model.\n2. Add reminder scheduling.\n3. Add priority and status controls.\n4. Test create, update, and completion flows.",
            ]
            try:
                with patch.object(self.agent, "_plugin_runtime_for_surface", return_value=RuntimeStub()), \
                        patch.object(self.agent, "_touch_detached_learning_heartbeat"), \
                        patch.object(self.agent, "dispatch_learning_cycle"), \
                        patch.object(self.agent, "_build_messages", return_value=[]), \
                        patch.object(self.agent, "_build_memory_context", return_value=""), \
                        patch.object(self.agent, "_classify_complexity", return_value="simple"), \
                        patch.object(self.agent, "_call_llm_with_spinner", side_effect=responses), \
                        patch.object(self.agent, "_finish_learning_run", side_effect=lambda run, receipts, task,
                                     result, tool_records, tokens, started: records.extend(tool_records) or result):
                    reply = self.agent._chat_turn(
                        "I want you to build a todo list with reminders, priority, and status."
                    )
                pending = self.agent._interaction_controller.state()["pending_plan"]
                self.assertNotIn("bounded recovery", reply)
                self.assertIsNotNone(pending)
                self.assertEqual(pending["title"], "Define the todo data model.")
                self.assertEqual(len(pending["steps"]), 4)
                self.assertEqual([record["action"] for record in records], ["list_dir"])
            finally:
                (self.agent.tasks, self.agent._interaction_controller, self.agent.learning_engine,
                 previous_root, self.agent._execution_capability_token) = original
                self.agent._set_workspace_root(previous_root)

    def test_plan_mode_accepts_unwrapped_provider_plan_json(self):
        mode_token = self.agent._active_interaction_mode.set("plan")
        try:
            parsed = self.agent._parse_model_response(
                '{"title":"Todo list","summary":"Build the requested workflow",'
                '"steps":[{"title":"Model todos","description":"Store reminder, priority, and status fields",'
                '"acceptance":["Todo records support the requested fields"]}]}'
            )
        finally:
            self.agent._active_interaction_mode.reset(mode_token)
        self.assertIsNone(parsed["protocol_error"])
        self.assertEqual(parsed["plan_proposal"]["title"], "Todo list")
        self.assertEqual(len(parsed["plan_proposal"]["steps"]), 1)

    def test_plan_recovery_exhaustion_is_stable_and_never_executes_mutations(self):
        class LearningStub:
            def feedback_signal(self, _text): return None
            def route_profile(self, _text, _profile=None): return "coder"
            def begin_run(self, profile, _task, provider_model=None):
                return {"run_id": "plan-exhaust", "profile": profile, "provider_model": provider_model}
            def artifact_context(self, _run): return "", []

        class RuntimeStub:
            def turn_start(self, **_kwargs): return None
            def turn_end(self, **_kwargs): return None

        original = (self.agent.tasks, self.agent._interaction_controller, self.agent.learning_engine,
                    self.agent._get_workspace_root(), self.agent._execution_capability_token)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "state.sqlite3")
            self.agent.tasks = TaskManager(store, workspace_id="exhaust", session_id="exhaust")
            self.agent._interaction_controller = InteractionController(store, workspace_id="exhaust", session_id="exhaust")
            self.agent._interaction_controller.set_mode("plan")
            self.agent.learning_engine = LearningStub()
            self.agent._set_workspace_root(root)
            responses = [
                'Action: {"action":"write_file","args":"unsafe.txt|bad"}',
                'Action: {"action":"run_cmd","args":"touch unsafe.txt"}',
                'Action: {"action":"write_file","args":"unsafe.txt|bad"}',
                'Action: {"action":"run_cmd","args":"touch unsafe.txt"}',
            ]
            try:
                with patch.object(self.agent, "_plugin_runtime_for_surface", return_value=RuntimeStub()), \
                        patch.object(self.agent, "_touch_detached_learning_heartbeat"), \
                        patch.object(self.agent, "dispatch_learning_cycle"), \
                        patch.object(self.agent, "_build_messages", return_value=[]), \
                        patch.object(self.agent, "_classify_complexity", return_value="complex"), \
                        patch.object(self.agent, "_call_llm_with_spinner", side_effect=responses), \
                        patch.object(self.agent, "_execute_turn_action", side_effect=AssertionError("mutation executed")), \
                        patch.object(self.agent, "_finish_learning_run", side_effect=lambda run, receipts, task,
                                     result, records, tokens, started: result):
                    reply = self.agent._chat_turn("Fix the project")
                self.assertIn("bounded recovery", reply)
                self.assertIn("no changes were made", reply)
                self.assertFalse((root / "unsafe.txt").exists())
            finally:
                (self.agent.tasks, self.agent._interaction_controller, self.agent.learning_engine,
                 previous_root, self.agent._execution_capability_token) = original
                self.agent._set_workspace_root(previous_root)

    def test_plan_inspection_spans_rounds_and_repairs_malformed_proposal(self):
        class LearningStub:
            def feedback_signal(self, _text): return None
            def route_profile(self, _text, _profile=None): return "coder"
            def begin_run(self, profile, _task, provider_model=None):
                return {"run_id": "plan-inspect", "profile": profile, "provider_model": provider_model}
            def artifact_context(self, _run): return "", []

        class RuntimeStub:
            def turn_start(self, **_kwargs): return None
            def turn_end(self, **_kwargs): return None

        original = (self.agent.tasks, self.agent._interaction_controller, self.agent.learning_engine,
                    self.agent._get_workspace_root(), self.agent._execution_capability_token)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note.txt").write_text("evidence", encoding="utf-8")
            store = EventStore(root / "state.sqlite3")
            self.agent.tasks = TaskManager(store, workspace_id="inspect", session_id="inspect")
            self.agent._interaction_controller = InteractionController(store, workspace_id="inspect", session_id="inspect")
            self.agent._interaction_controller.set_mode("plan")
            self.agent.learning_engine = LearningStub()
            self.agent._set_workspace_root(root)
            records = []
            responses = [
                'Action: {"action":"read_file","args":"note.txt"}',
                'Action: {"action":"list_dir","args":"."}',
                'PlanProposal:\n```json\n{"title":"Broken","steps":[}\n```',
                'PlanProposal:\n```json\n{"title":"Evidence plan","summary":"Use inspected evidence.",'
                '"assumptions":[],"steps":[{"id":"step-1","title":"Apply change",'
                '"description":"Apply the approved change.","acceptance":["Change verified"]}]}\n```',
            ]
            try:
                with patch.object(self.agent, "_plugin_runtime_for_surface", return_value=RuntimeStub()), \
                        patch.object(self.agent, "_touch_detached_learning_heartbeat"), \
                        patch.object(self.agent, "dispatch_learning_cycle"), \
                        patch.object(self.agent, "_build_messages", return_value=[]), \
                        patch.object(self.agent, "_build_memory_context", return_value=""), \
                        patch.object(self.agent, "_classify_complexity", return_value="complex"), \
                        patch.object(self.agent, "_call_llm_with_spinner", side_effect=responses), \
                        patch.object(self.agent, "_finish_learning_run", side_effect=lambda run, receipts, task,
                                     result, tool_records, tokens, started: records.extend(tool_records) or result):
                    reply = self.agent._chat_turn("Plan the evidence-backed change")
                self.assertIn("Evidence plan", reply)
                self.assertEqual([record["action"] for record in records], ["read_file", "list_dir"])
                self.assertEqual(self.agent._interaction_controller.state()["effective_mode"], "plan")
            finally:
                (self.agent.tasks, self.agent._interaction_controller, self.agent.learning_engine,
                 previous_root, self.agent._execution_capability_token) = original
                self.agent._set_workspace_root(previous_root)

    def test_control_blocks_combined_with_actions_fail_closed(self):
        parsed = self.agent._observe_model_response(
            'AskUser:\n```json\n{"questions":[{"id":"scope","header":"Scope",'
            '"prompt":"Which target?","choices":["Core","All"]}]}\n```\n'
            'Action: {"action":"write_file","args":"unsafe.txt|bad"}'
        )
        self.assertIn("only control block", parsed["protocol_error"])
        self.assertEqual(parsed["tool_calls"][0]["action"], "write_file")
        self.assertIsNotNone(parsed["question"])
        previous_controller = self.agent._interaction_controller
        token = self.agent._interaction_controls_enabled.set(False)
        self.agent._interaction_controller = self.controller
        try:
            question_only = self.agent._observe_model_response(
                'AskUser:\n```json\n{"questions":[{"id":"scope","header":"Scope",'
                '"prompt":"Which target?","choices":["Core","All"]}]}\n```'
            )
            self.assertIn("non-interactive", self.agent._interaction_gate(question_only, "Inspect"))
            self.assertIsNone(self.controller.state()["pending_question"])
        finally:
            self.agent._interaction_controller = previous_controller
            self.agent._interaction_controls_enabled.reset(token)

    def test_read_only_modes_deny_mutating_and_dynamic_tools(self):
        previous_token = self.agent._execution_capability_token
        previous_root = self.agent._get_workspace_root()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "notes.txt").write_text("safe", encoding="utf-8")
            self.agent._set_workspace_root(root)
            self.agent._execution_capability_token = issue_capability_token(
                "test:plan", mode_capabilities(
                    frozenset({"read", "write", "shell", "network", "git", "browser", "dynamic"}),
                    "plan",
                ),
            )
            self.agent.AVAILABLE_TOOLS["temporary_dynamic_tool"] = lambda args: f"ran:{args}"
            try:
                self.assertIn("safe", self.agent._run_tool("read_file", "notes.txt"))
                for action, args in (
                    ("write_file", "blocked.txt|bad"),
                    ("run_cmd", "touch blocked-shell.txt"),
                    ("git_commit", "blocked"),
                    ("browser_click", "button"),
                    ("temporary_dynamic_tool", "value"),
                ):
                    result = self.agent._run_tool(action, args)
                    self.assertTrue(result.startswith("Error:"), (action, result))
                self.assertFalse((root / "blocked.txt").exists())
                self.assertFalse((root / "blocked-shell.txt").exists())
            finally:
                self.agent.AVAILABLE_TOOLS.pop("temporary_dynamic_tool", None)
                self.agent._execution_capability_token = previous_token
                self.agent._set_workspace_root(previous_root)

    def test_accepted_plan_rejects_unrelated_mutation_before_execution(self):
        previous = (
            self.agent.tasks, self.agent._interaction_controller, self.agent._execution_capability_token,
            self.agent._get_workspace_root(),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "state.sqlite3")
            manager = TaskManager(store, workspace_id="bound", session_id="surface:tui")
            controller = InteractionController(
                store, workspace_id="bound", session_id="surface:tui",
            )
            proposal = controller.propose_plan({
                "title": "Create page", "summary": "Create only the accepted page.",
                "assumptions": [], "steps": [{
                    "id": "page", "title": "Create index.html",
                    "description": "Write index.html with the requested markup.",
                    "acceptance": ["index.html exists"],
                }],
            })
            controller.accept_plan(
                manager, plan_id=proposal["plan_id"], version=proposal["version"],
            )
            self.agent.tasks = manager
            self.agent._interaction_controller = controller
            self.agent._set_workspace_root(root)
            self.agent._execution_capability_token = issue_capability_token(
                "test:accepted-plan", frozenset({"write"}),
            )
            try:
                operations: set[str] = set()
                rejected = self.agent._execute_turn_action(
                    "write_file", "README.md|wrong", operation_scope="bound",
                    successful_operations=operations,
                )
                self.assertFalse(rejected.success)
                self.assertEqual(rejected.failure, "plan_action_mismatch")
                self.assertFalse((root / "README.md").exists())

                accepted = self.agent._execute_turn_action(
                    "write_file", "index.html|ready", operation_scope="bound",
                    successful_operations=operations,
                )
                self.assertTrue(accepted.success, accepted.result)
                self.assertEqual((root / "index.html").read_text(encoding="utf-8"), "ready")
            finally:
                (self.agent.tasks, self.agent._interaction_controller, self.agent._execution_capability_token,
                 previous_root) = previous
                self.agent._set_workspace_root(previous_root)

    def test_mocked_provider_flow_questions_revision_acceptance_and_agent_receipt(self):
        class LearningStub:
            def feedback_signal(self, _text): return None
            def route_profile(self, _text, _profile=None): return "coder"
            def begin_run(self, profile, _task, provider_model=None):
                return {"run_id": "interaction-flow", "profile": profile, "provider_model": provider_model}
            def artifact_context(self, _run): return "", []

        class RuntimeStub:
            def turn_start(self, **_kwargs): return None
            def turn_end(self, **_kwargs): return None

        original = (
            self.agent.tasks, self.agent._interaction_controller, self.agent.learning_engine,
            self.agent._get_workspace_root(), self.agent._execution_capability_token,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "state.sqlite3")
            self.agent.tasks = TaskManager(store, workspace_id="flow", session_id="flow")
            self.agent._interaction_controller = InteractionController(
                store, workspace_id="flow", session_id="flow",
            )
            self.agent.learning_engine = LearningStub()
            self.agent._set_workspace_root(root)
            responses = [
                'Plan:\n1. Clarify the target before changing it.\n'
                'TaskList:\n```json\n[{"id":"premature","description":"Must not execute before acceptance"}]\n```',
                'Action: {"action":"list_dir","args":"."}',
                'AskUser:\n```json\n{"questions": [}\n```',
                '```json\n{"questions":[{"id":"scope","header":"Scope",'
                '"prompt":"Which target?","choices":["Core","All"]}]}\n```',
                'PlanProposal:\n```json\n{"title":"Marker","summary":"Create a marker safely.",'
                '"assumptions":[],"steps":[{"id":"write-marker","title":"Write marker",'
                '"description":"Create marker.txt with requested content.","acceptance":["marker.txt exists"]}]}\n```',
                'PlanProposal:\n```json\n{"title":"Marker with verification","summary":"Create and verify a marker.",'
                '"assumptions":[],"steps":[{"id":"write-marker","title":"Write marker",'
                '"description":"Create marker.txt with requested content.","acceptance":["marker.txt exists"]}]}\n```',
                'Action: {"action":"write_file","args":"marker.txt|ready"}',
                'AskUser:\n```json\n{"questions":[{"id":"verify","header":"Verification",'
                '"prompt":"Which accepted check should finish the task?","choices":["File exists","Read contents"]}]}\n```',
                'Action: {"action":"read_file","args":"marker.txt"}',
                'TaskDone: 0\nMarker created and verified.',
            ]
            try:
                stream_events = []
                stream_token = self.agent._stream_event_callback.set(stream_events.append)
                with patch.object(self.agent, "_plugin_runtime_for_surface", return_value=RuntimeStub()), \
                        patch.object(self.agent, "_touch_detached_learning_heartbeat"), \
                        patch.object(self.agent, "dispatch_learning_cycle"), \
                        patch.object(self.agent, "_build_messages", return_value=[]), \
                        patch.object(self.agent, "_build_memory_context", return_value=""), \
                        patch.object(self.agent, "_classify_complexity", return_value="simple"), \
                        patch.object(self.agent, "_call_llm_with_spinner", side_effect=responses), \
                        patch.object(self.agent, "_finish_learning_run", side_effect=lambda run, receipts, task,
                                     result, records, tokens, started: result):
                    first = self.agent._chat_turn("Create a marker file")
                    self.assertIn("Which target?", first)
                    self.assertEqual(self.agent.tasks.tasks, [])
                    initial_plan = self.agent._chat_turn("Core")
                    self.assertIn("Marker (v1)", initial_plan)
                    revised_plan = self.agent._chat_turn("Add explicit verification")
                    self.assertIn("v2", revised_plan)
                    suspended = self.agent._chat_turn("accept plan")
                    self.assertIn("Which accepted check", suspended)
                    self.assertIsNotNone(self.agent._interaction_controller.state()["executing_plan"])
                    self.assertTrue(any(
                        event.get("event") == "interaction"
                        and event.get("interaction", {}).get("effective_mode") == "agent"
                        for event in stream_events
                    ))
                    executed = self.agent._chat_turn("Read contents")
                self.assertIn("Marker created", executed)
                self.assertEqual((root / "marker.txt").read_text(encoding="utf-8"), "ready")
                self.assertEqual(len(self.agent.tasks.tasks), 1)
                self.assertEqual(self.agent.tasks.tasks[0]["status"], "succeeded")
                accepted = store.list_events(
                    "interaction.plan_accepted", workspace_id="flow", session_id="flow",
                )
                self.assertEqual(len(accepted), 1)
                state = self.agent._interaction_controller.state()
                self.assertEqual(state["preference_mode"], "auto")
                self.assertEqual(state["effective_mode"], "ask")
            finally:
                if 'stream_token' in locals():
                    self.agent._stream_event_callback.reset(stream_token)
                (self.agent.tasks, self.agent._interaction_controller, self.agent.learning_engine,
                 previous_root, self.agent._execution_capability_token) = original
                self.agent._set_workspace_root(previous_root)


if __name__ == "__main__":
    unittest.main()
