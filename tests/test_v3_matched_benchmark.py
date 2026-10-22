import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.v3_matched import BudgetLedger, summarize


class V3MatchedBenchmarkTests(unittest.TestCase):
    def test_default_budget_is_below_the_approved_fifty_rmb_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = BudgetLedger(Path(directory) / "budget.sqlite3")
            self.assertEqual(budget.remaining_rmb, 49)
            for limit in ("0", "50", "51"):
                with self.assertRaises(ValueError):
                    BudgetLedger(Path(directory) / f"{limit}.sqlite3", limit_rmb=limit)

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
            self.assertAlmostEqual(float(resumed.remaining_rmb), 0.0263)
            self.assertFalse(resumed.reserve("attempt-2", input_token_bound=50_000, output_token_bound=0))

    def test_known_usage_releases_unused_reservation_at_user_rates(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = BudgetLedger(Path(directory) / "budget.sqlite3", limit_rmb="0.1")

            self.assertTrue(budget.reserve("attempt-1", input_token_bound=100_000, output_token_bound=1_000))
            budget.settle("attempt-1", {
                "input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 20,
                "cache_write_tokens": 30,
            })

            self.assertAlmostEqual(float(budget.spent_rmb), 0.00008)
            self.assertGreater(budget.remaining_rmb, 0.09)

    def test_campaign_journal_recovers_call_metadata_and_marks_reserved_calls_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = BudgetLedger(Path(directory) / "budget.sqlite3")
            metadata = {"arm": "v3", "fixture_id": "bug_fix-1", "repeat": 0,
                        "request_sha256": "hash", "model": "gemini-3.6-flash-low",
                        "max_output_tokens": 4096, "reserved_rmb": "0.00044"}
            self.assertTrue(budget.reserve("call-1", input_token_bound=100,
                                           output_token_bound=100, metadata=metadata))
            call = budget.campaign_calls()[0]
            self.assertEqual(call["request_sha256"], "hash")
            self.assertEqual(call["status"], "unknown")
            self.assertEqual(call["usage"], None)
            self.assertGreater(budget.remaining_rmb, 0)

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
        self.assertAlmostEqual(result["summary"]["v3"]["cost_rmb"], 0.020655)
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
