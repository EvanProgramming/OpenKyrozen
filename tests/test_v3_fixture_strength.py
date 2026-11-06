"""Adversarial offline checks for the frozen matched fixture oracles."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.v3_fixtures import FIXTURES
from benchmarks.v3_matched_live import prepare_fixture, verify_fixture


def fixture(identifier):
    return next(item for item in FIXTURES if item["fixture_id"] == identifier)


def healthy_worker():
    return {"provider_error": False, "processes_alive": 0, "scope_preserved": True,
            "plan_revisions": 1, "task_records": [{"description": "Verify duplicate names", "status": "succeeded"}]}


class MatchedFixtureStrengthTests(unittest.TestCase):
    def test_refactoring_noop_cannot_pass_only_because_outputs_already_match(self):
        for index in (1, 2, 3):
            case = fixture(f"refactoring-{index}")
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                prepared = prepare_fixture(root, case)
                with patch("benchmarks.v3_matched_live._run_fixture_tests", return_value=True):
                    result = verify_fixture(case, root, "Outputs are unchanged.", healthy_worker(), prepared)
                self.assertEqual(result["result"], "failed")

    def test_multi_file_implementation_requires_both_declared_production_changes(self):
        case = fixture("implementation-1")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_fixture(root, case)
            with patch("benchmarks.v3_matched_live._run_fixture_tests", return_value=True):
                result = verify_fixture(case, root, "Implemented.", healthy_worker(), prepared)
            self.assertEqual(result["result"], "failed")

    def test_git_answer_without_successful_git_tool_evidence_is_rejected(self):
        case = fixture("git_history-2")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_fixture(root, case)
            answer = f"increase retry count {prepared['expected_commit']} changed RETRIES = 1 to RETRIES = 3"
            result = verify_fixture(case, root, answer, healthy_worker(), prepared)
            self.assertEqual(result["result"], "failed")

    def test_git_tool_reference_must_match_the_independently_prepared_commit(self):
        case = fixture("git_history-2")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_fixture(root, case)
            answer = f"increase retry count {prepared['expected_commit']} changed RETRIES = 1 to RETRIES = 3"
            worker = healthy_worker()
            worker["tool_receipts"] = [
                {"action": "git_log", "success": True, "authorized": True, "git_reference": None},
                {"action": "git_show", "success": True, "authorized": True, "git_reference": "wrong-commit"},
            ]
            self.assertEqual(verify_fixture(case, root, answer, worker, prepared)["result"], "failed")
            worker["tool_receipts"][-1]["git_reference"] = prepared["expected_commit_full"]
            self.assertEqual(verify_fixture(case, root, answer, worker, prepared)["result"], "passed")

    def test_long_context_answer_without_a_long_session_and_context_measurement_is_rejected(self):
        for index in (1, 2, 3):
            case = fixture(f"long_context-{index}")
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                prepared = prepare_fixture(root, case)
                answer = json.dumps(case["verifier"]["expected"])
                result = verify_fixture(case, root, answer, healthy_worker(), prepared)
                self.assertEqual(result["result"], "failed")

class FixtureOraclePositiveTests(unittest.TestCase):
    def test_inprocess_api_oracle_requires_validation_duplicates_and_shared_state(self):
        case = fixture("implementation-1")
        service = '''ITEMS = []
class DuplicateName(ValueError): pass
def list_items(): return list(ITEMS)
def create_item(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('name'), str) or not payload['name'].strip():
        raise ValueError('nonempty name')
    name = payload['name'].strip()
    if any(item['name'] == name for item in ITEMS): raise DuplicateName('duplicate')
    item = {'id': len(ITEMS) + 1, 'name': name}
    ITEMS.append(item)
    return item
'''
        api = '''import json
from service import list_items, create_item, DuplicateName
def app(environ, start_response):
    if environ['PATH_INFO'] != '/items': status, body = '404 Not Found', {}
    elif environ['REQUEST_METHOD'] == 'GET': status, body = '200 OK', list_items()
    elif environ['REQUEST_METHOD'] == 'POST':
        try:
            payload = json.loads(environ['wsgi.input'].read(int(environ['CONTENT_LENGTH'])))
            body = create_item(payload)
            status = '201 Created'
        except DuplicateName: status, body = '409 Conflict', {'error': 'duplicate'}
        except (ValueError, TypeError): status, body = '400 Bad Request', {'error': 'invalid'}
    else: status, body = '404 Not Found', {}
    start_response(status, [('Content-Type', 'application/json')])
    return [json.dumps(body).encode('utf-8')]
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_fixture(root, case)
            (root / "service.py").write_text(service)
            (root / "api.py").write_text(api)
            self.assertEqual(verify_fixture(case, root, "implemented", healthy_worker(), prepared)["result"], "passed")
            # Both modules change, but accepting every POST must still fail real tests.
            (root / "api.py").write_text(api.replace("'409 Conflict'", "'201 Created'"))
            self.assertEqual(verify_fixture(case, root, "implemented", healthy_worker(), prepared)["result"], "failed")

    def test_refactor_oracles_require_shared_extraction_and_preserved_behavior(self):
        implementations = {
            1: "def _format_label(name): return name.strip().title()\ndef primary(name): return _format_label(name)\ndef secondary(name): return _format_label(name)\n",
            2: "def _in_bounds(x): return 0 <= x <= 10\ndef first(x): return _in_bounds(x)\ndef second(x): return _in_bounds(x)\n",
            3: "import json\ndef _serialize(v): return json.dumps(v, ensure_ascii=False, sort_keys=True)\ndef one(v): return _serialize(v)\ndef two(v): return _serialize(v)\n",
        }
        for index, implementation in implementations.items():
            case = fixture(f"refactoring-{index}")
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                prepared = prepare_fixture(root, case)
                path = root / case["verifier"]["shared_helper"]["path"]
                path.write_text(implementation)
                self.assertEqual(verify_fixture(case, root, "refactored", healthy_worker(), prepared)["result"], "passed")
                name = case["verifier"]["shared_helper"]["name"]
                path.write_text(implementation.replace("return name.strip().title()", "return 'wrong'")
                    .replace("return 0 <= x <= 10", "return False")
                    .replace("return json.dumps(v, ensure_ascii=False, sort_keys=True)", "return 'wrong'"))
                self.assertEqual(verify_fixture(case, root, "refactored", healthy_worker(), prepared)["result"], "failed")

class CohortFreezeTests(unittest.TestCase):
    def test_repaired_catalog_keeps_exactly_thirty_ids_and_fixed_context_settings(self):
        from collections import Counter
        from benchmarks.v3_fixtures import CATEGORIES, FROZEN_COHORT_SHA256, cohort_digest
        self.assertEqual(cohort_digest(), FROZEN_COHORT_SHA256)
        self.assertEqual({item["fixture_id"] for item in FIXTURES},
                         {f"{category}-{index}" for category in CATEGORIES for index in (1, 2, 3)})
        self.assertEqual(Counter(item["category"] for item in FIXTURES), {category: 3 for category in CATEGORIES})
        self.assertTrue(all(item["output_reserve_tokens"] == 4096 for item in FIXTURES))
        self.assertTrue(all(item["context_window_tokens"] == (16384 if item["category"] == "long_context" else 1_000_000)
                            for item in FIXTURES))

    def test_other_worktree_worker_must_match_the_observer_and_helpers_in_the_apparatus_hash(self):
        from benchmarks import v3_matched_live as live
        from tests.test_v3_matched_live import prepare_candidate_apparatus
        for changed in ("prompt_worker.py", "v3_shell_launcher.py"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                other = root / "other-worktree"
                prepare_candidate_apparatus(other)
                (other / "benchmarks" / changed).write_text("print('different observer or helper')\n")
                with patch.object(live, "_source_snapshot", return_value="other-clean-candidate"), \
                     patch("openkyrozen.tools.sandbox.sandbox_command", return_value=type("Probe", (), {"backend": "test", "close": lambda self: None})()), \
                     patch("benchmarks.v3_protected_worker.verify_worker_protection", return_value={}), \
                     patch.object(live, "_credential", side_effect=AssertionError("credential lookup before worker validation")) as credential, \
                     patch.object(live, "ResponsesRelay") as relay, patch.object(live, "_run_worker") as worker:
                    with self.assertRaisesRegex(ValueError, "dispatched worker.*apparatus"):
                        live.run(source=other, repo=root, output=root / "report.json",
                                 budget_path=root / "budget.sqlite3", prior_hold_usd="1.25")
                    credential.assert_not_called()
                    relay.assert_not_called()
                    worker.assert_not_called()

    def test_old_or_changed_cohort_and_apparatus_cannot_resume_before_credentials(self):
        from benchmarks import v3_matched_live as live
        from benchmarks.v3_matched import BudgetLedger, USD_CNY
        from decimal import Decimal
        for altered in ("missing", "cohort_sha256", "apparatus_sha256", "context_settings", "old_row"):
            with self.subTest(altered=altered), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                report, ledger = root / "report.json", root / "budget.sqlite3"
                BudgetLedger(ledger, limit_rmb=str(Decimal(20) * USD_CNY), prior_hold_rmb=str(Decimal("1.25") * USD_CNY))
                identity = live.apparatus_identity()
                checkpoint = {**identity, "protocol": "openkyrozen-v3-matched-live-v1", "candidate_sha": "candidate",
                    "baseline_sha": live.BASELINE, "model": live.MODEL, "repeats": 3,
                    "budget": {"prior_campaign_hold_usd": "1.25"}, "rows": [], "calls": []}
                if altered == "missing":
                    for key in identity: checkpoint.pop(key)
                elif altered == "old_row": checkpoint["rows"] = [{"fixture_id": "investigation-1", "repeat": 0, "arm": "v2"}]
                else: checkpoint[altered] = "changed"
                live._write_checkpoint(report, ledger, checkpoint)
                with patch.object(live, "_source_snapshot", return_value="candidate"), \
                     patch("openkyrozen.tools.sandbox.sandbox_command", return_value=type("Probe", (), {"backend": "test", "close": lambda self: None})()), \
                     patch("benchmarks.v3_protected_worker.verify_worker_protection", return_value={}), \
                     patch.object(live, "_credential") as credential:
                    with self.assertRaisesRegex(ValueError, "checkpoint.*(?:cohort|apparatus)"):
                        live.run(source=root / "source", repo=root, output=report, budget_path=ledger, prior_hold_usd="1.25")
                    credential.assert_not_called()


if __name__ == "__main__":
    unittest.main()
