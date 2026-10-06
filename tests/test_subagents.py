import tempfile
import unittest
import json
import threading
from contextlib import contextmanager
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from openkyrozen.persistence.store import EventStore
from openkyrozen.learning.engine import LearningEngine
from openkyrozen.app.bootstrap import build_memory as MemoryBank
from openkyrozen.app.bootstrap import build_application
_application = build_application(surface="cli")
main = _application.runtime
from openkyrozen.providers import OpenAICompatProvider, ProviderConfig
from openkyrozen.agent.subagents import AgentProfile, SubAgentManager
from openkyrozen.agent.delegation_runtime import _delegation_tool_access
from openkyrozen.agent.delegation import WorkspaceAccess


class SubAgentTests(unittest.TestCase):
    def test_edit_file_cannot_escape_subagent_assigned_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "other.py").write_text("value = 1", encoding="utf-8")
            adapters = SimpleNamespace(_resolve_workspace_path=lambda raw: (root / raw).resolve())
            coordinator = SimpleNamespace(
                root=root, access=None, check_cancelled=lambda _run: None,
                runs={"child": {"assignment": {"scope": ["allowed.py"]}}},
                cancelled={"child": threading.Event()},
            )
            parent = SimpleNamespace(interaction=SimpleNamespace(state=lambda: {}))
            child = SimpleNamespace(workspace=SimpleNamespace(adapters=adapters), _delegation_parent=parent)
            agent = SimpleNamespace(
                execution_context=SimpleNamespace(coordinator=coordinator, child_run_id="child"),
                current_session=child, _workspace_access={},
                _get_workspace_root=lambda: root,
                _is_state_changing_action=lambda *_args: True,
            )
            args = json.dumps({"path": "other.py", "old_text": "1", "new_text": "2",
                               "expected_sha256": "0" * 64})
            with self.assertRaisesRegex(ValueError, "outside its assigned files"):
                with _delegation_tool_access(agent, "edit_file", args):
                    self.fail("out-of-scope edit entered the guarded operation")

    def test_edit_file_scope_parsing_keeps_json_text_after_pipe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            adapters = SimpleNamespace(_resolve_workspace_path=lambda raw: (root / raw).resolve())
            coordinator = SimpleNamespace(
                root=root, access=WorkspaceAccess(), check_cancelled=lambda _run: None,
                runs={"child": {"assignment": {"scope": ["allowed.py"]}}},
                cancelled={"child": threading.Event()},
            )
            parent = SimpleNamespace(interaction=SimpleNamespace(state=lambda: {}))
            child = SimpleNamespace(workspace=SimpleNamespace(adapters=adapters), _delegation_parent=parent)
            agent = SimpleNamespace(
                execution_context=SimpleNamespace(coordinator=coordinator, child_run_id="child"),
                current_session=child, _workspace_access={}, _get_workspace_root=lambda: root,
                _is_state_changing_action=lambda *_args: True,
            )
            args = json.dumps({"path": "allowed.py", "old_text": "a || b", "new_text": "a",
                               "expected_sha256": "0" * 64})
            with _delegation_tool_access(agent, "edit_file", args):
                self.assertEqual((root / "allowed.py").parent, root)

    def test_delegated_search_uses_cancellable_workspace_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            entered = []

            class Access:
                @contextmanager
                def acquire(self, path, cancelled, owner, *, reading=False):
                    entered.append((path, owner, reading, cancelled()))
                    yield

            coordinator = SimpleNamespace(access=Access(), check_cancelled=lambda _run: None,
                                          cancelled={"child": threading.Event()})
            adapters = SimpleNamespace(_resolve_workspace_path=lambda raw: (root / raw).resolve())
            agent = SimpleNamespace(
                execution_context=SimpleNamespace(coordinator=coordinator, child_run_id="child"),
                current_session=SimpleNamespace(workspace=SimpleNamespace(adapters=adapters)), _workspace_access={},
                _get_workspace_root=lambda: root,
                _is_state_changing_action=lambda *_args: False,
            )
            with _delegation_tool_access(agent, "search_files", '{"query":"needle"}'):
                pass
        self.assertEqual(entered, [(str(root), "child", True, False)])

    def test_delegated_search_lock_uses_requested_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            entered = []

            class Access:
                @contextmanager
                def acquire(self, path, cancelled, owner, *, reading=False):
                    entered.append((path, owner, reading, cancelled()))
                    yield

            coordinator = SimpleNamespace(access=Access(), check_cancelled=lambda _run: None,
                                          cancelled={"child": threading.Event()})
            adapters = SimpleNamespace(_resolve_workspace_path=lambda raw: (root / raw).resolve())
            agent = SimpleNamespace(
                execution_context=SimpleNamespace(coordinator=coordinator, child_run_id="child"),
                current_session=SimpleNamespace(workspace=SimpleNamespace(adapters=adapters)),
                _workspace_access={},
                _get_workspace_root=lambda: root,
                _is_state_changing_action=lambda *_args: False,
            )
            with _delegation_tool_access(agent, "search_files", '{"query":"needle","path":"docs"}'):
                pass
        self.assertEqual(entered, [(str((root / "docs").resolve()), "child", True, False)])

    def test_profile_has_independent_session_memory_and_capabilities(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryBank(Path(directory) / "state.sqlite3", workspace_id="project")
            manager = SubAgentManager(
                memory, runner=lambda profile, task, context, tools, *, run_id: f"done:{profile.name}",
            )
            manager.register(AgentProfile("tester", "Return a test result", "readonly"))
            result = manager.run("tester", "check the repository")
            self.assertTrue(result["run_id"].startswith("subagent_"))
            self.assertNotIn("write_file", result["tools"])
            events = memory.store.list_events(workspace_id="project", session_id=result["run_id"], limit=10)
            event_types = [event["event_type"] for event in events]
            self.assertIn("subagent.started", event_types)
            self.assertIn("subagent.completed", event_types)

    def test_action_loop_writes_file_and_returns_final_model_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            previous_root = main._get_workspace_root()
            main._set_workspace_root(root)
            profile = AgentProfile("coder", "Modify files and verify the result.", "workspace", max_steps=3)
            responses = [
                'Action: {"action":"write_file","args":"subagent-result.txt|created by subagent"}',
                "The file was created and verified.",
            ]
            try:
                with patch.object(main, "_get_llm_response", side_effect=responses):
                    result = main._run_subagent_llm(profile, "Create subagent-result.txt", [], {"write_file"})
            finally:
                main._set_workspace_root(previous_root)
            self.assertEqual(result["result"], "The file was created and verified.")
            self.assertTrue((root / "subagent-result.txt").is_file())
            self.assertTrue(result["tool_records"][0]["success"])
            self.assertTrue(result["evidence"])

    def test_action_loop_rejects_unauthorized_write_without_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            previous_root = main._get_workspace_root()
            main._set_workspace_root(root)
            profile = AgentProfile("reviewer", "Review only.", "readonly", max_steps=2,
                                   evolution_enabled=False)
            responses = [
                'Action: {"action":"write_file","args":"should-not-exist.txt|blocked"}',
                "The write was not authorized.",
            ]
            try:
                with patch.object(main, "_get_llm_response", side_effect=responses):
                    result = main._run_subagent_llm(profile, "Write should-not-exist.txt", [], {"read_file"})
            finally:
                main._set_workspace_root(previous_root)
            self.assertFalse((root / "should-not-exist.txt").exists())
            self.assertFalse(result["tool_records"][0]["success"])
            self.assertFalse(result["tool_records"][0]["authorized"])

    def test_action_loop_retries_unsupported_provider_wrapper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "notes.txt").write_text("safe", encoding="utf-8")
            previous_root = main._get_workspace_root()
            main._set_workspace_root(root)
            profile = AgentProfile("reviewer", "Review files.", "readonly", max_steps=3,
                                   evolution_enabled=False)
            responses = [
                '<SSAI_ACTION> Input: {"path":"notes.txt"} </SSAI_ACTION>',
                'Action: {"action":"read_file","args":"notes.txt"}',
                "The notes were read successfully.",
            ]
            try:
                with patch.object(main, "_get_llm_response", side_effect=responses):
                    result = main._run_subagent_llm(profile, "Read notes.txt", [], {"read_file"})
            finally:
                main._set_workspace_root(previous_root)
            self.assertEqual(result["result"], "The notes were read successfully.")
            self.assertEqual(len(result["tool_records"]), 2)
            self.assertFalse(result["tool_records"][0]["success"])
            self.assertTrue(result["tool_records"][1]["success"])
            self.assertNotIn("SSAI_ACTION", result["result"])

    def test_manager_persists_tool_receipts_failures_and_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryBank(Path(directory) / "state.sqlite3", workspace_id="project")
            manager = SubAgentManager(memory, runner=lambda profile, task, context, tools, *, run_id: {
                "result": "completed",
                "tool_records": [{"receipt_id": "receipt-1", "action": "write_file",
                                  "args": "marker.txt|ok", "result": "wrote", "success": True,
                                  "evidence": "marker exists"},
                                 {"receipt_id": "receipt-2", "action": "run_cmd",
                                  "args": "blocked", "result": "Error: denied", "success": False}],
                "evidence": [{"receipt_id": "receipt-1", "action": "write_file",
                              "evidence": "marker exists", "success": True}],
            })
            result = manager.run("coder", "complete the task")
            self.assertEqual(len(result["tool_receipts"]), 2)
            events = memory.store.list_events(workspace_id="project", session_id=result["run_id"], limit=20)
            event_types = [event["event_type"] for event in events]
            self.assertIn("subagent.tool_executed", event_types)
            self.assertIn("subagent.tool_failed", event_types)
            self.assertIn("subagent.evidence", event_types)
            completed = next(event for event in events if event["event_type"] == "subagent.completed")
            self.assertEqual(len(completed["payload"]["tool_receipts"]), 2)

    def test_manager_persists_nonzero_runner_metrics_without_zero_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryBank(Path(directory) / "state.sqlite3", workspace_id="project")
            manager = SubAgentManager(
                memory, learning_engine=LearningEngine(memory),
                provider_model=lambda: "deepseek:deepseek-v4-flash",
                runner=lambda profile, task, context, tools, *, run_id: {
                    "result": "completed",
                    "metrics": {"provider_model": "deepseek:deepseek-v4-flash", "tokens": 37,
                                "latency_ms": 250, "usage_status": "authoritative"},
                },
            )
            result = manager.run("researcher", "report the result")
            completed = memory.store.list_events(
                "learning.run_completed", workspace_id="project", limit=10,
            )[0]["payload"]
            self.assertEqual(result["metrics"]["tokens"], 37)
            self.assertEqual(completed["provider_model"], "deepseek:deepseek-v4-flash")
            self.assertEqual(completed["tokens"], 37)
            self.assertEqual(completed["latency"], 0.25)

    def test_main_subagent_runner_uses_its_usage_ledger_rows(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="sub-agent complete"))],
            usage=SimpleNamespace(
                prompt_tokens=10, completion_tokens=4,
                prompt_cache_hit_tokens=3, prompt_cache_miss_tokens=7,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
            ),
        )
        provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
        provider.config = ProviderConfig(provider="deepseek")
        provider._client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kwargs: response),
        ))
        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryBank(Path(directory) / "state.sqlite3", workspace_id="project")
            manager = SubAgentManager(
                memory, runner=main._run_subagent_llm, learning_engine=LearningEngine(memory),
                provider_model=lambda: "deepseek:deepseek-v4-flash",
            )
            with (patch.object(main.current_session, "memory", memory),
                  patch.object(main, "llm_provider", provider),
                  patch.object(main.execution_context, "model", "deepseek-v4-flash")):
                result = manager.run("researcher", "report the result")
            ledger = memory.store.usage_totals(workspace_id="project", run_id=result["run_id"])
            completed = memory.store.list_events(
                "learning.run_completed", workspace_id="project", limit=10,
            )[0]["payload"]
            self.assertEqual(result["metrics"]["provider_model"], "deepseek:deepseek-v4-flash")
            self.assertEqual(result["metrics"]["tokens"], 14)
            self.assertEqual(ledger["prompt_tokens"], 10)
            self.assertEqual(ledger["completion_tokens"], 4)
            self.assertEqual(completed["tokens"], 14)


if __name__ == "__main__":
    unittest.main()
