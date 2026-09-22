import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from event_store import EventStore
from interaction import (
    InteractionController,
    InteractionError,
    is_plan_acceptance,
    mode_capabilities,
    parse_control_block,
    route_mode,
)
from task_engine import TaskManager
from capability_tokens import issue_capability_token
from workspace_context import resolve_launch_context


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = EventStore(Path(self.directory.name) / "state.sqlite3")
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

    def test_project_interaction_state_is_isolated_from_other_projects(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as first, \
                tempfile.TemporaryDirectory() as second:
            first_context = resolve_launch_context(home=home, project_path=first)
            second_context = resolve_launch_context(home=home, project_path=second)
            first_scope = main.interaction_workspace_id(first_context)
            second_scope = main.interaction_workspace_id(second_context)
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
                main.interaction_workspace_id(global_context), main.memory_bank.workspace_id,
            )

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
        previous_tasks = main.tasks
        previous_controller = main._interaction_controller
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
        main.tasks = manager
        main._interaction_controller = self.controller
        mode_token = main._active_interaction_mode.set("agent")
        try:
            repeated = "Plan:\n" + "\n".join(
                f"{index}. Perform step {index}." for index in range(1, 9)
            )
            main._tasks_from_plan(repeated)
            main._tasks_from_plan(repeated)
            main._observe_model_response(
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
            main.tasks = previous_tasks
            main._interaction_controller = previous_controller
            main._active_interaction_mode.reset(mode_token)

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

    def test_provider_shaped_plan_is_normalized_but_actions_are_not(self):
        shaped = json.dumps({
            "plan_name": "Status page", "mode": "plan",
            "do_not_execute_until_approved": True,
            "overview": "Build after approval.", "assumptions": ["Python exists"],
            "steps": [{"step": 1, "title": "Create page", "details": "Write index.html.",
                       "acceptance_criteria": ["index.html exists"]}],
        })
        parsed = main._observe_model_response(shaped)
        self.assertEqual(parsed["plan_proposal"]["title"], "Status page")
        self.assertEqual(parsed["plan_proposal"]["steps"][0]["id"], "step-1")
        self.assertEqual(parsed["tool_calls"], [])

        executable = json.dumps({
            "mode": "plan", "plan_name": "Unsafe", "overview": "Write now.",
            "steps": [{"title": "Write", "details": "Write README.",
                       "acceptance_criteria": ["written"], "action": "write_file"}],
        })
        rejected = main._observe_model_response(executable)
        self.assertIsNone(rejected["plan_proposal"])

    def test_plan_mode_converts_plain_plan_and_discards_early_actions(self):
        mode_token = main._active_interaction_mode.set("plan")
        previous_controller = main._interaction_controller
        main._interaction_controller = self.controller
        try:
            parsed = main._parse_model_response(
                "Plan:\n"
                "1. Create index.html with the requested heading.\n"
                "2. Serve it locally and verify HTTP 200.\n"
                'Action: {"action":"write_file","args":"index.html|unsafe"}'
            )
            self.assertEqual(parsed["tool_calls"], [])
            self.assertIsNone(parsed["protocol_error"])
            rendered = main._interaction_gate(parsed, "Create a local page")
            self.assertIn("Create index.html", rendered)
            self.assertEqual(len(self.controller.state()["pending_plan"]["steps"]), 2)
            self.assertEqual(
                self.store.list_events(
                    "execution.receipt", workspace_id="workspace", session_id="session",
                ),
                [],
            )

            action_only = main._parse_model_response(
                'Action: {"action":"run_cmd","args":"python -m http.server"}'
            )
            self.assertEqual(action_only["tool_calls"], [])
            self.assertIn("rejected an executable action", action_only["protocol_error"])
        finally:
            main._interaction_controller = previous_controller
            main._active_interaction_mode.reset(mode_token)

    def test_control_blocks_combined_with_actions_fail_closed(self):
        parsed = main._observe_model_response(
            'AskUser:\n```json\n{"questions":[{"id":"scope","header":"Scope",'
            '"prompt":"Which target?","choices":["Core","All"]}]}\n```\n'
            'Action: {"action":"write_file","args":"unsafe.txt|bad"}'
        )
        self.assertIn("only control block", parsed["protocol_error"])
        self.assertEqual(parsed["tool_calls"][0]["action"], "write_file")
        self.assertIsNotNone(parsed["question"])
        previous_controller = main._interaction_controller
        token = main._interaction_controls_enabled.set(False)
        main._interaction_controller = self.controller
        try:
            question_only = main._observe_model_response(
                'AskUser:\n```json\n{"questions":[{"id":"scope","header":"Scope",'
                '"prompt":"Which target?","choices":["Core","All"]}]}\n```'
            )
            self.assertIn("non-interactive", main._interaction_gate(question_only, "Inspect"))
            self.assertIsNone(self.controller.state()["pending_question"])
        finally:
            main._interaction_controller = previous_controller
            main._interaction_controls_enabled.reset(token)

    def test_read_only_modes_deny_mutating_and_dynamic_tools(self):
        previous_token = main._execution_capability_token
        previous_root = main._get_workspace_root()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "notes.txt").write_text("safe", encoding="utf-8")
            main._set_workspace_root(root)
            main._execution_capability_token = issue_capability_token(
                "test:plan", mode_capabilities(
                    frozenset({"read", "write", "shell", "network", "git", "browser", "dynamic"}),
                    "plan",
                ),
            )
            main.AVAILABLE_TOOLS["temporary_dynamic_tool"] = lambda args: f"ran:{args}"
            try:
                self.assertIn("safe", main._run_tool("read_file", "notes.txt"))
                for action, args in (
                    ("write_file", "blocked.txt|bad"),
                    ("run_cmd", "touch blocked-shell.txt"),
                    ("git_commit", "blocked"),
                    ("browser_click", "button"),
                    ("temporary_dynamic_tool", "value"),
                ):
                    result = main._run_tool(action, args)
                    self.assertTrue(result.startswith("Error:"), (action, result))
                self.assertFalse((root / "blocked.txt").exists())
                self.assertFalse((root / "blocked-shell.txt").exists())
            finally:
                main.AVAILABLE_TOOLS.pop("temporary_dynamic_tool", None)
                main._execution_capability_token = previous_token
                main._set_workspace_root(previous_root)

    def test_accepted_plan_rejects_unrelated_mutation_before_execution(self):
        previous = (
            main.tasks, main._interaction_controller, main._execution_capability_token,
            main._get_workspace_root(),
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
            main.tasks = manager
            main._interaction_controller = controller
            main._set_workspace_root(root)
            main._execution_capability_token = issue_capability_token(
                "test:accepted-plan", frozenset({"write"}),
            )
            try:
                operations: set[str] = set()
                rejected = main._execute_turn_action(
                    "write_file", "README.md|wrong", operation_scope="bound",
                    successful_operations=operations,
                )
                self.assertFalse(rejected.success)
                self.assertEqual(rejected.failure, "plan_action_mismatch")
                self.assertFalse((root / "README.md").exists())

                accepted = main._execute_turn_action(
                    "write_file", "index.html|ready", operation_scope="bound",
                    successful_operations=operations,
                )
                self.assertTrue(accepted.success, accepted.result)
                self.assertEqual((root / "index.html").read_text(encoding="utf-8"), "ready")
            finally:
                (main.tasks, main._interaction_controller, main._execution_capability_token,
                 previous_root) = previous
                main._set_workspace_root(previous_root)

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
            main.tasks, main._interaction_controller, main.learning_engine,
            main._get_workspace_root(), main._execution_capability_token,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "state.sqlite3")
            main.tasks = TaskManager(store, workspace_id="flow", session_id="flow")
            main._interaction_controller = InteractionController(
                store, workspace_id="flow", session_id="flow",
            )
            main.learning_engine = LearningStub()
            main._set_workspace_root(root)
            responses = [
                'Plan:\n1. Clarify the target before changing it.\n'
                'TaskList:\n```json\n[{"id":"premature","description":"Must not execute before acceptance"}]\n```',
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
                stream_token = main._stream_event_callback.set(stream_events.append)
                with patch.object(main, "_plugin_runtime_for_surface", return_value=RuntimeStub()), \
                        patch.object(main, "_touch_detached_learning_heartbeat"), \
                        patch.object(main, "dispatch_learning_cycle"), \
                        patch.object(main, "_summarize_old_turns"), \
                        patch.object(main, "_build_messages", return_value=[]), \
                        patch.object(main, "_build_memory_context", return_value=""), \
                        patch.object(main, "_classify_complexity", return_value="simple"), \
                        patch.object(main, "_call_llm_with_spinner", side_effect=responses), \
                        patch.object(main, "_finish_learning_run", side_effect=lambda run, receipts, task,
                                     result, records, tokens, started: result):
                    first = main._chat_turn("Create a marker file")
                    self.assertIn("Which target?", first)
                    self.assertEqual(main.tasks.tasks, [])
                    initial_plan = main._chat_turn("Core")
                    self.assertIn("Marker (v1)", initial_plan)
                    revised_plan = main._chat_turn("Add explicit verification")
                    self.assertIn("v2", revised_plan)
                    suspended = main._chat_turn("accept plan")
                    self.assertIn("Which accepted check", suspended)
                    self.assertIsNotNone(main._interaction_controller.state()["executing_plan"])
                    self.assertTrue(any(
                        event.get("event") == "interaction"
                        and event.get("interaction", {}).get("effective_mode") == "agent"
                        for event in stream_events
                    ))
                    executed = main._chat_turn("Read contents")
                self.assertIn("Marker created", executed)
                self.assertEqual((root / "marker.txt").read_text(encoding="utf-8"), "ready")
                self.assertEqual(len(main.tasks.tasks), 1)
                self.assertEqual(main.tasks.tasks[0]["status"], "succeeded")
                accepted = store.list_events(
                    "interaction.plan_accepted", workspace_id="flow", session_id="flow",
                )
                self.assertEqual(len(accepted), 1)
                state = main._interaction_controller.state()
                self.assertEqual(state["preference_mode"], "auto")
                self.assertEqual(state["effective_mode"], "ask")
            finally:
                if 'stream_token' in locals():
                    main._stream_event_callback.reset(stream_token)
                (main.tasks, main._interaction_controller, main.learning_engine,
                 previous_root, main._execution_capability_token) = original
                main._set_workspace_root(previous_root)


if __name__ == "__main__":
    unittest.main()
