import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.v3_fixtures import FIXTURES
from benchmarks.v3_matched_live import (BASELINE, MODEL, _load_checkpoint, _run_fixture_tests, _worker_env,
                                         _extract_baseline, _initial_plan, _workflow_state_correct,
                                         _write_checkpoint, prepare_fixture, run, snapshot_workspace,
                                         verify_fixture)
from benchmarks.v3_matched import BudgetLedger


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
            from openkyrozen.tools.sandbox import SandboxUnavailableError, sandbox_command
            try:
                probe = sandbox_command("true", workspace=str(root), cwd=str(root), env={})
            except SandboxUnavailableError:
                available = False
            else:
                available = probe.backend != "unavailable"
                probe.close()
            if not available:
                self.skipTest("no OS filesystem sandbox is available")
            self.assertTrue(_run_fixture_tests(root, fixture["verifier"]["command"]))

    def test_live_runner_blocks_on_unavailable_sandbox_before_credentials(self):
        from unittest.mock import Mock
        from openkyrozen.tools.sandbox import SandboxedCommand

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            probe = SandboxedCommand("true", True, {}, "unavailable", unavailable_reason="probe failed")
            probe.close = Mock()
            with patch("benchmarks.v3_matched_live._source_snapshot", return_value="candidate"), \
                    patch("openkyrozen.tools.sandbox.sandbox_command", return_value=probe), \
                    patch("benchmarks.v3_matched_live._credential", side_effect=AssertionError("credential read")) as credential:
                with self.assertRaisesRegex(ValueError, "requires an available"):
                    run(source=root / "src", repo=root, output=root / "report.json",
                        budget_path=root / "budget.sqlite3")
            credential.assert_not_called()
            probe.close.assert_called_once()

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

    def test_permission_fixture_requires_a_denial_receipt_and_rejects_secret_disclosure(self):
        fixture = next(item for item in FIXTURES if item["fixture_id"] == "permissions-2")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_fixture(root, fixture)
            denial = {"tool_receipts": [{"action": "read_file", "target_path": "secret.txt",
                                         "authorized": False, "error_code": "permission_denied"}]}
            accepted = verify_fixture(fixture, root, "I cannot read secret.txt because permission is denied.",
                                      denial, prepared)
            unrelated = verify_fixture(fixture, root, "I cannot read secret.txt because permission is denied.",
                {"tool_receipts": [{"action": "read_file", "target_path": "other.txt",
                                     "authorized": False, "error_code": "permission_denied"}]}, prepared)
            leaked = verify_fixture(fixture, root, "permission denied, secret.txt: fixture-only secret sentinel",
                                     denial, prepared)
        self.assertTrue(accepted["verified_success"])
        self.assertFalse(unrelated["verified_success"])
        self.assertFalse(leaked["verified_success"])

    def test_recovery_requires_failed_tool_then_successful_retry(self):
        from benchmarks.v3_matched_live import _recovery_observed

        self.assertFalse(_recovery_observed({"tool_receipts": [
            {"action": "run_cmd", "success": True},
        ]}))
        self.assertFalse(_recovery_observed({"tool_receipts": [
            {"action": "edit_file", "success": False},
            {"action": "run_cmd", "success": True},
        ]}))
        self.assertTrue(_recovery_observed({"tool_receipts": [
            {"action": "edit_file", "success": False},
            {"action": "edit_file", "success": True},
        ]}))

    @unittest.skipUnless(__import__("os").name == "posix", "POSIX process-tree check")
    def test_timeout_cleanup_kills_detached_descendant_processes(self):
        import os
        import subprocess
        import sys
        import time
        from benchmarks.v3_matched_live import _kill_process_tree

        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "pid"
            code = ("import subprocess,sys,time; "
                    "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True); "
                    f"open({str(pid_file)!r},'w').write(str(p.pid)); time.sleep(30)")
            parent = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(pid_file.exists())
            descendant = int(pid_file.read_text())
            _kill_process_tree(parent.pid)
            parent.wait(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(descendant, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            else:
                self.fail("detached descendant survived timeout cleanup")

    def test_subagent_fixture_requires_succeeded_delegation_in_addition_to_tests(self):
        fixture = next(item for item in FIXTURES if item["fixture_id"] == "subagents-1")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_fixture(root, fixture)
            (root / "parser.py").write_text("def count_items(values):\n    return 0 if values is None else len(values)\n")
            direct_only = verify_fixture(fixture, root, "done", {}, prepared)
            delegated = verify_fixture(fixture, root, "done", {
                "events": [{"event": "subagent", "agent": {"status": "succeeded"}}]}, prepared)
        self.assertFalse(direct_only["plan_scenario_passed"])
        self.assertTrue(delegated["plan_scenario_passed"])

    def test_read_only_permission_success_does_not_require_attempted_write(self):
        fixture = next(item for item in FIXTURES if item["fixture_id"] == "permissions-3")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_fixture(root, fixture)
            result = verify_fixture(fixture, root, "Read only and unchanged.", {}, prepared)
        self.assertTrue(result["verified_success"])

    def test_frozen_v2_worker_runs_without_v3_plan_snapshot_api(self):
        import subprocess
        import sys

        fixture = FIXTURES[0]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, workspace, home = base / "baseline", base / "workspace", base / "home"
            workspace.mkdir()
            home.mkdir()
            baseline = subprocess.run(["git", "-C", str(Path.cwd()), "cat-file", "-e",
                                       f"{BASELINE}^{{commit}}"], capture_output=True)
            if baseline.returncode:
                self.skipTest("immutable V2 baseline is absent from this shallow checkout")
            _extract_baseline(source, Path.cwd(), BASELINE)
            prepare_fixture(workspace, fixture)
            prompt_file, plan_file, result_file = base / "prompt.txt", base / "plan.json", base / "result.json"
            prompt_file.write_text(fixture["prompt"], encoding="utf-8")
            plan_file.write_text(json.dumps(_initial_plan(fixture)), encoding="utf-8")
            env = _worker_env(base, fixture, type("Relay", (), {"client_key": "local"})())
            completed = subprocess.run(
                [sys.executable, str(Path.cwd() / "benchmarks/prompt_worker.py"),
                 "--source", str(source), "--workspace", str(workspace),
                 "--url", "http://127.0.0.1:1/v1", "--model", MODEL,
                 "--protocol", "responses", "--prompt-file", str(prompt_file),
                 "--plan-file", str(plan_file), "--result", str(result_file)],
                cwd=workspace, env=env, capture_output=True, text=True, timeout=30)
            result_data = json.loads(result_file.read_text(encoding="utf-8")) if result_file.exists() else None
        self.assertEqual(completed.returncode, 0, completed.stderr[-1000:])
        self.assertIsNotNone(result_data)
        self.assertTrue(result_data["provider_error"])

    def test_live_runner_rejects_same_ids_with_changed_fixture_content(self):
        fixture = dict(FIXTURES[0], prompt="altered prompt")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("benchmarks.v3_matched_live._source_snapshot", return_value="candidate"):
                with self.assertRaisesRegex(ValueError, "exact frozen fixture"):
                    run(source=root / "src", repo=root, output=root / "report.json",
                        budget_path=root / "budget.sqlite3", fixtures=(fixture,) + FIXTURES[1:])
            self.assertFalse((root / "budget.sqlite3").exists())

    def test_live_runner_rejects_report_without_its_original_budget_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.json"
            report.write_text('{"summary":{"comparison_thresholds_met":true}}', encoding="utf-8")
            with patch("benchmarks.v3_matched_live._source_snapshot", return_value="candidate"):
                with self.assertRaisesRegex(ValueError, "not pinned"):
                    run(source=root / "src", repo=root, output=report, budget_path=root / "budget.sqlite3")

    def test_budget_checkpoint_restores_a_tampered_or_interrupted_report_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report, ledger = root / "report.json", root / "budget.sqlite3"
            BudgetLedger(ledger)
            _write_checkpoint(report, ledger, {"protocol": "openkyrozen-v3-matched-live-v1",
                "candidate_sha": "candidate", "baseline_sha": "baseline", "model": "model",
                "repeats": 3, "rows": [], "calls": []})
            report.write_text('{"summary":{"comparison_thresholds_met":true}}', encoding="utf-8")
            checkpoint = _load_checkpoint(report, ledger)
            self.assertEqual(checkpoint["candidate_sha"], "candidate")
            self.assertEqual(json.loads(report.read_text(encoding="utf-8")), checkpoint)

    def test_resume_reconciles_orphan_calls_without_dropping_earlier_journal_entries(self):
        fixtures = [FIXTURES[0], FIXTURES[1]]
        for include_completed_row in (False, True):
            with self.subTest(include_completed_row=include_completed_row), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                report, ledger_path = root / "report.json", root / "budget.sqlite3"
                budget = BudgetLedger(ledger_path)
                first_key = (fixtures[0]["fixture_id"], 0, "v2")
                orphan_key = (fixtures[1]["fixture_id"], 0, "v3")
                keys = [first_key, orphan_key] if include_completed_row else [orphan_key]
                for index, (fixture_id, repeat, arm) in enumerate(keys):
                    attempt = f"call-{index}"
                    usage = {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 0}
                    metadata = {"attempt_id": attempt, "fixture_id": fixture_id, "repeat": repeat,
                                "arm": arm, "request_sha256": attempt, "model": MODEL,
                                "max_output_tokens": 4096, "reserved_rmb": "0.00044"}
                    self.assertTrue(budget.reserve(attempt, input_token_bound=100, output_token_bound=100,
                                                   metadata=metadata))
                    budget.settle(attempt, usage)
                rows = []
                if include_completed_row:
                    rows.append({"fixture_id": first_key[0], "repeat": first_key[1], "arm": first_key[2],
                        "attempts": 1, "requests": 1, "input_tokens": 100, "output_tokens": 10,
                        "cache_read_tokens": 0, "usage_complete": True, "worker_result_complete": True})
                _write_checkpoint(report, ledger_path, {"protocol": "openkyrozen-v3-matched-live-v1",
                    "candidate_sha": "candidate", "baseline_sha": BASELINE, "model": MODEL,
                    "repeats": 3, "rows": rows, "calls": []})
                relay = type("Relay", (), {"rows": []})()
                from openkyrozen.tools.sandbox import SandboxedCommand
                probe = SandboxedCommand("true", False, {}, "test")
                with patch("benchmarks.v3_matched_live._source_snapshot", return_value="candidate"), \
                        patch("benchmarks.v3_matched_live._credential", return_value=("https://example.invalid", "key")), \
                        patch("openkyrozen.tools.sandbox.sandbox_command", return_value=probe), \
                        patch("benchmarks.v3_matched_live.ResponsesRelay", return_value=relay), \
                        patch("benchmarks.v3_matched_live._extract_baseline", side_effect=RuntimeError("stop")):
                    with self.assertRaisesRegex(RuntimeError, "stop"):
                        run(source=root / "src", repo=root, output=report, budget_path=ledger_path)
                self.assertEqual({call["attempt_id"] for call in relay.rows},
                                 {f"call-{index}" for index in range(len(keys))})

    def test_cancellation_verifier_requires_a_started_and_stopped_process(self):
        fixture = next(item for item in FIXTURES if item["fixture_id"] == "cancellation-1")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_fixture(root, fixture)
            idle = verify_fixture(fixture, root, "done", {"processes_started": 0,
                "processes_cancelled": 0, "processes_alive": 0, "process_pids": []}, prepared)
            stopped = verify_fixture(fixture, root, "done", {"processes_started": 1,
                "processes_cancelled": 1, "processes_alive": 0, "process_pids": [2**31],
                "process_marker_seen": True}, prepared)
        self.assertEqual(idle["result"], "failed")
        self.assertEqual(stopped["result"], "passed")

    def test_required_plan_cannot_pass_with_missing_or_unfinished_tasks(self):
        fixture = next(item for item in FIXTURES if item.get("plan_tasks"))
        self.assertFalse(_workflow_state_correct(fixture, {"tasks": []}))
        self.assertFalse(_workflow_state_correct(fixture, {"tasks": ["cancelled"]}))
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
