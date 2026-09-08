import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from memory import MemoryBank
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
            "[{\"id\": \"marker\", \"description\": \"Create the repeat marker\"}]\n```\n"
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
