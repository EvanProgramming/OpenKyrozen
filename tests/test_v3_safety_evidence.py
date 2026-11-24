"""Independent safety evidence must not infer success from missing worker data."""
import tempfile
import unittest
from pathlib import Path

from benchmarks.v3_matched_live import prepare_fixture, verify_fixture


class SafetyEvidenceTests(unittest.TestCase):
    def verify(self, worker):
        fixture = {"fixture_id": "safety-probe", "category": "investigation",
                   "files": {"source.txt": "original"}, "capabilities": ["read"],
                   "verifier": {"kind": "answer_contains", "all": ["observed"]}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_fixture(root, fixture)
            return verify_fixture(fixture, root, "observed", worker, prepared)

    def test_missing_worker_evidence_cannot_pass_safety(self):
        self.assertFalse(self.verify({})["safety_passed"])

    def test_boolean_process_count_cannot_pass_safety(self):
        self.assertFalse(self.verify({"provider_error": False,
                                      "processes_alive": False})["safety_passed"])

    def test_independent_components_are_retained(self):
        result = self.verify({"provider_error": False, "processes_alive": 0})
        self.assertTrue(result["safety_passed"])
        self.assertEqual(result["safety_components"], {
            "scope_safe": True, "original_tests_unchanged": True,
            "provider_ok": True, "no_running_processes": True, "changed_paths": [],
        })

    def test_provider_failure_remains_distinguishable_from_scope_and_process(self):
        result = self.verify({"provider_error": True, "processes_alive": 0})
        self.assertFalse(result["safety_passed"])
        self.assertFalse(result["safety_components"]["provider_ok"])
        self.assertTrue(result["safety_components"]["scope_safe"])
        self.assertTrue(result["safety_components"]["no_running_processes"])

class CampaignSafetyStopTests(unittest.TestCase):
    def campaign(self, *, worker=None, exit_code=0, resume=False):
        import json
        from unittest.mock import patch
        import benchmarks.v3_matched_live as live
        commands = []
        from tests.test_v3_matched_live import ActorRelayStub
        class Relay(ActorRelayStub):
            url = 'http://127.0.0.1:1/v1'
            client_key = 'local'
            provider_error = None
            budget_blocked = False
            def __init__(self, **kwargs): self.rows = []
            def start(self): pass
            def close(self): pass
        def execute(command, **kwargs):
            from tests.test_v3_matched_live import valid_worker_result
            commands.append(command)
            result = Path(command[command.index('--result') + 1])
            root = Path(kwargs['cwd'])
            if worker == 'scope':
                (root / 'undeclared.txt').write_text('outside fixture declaration')
            result.write_text('not-json' if worker == 'malformed' else
                              json.dumps(valid_worker_result(provider_error=worker == 'provider')))
            if worker == 'cleanup':
                raise RuntimeError('synthetic private cleanup detail')
            return exit_code, ''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            import shutil
            (source / 'benchmarks').mkdir()
            for name in live.apparatus_identity()['apparatus_files']:
                shutil.copy2(Path(live.__file__).parent / name, source / 'benchmarks' / name)
            with patch.object(live, '_source_snapshot', return_value='candidate'), \
                 patch('openkyrozen.tools.sandbox.sandbox_command', return_value=type('Probe', (), {'backend':'test','close':lambda self:None})()), \
                 patch.object(live, '_credential', return_value=('http://example.invalid', 'local')), \
                 patch.object(live, 'ResponsesRelay', Relay), \
                 patch.object(live, '_extract_baseline', side_effect=lambda path, *_: path.mkdir()), \
                 patch.object(live, '_run_worker', side_effect=execute), \
                 patch('benchmarks.v3_protected_worker.verify_worker_protection', return_value={}), \
                 patch.object(live, '_run_fixture_tests', return_value=False), patch('builtins.print'):
                report = live.run(source=source, repo=source, output=root/'report.json',
                                  budget_path=root/'budget.sqlite3', prior_hold_usd='1')
                if resume:
                    with self.assertRaisesRegex(ValueError, 'safety failure'):
                        live.run(source=source, repo=source, output=root/'report.json',
                                 budget_path=root/'budget.sqlite3', prior_hold_usd='1')
            saved = json.loads((root/'report.json').read_text())
            self.assertEqual(saved, report)
            self.assertEqual(live._load_checkpoint(root/'report.json', root/'budget.sqlite3'), report)
        return commands, report

    def test_scope_failure_stops_after_first_worker_and_checkpoints_components(self):
        commands, report = self.campaign(worker='scope')
        self.assertEqual(len(commands), 1)
        self.assertEqual(report['status'], 'blocked_safety_failure')
        self.assertFalse(report['rows'][0]['safety_components']['scope_safe'])

    def test_provider_failure_is_distinct_from_scope_failure(self):
        commands, report = self.campaign(worker='provider')
        self.assertEqual(len(commands), 1)
        self.assertEqual(report['status'], 'blocked_provider_error')
        self.assertTrue(report['rows'][0]['worker_result_complete'])
        self.assertTrue(report['rows'][0]['safety_components']['scope_safe'])
        self.assertFalse(report['rows'][0]['safety_components']['provider_ok'])

    def test_failed_worker_cannot_report_safe_success(self):
        commands, report = self.campaign(exit_code=3)
        self.assertEqual(len(commands), 1)
        self.assertEqual(report['status'], 'blocked_safety_failure')
        self.assertFalse(report['rows'][0]['safety_components']['worker_complete'])

    def test_malformed_worker_result_is_checkpointed_as_incomplete(self):
        commands, report = self.campaign(worker='malformed')
        self.assertEqual(len(commands), 1)
        self.assertEqual(report['status'], 'blocked_safety_failure')
        self.assertFalse(report['rows'][0]['worker_result_complete'])

    def test_parent_cleanup_failure_is_checkpointed_without_starting_another_cell(self):
        commands, report = self.campaign(worker='cleanup')
        self.assertEqual(len(commands), 1)
        self.assertEqual(report['status'], 'blocked_safety_failure')
        row = report['rows'][0]
        self.assertFalse(row['worker_result_complete'])
        self.assertFalse(row['safety_components']['worker_complete'])
        self.assertIsNone(row['safety_components']['no_running_processes'])
        self.assertEqual(row['worker_error'], 'parent_execution_failed:RuntimeError')
        self.assertNotIn('synthetic private', str(report))

    def test_resume_cannot_skip_a_recorded_safety_failure(self):
        commands, report = self.campaign(worker='scope', resume=True)
        self.assertEqual(len(commands), 1)
