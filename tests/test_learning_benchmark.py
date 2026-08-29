import unittest

from learning_benchmark import compare, summarize


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


if __name__ == "__main__":
    unittest.main()
