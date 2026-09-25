import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

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
        self.original_hydrated_preferences = dict(main._hydrated_preferences)
        self.original_preference_scope = main._preference_scope
        self.original_graph = {key: list(value) for key, value in main._knowledge_graph.items()}
        self.original_project_graph = main._project_graph
        self.original_libraries = set(main._known_libraries)
        self.original_provider = main.llm_provider
        self.original_provider_config = main._provider_config

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
        main._hydrated_preferences.clear()
        main._hydrated_preferences.update(self.original_hydrated_preferences)
        main._preference_scope = self.original_preference_scope
        main._knowledge_graph.clear()
        main._knowledge_graph.update({key: list(value) for key, value in self.original_graph.items()})
        main._project_graph = self.original_project_graph
        main._known_libraries.clear()
        main._known_libraries.update(self.original_libraries)
        main.llm_provider = self.original_provider
        main._provider_config = self.original_provider_config

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
            main._project_graph = Mock()
            main._project_graph.snapshot.return_value = {
                "status": "missing", "nodes": 0, "edges": 0, "communities": 0,
                "mini": {"nodes": [], "edges": []},
            }
            main._project_graph.refresh_async.return_value = True
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
            self.assertIsNone(file_row)
            main._project_graph.refresh_async.assert_called_once_with()

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

    def test_verified_preferences_hydrate_in_fresh_runtime_and_prompt(self):
        with tempfile.TemporaryDirectory(prefix="openkyrozen-preferences-") as directory:
            root = Path(directory)
            db_path = root / "state.sqlite3"
            memory = MemoryBank(db_path, user_id="local", workspace_id="default")
            engine = main.LearningEngine(memory)
            self.assertEqual(
                engine.submit("preference", "PREF: naming_style=snake_case", evidence_id="signal-one")["status"],
                "candidate",
            )
            self.assertEqual(
                engine.submit("preference", "PREF: naming_style=snake_case", evidence_id="signal-two")["status"],
                "active",
            )
            env = os.environ.copy()
            env.update({
                "HOME": str(root), "KYROZEN_DB_PATH": str(db_path),
                "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_WORKSPACE_ROOT": str(root),
                "PYTHONPATH": str(Path(__file__).parents[1]),
            })
            completed = subprocess.run(
                [sys.executable, "-c", (
                    "import main; "
                    "print(main._build_preference_context()); "
                    "print('\\n'.join(item['content'] for item in main._build_messages('implement feature')))"
                )],
                cwd=Path(__file__).parents[1], env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("naming_style=snake_case", completed.stdout)
            self.assertIn("Known user preferences", completed.stdout)

    def test_detected_preference_promotes_and_survives_restart(self):
        with tempfile.TemporaryDirectory(prefix="openkyrozen-detected-preference-") as directory:
            root = Path(directory)
            db_path = root / "state.sqlite3"
            env = os.environ.copy()
            env.update({
                "HOME": str(root), "KYROZEN_DB_PATH": str(db_path),
                "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_WORKSPACE_ROOT": str(root),
                "PYTHONPATH": str(Path(__file__).parents[1]),
            })
            observe = subprocess.run(
                [sys.executable, "-c", (
                    "import main; "
                    f"main.configure_launch_context(project_path={str(root)!r}); "
                    "[main.dispatch_learning_cycle(surface='cli', trigger='turn', max_features=1, "
                    "user_input='Please use concise Python and snake_case names.', "
                    "feature_names=('detect_user_preferences',)) for _ in range(2)]; "
                    "print(main.learning_engine.status(100)[0]['status'])"
                )],
                cwd=Path(__file__).parents[1], env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(observe.returncode, 0, observe.stderr)
            self.assertIn("active", observe.stdout)

            fresh = subprocess.run(
                [sys.executable, "-c", (
                    "import main; "
                    f"main.configure_launch_context(project_path={str(root)!r}); "
                    "print(main._build_preference_context())"
                )],
                cwd=Path(__file__).parents[1], env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(fresh.returncode, 0, fresh.stderr)
            self.assertIn("language=python", fresh.stdout)
            self.assertIn("naming_style=snake_case", fresh.stdout)
            self.assertIn("verbosity=concise", fresh.stdout)

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
        with patch.object(server._agent, "learning_runtime", return_value={"status": "ready"}), \
             patch.object(server._agent, "dispatch_learning_cycle", return_value=[]) as dispatch:
            server._run_scheduled_job({"payload": {"type": "learning_cycle"}})
        dispatch.assert_called_once_with(surface="web", trigger="scheduled", max_features=4)

    def test_setup_required_blocks_remote_learning_and_reports_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            remote = Mock()
            main.llm_provider = remote
            self.assertIsNone(main._learning_model_response([{"role": "system", "content": "test"}], feature="test"))
            remote.chat.assert_not_called()
            self.assertEqual(main._learning_provider_class(), "none")
            status = main.learning_feature_status()
            self.assertEqual(status[0]["policy"], "setup_required")
            self.assertIn("Choose Local or Remote", status[0]["skip_reason"])
            payload = asyncio.run(server.api_v2_learning_features())
            self.assertEqual(payload["provider_class"], "none")
            self.assertEqual(payload["cost_source"], "No learning model selected")
            events = main.memory_bank.store.list_events(
                "learning.model_skipped", limit=1,
                user_id="learning-user", workspace_id="learning-project",
            )
            self.assertIn("Choose Local or Remote", events[0]["payload"]["reason"])

    def test_local_learning_never_uses_fallback_and_rejects_remote_endpoint(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"KYROZEN_LEARNING_OLLAMA_BASE_URL": "http://localhost:11434/v1"}, clear=False):
            self._isolated_runtime(Path(directory))
            main._set_learning_runtime("local", "ready", model="qwen2.5:7b")
            local = Mock()
            local.chat.return_value = ("local result", {"prompt_tokens": 1, "completion_tokens": 1})
            with patch.object(main, "get_provider", return_value=local) as provider, \
                 patch.object(main, "get_fallback_provider") as fallback:
                self.assertEqual(main._learning_model_response([{"role": "system", "content": "test"}], feature="test"), "local result")
            provider.assert_called_once()
            self.assertEqual(provider.call_args.args[0].provider, "ollama_native")
            self.assertEqual(provider.call_args.args[0].base_url, "http://localhost:11434/v1")
            fallback.assert_not_called()
            local.chat.assert_called_once_with(ANY, "qwen2.5:7b")
            with patch.dict(os.environ, {"KYROZEN_LEARNING_OLLAMA_BASE_URL": "https://api.example.test/v1"}, clear=False), \
                 patch.object(main, "get_provider") as provider:
                self.assertIsNone(main._learning_model_response([{"role": "system", "content": "test"}], feature="test"))
            provider.assert_not_called()

    def test_remote_learning_reuses_chat_provider_and_marks_learning_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            main._set_learning_runtime("remote", "ready")
            remote = Mock()
            remote.chat.return_value = ("remote result", {})
            main.llm_provider = remote
            main._provider_config = main.ProviderConfig(provider="deepseek", model_simple="configured-model")
            self.assertEqual(main._learning_model_response([{"role": "system", "content": "test"}], feature="test"), "remote result")
            remote.chat.assert_called_once_with(ANY, "configured-model")

    def test_local_bootstrap_marks_resource_failure_durably(self):
        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            main._set_learning_runtime("local", "installing", model="qwen2.5:7b")
            with patch.object(main, "_local_learning_resources_ok", return_value=(False, "need RAM")):
                main._bootstrap_local_learning()
            runtime = main.learning_runtime()
            self.assertEqual((runtime["mode"], runtime["status"]), ("local", "failed"))
            self.assertEqual(runtime["detail"], "need RAM")

    def test_local_bootstrap_marks_pull_failure_durably(self):
        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            main._set_learning_runtime("local", "installing", model="qwen2.5:7b")
            failed_pull = SimpleNamespace(returncode=1, stdout="", stderr="download failed")
            with patch.object(main, "_local_learning_resources_ok", return_value=(True, "")), \
                 patch.object(main, "_ollama_command", return_value="ollama"), \
                 patch.object(main, "_ollama_ready", return_value=True), \
                 patch.object(main.subprocess, "run", return_value=failed_pull):
                main._bootstrap_local_learning()
            self.assertEqual(main.learning_runtime()["status"], "failed")
            self.assertIn("download failed", main.learning_runtime()["detail"])

    def test_local_bootstrap_reuses_ollama_and_smokes_qwen(self):
        with tempfile.TemporaryDirectory() as directory:
            self._isolated_runtime(Path(directory))
            main._set_learning_runtime("local", "installing", model="qwen2.5:7b")
            completed = SimpleNamespace(returncode=0, stdout="NAME ID SIZE\nqwen2.5:7b x 4.7 GB", stderr="")
            local = Mock()
            local.chat.return_value = ("OK", {})
            with patch.object(main, "_local_learning_resources_ok", return_value=(True, "")), \
                 patch.object(main, "_ollama_command", return_value="ollama"), \
                 patch.object(main, "_ollama_ready", return_value=True), \
                 patch.object(main.subprocess, "run", return_value=completed), \
                 patch.object(main, "get_provider", return_value=local):
                main._bootstrap_local_learning()
            self.assertEqual(main.learning_runtime()["status"], "ready")
            local.chat.assert_called_once_with(ANY, "qwen2.5:7b")

    def test_cli_idle_loop_uses_the_shared_dispatcher(self):
        original_interaction = main._last_user_interaction
        main._last_user_interaction = 0
        try:
            with patch.object(main, "learning_runtime", return_value={"status": "ready"}), \
                 patch.object(main.time, "sleep", side_effect=[None, KeyboardInterrupt]), \
                 patch.object(main, "dispatch_learning_cycle", return_value=[]) as dispatch:
                with self.assertRaises(KeyboardInterrupt):
                    main._background_learning_loop()
        finally:
            main._last_user_interaction = original_interaction
        dispatch.assert_called_once_with(surface="cli", trigger="background", max_features=4)

    def test_web_startup_persists_a_learning_cycle_job(self):
        with patch.object(server._agent, "learning_runtime", return_value={"status": "ready"}), \
             patch.object(server._agent, "_prompt_and_init_deepseek"), \
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
