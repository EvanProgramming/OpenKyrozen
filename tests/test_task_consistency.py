import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from memory import MemoryBank
from learning_engine import LearningEngine
from task_engine import TaskManager


class TaskConsistencyTests(unittest.TestCase):
    def test_explicit_task_ids_win_over_description_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryBank(Path(directory) / "state.sqlite3").store
            manager = TaskManager(store, workspace_id="project", session_id="turn")
            block = (
                'TaskList:\n```json\n'
                '[{"id":"step-a","description":"same work"},'
                '{"id":"step-b","description":"same work"}]\n```'
            )
            manager.from_llm_block(block)
            self.assertEqual(len(manager.tasks), 2)
            ids = {task["id"] for task in manager.tasks}
            self.assertEqual(len(ids), 2)

            first = manager.tasks[0]
            manager.request_completion(0)
            manager.record_evidence(task_id=first["id"], action="verify", result="passed",
                                    success=True, acceptance="verified")
            manager.from_llm_block(
                'TaskList:\n```json\n'
                '[{"id":"step-a","description":"same work (updated)"},'
                '{"id":"step-b","description":"same work"}]\n```'
            )
            self.assertEqual(len(manager.tasks), 2)
            self.assertEqual(manager.tasks[0]["status"], "succeeded")
            self.assertEqual(manager.tasks[0]["description"], "same work (updated)")

            # A legacy item without an id still uses its description as the
            # compatibility merge key, without collapsing the explicit pair.
            manager.from_llm_block('TaskList:\n```json\n["same work"]\n```')
            self.assertEqual(len(manager.tasks), 2)

    def test_protocol_blocks_are_not_user_facing(self):
        response = (
            "Plan:\n1. Write the file\n2. Verify it\n\n"
            "The file was written and verified.\n"
            'Action: {"action":"write_file","args":"result.txt|ok"}\n'
            "TaskDone: 0"
        )
        clean = main._clean_final_response(response)
        self.assertEqual(clean, "The file was written and verified.")
        self.assertNotIn("Plan:", clean)
        self.assertNotIn("Action:", clean)
        self.assertNotIn("TaskDone:", clean)

    def test_inline_and_malformed_protocol_is_removed_before_learning_persistence(self):
        raw = (
            'I checked the project. Action: list_dir "." Action: run_cmd "git status" '
            '< invoke name="read_file">notes.py</ calls> '
            'The action word and invoke word are ordinary prose.'
        )
        expected = "I checked the project. The action word and invoke word are ordinary prose."
        clean = main._clean_final_response(raw)
        self.assertEqual(" ".join(clean.split()), expected)
        self.assertNotRegex(clean, r"Action:|</?\s*(?:invoke|parameter|calls)\b")

        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryBank(Path(directory) / "state.sqlite3", workspace_id="protocol")
            original_memory, original_learning = main.memory_bank, main.learning_engine
            main.memory_bank = memory
            main.learning_engine = LearningEngine(memory)
            try:
                result = main._finish_learning_run(
                    {"run_id": "protocol-run", "profile": "coder"}, [],
                    "What do you remember about this project and my preferences?",
                    raw, [], 0, time.time(),
                )
                memory.add_log(f"User: prompt\nAssistant: {result}")
            finally:
                main.memory_bank, main.learning_engine = original_memory, original_learning
            completed = memory.store.list_events("learning.run_completed", workspace_id="protocol")
            self.assertEqual(" ".join(completed[-1]["payload"]["result"].split()), expected)
            self.assertNotRegex(memory.get_recent(1)[0], r"Action:|</?\s*(?:invoke|parameter|calls)\b")

    def test_progress_counts_done_and_succeeded_consistently(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryBank(Path(directory) / "state.sqlite3").store
            manager = TaskManager(store, workspace_id="project", session_id="progress")
            manager.add_task("legacy complete")
            manager.add_task("durable complete")
            manager.tasks[0]["status"] = "done"
            manager.tasks[1]["status"] = "succeeded"
            original = main.tasks
            main.tasks = manager
            try:
                hint = main._build_task_progress_hint()
            finally:
                main.tasks = original
            self.assertIn("(2/2 succeeded)", hint)
            self.assertIn("✓ legacy complete", hint)
            self.assertIn("✓ durable complete", hint)

    def test_blocked_tasks_are_visible_and_cannot_be_summarised_as_complete(self):
        class StubLearning:
            def feedback_signal(self, _text):
                return None

            def route_profile(self, _text, _profile=None):
                return "coder"

            def begin_run(self, profile, _task, provider_model=None):
                return {"run_id": "blocked-run", "profile": profile, "provider_model": provider_model}

            def artifact_context(self, _run):
                return "", []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MemoryBank(root / "state.sqlite3").store
            manager = TaskManager(store, workspace_id="project", session_id="blocked-test")
            index = manager.add_task("reconcile the four actions")
            manager.set_status(index, "blocked")
            original_root = main._get_workspace_root()
            original_tasks = main.tasks
            original_learning = main.learning_engine
            main._set_workspace_root(root)
            main.tasks = manager
            main.learning_engine = StubLearning()
            try:
                panel = main._tasks_panel_content()
                self.assertIn("blocked=1", panel)
                self.assertIn("Tasks require attention", panel)
                self.assertNotIn("All tasks complete", panel)
                with patch.object(main, "_classify_complexity", return_value="simple"), \
                     patch.object(main, "_build_messages", return_value=[]), \
                     patch.object(main, "_build_memory_context", return_value=""), \
                     patch.object(main, "_call_llm_with_spinner", side_effect=[
                         'Action: {"action":"read_file","args":"first.txt"}\n'
                         'Action: {"action":"read_file","args":"second.txt"}',
                         "All requested file and Git operations completed.",
                     ]), \
                     patch.object(main, "_get_llm_response", return_value="All requested file and Git operations completed."), \
                     patch.object(main, "_update_tasks_panel"), \
                     patch.object(main, "_finish_learning_run", side_effect=lambda run, receipts, task,
                                  result, records, tokens, started: result):
                    reply = main._chat_turn("continue the work", clear_tasks=False)
            finally:
                main._set_workspace_root(original_root)
                main.tasks = original_tasks
                main.learning_engine = original_learning

            self.assertIn("blocked", reply.lower())
            self.assertIn("Recovery required", reply)
            self.assertNotIn("All requested file and Git operations completed.", reply)
            self.assertEqual(
                store.list_tasks(workspace_id="project", session_id="blocked-test")[0]["status"],
                "blocked",
            )

    def test_chat_turn_executes_multiple_real_tools_and_returns_latest_prose(self):
        class StubLearning:
            def feedback_signal(self, _text):
                return None

            def route_profile(self, _text, _profile=None):
                return "coder"

            def begin_run(self, profile, _task, provider_model=None):
                return {"run_id": "test-run", "profile": profile, "provider_model": provider_model}

            def artifact_context(self, _run):
                return "", []

        responses = [
            'Action: {"action":"write_file","args":"one.txt|one"}',
            'Action: {"action":"write_file","args":"two.txt|two"}',
            "Both files were written successfully.",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_root = main._get_workspace_root()
            original_tasks = main.tasks
            original_learning = main.learning_engine
            main._set_workspace_root(root)
            main.tasks = TaskManager(MemoryBank(root / "state.sqlite3").store,
                                     workspace_id="project", session_id="chat-test")
            main.learning_engine = StubLearning()
            try:
                with patch.object(main, "_classify_complexity", return_value="simple"), \
                     patch.object(main, "_build_messages", return_value=[]), \
                     patch.object(main, "_build_memory_context", return_value=""), \
                     patch.object(main, "_call_llm_with_spinner", side_effect=responses), \
                     patch.object(main, "_get_llm_response", return_value=""), \
                     patch.object(main, "_update_tasks_panel"), \
                     patch.object(main, "_finish_learning_run", side_effect=lambda run, receipts, task,
                                  result, records, tokens, started: result):
                    reply = main._chat_turn("write two files", clear_tasks=True)
            finally:
                main._set_workspace_root(original_root)
                main.tasks = original_tasks
                main.learning_engine = original_learning

            self.assertEqual((root / "one.txt").read_text(encoding="utf-8"), "one")
            self.assertEqual((root / "two.txt").read_text(encoding="utf-8"), "two")
            self.assertIn("Both files were written successfully.", reply)
            self.assertNotIn("Action:", reply)
            self.assertNotIn("Plan:", reply)

    def test_receipts_complete_verified_task_and_refuse_duplicate_write(self):
        class StubLearning:
            def feedback_signal(self, _text):
                return None

            def route_profile(self, _text, _profile=None):
                return "coder"

            def begin_run(self, profile, _task, provider_model=None):
                return {"run_id": "receipt-run", "profile": profile, "provider_model": provider_model}

            def artifact_context(self, _run):
                return "", []

        responses = [
            "Plan:\n1. Create the repeat marker\nTaskList:\n```json\n"
            "[{\"id\": \"marker\", \"description\": \"Create the repeat marker\","
            "\"action\":\"write_file\",\"args\":\"repeat-marker.txt|REPEAT_OK\"}]\n```\n"
            'Action: {"action":"write_file","args":"repeat-marker.txt|REPEAT_OK"}',
            'Action: {"action":"write_file","args":"repeat-marker.txt|REPEAT_OK"}',
            "The marker is complete.",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_root = main._get_workspace_root()
            original_tasks = main.tasks
            original_learning = main.learning_engine
            original_write = main.AVAILABLE_TOOLS["write_file"]
            writes: list[str] = []

            def counted_write(args: str) -> str:
                writes.append(args)
                return original_write(args)

            main._set_workspace_root(root)
            store = MemoryBank(root / "state.sqlite3").store
            main.tasks = TaskManager(store, workspace_id="project", session_id="receipt-test")
            main.learning_engine = StubLearning()
            try:
                with patch.object(main, "_classify_complexity", return_value="complex"), \
                     patch.object(main, "_build_messages", return_value=[]), \
                     patch.object(main, "_build_memory_context", return_value=""), \
                     patch.object(main, "_call_llm_with_spinner", side_effect=responses), \
                     patch.object(main, "_get_llm_response", return_value=""), \
                     patch.object(main, "_update_tasks_panel"), \
                     patch.object(main, "_finish_learning_run", side_effect=lambda run, receipts, task,
                                  result, records, tokens, started: result), \
                     patch.dict(main.AVAILABLE_TOOLS, {"write_file": counted_write}):
                    reply = main._chat_turn("Create the repeat marker", clear_tasks=True)
            finally:
                main._set_workspace_root(original_root)
                main.tasks = original_tasks
                main.learning_engine = original_learning

            self.assertEqual(writes, ["repeat-marker.txt|REPEAT_OK"])
            self.assertEqual((root / "repeat-marker.txt").read_text(encoding="utf-8"), "REPEAT_OK")
            self.assertEqual(store.list_tasks(workspace_id="project", session_id="receipt-test")[0]["status"], "succeeded")
            receipts = store.list_events("execution.receipt", workspace_id="project", session_id="receipt-test")
            self.assertEqual(len(receipts), 2)
            self.assertEqual({item["payload"]["operation_id"] for item in receipts},
                             {receipts[0]["payload"]["operation_id"]})
            self.assertEqual({item["payload"]["success"] for item in receipts}, {True, False})
            self.assertIn("duplicate_operation", {item["payload"]["failure"] for item in receipts})
            self.assertIn("marker is complete", reply.lower())

    def test_multi_task_plan_reconciles_receipts_without_taskdone_and_survives_restart(self):
        class StubLearning:
            def feedback_signal(self, _text):
                return None

            def route_profile(self, _text, _profile=None):
                return "coder"

            def begin_run(self, profile, _task, provider_model=None):
                return {"run_id": "multi-task-run", "profile": profile, "provider_model": provider_model}

            def artifact_context(self, _run):
                return "", []

        responses = [
            "Plan:\n1. Check the repository status\n2. Create the audit file\n"
            "3. Stage the audit file\n4. Commit the audit file\n"
            "TaskList:\n```json\n"
            "[{\"id\":\"status\",\"description\":\"Check the repository status\",\"action\":\"git_status\",\"args\":\".\"},"
            "{\"id\":\"write\",\"description\":\"Create the audit file\",\"action\":\"write_file\",\"args\":\"audit-task.txt|AUDIT_OK\"},"
            "{\"id\":\"stage\",\"description\":\"Stage the audit file\",\"action\":\"git_add\",\"args\":\"audit-task.txt\"},"
            "{\"id\":\"commit\",\"description\":\"Commit the audit file\",\"action\":\"git_commit\",\"args\":\"audit: post-merge\"}]\n```\n"
            'Action: {"action":"git_status","args":"."}',
            'Action: {"action":"write_file","args":"audit-task.txt|AUDIT_OK"}',
            'Action: {"action":"git_add","args":"audit-task.txt"}',
            'Action: {"action":"git_commit","args":"audit: post-merge"}',
            "Committed audit file.",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "OpenKyrozen Test"], cwd=root, check=True)
            store = MemoryBank(root / "state.sqlite3").store
            original_root = main._get_workspace_root()
            original_tasks = main.tasks
            original_learning = main.learning_engine
            main._set_workspace_root(root)
            main.tasks = TaskManager(store, workspace_id="project", session_id="multi-task")
            main.learning_engine = StubLearning()
            try:
                with patch.object(main, "_classify_complexity", return_value="complex"), \
                     patch.object(main, "_build_messages", return_value=[]), \
                     patch.object(main, "_build_memory_context", return_value=""), \
                     patch.object(main, "_call_llm_with_spinner", side_effect=responses), \
                     patch.object(main, "_get_llm_response", return_value=""), \
                     patch.object(main, "_update_tasks_panel"), \
                     patch.object(main, "_finish_learning_run", side_effect=lambda run, receipts, task,
                                  result, records, tokens, started: result):
                    reply = main._chat_turn("Create the audit file and commit it with Git.", clear_tasks=True)
            finally:
                main._set_workspace_root(original_root)
                main.tasks = original_tasks
                main.learning_engine = original_learning

            rows = store.list_tasks(workspace_id="project", session_id="multi-task")
            self.assertEqual(len(rows), 4)
            self.assertEqual({row["status"] for row in rows}, {"succeeded"})
            self.assertTrue((root / "audit-task.txt").is_file())
            self.assertIn("audit: post-merge", subprocess.run(
                ["git", "log", "-1", "--pretty=%s"], cwd=root, capture_output=True, text=True, check=True,
            ).stdout)
            receipts = store.list_events("execution.receipt", workspace_id="project", session_id="multi-task")
            self.assertEqual(len(receipts), 4)
            self.assertEqual({event["task_id"] for event in receipts}, {row["id"] for row in rows})
            self.assertIn("Committed audit file", reply)
            reopened = TaskManager(store, workspace_id="project", session_id="multi-task")
            self.assertEqual(reopened.recover(), [])

    def test_web_natural_plan_receipts_match_and_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("Usage: run the baseline check.\n", encoding="utf-8")
            store = MemoryBank(root / "state.sqlite3").store
            manager = TaskManager(store, workspace_id="web-project", session_id="web-natural")
            manager.add_task("Read the existing Python file before making changes.",
                             checkpoint={"action": "read_file", "args": "notes.py"})
            manager.add_task("Run the project check to confirm the baseline is still valid.",
                             checkpoint={"action": "run_cmd", "args": "python -c \"print('baseline-ok')\""})
            manager.add_task("Make one small, focused README usage improvement.",
                             checkpoint={"action": "write_file", "args": "README.md|Usage: run the baseline check.\nSee the quickstart."})

            original_root = main._get_workspace_root()
            original_tasks = main.tasks
            main._set_workspace_root(root)
            main.tasks = manager
            try:
                receipts = [
                    main._make_execution_receipt(
                        action="read_file", args="notes.py", authorized=True, started_at=main.utc_now(),
                        success=True, result="notes.py contents", operation_scope="web-natural",
                    ),
                    main._make_execution_receipt(
                        action="run_cmd", args="python -c \"print('baseline-ok')\"", authorized=True,
                        started_at=main.utc_now(), success=True, result="baseline-ok",
                        operation_scope="web-natural",
                    ),
                    main._make_execution_receipt(
                        action="write_file", args="README.md|Usage: run the baseline check.\nSee the quickstart.",
                        authorized=True, started_at=main.utc_now(), success=True,
                        result="updated README.md", operation_scope="web-natural",
                    ),
                ]
                records = [main._record_turn_receipt(receipt) for receipt in receipts]
            finally:
                main._set_workspace_root(original_root)
                main.tasks = original_tasks

            rows = store.list_tasks(workspace_id="web-project", session_id="web-natural")
            self.assertEqual(len(rows), 3)
            self.assertEqual({row["status"] for row in rows}, {"succeeded"})
            self.assertEqual({record["task_id"] for record in records}, {row["id"] for row in rows})
            persisted = store.list_events(
                "execution.receipt", workspace_id="web-project", session_id="web-natural",
            )
            self.assertEqual(len(persisted), 3)
            self.assertTrue(all(event["task_id"] for event in persisted))
            reopened = TaskManager(store, workspace_id="web-project", session_id="web-natural")
            self.assertEqual(reopened.recover(), [])

    def test_receipt_cannot_satisfy_a_neighboring_task_without_exact_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryBank(Path(directory) / "state.sqlite3").store
            manager = TaskManager(store, workspace_id="project", session_id="receipt-match")
            read_index = manager.add_task(
                "Inspect the README", checkpoint={"action": "read_file", "args": "README.md"}
            )
            write_index = manager.add_task(
                "Apply the README edit", checkpoint={"action": "write_file", "args": "README.md|updated"}
            )
            manager.request_completion(read_index)
            manager.request_completion(write_index)

            unmatched = manager.record_evidence(
                action="read_file", args="notes.py", result="notes", success=True,
                acceptance="verified read",
            )
            self.assertNotIn("task_id", unmatched)
            self.assertEqual([task["status"] for task in manager.tasks], ["pending", "pending"])

            matched = manager.record_evidence(
                action="read_file", args="README.md", result="README", success=True,
            )
            self.assertEqual(matched["task_id"], manager.tasks[read_index]["id"])
            self.assertEqual(manager.tasks[read_index]["status"], "succeeded")
            self.assertEqual(manager.tasks[write_index]["status"], "pending")

            reopened = TaskManager(store, workspace_id="project", session_id="receipt-match")
            self.assertEqual(reopened.recover()[0]["status"], "pending")

    def test_blocked_durable_tasks_cannot_be_recorded_as_verified_learning_success(self):
        class LearningRecorder:
            def __init__(self):
                self.completed = None
                self.outcome = None

            def complete_run(self, *args, **kwargs):
                self.completed = kwargs

            def record_outcome(self, *args, **kwargs):
                self.outcome = kwargs
                return []

        with tempfile.TemporaryDirectory() as directory:
            store = MemoryBank(Path(directory) / "state.sqlite3").store
            manager = TaskManager(store, workspace_id="project", session_id="learning-blocked")
            index = manager.add_task(
                "Run the verification command",
                checkpoint={"action": "run_cmd", "args": "python -m unittest"},
            )
            manager.set_status(index, "blocked")
            recorder = LearningRecorder()
            original_tasks, original_learning = main.tasks, main.learning_engine
            main.tasks, main.learning_engine = manager, recorder
            try:
                main._finish_learning_run(
                    {"run_id": "blocked-learning", "profile": "coder"}, [],
                    "run the verification command", "The command passed.",
                    [{"action": "run_cmd", "success": True, "acceptance": "tests passed"}],
                    10, time.time(),
                )
            finally:
                main.tasks, main.learning_engine = original_tasks, original_learning

            self.assertFalse(recorder.completed["eligible"])
            self.assertEqual(recorder.completed["task_statuses"][0]["status"], "blocked")
            self.assertFalse(recorder.outcome["verified"])
            self.assertFalse(recorder.outcome["success"])

    def test_unwrapped_provider_aliases_are_bounded_and_canonicalized(self):
        response = (
            "Visible progress. bash: git status\n"
            "read_file: README.md\n"
            "run_cmd: ```bash\nprintf alias-ok\n```\n"
        )
        self.assertEqual(
            main._collect_tool_calls(response),
            [
                {"action": "run_cmd", "args": "git status"},
                {"action": "read_file", "args": "README.md"},
                {"action": "run_cmd", "args": "printf alias-ok"},
            ],
        )
        self.assertEqual(
            main._collect_tool_calls("run_cmd:\n```bash\nprintf newline-fence\n```")[-1],
            {"action": "run_cmd", "args": "printf newline-fence"},
        )

        malformed = main._parse_model_response("run_cmd: ```bash\nprintf alias-ok")
        self.assertEqual(malformed["tool_calls"], [])
        self.assertIn("No tool was executed", malformed["protocol_error"])

    def test_tool_names_in_prose_are_not_filtered_or_executed(self):
        prose = (
            "Use run_cmd: to execute a command.\n"
            "The tool name is read_file: and it reads text.\n"
            "For example, browser_open: accepts a URL.\n"
            "The following is a label, not a call: write_file: README.md\n"
            "Use plan: for a project plan.\n"
            "工具名称是 read_file:，用于读取文本。\n"
            "run_cmd: is a shell command tool."
        )
        self.assertEqual(main.DeepSeekDSMLFilter().feed(prose, final=True), prose)
        self.assertEqual(main._clean_final_response(prose), prose)
        parsed = main._parse_model_response(prose)
        self.assertEqual(parsed["tool_calls"], [])
        self.assertIsNone(parsed["protocol_error"])

    def test_malformed_unwrapped_alias_stops_without_retrying_or_executing(self):
        class StubLearning:
            def feedback_signal(self, _text):
                return None

            def route_profile(self, _text, _profile=None):
                return "coder"

            def begin_run(self, profile, _task, provider_model=None):
                return {"run_id": "malformed-alias", "profile": profile, "provider_model": provider_model}

            def artifact_context(self, _run):
                return "", []

        original_root = main._get_workspace_root()
        original_tasks, original_learning = main.tasks, main.learning_engine
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main._set_workspace_root(root)
            main.tasks = TaskManager(MemoryBank(root / "state.sqlite3").store,
                                     workspace_id="project", session_id="malformed-alias")
            main.learning_engine = StubLearning()
            try:
                with patch.object(main, "_classify_complexity", return_value="simple"), \
                     patch.object(main, "_build_messages", return_value=[]), \
                     patch.object(main, "_build_memory_context", return_value=""), \
                     patch.object(main, "_call_llm_with_spinner",
                                  return_value="run_cmd: ```bash\nprintf alias-ok" ) as provider, \
                     patch.object(main, "_finish_learning_run",
                                  side_effect=lambda run, receipts, task, result, records, tokens, started: result):
                    reply = main._chat_turn("run the audit command", clear_tasks=True)
            finally:
                main._set_workspace_root(original_root)
                main.tasks, main.learning_engine = original_tasks, original_learning

        self.assertEqual(provider.call_count, 1)
        self.assertIn("No tool was executed", reply)

    def test_failed_operation_can_retry_and_provider_deadline_is_honest(self):
        operations: set[str] = set()
        with patch.object(main, "run_command", side_effect=[
            main.CommandResult("Exit code 1", False, 1, "nonzero_exit"),
            main.CommandResult("(no output)", True, 0),
        ]):
            failed = main._execute_turn_action("run_cmd", "python -c pass", operation_scope="retry", successful_operations=operations)
            retried = main._execute_turn_action("run_cmd", "python -c pass", operation_scope="retry", successful_operations=operations)
        self.assertFalse(failed.success)
        self.assertEqual(failed.failure, "nonzero_exit")
        self.assertTrue(retried.success)

        class SlowProvider:
            def chat(self, _messages, _model):
                time.sleep(0.2)
                return "late", None

        original_provider = main.llm_provider
        main.llm_provider = SlowProvider()
        try:
            with patch.object(main, "_provider_timeout_seconds", return_value=0.01):
                started = time.monotonic()
                response = main._get_llm_response([])
        finally:
            main.llm_provider = original_provider
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertIn("Provider timed out", response)


if __name__ == "__main__":
    unittest.main()
