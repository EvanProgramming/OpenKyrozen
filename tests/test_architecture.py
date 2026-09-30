import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.check_architecture import check
from openkyrozen.app.bootstrap import build_application, build_memory
from openkyrozen.memory.service import MemoryBank
from openkyrozen.persistence.store import EventStore

ROOT = Path(__file__).resolve().parents[1]


class ArchitectureTests(unittest.TestCase):
    def test_dependency_graph_and_legacy_imports(self):
        self.assertEqual(check(), [])

    def test_core_and_launch_imports_create_no_application_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env.update(HOME=directory, KYROZEN_DB_PATH=str(Path(directory) / "forbidden.sqlite3"))
            code = """
import sqlite3, subprocess, threading, httpx
from unittest.mock import patch
with patch.object(sqlite3, 'connect', side_effect=AssertionError('database created during import')), \
     patch.object(subprocess, 'Popen', side_effect=AssertionError('process started during import')), \
     patch.object(threading.Thread, 'start', side_effect=AssertionError('worker started during import')), \
     patch.object(httpx.Client, '__init__', side_effect=AssertionError('client created during import')), \
     patch.object(httpx.AsyncClient, '__init__', side_effect=AssertionError('async client created during import')):
    import importlib, pkgutil, openkyrozen
    for module in pkgutil.walk_packages(openkyrozen.__path__, "openkyrozen."):
        importlib.import_module(module.name)
    import openkyrozen.agent.runtime
    import openkyrozen.memory.service
    import openkyrozen.learning.engine
    import openkyrozen.tasks.engine
    import openkyrozen.providers
    import main, server, tui_backend, learning_worker
    assert server.app.state.service._application is None
print('imports are inert')
"""
            result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((Path(directory) / "forbidden.sqlite3").exists())
            self.assertFalse((Path(directory) / ".kyrozen").exists())

    def test_actor_session_and_workspace_ownership_survives_switching(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HOME": directory}):
            root = Path(directory)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            memory = build_memory(root / "state.sqlite3", index=_VectorIndex())
            application = build_application(memory=memory)
            runtime = application.runtime
            try:
                runtime.configure_launch_context(project_path=first)
                alice = runtime.open_session("same-id", user_id="alice")
                bob = runtime.open_session("same-id", user_id="bob")
                alice.messages.append({"role": "user", "content": "private conversation"})
                alice.tasks.add_task("alice task")
                runtime.bind_interaction_scope("same-id", user_id="alice")
                runtime.configure_launch_context(project_path=second)
                other_project = runtime.open_session("same-id", user_id="alice")
                self.assertIsNot(alice.workspace, other_project.workspace)
                self.assertEqual(bob.messages, [])
                self.assertEqual(bob.tasks.tasks, [])
                self.assertEqual(other_project.messages, [])
                self.assertEqual(alice.workspace.root, str(first.resolve()))
                self.assertNotEqual(alice.memory.file_scope_id, other_project.memory.file_scope_id)
                alice.preferences["language"] = "python"
                self.assertFalse(bob.preferences["language"])
                with runtime.use_session(alice):
                    runtime.AVAILABLE_TOOLS["write_file"]("owner.txt|alice")
                self.assertEqual((first / "owner.txt").read_text(), "alice")
                self.assertFalse((second / "owner.txt").exists())
                runtime.configure_launch_context(project_path=first)
                self.assertIs(runtime.open_session("same-id", user_id="alice"), alice)
            finally:
                application.close()

    def test_parallel_execution_keeps_turn_tokens_and_sessions_local(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HOME": directory}):
            application = build_application(memory=build_memory(Path(directory) / "state.sqlite3", index=_VectorIndex()))
            runtime = application.runtime
            runtime.configure_launch_context(project_path=directory)
            sessions = [runtime.open_session(name) for name in ("a", "b")]
            barrier = threading.Barrier(2)
            original_model = runtime.DEEPSEEK_MODEL

            def read(_args):
                runtime.DEEPSEEK_MODEL = runtime.current_session.session_id
                barrier.wait(timeout=5)
                return runtime.current_session.session_id + ":" + runtime.DEEPSEEK_MODEL

            runtime.AVAILABLE_TOOLS["read_file"] = read
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(runtime.execute, session, "read_file", "file", capabilities="readonly")
                               for session in sessions]
                    receipts = [future.result(timeout=10) for future in futures]
                self.assertEqual([r.result for r in receipts], ["a:a", "b:b"])
                self.assertTrue(all(r.success and r.authorized for r in receipts))
                self.assertEqual(runtime.DEEPSEEK_MODEL, original_model)
                for session in sessions:
                    events = runtime.memory_bank.store.list_events("execution.receipt", session_id=session.session_id)
                    self.assertEqual(len(events), 1)
            finally:
                application.close()

    def test_vector_failure_falls_back_to_authoritative_store_and_rebuilds(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            index = _VectorIndex()
            memory = MemoryBank(store.path, store=store, index=index)
            memory.add_log("FACT: modular architecture", kind="fact")
            self.assertIn("FACT: modular architecture", memory.recall("modular architecture"))
            self.assertEqual(memory.rebuild_index(), 1)
            self.assertEqual(len(index.memories.documents), 1)
            reopened = MemoryBank(store.path, store=EventStore(store.path))
            self.assertIn("FACT: modular architecture", reopened.recall("modular architecture"))


class _Collection:
    def __init__(self):
        self.documents = {}

    def upsert(self, *, ids, documents, metadatas):
        self.documents.update(zip(ids, documents))

    def count(self):
        return len(self.documents)

    def query(self, **kwargs):
        raise RuntimeError("index temporarily unavailable")

    def delete(self, *, ids):
        for item in ids:
            self.documents.pop(item, None)


class _VectorIndex:
    def __init__(self):
        self.memories = _Collection()
        self.files = _Collection()
