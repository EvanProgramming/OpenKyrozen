from contextlib import ExitStack, closing
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

from openkyrozen.persistence.store import EventStore
from fastapi.testclient import TestClient
from openkyrozen.app.bootstrap import build_memory as MemoryBank
from openkyrozen.providers import OpenAICompatProvider, ProviderConfig, _track_cost, get_cost_summary, usage_scope
from openkyrozen.interfaces.web.service import WebService
server = WebService(None)
class UsageLedgerTests(unittest.TestCase):
    def test_google_model_tariffs_meter_children_and_reconstruct_display(self):
        from openkyrozen.agent.delegation import DelegationBudget

        with tempfile.TemporaryDirectory() as directory, closing(EventStore(Path(directory) / "usage.db")) as store:
            budget = DelegationBudget()
            budget.reserve("child", review=False)
            with usage_scope(store=store, delegation_budget=budget, delegation_budget_run_id="child"):
                for _ in range(4):
                    _track_cost("google", {"prompt_tokens": 100_000, "completion_tokens": 1000},
                                model="gemini-3.5-flash-lite",
                                occurred_at=datetime(2026, 11, 9, tzinfo=timezone.utc))
            totals = store.usage_totals()
            self.assertEqual(totals["cost_picos"], 130_000_000_000)
            self.assertFalse(budget.can_continue("child", cost_picos=totals["cost_picos"]))
            budget.settle("child", cost_picos=totals["cost_picos"])
            self.assertEqual(budget.remaining_picos, budget.TURN_LIMIT - 130_000_000_000)
            self.assertEqual(get_cost_summary(store=store), "google: 400K in / 4K out ~$0.13")
            attempts = store.list_usage_attempts()
            self.assertTrue(all(row["usage_status"] == "estimated" for row in attempts))
            self.assertTrue(all(row["pricing_snapshot"]["rate_policy"] == "standard_paid_rate_estimate"
                                for row in attempts))

    def test_google_cache_tiers_dates_and_unknown_model_pricing(self):
        cases = [
            ("gemini-3.5-flash-lite", 2026, 11, 200_000, 50_000, 1000, 49_000_000_000),
            ("gemini-2.5-flash", 2026, 11, 200_000, 50_000, 1000, 49_000_000_000),
            ("gemini-3.1-pro-preview", 2026, 11, 200_000, 50_000, 1000, 322_000_000_000),
            ("gemini-3.1-pro-preview", 2026, 11, 200_001, 50_000, 1000, 638_004_000_000),
            ("gemini-3.8-flash", 2026, 12, 200_000, 50_000, 1000, 120_000_000_000),
            ("gemini-3.8-flash", 2027, 1, 200_000, 50_000, 1000, 240_000_000_000),
            ("gemini-unknown", 2026, 11, 200_000, 50_000, 1000, None),
            ("3.0", 2026, 11, 200_000, 50_000, 1000, None),
        ]
        with tempfile.TemporaryDirectory() as directory, closing(EventStore(Path(directory) / "usage.db")) as store:
            for model, year, month, prompt, hit, output, expected in cases:
                with self.subTest(model=model, year=year, month=month), usage_scope(store=store):
                    attempt_id = _track_cost("google", {
                        "prompt_tokens": prompt, "completion_tokens": output,
                        "prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": prompt - hit,
                    }, model=model, occurred_at=datetime(year, month, 9, tzinfo=timezone.utc))
                    row = next(row for row in store.list_usage_attempts() if row["id"] == attempt_id)
                    self.assertEqual(row["cost_picos"], expected)
                    self.assertEqual(row["usage_status"], "unknown" if expected is None else "estimated")
                    self.assertEqual(row["cache_status"], "mixed")

    def test_google_adapter_preserves_cache_and_served_identity(self):
        from openkyrozen.providers.google import GoogleProvider
        from unittest.mock import Mock, patch

        provider = GoogleProvider.__new__(GoogleProvider)
        provider.config = ProviderConfig(provider="google", model_simple="gemini-3.5-flash-lite")
        wire = SimpleNamespace(text="ok", candidates=[], model_version="gemini-3.5-flash-lite-07-2026",
            usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=3,
                thoughts_token_count=11, cached_content_token_count=40))
        provider._client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(return_value=wire)))
        with patch("openkyrozen.providers.usage._track_cost") as charge:
            response = provider.chat_response([])
        self.assertEqual(response.usage["prompt_cache_hit_tokens"], 40)
        self.assertEqual(response.usage["prompt_cache_miss_tokens"], 60)
        self.assertEqual(response.usage["completion_tokens"], 14)
        self.assertEqual(charge.call_args.kwargs["model"], wire.model_version)
        self.assertEqual(charge.call_args.kwargs["requested_model"], provider.config.model_simple)

    def test_google_sync_stream_share_tariff_and_unknown_coverage(self):
        from openkyrozen.providers.google import GoogleProvider
        from tests.test_provider_native_tools import _google_schema_sdk
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as directory, closing(EventStore(Path(directory) / "usage.db")) as store:
            provider = GoogleProvider.__new__(GoogleProvider)
            provider.config = ProviderConfig(provider="google", model_simple="gemini-3.5-flash-lite")
            wire = SimpleNamespace(response_id="r", prompt_feedback=None,
                model_version="gemini-3.5-flash-lite-07-2026",
                candidates=[SimpleNamespace(finish_reason="STOP", content=SimpleNamespace(
                    parts=[SimpleNamespace(text="ok", thought=False, function_call=None)]))],
                usage_metadata=SimpleNamespace(prompt_token_count=20, candidates_token_count=3,
                    thoughts_token_count=11, cached_content_token_count=5))
            provider._client = SimpleNamespace(models=SimpleNamespace(
                generate_content=Mock(return_value=wire),
                generate_content_stream=Mock(side_effect=lambda **kwargs: iter([wire]))))
            with usage_scope(store=store), _google_schema_sdk():
                provider.chat_response([])
                list(provider.chat_response_stream([]))
                wire.model_version = "3.0"
                list(provider.chat_response_stream([]))
                wire.model_version = "gemini-3.5-flash-lite-07-2026"
                invalid_terminal = SimpleNamespace(**vars(wire))
                invalid_terminal.usage_metadata = SimpleNamespace(prompt_token_count=20,
                    candidates_token_count=None)
                provider._client.models.generate_content_stream.side_effect = lambda **kwargs: iter([wire, invalid_terminal])
                list(provider.chat_response_stream([]))
            rows = store.list_usage_attempts()
            known = [row for row in rows if row["usage_status"] == "estimated"]
            self.assertEqual(len(known), 2)
            self.assertEqual({row["cost_picos"] for row in known}, {39_650_000})
            self.assertTrue(all(row["pricing_snapshot"]["requested_model"] == provider.config.model_simple
                                for row in rows))
            unknown = next(row for row in rows if row["model"] == "3.0")
            self.assertIsNone(unknown["cost_picos"])
            report = store.usage_totals()
            self.assertEqual(report["estimated_attempts"], 2)
            self.assertEqual(report["unknown_attempts"], 2)
            self.assertEqual(report["authoritative_attempts"], 0)
            self.assertEqual(report["cost_picos"], 79_300_000)

    def test_google_incomplete_or_invalid_usage_never_meters_a_known_amount(self):
        from openkyrozen.providers.google import GoogleProvider

        for prompt, candidate, thoughts, cached in [
            (None, 3, 11, None), (20, None, 11, None),
            (-1, 3, 11, None), (20, 3, -1, None), (20, 3, 11, 21),
        ]:
            with self.subTest(prompt=prompt, candidate=candidate, thoughts=thoughts, cached=cached):
                self.assertIsNone(GoogleProvider._usage(SimpleNamespace(usage_metadata=SimpleNamespace(
                    prompt_token_count=prompt, candidates_token_count=candidate,
                    thoughts_token_count=thoughts, cached_content_token_count=cached))))
        with tempfile.TemporaryDirectory() as directory, closing(EventStore(Path(directory) / "usage.db")) as store:
            with usage_scope(store=store):
                for data in [{"completion_tokens": 14}, {"prompt_tokens": 20},
                             {"prompt_tokens": 20, "completion_tokens": 14, "prompt_cache_hit_tokens": 21},
                             {"prompt_tokens": 20, "completion_tokens": 14, "_estimated": 1}]:
                    _track_cost("google", data, model="gemini-3.5-flash-lite")
            self.assertTrue(all(row["cost_picos"] is None and row["usage_status"] == "unknown"
                                for row in store.list_usage_attempts()))
            totals = store.usage_totals()
            self.assertEqual(totals["cost_picos"], 0)
            self.assertEqual(totals["unknown_attempts"], 4)
            self.assertEqual(totals["providers"][0]["unknown_attempts"], 4)

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
            "import json; from fastapi.testclient import TestClient; from openkyrozen.interfaces.web.service import WebService; server = WebService(); "
            "print(json.dumps(TestClient(server.app).get('/api/cost').json()))"
        )
        with tempfile.TemporaryDirectory(prefix="openkyrozen-usage-restart-") as directory, ExitStack() as sqlite_owners:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            database = root / "state.sqlite3"
            self._record(sqlite_owners.enter_context(closing(EventStore(database))), workspace="default")
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
        with tempfile.TemporaryDirectory(prefix="openkyrozen-usage-concurrent-") as directory, ExitStack() as sqlite_owners:
            store = sqlite_owners.enter_context(closing(EventStore(Path(directory) / "state.sqlite3")))

            def record(index: int) -> None:
                with usage_scope(store=store, workspace_id="project", session_id=f"session-{index % 2}",
                                 run_id=f"run-{index}", surface="mcp"):
                    _track_cost("deepseek", {
                        "prompt_tokens": 1, "prompt_cache_hit_tokens": 0,
                        "prompt_cache_miss_tokens": 1, "completion_tokens": 2,
                    },
                                model="deepseek-chat", latency_ms=index,
                                occurred_at=datetime(2026, 9, 7, 2, tzinfo=timezone.utc))

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
        with tempfile.TemporaryDirectory(prefix="openkyrozen-usage-reset-") as directory, ExitStack() as sqlite_owners:
            memory = sqlite_owners.enter_context(closing(MemoryBank(Path(directory) / "state.sqlite3", workspace_id="project")))
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
        with tempfile.TemporaryDirectory(prefix="openkyrozen-usage-precision-") as directory, ExitStack() as sqlite_owners:
            store = sqlite_owners.enter_context(closing(EventStore(Path(directory) / "state.sqlite3")))
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
        with tempfile.TemporaryDirectory(prefix="openkyrozen-deepseek-pricing-") as directory, ExitStack() as sqlite_owners:
            store = sqlite_owners.enter_context(closing(EventStore(Path(directory) / "state.sqlite3")))
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
        with tempfile.TemporaryDirectory(prefix="openkyrozen-deepseek-usage-") as directory, ExitStack() as sqlite_owners:
            store = sqlite_owners.enter_context(closing(EventStore(Path(directory) / "state.sqlite3")))
            with usage_scope(store=store, workspace_id="project"):
                text, usage = provider.chat([], "deepseek-v4-flash")
            attempt = store.list_usage_attempts(workspace_id="project", user_id="local")[0]
            self.assertEqual(text, "ok")
            self.assertEqual(usage["prompt_cache_hit_tokens"], 3)
            self.assertEqual(attempt["cache_hit_tokens"], 3)
            self.assertEqual(attempt["cache_miss_tokens"], 7)
            self.assertEqual(attempt["reasoning_tokens"], 2)

    def test_current_flash_aliases_and_historical_rate_boundary(self):
        from openkyrozen.providers.usage import _deepseek_v4_pricing_snapshot
        for model in ("deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp", "deepseek-chat", "deepseek-reasoner"):
            with self.subTest(model=model):
                card, known = _deepseek_v4_pricing_snapshot(model, datetime(2026, 9, 11, 2, tzinfo=timezone.utc))
                self.assertIsNotNone(card)
                self.assertTrue(known)
                self.assertEqual(card["model"], "deepseek-flash")
                self.assertEqual(card["cache_hit_picos_per_million"], 6_000_000_000)
                self.assertEqual(card["cache_miss_picos_per_million"], 300_000_000_000)
                self.assertEqual(card["output_picos_per_million"], 1_200_000_000_000)
        before, _ = _deepseek_v4_pricing_snapshot("deepseek-v4-flash", datetime(2026, 9, 10, 3, 59, tzinfo=timezone.utc))
        after, _ = _deepseek_v4_pricing_snapshot("deepseek-v4-flash", datetime(2026, 9, 10, 4, tzinfo=timezone.utc))
        self.assertEqual(before["output_picos_per_million"], 1_320_000_000_000)
        self.assertEqual(after["output_picos_per_million"], 600_000_000_000)
        self.assertEqual(after["version"], "deepseek-flash-pricing-2026-09-10")
        pro, _ = _deepseek_v4_pricing_snapshot("deepseek-v4-pro", datetime(2026, 9, 11, 2, tzinfo=timezone.utc))
        self.assertEqual(pro["output_picos_per_million"], 3_960_000_000_000)

    def test_holiday_discount_and_unknown_future_calendar_are_truthful(self):
        from openkyrozen.providers.usage import _deepseek_v4_pricing_snapshot
        for day in (1, 5, 7):
            card, known = _deepseek_v4_pricing_snapshot("deepseek-v4-pro", datetime(2026, 10, day, 2, tzinfo=timezone.utc))
            self.assertEqual(card["billing_window"], "off_peak")
            self.assertEqual(card["output_picos_per_million"], 1_980_000_000_000)
            self.assertTrue(known)
        card, known = _deepseek_v4_pricing_snapshot("deepseek-v4-pro", datetime(2027, 1, 4, 2, tzinfo=timezone.utc))
        self.assertFalse(known)
        self.assertEqual(card["pricing_status"], "estimated_current_schedule")

    def test_stream_uses_returned_model_for_billing(self):
        from unittest.mock import patch
        chunks = [SimpleNamespace(model="deepseek-flash", choices=[SimpleNamespace(
            delta=SimpleNamespace(content=None, tool_calls=None), finish_reason="stop")], usage=None),
            SimpleNamespace(model="deepseek-flash", choices=[], usage=SimpleNamespace(
            prompt_tokens=1, completion_tokens=2, prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=1,
        ))]
        provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
        provider.config = ProviderConfig(provider="deepseek")
        provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: iter(chunks))))
        with patch("openkyrozen.providers.usage._track_cost") as track:
            list(provider.chat_stream([], "deepseek-v4-pro"))
        self.assertEqual(track.call_args.kwargs["model"], "deepseek-flash")

    def test_completed_stream_records_final_usage_once(self):
        final_usage = SimpleNamespace(
            prompt_tokens=10, completion_tokens=4,
            prompt_cache_hit_tokens=3, prompt_cache_miss_tokens=7,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
        )
        chunks = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="STREAM"))], usage=None),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="_OK"), finish_reason="stop")], usage=None),
            SimpleNamespace(choices=[], usage=final_usage),
        ]
        calls = []
        provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
        provider.config = ProviderConfig(provider="deepseek")
        provider._client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs) or iter(chunks)),
        ))
        with tempfile.TemporaryDirectory(prefix="openkyrozen-stream-usage-") as directory, ExitStack() as sqlite_owners:
            store = sqlite_owners.enter_context(closing(EventStore(Path(directory) / "state.sqlite3")))
            with usage_scope(store=store, workspace_id="project"):
                self.assertEqual(list(provider.chat_stream([], "deepseek-v4-flash")), ["STREAM", "_OK"])
            attempts = store.list_usage_attempts(workspace_id="project", user_id="local")
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["completion_tokens"], 4)
            self.assertEqual(attempts[0]["usage_status"], "authoritative")
            self.assertEqual(calls[0]["stream_options"], {"include_usage": True})


if __name__ == "__main__":
    unittest.main()
