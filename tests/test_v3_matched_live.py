import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from benchmarks.v3_fixtures import FIXTURES
from benchmarks.v3_matched_live import (BASELINE, MODEL, _load_checkpoint, _row_can_resume_as_complete, _run_fixture_tests, _worker_env,
                                         _extract_baseline, _initial_plan, _run_worker, _workflow_state_correct,
                                         _write_checkpoint, prepare_fixture, run, snapshot_workspace,
                                         verify_fixture)
from benchmarks.v3_matched import BudgetLedger, USD_CNY, _cost

def prepare_candidate_apparatus(source):
    from benchmarks import v3_matched_live as live
    target = source / "benchmarks"
    target.mkdir(parents=True, exist_ok=True)
    for name in live.apparatus_identity()["apparatus_files"]:
        (target / name).write_bytes((Path(live.__file__).parent / name).read_bytes())


def valid_worker_result(**overrides):
    """The shared candidate observer emits this schema for either product arm."""
    result = {"answer": "", "tool_calls": 0, "tool_actions": [], "tool_receipts": [], "events": [],
              "conversation_records": [], "request_context_records": [], "tool_successes": 0,
              "tasks": [], "task_records": [], "working_plan": None, "initial_plan_fingerprint": None,
              "accepted_plan_fingerprints": [], "material_plan_proposals": 0, "pending_material_revision": False,
              "executing_plan_fingerprint": None, "plan_revisions": 0, "cancelled_tasks": 0,
              "material_revisions": 0, "unnecessary_churn": 0, "duplicate_calls": 0, "protocol_recovery": 0,
              "processes_started": 0, "processes_cancelled": 0, "processes_alive": 0, "process_pids": [],
              "process_marker_seen": False, "first_delta_latency_ms": None, "provider_error": False,
              "accepted_scope_preserved": True, "scope_preserved": True}
    result.update(overrides)
    return result


def worker_result_bytes(**overrides):
    return json.dumps(valid_worker_result(**overrides)).encode()


TEST_PRIOR_HOLD_USD = "1.25"
TEST_PRIOR_HOLD_RMB = str(Decimal(TEST_PRIOR_HOLD_USD) * USD_CNY)


class V3MatchedLiveTests(unittest.TestCase):
    def test_live_runner_uses_candidate_worker_for_both_matched_arms(self):
        import benchmarks.v3_matched_live as live
        import warnings

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, repo = root / "candidate", root / "repository"
            source.mkdir()
            repo.mkdir()
            prepare_candidate_apparatus(source)
            commands = []

            class Relay:
                url = "http://127.0.0.1:1/v1"
                client_key = "local"
                provider_error = None
                budget_blocked = False

                def __init__(self, **kwargs):
                    self.rows = [{"attempt_id": "unknown-attempt", "arm": "v2",
                                  "fixture_id": "investigation-1", "repeat": 0,
                                  "reserved_rmb": "0.010", "status": "unknown"}]

                def start(self):
                    pass

                def close(self):
                    pass

            def run_worker(command, **kwargs):
                commands.append(command)
                Path(command[command.index("--result") + 1]).write_text(json.dumps(valid_worker_result(
                    answer="retained answer", events=[{"text": "retained event"}],
                    tool_receipts=[{"action": "read_file", "success": True}], tasks=["succeeded"],
                    task_records=[{"id": "task", "status": "succeeded"}])) , encoding="utf-8")
                return 0, ""

            def extract_baseline(destination, *_args):
                destination.mkdir(parents=True)

            with warnings.catch_warnings(), \
                    patch.object(live, "_source_snapshot", return_value="candidate"), \
                    patch("openkyrozen.tools.sandbox.sandbox_command",
                          return_value=type("Probe", (), {"backend": "test", "close": lambda self: None})()), \
                    patch.object(live, "_credential", return_value=("http://example.invalid", "private-provider-key")), \
                    patch.object(live, "ResponsesRelay", Relay), \
                    patch.object(live, "_extract_baseline", side_effect=extract_baseline), \
                    patch.object(live, "_run_worker", side_effect=run_worker), \
                    patch("benchmarks.v3_protected_worker.verify_worker_protection", return_value={}), \
                    patch("builtins.print"), \
                    patch.object(live, "verify_fixture", return_value={
                        "result": "failed", "safety_passed": True,
                        "safety_components": {"scope_safe": True, "original_tests_unchanged": True,
                                              "provider_ok": True, "no_running_processes": True},
                        "plan_scenario_passed": True, "verified_success": False,
                        "changed_paths": [],
                    }):
                warnings.simplefilter("ignore", ResourceWarning)
                run(source=source, repo=repo, output=root / "report.json",
                    budget_path=root / "budget.sqlite3", prior_hold_usd="10.25")
            report = json.loads((root / "report.json").read_text())
            self.assertTrue(report["summary"]["worker_evidence_complete"])
            self.assertNotIn("private-provider-key", (root / "report.json").read_text())
            for row in report["rows"]:
                self.assertTrue(live._validate_worker_evidence(root / "report.json", row, "candidate", live.apparatus_identity()))
                document = json.loads((root / row["worker_evidence_file"]).read_text())
                self.assertEqual(document["worker"]["answer"], "retained answer")
                self.assertEqual(document["worker"]["events"], [{"text": "retained event"}])
                self.assertEqual(document["worker"]["tasks"], ["succeeded"])
                self.assertEqual(document["worker"]["task_records"][0]["id"], "task")
            with sqlite3.connect(root / "budget.sqlite3") as db:
                hold = json.loads(db.execute(
                    "SELECT metadata_json FROM attempts WHERE id='prior-campaign-reservations'"
                ).fetchone()[0])["prior_cny_reservations"]
            from benchmarks.v3_matched import USD_CNY
            from decimal import Decimal
            self.assertEqual(Decimal(hold), Decimal("10.25") * USD_CNY)
            self.assertEqual(json.loads((root / "report.json").read_text())[
                "budget"]["prior_campaign_hold_usd"], "10.25")
            self.assertEqual(json.loads((root / "report.json").read_text())[
                "held_reservations"], [{"attempt_id": "unknown-attempt", "arm": "v2",
                    "fixture_id": "investigation-1", "repeat": 0,
                    "reserved_rmb": "0.010", "status": "unknown"}])

        candidate_worker = str(source.resolve() / "benchmarks" / "prompt_worker.py")
        self.assertEqual(len(commands), len(FIXTURES) * 3 * 2)
        self.assertTrue(all(command[1] == candidate_worker for command in commands), repr(commands[:2]))

    def test_invalid_or_credential_exposed_worker_is_checkpointed_incomplete_and_stops(self):
        cases = [(b'{"answer":"private-provider-key",', "malformed", None),
                 (worker_result_bytes(protocol_recovery="bad"), "incomplete_schema",
                  valid_worker_result(protocol_recovery="bad")),
                 (worker_result_bytes(answer="private-provider-key", provider_error=True), "credential_exposure",
                  valid_worker_result(answer="[REDACTED]", provider_error=True))]
        for raw, classification, expected_worker in cases:
            with self.subTest(classification=classification):
                self._assert_invalid_campaign_is_checkpointed(raw, classification, expected_worker)

    def _assert_invalid_campaign_is_checkpointed(self, raw, classification, expected_worker):
        from benchmarks import v3_matched_live as live
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "candidate"
            prepare_candidate_apparatus(source)
            class Relay:
                url, client_key = "http://127.0.0.1:1/v1", "local-token"
                provider_error, budget_blocked = None, False
                def __init__(self, **kwargs): self.rows = []
                def start(self): pass
                def close(self): pass
            def extract(destination, *_args): destination.mkdir(parents=True)
            def worker(command, **kwargs):
                Path(command[command.index("--result") + 1]).write_bytes(raw)
                return 0, ""
            with patch.object(live, "_source_snapshot", return_value="candidate"), \
                    patch("openkyrozen.tools.sandbox.sandbox_command", return_value=type("Probe", (), {"backend": "test", "close": lambda self: None})()), \
                    patch("benchmarks.v3_protected_worker.verify_worker_protection", return_value={}), \
                    patch.object(live, "_credential", return_value=("https://invalid.example", "private-provider-key")) as credential, \
                    patch.object(live, "ResponsesRelay", Relay), \
                    patch.object(live, "_extract_baseline", side_effect=extract), \
                    patch.object(live, "_run_worker", side_effect=worker) as dispatch, \
                    patch.object(live, "verify_fixture", return_value={"result": "passed", "verified_success": True, "safety_passed": True,
                        "plan_scenario_passed": True, "changed_paths": [], "safety_components": {
                        "scope_safe": True, "original_tests_unchanged": True, "provider_ok": True, "no_running_processes": True}}) as oracle, \
                    patch.object(live, "summarize", return_value={"complete": True, "accounting_complete": True, "comparison_thresholds_met": True}), \
                    patch("builtins.print"):
                report = live.run(source=source, repo=root, output=root / "report.json",
                                  budget_path=root / "budget.sqlite3", prior_hold_usd=TEST_PRIOR_HOLD_USD)
            self.assertEqual(dispatch.call_count, 1)
            credential.assert_called_once()
            oracle.assert_not_called()
            self.assertEqual(report["status"], "blocked_safety_failure")
            self.assertFalse(report["summary"]["worker_evidence_complete"])
            self.assertFalse(report["summary"]["accounting_complete"])
            self.assertFalse(report["summary"]["comparison_thresholds_met"])
            row = report["rows"][0]
            self.assertFalse(row["worker_result_complete"])
            self.assertFalse(row["verified_success"])
            document = json.loads((root / row["worker_evidence_file"]).read_text())
            self.assertEqual(document["raw_worker_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(document["classification"], classification)
            self.assertEqual(document["worker"], expected_worker)
            self.assertIsNone(row["protocol_recovery"])
            self.assertEqual(live._load_checkpoint(root / "report.json", root / "budget.sqlite3")["rows"], [row])
            self.assertNotIn("private-provider-key", json.dumps(document))

    def test_held_reservations_report_in_flight_and_unknown_attempts(self):
        from benchmarks.v3_matched_live import _held_reservations
        calls = [{"attempt_id": "reserved", "arm": "v2", "fixture_id": "a", "repeat": 0,
                  "reserved_rmb": "1", "status": "reserved"},
                 {"attempt_id": "unknown", "arm": "v3", "fixture_id": "b", "repeat": 1,
                  "reserved_rmb": "2", "status": "unknown"},
                 {"attempt_id": "settled", "arm": "v2", "fixture_id": "c", "repeat": 2,
                  "reserved_rmb": "3", "status": "settled"}]
        self.assertEqual(_held_reservations(calls), [
            {key: call[key] for key in ("attempt_id", "arm", "fixture_id", "repeat", "reserved_rmb", "status")}
            for call in calls[:2]])

    def test_invalid_prior_campaign_hold_is_rejected_before_ledger_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("benchmarks.v3_matched_live._source_snapshot", return_value="candidate"):
                with self.assertRaisesRegex(ValueError, "valid USD amount"):
                    run(source=root / "src", repo=root, output=root / "report.json",
                        budget_path=root / "budget.sqlite3", prior_hold_usd="not-money")
            self.assertFalse((root / "budget.sqlite3").exists())

    def test_prior_campaign_hold_is_required_before_ledger_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("benchmarks.v3_matched_live._source_snapshot", return_value="candidate"):
                with self.assertRaisesRegex(TypeError, "prior_hold_usd"):
                    run(source=root / "src", repo=root, output=root / "report.json",
                        budget_path=root / "budget.sqlite3")
            self.assertFalse((root / "budget.sqlite3").exists())

    def test_keyword_correct_outcome_without_raw_worker_evidence_is_not_reusable(self):
        case = next(item for item in FIXTURES if item["fixture_id"] == "investigation-2")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_fixture(root, case)
            worker = {"answer": "timeout = 15 and retries = 2", "provider_error": False, "processes_alive": 0}
            verified = verify_fixture(case, root, worker["answer"], worker, prepared)
            self.assertEqual(verified["result"], "passed")
            row = {"fixture_id": case["fixture_id"], "repeat": 0, "arm": "v3", "attempts": 1,
                   "worker_result_complete": True, "usage_complete": True, "worker_exit_code": 0,
                   "verified_success": True, "verifier_result": "passed", "safety_passed": True,
                   "safety_components": {**verified["safety_components"], "worker_complete": True}}
            self.assertFalse(_row_can_resume_as_complete(row))

    def test_resume_retries_worker_failures_even_with_complete_usage(self):
        failed = {"worker_result_complete": True, "usage_complete": True,
                  "attempts": 3, "worker_exit_code": 1}
        succeeded = {**failed, "worker_exit_code": 0, "safety_passed": True,
                     "safety_components": {name: True for name in (
                         "scope_safe", "original_tests_unchanged", "provider_ok",
                         "no_running_processes", "worker_complete")}}

        self.assertFalse(_row_can_resume_as_complete(failed))
        from benchmarks import v3_matched_live as live
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            succeeded.update(fixture_id="investigation-1", repeat=0, arm="v3")
            result = root / "result.json"
            result.write_text(json.dumps(valid_worker_result(answer="checked")))
            worker, metadata = live._read_worker_result(result, ())
            identity = live.apparatus_identity()
            live._write_worker_evidence(root / "report.json", succeeded, "candidate", identity, worker, metadata)
            self.assertTrue(_row_can_resume_as_complete(succeeded, root / "report.json", "candidate", identity))

    def test_worker_stderr_exception_prefix_is_not_persisted_as_an_exception_name(self):
        import os
        import sys
        with tempfile.TemporaryDirectory() as directory:
            code, error = _run_worker([sys.executable, "-c",
                "import sys; sys.stderr.write('private-provider-keyError: private detail\\n'); sys.exit(1)"],
                cwd=Path(directory), env=os.environ.copy(), timeout=5)
        self.assertEqual(code, 1)
        self.assertEqual(error, "worker_exit_1")
        self.assertNotIn("private-provider-key", error)

    def test_worker_failure_records_exit_and_exception_type_without_stderr(self):
        import os
        import sys

        with tempfile.TemporaryDirectory() as directory:
            code, error = _run_worker(
                [sys.executable, "-c", "raise RuntimeError('private provider detail')"],
                cwd=Path(directory), env=os.environ.copy(), timeout=5)

        self.assertEqual(code, 1)
        self.assertEqual(error, "worker_exit_1:RuntimeError")
        self.assertNotIn("private provider detail", error)

    def test_fixture_setup_reconstructs_git_history_for_independent_verification(self):
        fixture = next(item for item in FIXTURES if item["fixture_id"] == "git_history-2")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_fixture(root, fixture)
            answer = f"increase retry count {prepared['expected_commit']} changed RETRIES = 1 to RETRIES = 3"
            worker = {"tool_receipts": [{"action": "git_log", "success": True, "authorized": True},
                {"action": "git_show", "success": True, "authorized": True,
                 "git_reference": prepared["expected_commit_full"]}]}
            result = verify_fixture(fixture, root, answer, worker, prepared)
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
                        budget_path=root / "budget.sqlite3", prior_hold_usd=TEST_PRIOR_HOLD_USD)
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
            with patch("benchmarks.v3_matched_live._run_fixture_tests", return_value=True):
                result = verify_fixture(fixture, root, "please approve the public API change", worker, prepared)
        self.assertTrue(result["plan_scenario_passed"])
        self.assertTrue(result["verified_success"])

    def test_strategic_plan_fixtures_do_not_claim_unavailable_process_authority(self):
        for scenario in ("scope_preserving", "material_revision"):
            with self.subTest(scenario=scenario):
                fixture = next(item for item in FIXTURES if item["plan_scenario"] == scenario)
                plan = _initial_plan(fixture)
                self.assertNotIn("shell", plan["bounds"]["capabilities"])
                self.assertNotIn("process", plan["bounds"]["effects"])

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

        try:
            process_listing = subprocess.run(["ps", "-axo", "pid=,ppid="],
                                             capture_output=True, timeout=5, check=False)
        except OSError as exc:
            self.skipTest(f"process tree inspection unavailable: {exc}")
        if process_listing.returncode:
            self.skipTest("process tree inspection unavailable")

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
        from benchmarks.v3_matched_live import _worker_schema_errors
        self.assertEqual(_worker_schema_errors(result_data), [])

    def test_live_runner_rejects_same_ids_with_changed_fixture_content(self):
        fixture = dict(FIXTURES[0], prompt="altered prompt")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("benchmarks.v3_matched_live._source_snapshot", return_value="candidate"):
                with self.assertRaisesRegex(ValueError, "exact frozen fixture"):
                    run(source=root / "src", repo=root, output=root / "report.json",
                        budget_path=root / "budget.sqlite3", prior_hold_usd="10.20",
                        fixtures=(fixture,) + FIXTURES[1:])
            self.assertFalse((root / "budget.sqlite3").exists())

    def test_live_runner_rejects_report_without_its_original_budget_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.json"
            report.write_text('{"summary":{"comparison_thresholds_met":true}}', encoding="utf-8")
            with patch("benchmarks.v3_matched_live._source_snapshot", return_value="candidate"):
                with self.assertRaisesRegex(ValueError, "not pinned"):
                    run(source=root / "src", repo=root, output=report, budget_path=root / "budget.sqlite3",
                        prior_hold_usd="10.20")

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
                budget = BudgetLedger(ledger_path, prior_hold_rmb=TEST_PRIOR_HOLD_RMB)
                first_key = (fixtures[0]["fixture_id"], 0, "v2")
                orphan_key = (fixtures[1]["fixture_id"], 0, "v3")
                keys = [first_key, orphan_key] if include_completed_row else [orphan_key]
                for index, (fixture_id, repeat, arm) in enumerate(keys):
                    attempt = f"call-{index}"
                    usage = {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 0}
                    metadata = {"attempt_id": attempt, "fixture_id": fixture_id, "repeat": repeat,
                                "arm": arm, "request_sha256": attempt, "model": MODEL,
                                "max_output_tokens": 4096, "reserved_rmb": str(_cost(100, 100))}
                    self.assertTrue(budget.reserve(attempt, input_token_bound=100, output_token_bound=100,
                                                   metadata=metadata))
                    budget.settle(attempt, usage)
                rows = []
                if include_completed_row:
                    rows.append({"fixture_id": first_key[0], "repeat": first_key[1], "arm": first_key[2],
                        "attempts": 1, "requests": 1, "input_tokens": 100, "output_tokens": 10,
                        "cache_read_tokens": 0, "usage_complete": True, "worker_result_complete": True,
                        "safety_passed": True, "safety_components": {name: True for name in (
                            "scope_safe", "original_tests_unchanged", "provider_ok",
                            "no_running_processes", "worker_complete")}})
                from benchmarks.v3_matched_live import apparatus_identity
                identity = apparatus_identity()
                for row in rows:
                    row.update({key: identity[key] for key in ("cohort_sha256", "apparatus_sha256")})
                    from benchmarks import v3_matched_live as live
                    result = root / "worker-result.json"
                    result.write_text(json.dumps(valid_worker_result(answer="retained")))
                    worker, metadata = live._read_worker_result(result, ())
                    live._write_worker_evidence(report, row, "candidate", identity, worker, metadata)
                _write_checkpoint(report, ledger_path, {**identity, "protocol": "openkyrozen-v3-matched-live-v1",
                    "candidate_sha": "candidate", "baseline_sha": BASELINE, "model": MODEL,
                    "repeats": 3, "budget": {"prior_campaign_hold_usd": TEST_PRIOR_HOLD_USD},
                    "rows": rows, "calls": []})
                prepare_candidate_apparatus(root / "src")
                relay = type("Relay", (), {"rows": []})()
                from openkyrozen.tools.sandbox import SandboxedCommand
                probe = SandboxedCommand("true", False, {}, "test")
                with patch("benchmarks.v3_matched_live._source_snapshot", return_value="candidate"), \
                        patch("benchmarks.v3_matched_live._credential", return_value=("https://example.invalid", "key")), \
                        patch("openkyrozen.tools.sandbox.sandbox_command", return_value=probe), \
                        patch("benchmarks.v3_matched_live.ResponsesRelay", return_value=relay), \
                        patch("benchmarks.v3_protected_worker.verify_worker_protection", return_value={}), \
                        patch("benchmarks.v3_matched_live._extract_baseline", side_effect=RuntimeError("stop")):
                    with self.assertRaisesRegex(RuntimeError, "stop"):
                        run(source=root / "src", repo=root, output=report, budget_path=ledger_path,
                            prior_hold_usd=TEST_PRIOR_HOLD_USD)
                self.assertEqual({call["attempt_id"] for call in relay.rows},
                                 {f"call-{index}" for index in range(len(keys))})

    def test_reconciled_unknown_orphan_row_survives_checkpoint_interruption_and_next_resume(self):
        from benchmarks import v3_matched_live as live
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "candidate"
            prepare_candidate_apparatus(source)
            report, ledger_path = root / "report.json", root / "budget.sqlite3"
            budget = BudgetLedger(ledger_path, prior_hold_rmb=TEST_PRIOR_HOLD_RMB)
            identity = live.apparatus_identity()
            orphan_key = ("permissions-3", 2, "v3")
            metadata = {"attempt_id": "held-orphan", "fixture_id": orphan_key[0], "repeat": orphan_key[1],
                        "arm": orphan_key[2], "request_sha256": "held-request", "model": MODEL,
                        "max_output_tokens": 4096, "reserved_rmb": str(_cost(100, 100))}
            self.assertTrue(budget.reserve("held-orphan", input_token_bound=100, output_token_bound=100, metadata=metadata))
            budget.settle("held-orphan", None)
            remaining = budget.remaining_rmb
            def ledger_state():
                with sqlite3.connect(ledger_path) as db:
                    return db.execute("SELECT id,reserved_picormb,actual_picormb,status,usage_json FROM attempts ORDER BY id").fetchall()
            held_state = ledger_state()
            live._write_checkpoint(report, ledger_path, {**identity, "protocol": "openkyrozen-v3-matched-live-v1",
                "candidate_sha": "candidate", "baseline_sha": BASELINE, "model": MODEL, "repeats": 3,
                "budget": {"prior_campaign_hold_usd": TEST_PRIOR_HOLD_USD}, "rows": [], "calls": []})
            class Relay:
                url, client_key = "http://127.0.0.1:1/v1", "offline"
                provider_error, budget_blocked = None, False
                def __init__(self, **kwargs): self.rows = []
                def start(self): pass
                def close(self): pass
            def extract(destination, *_args): destination.mkdir(parents=True)
            def worker(command, **kwargs):
                Path(command[command.index("--result") + 1]).write_text(json.dumps(valid_worker_result()))
                return 0, ""
            def checkpoint_then_interrupt(*args):
                live._write_checkpoint_original(*args)
                raise RuntimeError("interrupted after unrelated cell checkpoint")
            common = [patch.object(live, "_source_snapshot", return_value="candidate"),
                patch("openkyrozen.tools.sandbox.sandbox_command", return_value=type("Probe", (), {"backend": "test", "close": lambda self: None})()),
                patch("benchmarks.v3_protected_worker.verify_worker_protection", return_value={}),
                patch.object(live, "_credential", return_value=("https://invalid.example", "offline")),
                patch.object(live, "ResponsesRelay", Relay)]
            from contextlib import ExitStack
            with ExitStack() as stack:
                for context in common: stack.enter_context(context)
                stack.enter_context(patch.object(live, "_extract_baseline", side_effect=extract))
                stack.enter_context(patch.object(live, "_run_worker", side_effect=worker))
                stack.enter_context(patch.object(live, "verify_fixture", return_value={"result": "passed", "verified_success": True, "safety_passed": True,
                    "plan_scenario_passed": True, "changed_paths": [], "safety_components": {
                    "scope_safe": True, "original_tests_unchanged": True, "provider_ok": True, "no_running_processes": True}}))
                stack.enter_context(patch.object(live, "_write_checkpoint_original", live._write_checkpoint, create=True))
                stack.enter_context(patch.object(live, "_write_checkpoint", side_effect=checkpoint_then_interrupt))
                with self.assertRaisesRegex(RuntimeError, "interrupted after unrelated"):
                    live.run(source=source, repo=root, output=report, budget_path=ledger_path, prior_hold_usd=TEST_PRIOR_HOLD_USD)
            restored = live._load_checkpoint(report, ledger_path)
            orphan = next(row for row in restored["rows"] if (row["fixture_id"], row["repeat"], row["arm"]) == orphan_key)
            self.assertEqual(orphan.get("cohort_sha256"), identity["cohort_sha256"])
            self.assertEqual(orphan.get("apparatus_sha256"), identity["apparatus_sha256"])
            self.assertFalse(orphan["worker_result_complete"])
            self.assertFalse(orphan["usage_complete"])
            self.assertFalse(orphan["verified_success"])
            self.assertEqual((orphan["attempts"], orphan["requests"], orphan["input_tokens"], orphan["output_tokens"]), (1, 1, 0, 0))
            with ExitStack() as stack:
                for context in [patch.object(live, "_source_snapshot", return_value="candidate"),
                    patch("openkyrozen.tools.sandbox.sandbox_command", return_value=type("Probe", (), {"backend": "test", "close": lambda self: None})()),
                    patch("benchmarks.v3_protected_worker.verify_worker_protection", return_value={}),
                    patch.object(live, "_credential", return_value=("https://invalid.example", "offline")),
                    patch.object(live, "ResponsesRelay", Relay),
                    patch.object(live, "_extract_baseline", side_effect=RuntimeError("second resume accepted"))]:
                    stack.enter_context(context)
                with self.assertRaisesRegex(RuntimeError, "second resume accepted"):
                    live.run(source=source, repo=root, output=report, budget_path=ledger_path, prior_hold_usd=TEST_PRIOR_HOLD_USD)
            self.assertEqual(ledger_state(), held_state)
            self.assertEqual(budget.remaining_rmb, remaining)
            self.assertEqual(budget.spent_rmb, 0)

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



class WorkerEvidenceTests(unittest.TestCase):
    def _write(self, root, payload, secrets=()):
        from benchmarks import v3_matched_live as live
        result = root / "result.json"
        result.write_bytes(payload)
        worker, metadata = live._read_worker_result(result, secrets)
        row = {"fixture_id": "investigation-2", "repeat": 0, "arm": "v3",
               "worker_exit_code": 0, "worker_result_complete": metadata["classification"] == "valid",
               "verifier_result": "passed", "verified_success": True, "safety_passed": True,
               "usage_complete": True, "attempts": 1,
               "safety_components": {name: True for name in (
                   "scope_safe", "original_tests_unchanged", "provider_ok", "no_running_processes", "worker_complete")}}
        identity = live.apparatus_identity()
        live._write_worker_evidence(root / "report.json", row, "candidate", identity, worker, metadata)
        return row, identity

    def test_missing_or_wrong_observer_fields_are_incomplete_not_defaulted(self):
        from benchmarks import v3_matched_live as live
        for field, bad in (("answer", None), ("events", {}), ("tool_receipts", None),
                           ("task_records", {}), ("tasks", None), ("request_context_records", {}),
                           ("conversation_records", None), ("protocol_recovery", -1),
                           ("protocol_recovery", True), ("protocol_recovery", "0"),
                           ("provider_error", 0), ("processes_alive", False),
                           ("first_delta_latency_ms", 10 ** 400), ("first_delta_latency_ms", True)):
            for missing in (True, False):
                with self.subTest(field=field, missing=missing), tempfile.TemporaryDirectory() as directory:
                    payload = valid_worker_result(answer="keyword-correct but unavailable evidence")
                    if missing: payload.pop(field)
                    else: payload[field] = bad
                    root = Path(directory)
                    row, identity = self._write(root, json.dumps(payload).encode())
                    self.assertFalse(row["worker_result_complete"])
                    self.assertFalse(row["worker_evidence_verified"])
                    self.assertEqual(row["worker_evidence_status"], "incomplete_schema")
                    document = json.loads((root / row["worker_evidence_file"]).read_text())
                    self.assertEqual(document["worker"], payload)
                    self.assertFalse(live._row_can_resume_as_complete(row, root / "report.json", "candidate", identity))

    def test_full_result_retained_with_raw_hash_and_recursive_literal_key_redaction(self):
        import hashlib
        from benchmarks import v3_matched_live as live
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = worker_result_bytes(answer="confirmed actual-key", events=[{"text": "actual-key"}],
                tool_receipts=[{"arguments": {"body": "actual-key"}}],
                tasks=["succeeded"], task_records=[{"id": "t", "status": "succeeded"}],
                worker_evidence_verified=True)
            row, identity = self._write(root, raw, ("actual-key",))
            data = (root / row["worker_evidence_file"]).read_bytes()
            self.assertNotIn(b"actual-key", data)
            document = json.loads(data)
            self.assertEqual(document["raw_worker_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(document["worker"]["answer"], "confirmed [REDACTED]")
            self.assertEqual(document["worker"]["tasks"], ["succeeded"])
            self.assertEqual(document["worker"]["task_records"][0]["id"], "t")
            self.assertEqual(document["worker"]["tool_receipts"][0]["arguments"]["body"], "[REDACTED]")
            self.assertEqual(document["redaction_count"], 3)
            self.assertEqual(document["classification"], "credential_exposure")
            self.assertFalse(row["worker_result_complete"])
            self.assertFalse(live._row_can_resume_as_complete(row, root / "report.json", "candidate", identity))
            self.assertFalse(live._row_can_resume_as_complete({"worker_evidence_verified": True}))

    def test_missing_tampered_foreign_candidate_and_cross_row_evidence_fail_closed(self):
        from benchmarks import v3_matched_live as live
        for change in ("missing", "tamper", "row", "candidate", "apparatus", "symlink", "directory_symlink"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                row, identity = self._write(root, worker_result_bytes(answer="correct"))
                path = root / row["worker_evidence_file"]
                candidate = "candidate"
                if change == "missing": path.unlink()
                elif change == "tamper": path.write_bytes(b'{"answer":"forged"}')
                elif change == "row": row["repeat"] = 1
                elif change == "candidate": candidate = "different"
                elif change == "apparatus": identity = {**identity, "apparatus_sha256": "different"}
                elif change == "symlink":
                    target = root / "sentinel"
                    target.write_text("private sentinel")
                    path.unlink()
                    path.symlink_to(target)
                else:
                    directory_path = path.parent
                    moved = root / "moved"
                    directory_path.rename(moved)
                    directory_path.symlink_to(moved, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, "worker evidence"):
                    live._validate_rows_evidence(root / "report.json", [row], candidate, identity)
                self.assertFalse(row["worker_evidence_verified"])

    def test_malformed_and_oversized_results_retain_only_hash_diagnostic(self):
        import hashlib
        from benchmarks import v3_matched_live as live
        for payload in (b'{"answer":"actual-key",', b'\xff', b'[' * 2000, b'{"x":NaN}', b'{"x":1,"x":2}', b'{"nested":{"actual-key":"value"}}'):
            with self.subTest(payload=payload[:30]), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                row, identity = self._write(root, payload, ("actual-key",))
                document = json.loads((root / row["worker_evidence_file"]).read_bytes())
                self.assertEqual(document["raw_worker_sha256"], hashlib.sha256(payload).hexdigest())
                self.assertEqual(document["classification"], "malformed")
                self.assertIsNone(document["worker"])
                self.assertNotIn("actual-key", json.dumps(document))
                self.assertFalse(live._validate_rows_evidence(root / "report.json", [row], "candidate", identity))
        with tempfile.TemporaryDirectory() as directory, patch.object(live, "_MAX_WORKER_EVIDENCE_BYTES", 4096):
            root = Path(directory)
            payload = b'{"answer":"' + b'x' * 5000 + b'"}'
            row, _ = self._write(root, payload)
            document = json.loads((root / row["worker_evidence_file"]).read_bytes())
            self.assertIsNone(document["raw_worker_sha256"])
            self.assertEqual(document["raw_hash_scope"], "unavailable")
            self.assertEqual(document["observed_worker_bytes"], 0)
            self.assertEqual(document["raw_worker_bytes"], len(payload))
            self.assertEqual(document["classification"], "oversized")
            self.assertIsNone(document["worker"])

    def test_resume_rejects_missing_tampered_and_replayed_artifacts_before_credentials(self):
        from benchmarks import v3_matched_live as live
        for change in ("missing", "tamper", "cross_row", "foreign_candidate", "unsupported", "valid"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output, ledger = root / "report.json", root / "budget.sqlite3"
                row, identity = self._write(root, worker_result_bytes(answer="timeout = 15 and retries = 2"))
                path = root / row["worker_evidence_file"]
                if change == "missing": path.unlink()
                elif change == "tamper": path.write_text('{"worker":{"answer":"forged"}}')
                elif change == "cross_row": row["arm"] = "v2"
                elif change == "foreign_candidate":
                    worker, metadata = live._read_worker_result(root / "result.json", ())
                    live._write_worker_evidence(output, row, "different-candidate", identity, worker, metadata)
                row.update({key: identity[key] for key in ("cohort_sha256", "apparatus_sha256")})
                budget = BudgetLedger(ledger, prior_hold_rmb=TEST_PRIOR_HOLD_RMB)
                usage = {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 0}
                self.assertTrue(budget.reserve("attempt", input_token_bound=100, output_token_bound=100,
                    metadata={"attempt_id": "attempt", "fixture_id": row["fixture_id"],
                        "repeat": row["repeat"], "arm": row["arm"], "request_sha256": "request",
                        "model": MODEL, "max_output_tokens": 4096, "reserved_rmb": str(_cost(100, 100))}))
                budget.settle("attempt", usage)
                row.update(requests=1, input_tokens=100, output_tokens=10, cache_read_tokens=0, cache_write_tokens=0)
                live._write_checkpoint(output, ledger, {**identity, "protocol": "openkyrozen-v3-matched-live-v1",
                    "candidate_sha": "candidate", "baseline_sha": BASELINE, "model": MODEL, "repeats": 3,
                    "budget": {"prior_campaign_hold_usd": TEST_PRIOR_HOLD_USD}, "rows": [row], "calls": []})
                prepare_candidate_apparatus(root / "source")
                with patch.object(live, "_source_snapshot", return_value="candidate"), \
                        patch("openkyrozen.tools.sandbox.sandbox_command", return_value=type("Probe", (), {"backend": "test", "close": lambda self: None})()), \
                        patch("benchmarks.v3_protected_worker.verify_worker_protection", return_value={}), \
                        patch.object(live, "_credential", side_effect=RuntimeError("credential boundary")) as credential, \
                        patch.object(live.os, "supports_dir_fd", set() if change == "unsupported" else live.os.supports_dir_fd):
                    if change == "valid":
                        with self.assertRaisesRegex(RuntimeError, "credential boundary"):
                            live.run(source=root / "source", repo=root, output=output, budget_path=ledger,
                                     prior_hold_usd=TEST_PRIOR_HOLD_USD)
                        credential.assert_called_once()
                    else:
                        with self.assertRaisesRegex(ValueError, "worker evidence"):
                            live.run(source=root / "source", repo=root, output=output, budget_path=ledger,
                                     prior_hold_usd=TEST_PRIOR_HOLD_USD)
                        credential.assert_not_called()

    def test_fdopen_constructor_failure_closes_read_write_and_directory_descriptors(self):
        import errno
        from benchmarks import v3_matched_live as live
        original_open = live.os.open
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row, identity = self._write(root, worker_result_bytes(answer="first"))
            opened = []
            def capture_open(*args, **kwargs):
                descriptor = original_open(*args, **kwargs)
                opened.append(descriptor)
                return descriptor
            def assert_closed():
                self.assertTrue(opened)
                for descriptor in opened:
                    with self.assertRaises(OSError) as caught:
                        live.os.fstat(descriptor)
                    self.assertEqual(caught.exception.errno, errno.EBADF)
            with patch.object(live.os, "open", side_effect=capture_open), \
                    patch.object(live.os, "fdopen", side_effect=OSError("constructor failed")):
                _, metadata = live._read_worker_result(root / "result.json", ())
            self.assertEqual(metadata["classification"], "unavailable")
            assert_closed()
            opened.clear()
            (root / "result.json").write_bytes(worker_result_bytes(answer="second"))
            worker, metadata = live._read_worker_result(root / "result.json", ())
            # Mocking os.open changes its function identity, so preserve the
            # platform capability probe while injecting a constructor failure.
            with patch.object(live, "_require_secure_evidence_io"), \
                    patch.object(live.os, "open", side_effect=capture_open), \
                    patch.object(live.os, "fdopen", side_effect=OSError("constructor failed")):
                with self.assertRaisesRegex(OSError, "constructor failed"):
                    live._write_worker_evidence(root / "report.json", row, "candidate", identity, worker, metadata)
            assert_closed()
            self.assertEqual(list((root / row["worker_evidence_file"]).parent.glob(".worker-*")), [])

    def test_evidence_directory_swap_before_write_cannot_write_outside_directory(self):
        from benchmarks import v3_matched_live as live
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row, identity = self._write(root, worker_result_bytes(answer="first"))
            directory_path = (root / row["worker_evidence_file"]).parent
            outside = root / "outside"
            outside.mkdir()
            (root / "result.json").write_text(json.dumps(valid_worker_result(answer="second")))
            worker, metadata = live._read_worker_result(root / "result.json", ())
            original_replace = live.os.replace
            def swap_then_replace(*args, **kwargs):
                directory_path.rename(root / "old-evidence")
                directory_path.symlink_to(outside, target_is_directory=True)
                return original_replace(*args, **kwargs)
            with patch.object(live.os, "replace", side_effect=swap_then_replace):
                with self.assertRaisesRegex(ValueError, "worker evidence directory"):
                    live._write_worker_evidence(root / "report.json", row, "candidate", identity, worker, metadata)
            self.assertEqual(list(outside.iterdir()), [])

    def test_evidence_directory_swap_cannot_validate_outside_file(self):
        import shutil
        from benchmarks import v3_matched_live as live
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row, identity = self._write(root, worker_result_bytes(answer="correct"))
            path = root / row["worker_evidence_file"]
            outside = root / "outside"
            outside.mkdir()
            shutil.copyfile(path, outside / path.name)
            original_reader = live._read_regular_bytes
            def swap_then_read(target, *args, **kwargs):
                path.parent.rename(root / "old-evidence")
                path.parent.symlink_to(outside, target_is_directory=True)
                return original_reader(target, *args, **kwargs)
            with patch.object(live, "_read_regular_bytes", side_effect=swap_then_read):
                with self.assertRaisesRegex(ValueError, "worker evidence"):
                    live._validate_worker_evidence(root / "report.json", row, "candidate", identity)

    def test_oversized_file_reads_zero_bytes_and_growing_reader_stops_at_limit_plus_one(self):
        import hashlib
        from benchmarks import v3_matched_live as live
        original_fdopen = live.os.fdopen
        with tempfile.TemporaryDirectory() as directory, patch.object(live, "_MAX_WORKER_EVIDENCE_BYTES", 8):
            root = Path(directory)
            result = root / "result"
            requested = []
            growing = False
            class Stream:
                def __init__(self, raw): self.raw = raw
                def __enter__(self): return self
                def __exit__(self, *args): self.raw.close()
                def fileno(self): return self.raw.fileno()
                def read(self, count):
                    requested.append(count)
                    if not growing:
                        raise AssertionError("initially oversized file must not be read")
                    if len(requested) > 1:
                        raise AssertionError("growing input must stop at the size bound")
                    return b'x' * count
            def instrument(*args, **kwargs): return Stream(original_fdopen(*args, **kwargs))
            result.write_bytes(b'x' * 65)
            with patch.object(live.os, "fdopen", side_effect=instrument):
                worker, metadata = live._read_worker_result(result, ())
            self.assertEqual(requested, [])
            self.assertEqual(metadata["raw_worker_bytes"], 65)
            self.assertEqual(metadata["observed_worker_bytes"], 0)
            self.assertIsNone(metadata["raw_worker_sha256"])
            result.write_bytes(b'')
            growing = True
            with patch.object(live.os, "fdopen", side_effect=instrument):
                worker, metadata = live._read_worker_result(result, ())
            self.assertEqual(requested, [9])
            self.assertEqual(metadata["classification"], "growing_file")
            self.assertEqual(metadata["observed_worker_bytes"], 9)
            self.assertIsNone(metadata["raw_worker_sha256"])
            self.assertEqual(metadata["observed_prefix_sha256"], hashlib.sha256(b'x' * 9).hexdigest())

    def test_worker_result_symlink_fifo_and_credential_key_are_never_read_as_valid(self):
        import os
        from benchmarks import v3_matched_live as live
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / "sentinel"
            sentinel.write_text('{"answer":"private sentinel"}')
            result = root / "result"
            result.symlink_to(sentinel)
            worker, metadata = live._read_worker_result(result, ())
            self.assertEqual((worker, metadata["classification"], metadata["raw_worker_sha256"]), ({}, "unsafe_file", None))
            result.unlink()
            if hasattr(os, "mkfifo"):
                os.mkfifo(result)
                self.assertEqual(live._read_worker_result(result, ())[1]["classification"], "unsafe_file")
                result.unlink()
            result.write_text('{"actual-key":"do not serialize"}')
            worker, metadata = live._read_worker_result(result, ("actual-key",))
            self.assertEqual(worker, {})
            self.assertEqual(metadata["classification"], "malformed")


if __name__ == "__main__":
    unittest.main()
