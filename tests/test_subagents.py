import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from event_store import EventStore
from learning_engine import LearningEngine
from memory import MemoryBank
import main
from providers import OpenAICompatProvider, ProviderConfig
from subagents import AgentProfile, SubAgentManager


class SubAgentTests(unittest.TestCase):
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
            with patch.object(main, "memory_bank", memory), patch.object(main, "llm_provider", provider):
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
