import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from event_store import EventStore
from fastapi.testclient import TestClient
from memory import MemoryBank
from providers import PROVIDER_COSTS, _track_cost, usage_scope
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
                    _track_cost("deepseek", {"prompt_tokens": 1, "completion_tokens": 2},
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
            self.assertTrue(all(item["pricing_snapshot"]["version"] == "legacy-provider-costs-v1"
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
            original_cost = totals["cost_picos"]
            previous_rates = PROVIDER_COSTS["deepseek"]
            try:
                PROVIDER_COSTS["deepseek"] = (999.0, 999.0)
                self.assertEqual(
                    store.usage_totals(workspace_id="project", user_id="local")["cost_picos"],
                    original_cost,
                )
            finally:
                PROVIDER_COSTS["deepseek"] = previous_rates

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


if __name__ == "__main__":
    unittest.main()
