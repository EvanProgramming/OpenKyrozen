import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from event_store import EventStore
from fastapi.testclient import TestClient
from memory import MemoryBank
from providers import OpenAICompatProvider, ProviderConfig, _track_cost, get_cost_summary, usage_scope
import server


class UsageLedgerTests(unittest.TestCase):
    @staticmethod
    def _record(store: EventStore, *, workspace: str = "project", session: str | None = "session") -> None:
        with usage_scope(
            store=store, user_id="local", workspace_id=workspace, session_id=session,
            run_id="run-1", surface="cli",
        ):
            _track_cost("deepseek", {"prompt_tokens": 1100, "completion_tokens": 50},
                        model="deepseek-chat", latency_ms=25)

    def test_server_cost_api_reconstructs_usage_after_process_restart(self):
        repository = Path(__file__).parents[1]
        script = (
            "import json; from fastapi.testclient import TestClient; import server; "
            "print(json.dumps(TestClient(server.app).get('/api/cost').json()))"
        )
        with tempfile.TemporaryDirectory(prefix="openkyrozen-usage-restart-") as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            database = root / "state.sqlite3"
            self._record(EventStore(database), workspace="default")
            environment = os.environ.copy()
            environment.update({
                "HOME": str(home), "KYROZEN_DB_PATH": str(database),
                "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_PROVIDER": "ollama",
                "PYTHONPATH": str(repository),
            })
            environment.pop("KYROZEN_SERVER_TOKEN", None)
            reports = []
            for _ in range(2):
                result = subprocess.run(
                    [sys.executable, "-c", script], cwd=root, env=environment,
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                reports.append(json.loads(result.stdout.strip().splitlines()[-1]))
            self.assertEqual(reports[0]["summary"], reports[1]["summary"])
            self.assertEqual(reports[1]["totals"]["installation"]["prompt_tokens"], 1100)
            self.assertEqual(reports[1]["totals"]["workspace"]["completion_tokens"], 50)

    def test_concurrent_attempts_keep_unique_ids_and_pricing_snapshots(self):
        with tempfile.TemporaryDirectory(prefix="openkyrozen-usage-concurrent-") as directory:
            store = EventStore(Path(directory) / "state.sqlite3")

            def record(index: int) -> None:
                with usage_scope(store=store, workspace_id="project", session_id=f"session-{index % 2}",
                                 run_id=f"run-{index}", surface="mcp"):
                    _track_cost("deepseek", {
                        "prompt_tokens": 1, "prompt_cache_hit_tokens": 0,
                        "prompt_cache_miss_tokens": 1, "completion_tokens": 2,
                    },
                                model="deepseek-chat", latency_ms=index)

            threads = [threading.Thread(target=record, args=(index,)) for index in range(32)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            attempts = store.list_usage_attempts(workspace_id="project", user_id="local")
            totals = store.usage_totals(workspace_id="project", user_id="local")
            self.assertEqual(len(attempts), 32)
            self.assertEqual(len({item["id"] for item in attempts}), 32)
            self.assertEqual(totals["prompt_tokens"], 32)
            self.assertEqual(totals["completion_tokens"], 64)
            self.assertTrue(all(item["pricing_snapshot"]["version"] == "deepseek-v4-pricing-2026-08-16"
                                for item in attempts))
            self.assertEqual(
                {item["model"] for item in attempts}, {"deepseek-chat"},
            )
            self.assertEqual(
                {item["surface"] for item in attempts}, {"mcp"},
            )
            self.assertEqual(
                {item["session_id"] for item in attempts}, {"session-0", "session-1"},
            )
            self.assertEqual({item["run_id"] for item in attempts}, {f"run-{index}" for index in range(32)})
            self.assertTrue(all(item["usage_status"] == "authoritative" for item in attempts))
            self.assertTrue(all(item["completion_state"] == "completed" for item in attempts))

    def test_reset_requires_explicit_confirmation_and_preserves_installation_ledger(self):
        with tempfile.TemporaryDirectory(prefix="openkyrozen-usage-reset-") as directory:
            memory = MemoryBank(Path(directory) / "state.sqlite3", workspace_id="project")
            self._record(memory.store)
            original_memory = server._agent.memory_bank
            server._agent.memory_bank = memory
            try:
                client = TestClient(server.app)
                self.assertEqual(client.post("/api/cost/reset", json={"scope": "workspace"}).status_code, 400)
                reset = client.post("/api/cost/reset", json={
                    "scope": "workspace", "confirm": "reset-cost",
                })
                self.assertEqual(reset.status_code, 200, reset.text)
                self.assertEqual(reset.json()["totals"]["workspace"]["attempts"], 0)
                self.assertEqual(reset.json()["totals"]["installation"]["attempts"], 1)
                self.assertTrue(memory.store.list_events(
                    "usage.reset", workspace_id="project", user_id=server._SERVER_ACTOR_ID,
                ))
            finally:
                server._agent.memory_bank = original_memory

    def test_short_and_mixed_calls_aggregate_before_display_rounding(self):
        with tempfile.TemporaryDirectory(prefix="openkyrozen-usage-precision-") as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            peak_time = datetime(2026, 9, 7, 2, tzinfo=timezone.utc)
            with usage_scope(store=store, workspace_id="project", session_id="short"):
                for _ in range(1000):
                    _track_cost("deepseek", {"prompt_tokens": 0, "completion_tokens": 100},
                                model="deepseek-chat", occurred_at=peak_time)
            with usage_scope(store=store, workspace_id="project", session_id="mixed"):
                for _ in range(1000):
                    _track_cost("deepseek", {"prompt_tokens": 100, "completion_tokens": 100},
                                model="deepseek-chat", occurred_at=peak_time)
            with usage_scope(store=store, workspace_id="project", session_id="combined-short"):
                _track_cost("deepseek", {"prompt_tokens": 0, "completion_tokens": 100_000},
                            model="deepseek-chat", occurred_at=peak_time)
            with usage_scope(store=store, workspace_id="project", session_id="combined-mixed"):
                _track_cost("deepseek", {"prompt_tokens": 100_000, "completion_tokens": 100_000},
                            model="deepseek-chat", occurred_at=peak_time)

            short = store.usage_totals(workspace_id="project", session_id="short", user_id="local")
            mixed = store.usage_totals(workspace_id="project", session_id="mixed", user_id="local")
            combined_short = store.usage_totals(
                workspace_id="project", session_id="combined-short", user_id="local",
            )
            combined_mixed = store.usage_totals(
                workspace_id="project", session_id="combined-mixed", user_id="local",
            )
            self.assertEqual(short["cost_picos"], 132_000_000_000)
            self.assertEqual(mixed["cost_picos"], 176_000_000_000)
            self.assertEqual(short["cost_picos"], combined_short["cost_picos"])
            self.assertEqual(mixed["cost_picos"], combined_mixed["cost_picos"])
            self.assertEqual(
                get_cost_summary(store=store, workspace_id="project", session_id="short", scope="session"),
                "deepseek: 0K in / 100K out ~$0.13",
            )

    def test_deepseek_v4_cache_pricing_uses_fixed_peak_and_off_peak_timestamps(self):
        usage = {
            "prompt_tokens": 1_000_000,
            "prompt_cache_hit_tokens": 250_000,
            "prompt_cache_miss_tokens": 750_000,
            "completion_tokens": 1_000_000,
        }
        with tempfile.TemporaryDirectory(prefix="openkyrozen-deepseek-pricing-") as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            with usage_scope(store=store, workspace_id="project", session_id="peak"):
                _track_cost("deepseek", usage, model="deepseek-v4-flash",
                            occurred_at=datetime(2026, 9, 7, 2, tzinfo=timezone.utc))
            with usage_scope(store=store, workspace_id="project", session_id="off-peak"):
                _track_cost("deepseek", usage, model="deepseek-v4-flash",
                            occurred_at=datetime(2026, 9, 7, 4, tzinfo=timezone.utc))

            peak = store.usage_totals(workspace_id="project", session_id="peak", user_id="local")
            off_peak = store.usage_totals(workspace_id="project", session_id="off-peak", user_id="local")
            attempt = store.list_usage_attempts(workspace_id="project", session_id="peak", user_id="local")[0]
            self.assertEqual(peak["cost_picos"], 1_653_500_000_000)
            self.assertEqual(off_peak["cost_picos"], 826_750_000_000)
            self.assertEqual(attempt["cache_status"], "mixed")
            self.assertEqual(attempt["usage_status"], "authoritative")
            self.assertEqual(attempt["pricing_snapshot"]["billing_window"], "peak")
            self.assertEqual(attempt["pricing_snapshot"]["model"], "deepseek-v4-flash")

    def test_openai_compatible_provider_preserves_deepseek_cache_usage_fields(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
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
        with tempfile.TemporaryDirectory(prefix="openkyrozen-deepseek-usage-") as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            with usage_scope(store=store, workspace_id="project"):
                text, usage = provider.chat([], "deepseek-v4-flash")
            attempt = store.list_usage_attempts(workspace_id="project", user_id="local")[0]
            self.assertEqual(text, "ok")
            self.assertEqual(usage["prompt_cache_hit_tokens"], 3)
            self.assertEqual(attempt["cache_hit_tokens"], 3)
            self.assertEqual(attempt["cache_miss_tokens"], 7)
            self.assertEqual(attempt["reasoning_tokens"], 2)

    def test_completed_stream_records_final_usage_once(self):
        final_usage = SimpleNamespace(
            prompt_tokens=10, completion_tokens=4,
            prompt_cache_hit_tokens=3, prompt_cache_miss_tokens=7,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
        )
        chunks = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="STREAM"))], usage=None),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="_OK"))], usage=None),
            SimpleNamespace(choices=[], usage=final_usage),
        ]
        calls = []
        provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
        provider.config = ProviderConfig(provider="deepseek")
        provider._client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs) or iter(chunks)),
        ))
        with tempfile.TemporaryDirectory(prefix="openkyrozen-stream-usage-") as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            with usage_scope(store=store, workspace_id="project"):
                self.assertEqual(list(provider.chat_stream([], "deepseek-v4-flash")), ["STREAM", "_OK"])
            attempts = store.list_usage_attempts(workspace_id="project", user_id="local")
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["completion_tokens"], 4)
            self.assertEqual(attempts[0]["usage_status"], "authoritative")
            self.assertEqual(calls[0]["stream_options"], {"include_usage": True})


if __name__ == "__main__":
    unittest.main()
