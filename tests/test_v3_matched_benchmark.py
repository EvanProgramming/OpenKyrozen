import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from benchmarks.v3_matched import BudgetLedger, PRICING, _cost, summarize
from benchmarks.v3_responses_relay import MODEL


class V3MatchedBenchmarkTests(unittest.TestCase):
    def test_pricing_matches_the_approved_panstar_rates_at_recorded_fx(self):
        self.assertEqual(PRICING["source_currency"], "USD")
        self.assertEqual(PRICING["input_usd_per_million"], "0.033")
        self.assertEqual(PRICING["output_usd_per_million"], "0.275")
        self.assertEqual(PRICING["cache_read_usd_per_million"], "0.0033")
        self.assertEqual(PRICING["usd_cny"], "6.67135")

    def test_default_budget_is_the_twenty_dollar_cap_at_recorded_fx(self):
        from benchmarks.v3_matched import MAX_LIMIT_RMB, MAX_LIMIT_USD, USD_CNY

        with tempfile.TemporaryDirectory() as directory:
            budget = BudgetLedger(Path(directory) / "budget.sqlite3")
            self.assertEqual(budget.remaining_rmb, MAX_LIMIT_RMB)
            self.assertEqual(MAX_LIMIT_USD, Decimal("20"))
            self.assertEqual(budget.remaining_rmb, 20 * USD_CNY)
            self.assertEqual(BudgetLedger(Path(directory) / "limit.sqlite3", limit_rmb="33.3795").remaining_rmb,
                             Decimal("33.3795"))
            for limit in ("0", "133.4271", "134"):
                with self.assertRaises(ValueError):
                    BudgetLedger(Path(directory) / f"{limit}.sqlite3", limit_rmb=limit)

    def test_full_prior_cap_reservation_blocks_additional_requests(self):
        from benchmarks.v3_matched import MAX_LIMIT_RMB

        with tempfile.TemporaryDirectory() as directory:
            budget = BudgetLedger(Path(directory) / "budget.sqlite3",
                                  limit_rmb=MAX_LIMIT_RMB, prior_hold_rmb=MAX_LIMIT_RMB)
            self.assertEqual(budget.remaining_rmb, 0)
            self.assertFalse(budget.reserve("new-attempt", input_token_bound=1, output_token_bound=1))

    def test_cap_reduction_preserves_existing_reservations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.sqlite3"
            budget = BudgetLedger(path, prior_hold_rmb="6.652729942234")
            budget.reserve("unknown-attempt", input_token_bound=100_000, output_token_bound=1_000,
                           metadata={"fixture_id": "test", "arm": "v3",
                                     "reserved_rmb": str(_cost(100_000, 1_000))})
            budget.settle("unknown-attempt", None)
            reduced = BudgetLedger(path, limit_rmb="66.7135", prior_hold_rmb="6.652729942234")
            self.assertEqual(reduced.campaign_calls()[0]["status"], "unknown")
            self.assertAlmostEqual(float(reduced.remaining_rmb),
                                   float(Decimal("66.7135") - Decimal("6.652729942234")
                                         - _cost(100_000, 1_000)))

    def test_cap_reduction_rejects_commitments_above_new_cap_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.sqlite3"
            BudgetLedger(path)
            with sqlite3.connect(path) as db:
                db.execute("UPDATE budget_config SET limit_picormb=133427000000000 WHERE id=1")
                db.execute("INSERT INTO attempts(id,reserved_picormb,status) VALUES('held',66760000000000,'unknown')")
            with self.assertRaisesRegex(ValueError, "commitments exceed the requested cap"):
                BudgetLedger(path, limit_rmb="66.7135")

            with sqlite3.connect(path) as db:
                persisted_limit = db.execute("SELECT limit_picormb FROM budget_config").fetchone()[0]
            self.assertEqual(persisted_limit, 133_427_000_000_000)

    def test_prior_unknown_campaign_reservations_are_imported_once(self):
        from benchmarks.v3_matched import MAX_LIMIT_RMB

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.sqlite3"
            budget = BudgetLedger(path, prior_hold_rmb="2.020002495068")
            self.assertEqual(budget.campaign_calls(), [])
            self.assertAlmostEqual(float(budget.remaining_rmb), float(MAX_LIMIT_RMB) - 2.020002495068)
            BudgetLedger(path, prior_hold_rmb="2.020002495068")
            with self.assertRaisesRegex(ValueError, "missing the prior campaign reservation"):
                BudgetLedger(path, prior_hold_rmb="0.2")

    def test_old_usd_ledger_is_preserved_and_refused_under_new_rmb_pricing(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE budget_config (id INTEGER PRIMARY KEY, limit_picodollars INTEGER)")
                db.execute("INSERT INTO budget_config VALUES (1, 20000000000000)")
                db.execute("CREATE TABLE attempts (id TEXT, reserved_microdollars INTEGER)")
                db.execute("INSERT INTO attempts VALUES ('old', 123)")
            with self.assertRaisesRegex(ValueError, "legacy USD"):
                BudgetLedger(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT * FROM attempts").fetchall(), [("old", 123)])

    def test_rmb_reservations_persist_and_unknown_usage_remains_reserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.sqlite3"
            budget = BudgetLedger(path, limit_rmb="0.1")

            self.assertTrue(budget.reserve("attempt-1", input_token_bound=100_000, output_token_bound=1_000))
            budget.settle("attempt-1", None)
            with self.assertRaises(ValueError):
                BudgetLedger(path, limit_rmb="0.2")
            resumed = BudgetLedger(path, limit_rmb="0.1")
            self.assertAlmostEqual(float(resumed.remaining_rmb), 0.1 - float(_cost(100_000, 1_000)))
            self.assertFalse(resumed.reserve("attempt-2", input_token_bound=2_000_000, output_token_bound=0))

    def test_marking_an_attempt_unknown_twice_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = BudgetLedger(Path(directory) / "budget.sqlite3", limit_rmb="0.1")
            budget.reserve("attempt-1", input_token_bound=100, output_token_bound=100)

            budget.settle("attempt-1", None)
            budget.settle("attempt-1", None)

            with sqlite3.connect(budget.path) as db:
                self.assertEqual(db.execute("SELECT status FROM attempts WHERE id='attempt-1'").fetchone()[0],
                                 "unknown")

    def test_unknown_settlement_does_not_replace_known_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = BudgetLedger(Path(directory) / "budget.sqlite3", limit_rmb="0.1")
            budget.reserve("attempt-1", input_token_bound=100, output_token_bound=100)
            usage = {"input_tokens": 10, "output_tokens": 2,
                     "cache_read_tokens": 0, "cache_write_tokens": 0}

            budget.settle("attempt-1", usage)
            budget.settle("attempt-1", None)

            with sqlite3.connect(budget.path) as db:
                actual = db.execute("SELECT status,usage_json FROM attempts WHERE id='attempt-1'").fetchone()
            self.assertEqual(actual, ("settled", json.dumps(usage, sort_keys=True)))

    def test_known_usage_releases_unused_reservation_at_user_rates(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = BudgetLedger(Path(directory) / "budget.sqlite3", limit_rmb="0.1")

            self.assertTrue(budget.reserve("attempt-1", input_token_bound=100_000, output_token_bound=1_000))
            budget.settle("attempt-1", {
                "input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 20,
                "cache_write_tokens": 30,
            })

            self.assertAlmostEqual(float(budget.spent_rmb), float(_cost(100, 10, 20, 30)))
            self.assertGreater(budget.remaining_rmb, 0.09)

    def test_campaign_journal_recovers_call_metadata_and_marks_reserved_calls_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = BudgetLedger(Path(directory) / "budget.sqlite3")
            metadata = {"arm": "v3", "fixture_id": "bug_fix-1", "repeat": 0,
                        "request_sha256": "hash", "model": MODEL,
                        "max_output_tokens": 4096, "reserved_rmb": str(_cost(100, 100))}
            self.assertTrue(budget.reserve("call-1", input_token_bound=100,
                                           output_token_bound=100, metadata=metadata))
            call = budget.campaign_calls()[0]
            self.assertEqual(call["request_sha256"], "hash")
            self.assertEqual(call["status"], "unknown")
            self.assertEqual(call["usage"], None)
            self.assertGreater(budget.remaining_rmb, 0)

    def test_archived_unknown_attempt_keeps_its_reservation_out_of_the_new_cohort(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = BudgetLedger(Path(directory) / "budget.sqlite3")
            metadata = {"arm": "v3", "fixture_id": "investigation-1", "repeat": 0,
                        "model": "gemini-flash-3.6", "reserved_rmb": "0.00044",
                        "archived_reservation": True}
            self.assertTrue(budget.reserve("old-call", input_token_bound=100,
                                           output_token_bound=100, metadata=metadata))
            self.assertEqual(budget.campaign_calls(), [])
            self.assertGreater(budget.remaining_rmb, 0)
            with sqlite3.connect(budget.path) as db:
                self.assertEqual(db.execute("SELECT status FROM attempts WHERE id='old-call'").fetchone()[0],
                                 "unknown")

    def test_actual_overrun_poison_stops_every_later_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = BudgetLedger(Path(directory) / "budget.sqlite3", limit_rmb="0.01")
            self.assertTrue(budget.reserve("attempt-1", input_token_bound=1, output_token_bound=1))
            budget.settle("attempt-1", {"input_tokens": 100_000, "output_tokens": 0})
            self.assertEqual(budget.remaining_rmb, 0)
            self.assertFalse(budget.reserve("attempt-2", input_token_bound=1, output_token_bound=1))

    def test_summary_requires_all_matched_rows_and_known_usage(self):
        from benchmarks.v3_fixtures import FIXTURES

        rows = []
        for fixture in FIXTURES:
            for repeat in range(3):
                for arm in ("v2", "v3"):
                    rows.append({
                        "fixture_id": fixture["fixture_id"], "repeat": repeat, "arm": arm,
                        "verified_success": arm == "v3", "workflow_state_correct": True,
                        "requests": 2 if arm == "v3" else 3,
                        "input_tokens": 90 if arm == "v3" else 100,
                        "output_tokens": 45 if arm == "v3" else 50,
                        "cache_read_tokens": 0, "cache_write_tokens": 0,
                        "latency_ms": 100, "usage_complete": True,
                        "first_delta_latency_ms": 10, "attempts": 2,
                        "cost_rmb": 0.0001, "safety_passed": True,
                        "verifier_result": "passed", "durable_side_effect_expected": True,
                        "durable_side_effect_verified": True, "recovery_required": True,
                        "recovery_verified": True,
                        "protocol_recovery": 0, "duplicate_calls": 0,
                        "budget_ok": True,
                        "plan_revisions": 0, "cancelled_tasks": 0,
                        "material_revisions": 0, "unnecessary_churn": 0,
                        "worker_result_complete": True, "worker_exit_code": 0,
                        "plan_scenario": fixture.get("plan_scenario"), "plan_scenario_passed": True,
                    })

        result = summarize(rows, bootstrap_samples=500)

        self.assertTrue(result["complete"])
        self.assertGreater(result["paired_success_ci95"][0], 0)
        self.assertTrue(result["comparison_thresholds_met"])
        self.assertFalse(result["release_comparison_passed"])
        json.dumps(result)
        self.assertIsInstance(result["comparisons"]["token_savings_fraction"], float)
        self.assertEqual(result["summary"]["v3"]["workflow_state_correct"], 90)
        self.assertEqual(result["summary"]["v3"]["attempts"], 180)
        self.assertAlmostEqual(result["summary"]["v3"]["cost_rmb"], float(90 * _cost(90, 45)))
        self.assertTrue(result["summary"]["v3"]["all_safety_passed"])
        self.assertEqual(result["summary"]["v3"]["verifier_passed"], 90)
        self.assertEqual(result["summary"]["v3"]["durable_side_effects_expected"], 90)
        self.assertEqual(result["summary"]["v3"]["recoveries_required"], 90)
        baseline_gap = [dict(row) for row in rows]
        baseline_gap[0].update(verifier_result="failed", durable_side_effect_verified=False,
                               recovery_verified=False)
        self.assertTrue(summarize(baseline_gap, bootstrap_samples=50)["comparison_thresholds_met"])
        for field, value in (("verifier_result", "failed"), ("durable_side_effect_verified", False),
                             ("recovery_verified", False)):
            incomplete = [dict(row) for row in rows]
            next(row for row in incomplete if row["arm"] == "v3")[field] = value
            self.assertFalse(summarize(incomplete, bootstrap_samples=50)["comparison_thresholds_met"])
        rows[-1]["safety_passed"] = False
        self.assertFalse(summarize(rows, bootstrap_samples=50)["release_comparison_passed"])
        self.assertFalse(summarize(rows[:-1], bootstrap_samples=50)["complete"])
        rows[0]["usage_complete"] = False
        self.assertFalse(summarize(rows, bootstrap_samples=50)["accounting_complete"])
        incomplete_worker = [dict(row) for row in rows]
        incomplete_worker[0].update(usage_complete=True, worker_result_complete=False)
        self.assertFalse(summarize(incomplete_worker, bootstrap_samples=50)["accounting_complete"])
        missing_exit = [dict(row) for row in rows]
        missing_exit[0]["usage_complete"] = True
        missing_exit[0].pop("worker_exit_code")
        self.assertFalse(summarize(missing_exit, bootstrap_samples=50)["accounting_complete"])

    def test_summary_rejects_arbitrary_fixture_ids_and_overbudget_rows(self):
        from benchmarks.v3_fixtures import FIXTURES

        rows = []
        for fixture in FIXTURES:
            for repeat in range(3):
                for arm in ("v2", "v3"):
                    rows.append({"fixture_id": fixture["fixture_id"], "repeat": repeat, "arm": arm,
                                 "verified_success": False, "budget_ok": True})
        arbitrary = [dict(row, fixture_id="forged-" + row["fixture_id"]) for row in rows]
        self.assertFalse(summarize(arbitrary, bootstrap_samples=10)["complete"])
        rows[-1]["budget_ok"] = False
        self.assertFalse(summarize(rows, bootstrap_samples=10)["release_comparison_passed"])


if __name__ == "__main__":
    unittest.main()
