import tempfile
import unittest
from pathlib import Path

from benchmarks.v3_fixtures import FIXTURES
from benchmarks.v3_matched_live import (_run_fixture_tests, _worker_env, _workflow_state_correct,
                                         prepare_fixture, snapshot_workspace, verify_fixture)


class V3MatchedLiveTests(unittest.TestCase):
    def test_fixture_setup_reconstructs_git_history_for_independent_verification(self):
        fixture = next(item for item in FIXTURES if item["fixture_id"] == "git_history-2")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_fixture(root, fixture)
            answer = f"increase retry count {prepared['expected_commit']} changed RETRIES = 1 to RETRIES = 3"
            result = verify_fixture(fixture, root, answer, {}, prepared)
        self.assertEqual(result["result"], "passed")
        self.assertTrue(result["verified_success"])

    def test_verifier_rejects_missing_expected_answer_terms_and_unsafe_file_changes(self):
        fixture = next(item for item in FIXTURES if item["fixture_id"] == "permissions-2")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_fixture(root, fixture)
            (root / "secret.txt").write_text("copied secret\n")
            result = verify_fixture(fixture, root, "permission denied for secret.txt", {}, prepared)
        self.assertEqual(result["result"], "failed")
        self.assertFalse(result["safety_passed"])

    def test_test_verifier_fails_if_the_agent_changes_the_test(self):
        fixture = next(item for item in FIXTURES if item["fixture_id"] == "bug_fix-1")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_fixture(root, fixture)
            (root / "test_stats.py").write_text("import unittest\nclass T(unittest.TestCase): pass\n")
            result = verify_fixture(fixture, root, "tests pass", {}, prepared)
        self.assertEqual(result["result"], "failed")
        self.assertFalse(result["safety_passed"])

    def test_fixture_tests_verify_the_actual_workspace_source(self):
        fixture = next(item for item in FIXTURES if item["fixture_id"] == "bug_fix-1")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_fixture(root, fixture)
            (root / "stats.py").write_text("def mean(values):\n    return 0 if not values else sum(values) / len(values)\n")
            self.assertTrue(_run_fixture_tests(root, fixture["verifier"]["command"]))

    def test_plan_fixture_catalog_covers_scope_reapproval_and_churn(self):
        scenarios = {fixture["plan_scenario"] for fixture in FIXTURES if fixture["plan_scenario"]}
        self.assertTrue({"scope_preserving", "material_revision", "unnecessary_churn",
                         "add_discovered_edge_case", "cancel_obsolete_task_after_reproduction",
                         "reorder_after_dependency_discovery", "cancel_replace"} <= scenarios)

    def test_discovered_task_scenario_requires_an_actual_plan_revision(self):
        fixture = next(item for item in FIXTURES if item["fixture_id"] == "bug_fix-1")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_fixture(root, fixture)
            (root / "stats.py").write_text("def mean(values):\n    return 0 if not values else sum(values) / len(values)\n")
            worker = {"provider_error": False, "processes_alive": 0, "plan_revisions": 0,
                      "task_records": [{"description": text, "status": "succeeded"}
                                       for text in ["reproduce", "fix"]]}
            rejected = verify_fixture(fixture, root, "fixed", worker, prepared)
            worker["plan_revisions"] = 1
            worker["task_records"].append({"description": "verify empty iterable", "status": "succeeded"})
            accepted = verify_fixture(fixture, root, "fixed", worker, prepared)
        self.assertFalse(rejected["plan_scenario_passed"])
        self.assertTrue(accepted["plan_scenario_passed"])

    def test_material_revision_scenario_requires_pending_approval_and_preserved_scope(self):
        fixture = next(item for item in FIXTURES if item["fixture_id"] == "bug_fix-3")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_fixture(root, fixture)
            (root / "paging.py").write_text("def offset(page, size):\n    return (page - 1) * size\n")
            worker = {"provider_error": False, "processes_alive": 0,
                      "pending_material_revision": True, "accepted_scope_preserved": True,
                      "material_plan_proposals": 1}
            result = verify_fixture(fixture, root, "please approve the public API change", worker, prepared)
        self.assertTrue(result["plan_scenario_passed"])
        self.assertTrue(result["verified_success"])

    def test_scope_scenario_rejects_missing_active_strategic_binding(self):
        fixture = next(item for item in FIXTURES if item["plan_scenario"] == "scope_preserving")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_fixture(root, fixture)
            worker = {"provider_error": False, "processes_alive": 0, "scope_preserved": False}
            result = verify_fixture(fixture, root, "done", worker, prepared)
        self.assertFalse(result["plan_scenario_passed"])

    def test_empty_capability_list_is_not_promoted_to_full_access(self):
        fixture = next(item for item in FIXTURES if item["fixture_id"] == "permissions-2")
        with tempfile.TemporaryDirectory() as directory:
            env = _worker_env(Path(directory), fixture, type("Relay", (), {"client_key": "local"})())
        self.assertEqual(env["KYROZEN_CLI_CAPABILITIES"], "none")

    def test_cancellation_verifier_requires_a_started_and_stopped_process(self):
        fixture = next(item for item in FIXTURES if item["fixture_id"] == "cancellation-1")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_fixture(root, fixture)
            idle = verify_fixture(fixture, root, "done", {"processes_started": 0,
                "processes_cancelled": 0, "processes_alive": 0}, prepared)
            stopped = verify_fixture(fixture, root, "done", {"processes_started": 1,
                "processes_cancelled": 1, "processes_alive": 0}, prepared)
        self.assertEqual(idle["result"], "failed")
        self.assertEqual(stopped["result"], "passed")

    def test_required_plan_cannot_pass_with_missing_or_unfinished_tasks(self):
        fixture = next(item for item in FIXTURES if item.get("plan_tasks"))
        self.assertFalse(_workflow_state_correct(fixture, {"tasks": []}))
        self.assertFalse(_workflow_state_correct(fixture, {"tasks": ["succeeded", "running"]}))
        self.assertTrue(_workflow_state_correct(fixture, {"tasks": ["succeeded", "cancelled"]}))

    def test_snapshot_excludes_git_metadata_but_tracks_workspace_files(self):
        fixture = next(item for item in FIXTURES if item["fixture_id"] == "investigation-1")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare_fixture(root, fixture)
            first = snapshot_workspace(root)
            (root / ".git").mkdir()
            (root / ".git" / "internal").write_text("not task output")
            self.assertEqual(first, snapshot_workspace(root))


if __name__ == "__main__":
    unittest.main()
