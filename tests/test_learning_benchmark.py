import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from learning_benchmark import compare, main, summarize


class LearningBenchmarkTests(unittest.TestCase):
    def test_summary_and_comparison_are_harness_neutral(self):
        clean_results = [{"verified_success": True, "corrections": 1, "repeated_errors": 1,
                          "tool_calls": 10, "tokens": 1000, "latency": 10.0} for _ in range(6)]
        evolved_results = [{"verified_success": True, "corrections": 0, "repeated_errors": 0,
                            "tool_calls": 7, "tokens": 700, "latency": 7.0} for _ in range(6)]
        result = compare(summarize(clean_results), summarize(evolved_results), clean_results, evolved_results)
        self.assertTrue(result["completion_non_regressing"])
        self.assertTrue(result["credible_secondary_gain"])
        self.assertTrue(result["public_superiority_claim_supported"])

    def test_cli_exports_named_ablation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = root / "cases.jsonl"
            runner = root / "runner.py"
            output = root / "result.json"
            cases.write_text(json.dumps({"id": "one", "profile": "coder", "task": "test"}) + "\n")
            runner.write_text(
                "import json,sys\njson.load(sys.stdin)\nprint(json.dumps({'verified_success': True, 'corrections': 0, 'repeated_errors': 0, 'tool_calls': 1, 'tokens': 1, 'latency': 0.1}))\n"
            )
            command = f"{sys.executable} {runner}"
            main(["--cases", str(cases), "--clean-runner", command, "--evolved-runner", command,
                  "--ablation", f"no-memory={command}", "--output", str(output)])
            self.assertIn("no-memory", json.loads(output.read_text())["ablations"])

    def test_make_target_runs_repository_fixture_with_isolated_evidence(self):
        repository = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as home:
            env = os.environ.copy()
            env["HOME"] = home
            env["KYROZEN_DISABLE_VECTOR_INDEX"] = "1"
            for variable in ("KYROZEN_PROVIDER", "KYROZEN_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY",
                             "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "KYROZEN_DB_PATH"):
                env.pop(variable, None)
            result = subprocess.run(
                ["make", "benchmark"], cwd=repository, env=env,
                capture_output=True, text=True, timeout=120,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["protocol"], "openkyrozen-learning-benchmark-v1")
        self.assertEqual(report["case_order"], [
            "speaker-belief-isolation", "speaker-update", "private-leakage",
            "audience-language", "term-ambiguity",
        ])
        self.assertEqual(report["clean"]["summary"]["verified_successes"], 5)
        self.assertEqual(report["evolved"]["summary"]["verified_successes"], 5)
        self.assertEqual(report["metadata"]["runners"]["clean"]["providers"], ["deterministic-fixture"])
        self.assertEqual(report["metadata"]["runners"]["evolved"]["models"], ["openkyrozen-memory-policy-v1"])
        self.assertEqual(report["comparison"]["paired_evidence_status"], "insufficient")
        self.assertFalse(report["comparison"]["public_superiority_claim_supported"])


if __name__ == "__main__":
    unittest.main()
