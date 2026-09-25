import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import learning_worker
from event_store import EventStore


class LearningWorkerTests(unittest.TestCase):
    def test_feature_flags_persist_across_store_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            first = EventStore(path)
            first.set_learning_feature_flag("idle_reflection", False, workspace_id="project")
            first.set_learning_feature_flag("memory_importance_scoring", True, workspace_id="project")

            reopened = EventStore(path)
            self.assertEqual(
                reopened.list_learning_feature_flags(workspace_id="project"),
                {"idle_reflection": False, "memory_importance_scoring": True},
            )
            self.assertEqual(reopened.list_learning_feature_flags(workspace_id="other"), {})
            self.assertEqual(first.get_learning_runtime(workspace_id="project")["mode"], "setup_required")
            first.set_learning_policy("local_only", workspace_id="legacy")
            self.assertEqual(first.get_learning_runtime(workspace_id="legacy")["mode"], "setup_required")
            self.assertEqual(reopened.get_learning_runtime(workspace_id="legacy")["status"], "setup_required")
            first.set_learning_runtime("remote", "ready", workspace_id="project")
            self.assertEqual(reopened.get_learning_runtime(workspace_id="project")["mode"], "remote")
            self.assertEqual(reopened.get_learning_runtime(workspace_id="other")["mode"], "setup_required")

    def test_start_worker_uses_detached_singleton_process_and_heartbeat(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as project:
            with patch.dict(os.environ, {"HOME": home}, clear=False), \
                 patch.object(learning_worker, "worker_is_running", return_value=False), \
                 patch.object(learning_worker.subprocess, "Popen") as popen:
                self.assertTrue(learning_worker.start_worker(
                    workspace_root=project, launch_mode="project",
                ))
                _, heartbeat = learning_worker.worker_paths(project)
                self.assertTrue(heartbeat.exists())

            args, kwargs = popen.call_args
            self.assertEqual(args[0], [sys.executable, "-m", "learning_worker"])
            self.assertEqual(kwargs["env"]["KYROZEN_WORKSPACE_ROOT"], str(Path(project).resolve()))
            self.assertEqual(kwargs["env"]["KYROZEN_LAUNCH_MODE"], "project")
            self.assertEqual(kwargs["env"]["KYROZEN_EXECUTION_SURFACE"], "worker")
            if os.name == "nt":
                self.assertIn("creationflags", kwargs)
            else:
                self.assertTrue(kwargs["start_new_session"])

    def test_worker_runs_a_cycle_after_cli_heartbeat_is_stale(self):
        class OneCycleEvent:
            def __init__(self):
                self.waits = 0

            def wait(self, _seconds):
                self.waits += 1
                return self.waits > 1

        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "worker.pid"
            heartbeat = Path(directory) / "worker.heartbeat"
            fake_store = SimpleNamespace(append_event=lambda *args, **kwargs: None)
            fake_agent = SimpleNamespace(
                _SELF_LEARNING_FLAGS={"idle_reflection": True},
                configure_launch_context=lambda: None,
                _prompt_and_init_deepseek=lambda **kwargs: False,
                learning_runtime=lambda: {"status": "ready"},
                _record_learning_event=lambda *args, **kwargs: None,
                _restore_self_learning_flags=lambda: None,
                dispatch_learning_cycle=lambda **kwargs: fake_agent.dispatches.append(kwargs),
                dispatches=[],
                memory_bank=SimpleNamespace(
                    user_id="local", workspace_id="default", session_id=None, store=fake_store,
                ),
            )
            with patch.object(learning_worker, "worker_paths", return_value=(lock, heartbeat)), \
                 patch.object(learning_worker, "_claim_worker_lock", return_value=True), \
                 patch.object(learning_worker, "Event", return_value=OneCycleEvent()), \
                 patch.dict(sys.modules, {"main": fake_agent}):
                self.assertEqual(learning_worker.worker_main(), 0)

            self.assertEqual(fake_agent.dispatches, [{
                "surface": "worker", "trigger": "detached", "max_features": 4,
            }])
            self.assertFalse(lock.exists())

    def test_worker_exits_without_dispatching_before_learning_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "worker.pid"
            heartbeat = Path(directory) / "worker.heartbeat"
            events = []
            fake_agent = SimpleNamespace(
                configure_launch_context=lambda: None,
                _prompt_and_init_deepseek=lambda **kwargs: False,
                learning_runtime=lambda: {"status": "setup_required"},
                _record_learning_event=lambda *args, **kwargs: events.append(args),
                dispatch_learning_cycle=lambda **kwargs: self.fail("must not dispatch"),
            )
            with patch.object(learning_worker, "worker_paths", return_value=(lock, heartbeat)), \
                 patch.object(learning_worker, "_claim_worker_lock", return_value=True), \
                 patch.dict(sys.modules, {"main": fake_agent}):
                self.assertEqual(learning_worker.worker_main(), 0)
            self.assertEqual(events[0][0], "learning.worker_skipped")


if __name__ == "__main__":
    unittest.main()
