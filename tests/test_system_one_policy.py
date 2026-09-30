import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openkyrozen.routing.policy as system_one_policy
from benchmarks import system_one


class SystemOnePolicyTests(unittest.TestCase):
    def test_calibration_uses_holdout_and_records_dataset_model_hash(self):
        rows = ([{"score": 0.95, "correct": True}, {"score": 0.90, "correct": True},
                 {"score": 0.70, "correct": False}, {"score": 0.40, "correct": False},
                 {"score": 0.20, "correct": False}] * 2)
        policy = system_one_policy.calibrate(
            rows, backend="kev", model_version="kev-test", dataset_id="corpus-test",
            target_precision=1.0,
        )
        self.assertEqual(policy["backend"], "kev")
        self.assertEqual(policy["model_version"], "kev-test")
        self.assertEqual(policy["dataset_id"], "corpus-test")
        self.assertEqual(policy["calibration_count"], 8)
        self.assertEqual(policy["holdout_count"], 2)
        self.assertIn("calibration_id", policy)
        self.assertIn("bootstrap_precision_95", policy)

    def test_write_and_load_keep_backend_specific_validation(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
                system_one_policy, "_path", return_value=Path(directory) / "calibration.json"):
            written = system_one_policy.write(
                "kev", {"tool_output_review": {"probability": 0.99, "validated": True}},
                model_version="kev-test", dataset_id="data-test",
            )
            self.assertIn("kev", written["backends"])
            loaded = system_one_policy.load("kev", "tool_output_review")
            self.assertTrue(loaded["validated"])
            self.assertEqual(loaded["probability"], 0.99)
            self.assertFalse(system_one_policy.load("jev", "tool_output_review")["validated"])

    def test_calibration_metrics_report_brier_ece_and_reliability(self):
        metrics = system_one_policy.calibration_metrics([0.9, 0.2], [True, False])
        self.assertEqual(metrics["brier"], 0.025)
        self.assertIsNotNone(metrics["ece"])
        self.assertEqual(sum(item["count"] for item in metrics["reliability"]), 2)

    def test_calibration_does_not_create_zero_gate_and_preserves_tool_classes(self):
        rows = []
        for expected in (True, False):
            for case in range(3):
                rows.extend({"case": f"{expected}-{case}", "score": 0.9 if expected else 0.1,
                             "correct": True, "expected": expected} for _ in range(2))
        policy = system_one_policy.calibrate(
            rows, backend="jev", model_version="jev-test", target_precision=0.99,
            stratify_field="expected",
        )
        self.assertGreater(policy["threshold"], 0.0)
        _, holdout = system_one_policy.split_cases(rows, stratify_field="expected")
        self.assertEqual({row["expected"] for row in holdout}, {True, False})

    def test_calibration_maximizes_coverage_after_meeting_precision(self):
        rows = [
            {"case": "high", "score": 0.95, "correct": True},
            {"case": "medium", "score": 0.80, "correct": True},
            {"case": "low", "score": 0.65, "correct": True},
            {"case": "bad", "score": 0.30, "correct": False},
        ]
        policy = system_one_policy.calibrate(
            rows, backend="jev", model_version="jev-test", target_precision=1.0,
            calibration_fraction=0.75,
        )
        self.assertEqual(policy["threshold"], 0.65)

    def test_tool_benchmark_scores_the_positive_class_at_calibrated_threshold(self):
        response = {"answers": {"decision": {"type": "noul", "noul": 0.42}},
                    "wall_ms": 1, "usage": {}, "model": "jev-test"}
        with patch.object(system_one.fast_mode, "_request", return_value=response), \
                patch.object(system_one.system_one_policy, "load", return_value={"probability": 0.4}):
            row = system_one._call(
                "jev", "tool_output_review", {"output": "instruction"},
                {"decision": {"type": "noul"}}, "decision", True, 0, "tool",
            )
        self.assertTrue(row["accepted"])
        self.assertTrue(row["correct"])

    def test_tool_calibration_uses_the_separating_point_above_benign_scores(self):
        rows = []
        for case, score, expected in (("m1", 0.86, True), ("m2", 0.90, True),
                                      ("m3", 0.92, True), ("b1", 0.05, False),
                                      ("b2", 0.04, False), ("b3", 0.06, False)):
            rows.extend({"case": case, "score": score, "expected": expected, "correct": True}
                        for _ in range(2))
        policy = system_one._calibrate_tool_action(
            rows, backend="jev", model_version="jev-test", dataset_id="data-test",
            target_precision=0.995, minimum_coverage=0.1,
        )
        self.assertGreater(policy["threshold"], 0.06)
        self.assertTrue(policy["validated"])

    def test_system_one_dataset_hash_is_stable_with_set_labels(self):
        first = system_one.dataset_id()
        second = system_one.dataset_id()
        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)


if __name__ == "__main__":
    unittest.main()
