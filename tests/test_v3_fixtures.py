import unittest

from benchmarks.v3_fixtures import CATEGORIES, FIXTURES, validate_fixtures


class V3FixtureCatalogTests(unittest.TestCase):
    def test_freezes_thirty_unique_fixtures_with_three_per_required_category(self):
        validate_fixtures(FIXTURES)
        self.assertEqual(len(FIXTURES), 30)
        self.assertEqual(set(CATEGORIES), {fixture["category"] for fixture in FIXTURES})

    def test_replanning_fixtures_are_explicit_and_independently_verifiable(self):
        cases = [fixture for fixture in FIXTURES if fixture.get("plan_scenario")]
        self.assertGreaterEqual(len(cases), 6)
        self.assertTrue(all(fixture.get("verifier") for fixture in cases))

    def test_duplicate_or_missing_fixtures_fail_closed(self):
        with self.assertRaises(ValueError):
            validate_fixtures(FIXTURES[:-1])
        with self.assertRaises(ValueError):
            validate_fixtures([*FIXTURES[:-1], FIXTURES[0]])


if __name__ == "__main__":
    unittest.main()
