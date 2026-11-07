import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace

from openkyrozen.learning.engine import LearningEngine
from openkyrozen.memory.retrieval import _has_cjk, _cjk_approximate_words
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.routing.router import _classify_complexity, _select_model, decide_request


class SharedRoutingHeuristicTests(unittest.TestCase):
    def setUp(self):
        self.runtime = SimpleNamespace(_provider_config=ProviderConfig(
            provider="deepseek", model_simple="quick", model_complex="reasoning",
        ))
        self.runtime._has_cjk = MethodType(_has_cjk, self.runtime)
        self.runtime._cjk_approximate_words = MethodType(_cjk_approximate_words, self.runtime)

    def decision(self, request, mode="agent"):
        return decide_request(
            self.runtime._provider_config, {}, request,
            lambda text: _select_model(self.runtime, text),
            lambda text: _classify_complexity(self.runtime, text),
            LearningEngine.route_profile(request), "auto", mode,
        )

    def test_substantive_task_signals_survive_greetings_questions_and_short_requests(self):
        for request in (
            "Hey! Implement the cache and add coverage for expiry.",
            "What is needed here? First inspect settings, then repair and verify the result.",
            "你好，请重构模块并运行测试。",
            "Implement caching",
        ):
            with self.subTest(request=request):
                decision, complexity = self.decision(request)
                self.assertEqual(complexity, "complex")
                self.assertTrue(decision.planning_required)
                self.assertEqual(decision.model, "reasoning")
                self.assertFalse(self.decision(request, "ask")[0].planning_required)

    def test_exception_diagnostics_match_after_case_normalization(self):
        for request in ("Investigate a TypeError raised by the callback.",
                        "ValueError on startup", "ATTRIBUTEERROR during parsing"):
            with self.subTest(request=request):
                self.assertEqual(self.decision(request)[1], "complex")

    def test_factual_and_single_action_requests_do_not_gain_a_plan(self):
        for request, expected in (("Hello!", "simple"),
                                  ("What is a TypeError?", "simple"),
                                  ("What is the next prime after eleven?", "simple"),
                                  ("What is the first name on this list?", "simple"),
                                  ("Read configuration.json.", "medium")):
            with self.subTest(request=request):
                decision, complexity = self.decision(request)
                self.assertEqual(complexity, expected)
                self.assertFalse(decision.planning_required)

    def test_model_selection_distinguishes_code_concepts_from_requested_changes(self):
        for request in ("Describe what this class represents in one sentence.",
                        "What is a Python function?", "Briefly explain this code snippet.",
                        "用一句话解释这个函数。"):
            with self.subTest(request=request):
                self.assertEqual(_select_model(self.runtime, request), "quick")
        for request in ("Write a function that checks file hashes.",
                        "Refactor the query parser and add coverage.",
                        "First open settings, then read the selected config."):
            with self.subTest(request=request):
                self.assertEqual(_select_model(self.runtime, request), "reasoning")

    def test_profile_recognizes_regular_inflections_punctuation_and_contiguous_cjk(self):
        for request in ("The regression tests are failing.", "Review tested changes.",
                        "Document these classes.", "Inspect python-based tooling.",
                        "请检查数据库连接并补充测试。", "使用Python读取文件。"):
            with self.subTest(request=request):
                self.assertEqual(LearningEngine.route_profile(request), "coder")
                self.assertEqual(LearningEngine.route_profile(request, "researcher"), "researcher")
        for request in ("Summarize the climate report.", "Research classical music.",
                        "Explain contest eligibility.", "总结新闻报道。"):
            with self.subTest(request=request):
                self.assertEqual(LearningEngine.route_profile(request), "researcher")


class RoutingAblationTests(unittest.TestCase):
    def benchmark(self):
        self.assertIsNotNone(
            importlib.util.find_spec("benchmarks.v3_routing_ablation"),
            "The offline routing ablation has not been implemented",
        )
        return importlib.import_module("benchmarks.v3_routing_ablation")

    def test_offline_report_keeps_feature_ablations_independent(self):
        report = self.benchmark().run(repeats=2)
        rows = {row["case"]: row for row in report["rows"]}
        self.assertEqual(len(rows), 14)
        self.assertEqual(rows["route_fact"]["expected"], {
            "planning": False, "model": "simple", "profile": "researcher",
        })
        self.assertEqual(rows["route_code_change"]["expected"], {
            "planning": True, "model": "reasoning", "profile": "coder",
        })
        for row in rows.values():
            baseline = row["variants"]["default"]
            for feature, summary in report["features"].items():
                for variant in summary["constant_ablations"]:
                    actual = row["variants"][variant]
                    for unchanged in {"planning", "model", "profile"} - {feature}:
                        self.assertEqual(actual[unchanged], baseline[unchanged])
                correct = sum(item["variants"]["default"][feature] == item["expected"][feature]
                              for item in rows.values())
                self.assertEqual(summary["default"]["correct"], correct)
                self.assertGreaterEqual(summary["default"]["routing_median_us"], 0)
        self.assertEqual(report["provider_calls"], 0)
        self.assertFalse(report["model_quality_evaluated"])
        self.assertFalse(report["release_requirements_certified"])

    def test_candidate_binding_rejects_another_commit_before_running(self):
        benchmark = self.benchmark()
        with self.assertRaisesRegex(ValueError, "candidate"):
            benchmark.run(repeats=1, candidate_sha="0" * 40)
        for repeats in (0, -1, True):
            with self.assertRaises(ValueError):
                benchmark.run(repeats=repeats)

    def test_cli_is_offline_and_leaves_private_state_empty(self):
        self.benchmark()
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(private),
                   "KYROZEN_DB_PATH": str(private / "state.sqlite3"),
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                   "PYTHONDONTWRITEBYTECODE": "1"}
            command = (
                "import runpy, socket, sys; "
                "socket.socket.connect=lambda *a, **k: (_ for _ in ()).throw(AssertionError('network access')); "
                "sys.argv=['v3_routing_ablation','--repeats','1']; "
                "runpy.run_module('benchmarks.v3_routing_ablation',run_name='__main__')"
            )
            result = subprocess.run([sys.executable, "-c", command], cwd=private,
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["provider_calls"], 0)
            self.assertEqual(list(private.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
