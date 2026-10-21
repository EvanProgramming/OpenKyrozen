import tempfile
import unittest
from pathlib import Path

from benchmarks.v3_matched import BudgetLedger, summarize


class V3MatchedBenchmarkTests(unittest.TestCase):
    def test_dollar_reservations_persist_and_unknown_usage_remains_reserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.sqlite3"
            budget = BudgetLedger(path, limit_usd="0.005")

            self.assertTrue(budget.reserve("attempt-1", input_token_bound=100_000, output_token_bound=1_000))
            budget.settle("attempt-1", None)
            with self.assertRaises(ValueError):
                BudgetLedger(path, limit_usd="0.01")
            resumed = BudgetLedger(path, limit_usd="0.005")
            self.assertAlmostEqual(float(resumed.remaining_usd), 0.001425)
            self.assertFalse(resumed.reserve("attempt-2", input_token_bound=50_000, output_token_bound=0))

    def test_known_usage_releases_unused_reservation_at_user_rates(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = BudgetLedger(Path(directory) / "budget.sqlite3", limit_usd="0.01")

            self.assertTrue(budget.reserve("attempt-1", input_token_bound=100_000, output_token_bound=1_000))
            budget.settle("attempt-1", {
                "input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 20,
            })

            self.assertAlmostEqual(float(budget.spent_usd), 0.00000264 + 0.000000066 + 0.00000275)
            self.assertGreater(budget.remaining_usd, 0.009)

    def test_summary_requires_all_matched_rows_and_known_usage(self):
        rows = []
        for fixture in range(30):
            for repeat in range(3):
                for arm in ("v2", "v3"):
                    rows.append({
                        "fixture_id": f"fixture-{fixture}", "repeat": repeat, "arm": arm,
                        "verified_success": arm == "v3", "workflow_state_correct": True,
                        "requests": 2 if arm == "v3" else 3,
                        "input_tokens": 90 if arm == "v3" else 100,
                        "output_tokens": 45 if arm == "v3" else 50,
                        "cache_read_tokens": 0,
                        "latency_ms": 100, "usage_complete": True,
                        "first_delta_latency_ms": 10, "attempts": 2,
                        "cost_usd": 0.0001, "safety_passed": True,
                        "durable_side_effect_verified": True, "recovery_verified": True,
                        "plan_revisions": 0, "cancelled_tasks": 0,
                        "material_revisions": 0, "unnecessary_churn": 0,
                    })

        result = summarize(rows, bootstrap_samples=500)

        self.assertTrue(result["complete"])
        self.assertGreater(result["paired_success_ci95"][0], 0)
        self.assertTrue(result["release_comparison_passed"])
        self.assertEqual(result["summary"]["v3"]["workflow_state_correct"], 90)
        self.assertEqual(result["summary"]["v3"]["attempts"], 180)
        self.assertAlmostEqual(result["summary"]["v3"]["cost_usd"], 0.00138105)
        self.assertTrue(result["summary"]["v3"]["all_safety_passed"])
        rows[-1]["safety_passed"] = False
        self.assertFalse(summarize(rows, bootstrap_samples=50)["release_comparison_passed"])
        self.assertFalse(summarize(rows[:-1], bootstrap_samples=50)["complete"])
        rows[0]["usage_complete"] = False
        self.assertFalse(summarize(rows, bootstrap_samples=50)["accounting_complete"])


if __name__ == "__main__":
    unittest.main()
