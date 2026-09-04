import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import main
import server
from memory import MemoryBank
from task_engine import TaskManager


class LearningDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.original_memory = main.memory_bank
        self.original_tasks = main.tasks
        self.original_root = main._get_workspace_root()
        self.original_flags = main._SELF_LEARNING_FLAGS
        self.original_registry = main._LEARNING_FEATURE_REGISTRY
        self.original_cursor = main._learning_dispatch_cursor
        self.original_scan_time = main._last_project_scan_time
        self.original_preferences = dict(main._user_preferences)
        self.original_graph = {key: list(value) for key, value in main._knowledge_graph.items()}
        self.original_libraries = set(main._known_libraries)

    def tearDown(self):
        main.memory_bank = self.original_memory
        main.tasks = self.original_tasks
        main._set_workspace_root(self.original_root)
        main._SELF_LEARNING_FLAGS = self.original_flags
        main._LEARNING_FEATURE_REGISTRY = self.original_registry
        main._learning_dispatch_cursor = self.original_cursor
        main._last_project_scan_time = self.original_scan_time
        main._user_preferences.clear()
        main._user_preferences.update(self.original_preferences)
        main._knowledge_graph.clear()
        main._knowledge_graph.update({key: list(value) for key, value in self.original_graph.items()})
        main._known_libraries.clear()
        main._known_libraries.update(self.original_libraries)

    def _isolated_runtime(self, root: Path) -> None:
        memory = MemoryBank(
            root / "state.sqlite3", user_id="learning-user", workspace_id="learning-project",
            session_id="learning-session",
        )
        main.memory_bank = memory
        main.tasks = TaskManager(
            memory.store, user_id="learning-user", workspace_id="learning-project",
            session_id="learning-session",
        )
        main._set_workspace_root(root)

    def test_registry_has_twenty_independent_callable_features(self):
        self.assertEqual(len(main._LEARNING_FEATURE_ORDER), 20)
        self.assertEqual(set(main._LEARNING_FEATURE_ORDER), set(main._LEARNING_FEATURE_REGISTRY))
        for name in main._LEARNING_FEATURE_ORDER:
            self.assertTrue(main._LEARNING_FEATURE_REGISTRY[name]["description"])
            self.assertTrue(callable(main._LEARNING_FEATURE_REGISTRY[name]["executor"]))
            self.assertIn(name, main._SELF_LEARNING_FLAGS)

    def test_project_scan_and_memory_scoring_have_real_scoped_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "effect.py").write_text("VALUE = 7\n", encoding="utf-8")
            self._isolated_runtime(root)
            main._last_project_scan_time = 0

            scan = main.dispatch_learning_cycle(
                surface="test", trigger="smoke", max_features=1,
                feature_names=("load_project_files_into_memory",),
            )
            self.assertEqual(scan[0]["status"], "completed")
            self.assertTrue(scan[0]["changed"])
            with main.memory_bank.store.connection() as db:
                file_row = db.execute(
                    "SELECT content FROM files WHERE rel_path=? AND user_id=? AND workspace_id=?",
                    ("effect.py", "learning-user", main.memory_bank.file_scope_id),
                ).fetchone()
            self.assertIsNotNone(file_row)
            self.assertIn("VALUE = 7", file_row["content"])

            main.memory_bank.add_log("FACT: a real memory must be scored")
            scored = main.dispatch_learning_cycle(
                surface="test", trigger="smoke", max_features=1,
                feature_names=("memory_importance_scoring",),
            )
            self.assertTrue(scored[0]["changed"])
            events = main.memory_bank.store.list_events(
                "learning.memory_scored", limit=10,
                user_id="learning-user", workspace_id="learning-project",
                session_id="learning-session",
            )
            self.assertEqual(len(events), 1)
            self.assertGreaterEqual(events[0]["payload"]["count"], 1)

            lifecycle = main.memory_bank.store.list_events(
                limit=100, user_id="learning-user", workspace_id="learning-project",
                session_id="learning-session",
            )
            event_types = {event["event_type"] for event in lifecycle}
            self.assertIn("learning.feature_started", event_types)
            self.assertIn("learning.feature_completed", event_types)
            status = main.learning_feature_status()
            by_name = {item["name"]: item for item in status}
            self.assertEqual(by_name["load_project_files_into_memory"]["status"], "completed")
            self.assertEqual(by_name["memory_importance_scoring"]["status"], "completed")

    def test_round_robin_is_bounded_and_flags_are_independent(self):
        calls = []
        names = main._LEARNING_FEATURE_ORDER[:3]
        registry = dict(main._LEARNING_FEATURE_REGISTRY)
        for name in names:
            registry[name] = {
                "description": name,
                "executor": lambda _context, name=name: (
                    calls.append(name) or {"changed": True, "detail": "test effect"}
                ),
            }
        main._LEARNING_FEATURE_REGISTRY = registry
        main._SELF_LEARNING_FLAGS = {name: name in names for name in main._LEARNING_FEATURE_ORDER}
        main._learning_dispatch_cursor = 0

        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            for _ in range(3):
                result = main.dispatch_learning_cycle(surface="test", trigger="round-robin", max_features=1)
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["status"], "completed")
                self.assertTrue(result[0]["changed"])
            self.assertEqual(calls, list(names))

            main._SELF_LEARNING_FLAGS[names[1]] = False
            result = main.dispatch_learning_cycle(
                surface="test", trigger="disabled", max_features=20,
                feature_names=tuple(names),
            )
            self.assertEqual([item["feature"] for item in result], [names[0], names[2]])

    def test_feature_failure_is_recorded_without_stopping_the_cycle(self):
        names = main._LEARNING_FEATURE_ORDER[:2]
        registry = dict(main._LEARNING_FEATURE_REGISTRY)

        def fail(_context):
            raise RuntimeError("expected learning failure")

        registry[names[0]] = {"description": "failure", "executor": fail}
        registry[names[1]] = {
            "description": "success", "executor": lambda _context: {"changed": True, "detail": "ok"},
        }
        main._LEARNING_FEATURE_REGISTRY = registry
        main._SELF_LEARNING_FLAGS = {name: name in names for name in main._LEARNING_FEATURE_ORDER}

        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            result = main.dispatch_learning_cycle(
                surface="test", trigger="failure-isolation", max_features=2,
                feature_names=tuple(names),
            )
            self.assertEqual([item["status"] for item in result], ["failed", "completed"])
            events = main.memory_bank.store.list_events(
                limit=20, user_id="learning-user", workspace_id="learning-project",
                session_id="learning-session",
            )
            self.assertTrue(any(event["event_type"] == "learning.feature_failed" for event in events))
            self.assertTrue(any(event["event_type"] == "learning.feature_completed" for event in events))

    def test_web_scheduler_uses_the_shared_dispatcher(self):
        with patch.object(server._agent, "dispatch_learning_cycle", return_value=[]) as dispatch:
            server._run_scheduled_job({"payload": {"type": "learning_cycle"}})
        dispatch.assert_called_once_with(surface="web", trigger="scheduled", max_features=4)

    def test_cli_idle_loop_uses_the_shared_dispatcher(self):
        original_interaction = main._last_user_interaction
        main._last_user_interaction = 0
        try:
            with patch.object(main.time, "sleep", side_effect=[None, KeyboardInterrupt]), \
                 patch.object(main, "dispatch_learning_cycle", return_value=[]) as dispatch:
                with self.assertRaises(KeyboardInterrupt):
                    main._background_learning_loop()
        finally:
            main._last_user_interaction = original_interaction
        dispatch.assert_called_once_with(surface="cli", trigger="background", max_features=4)

    def test_web_startup_persists_a_learning_cycle_job(self):
        with patch.object(server._agent, "_prompt_and_init_deepseek"), \
             patch.object(server._agent, "_set_workspace_root"), \
             patch.object(server._agent, "_load_project_files_into_memory"), \
             patch.object(server, "_load_plugins"), \
             patch.object(server, "_trigger_hook"), \
             patch.object(server, "_recover_task_scopes", return_value=[]), \
             patch.object(server._scheduler, "list_jobs", return_value=[
                 {"payload": {"type": "task_worker"}},
             ]), \
             patch.object(server._scheduler, "schedule_every") as schedule, \
             patch.object(server._scheduler, "start"):
            errors = []

            def run_startup():
                try:
                    asyncio.run(server.startup())
                except Exception as exc:  # pragma: no cover - assertion below reports it
                    errors.append(exc)

            thread = threading.Thread(target=run_startup)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        self.assertTrue(any(
            call.args[0] == "learning-cycle"
            and call.kwargs.get("payload") == {"type": "learning_cycle"}
            for call in schedule.call_args_list
        ))


if __name__ == "__main__":
    unittest.main()
