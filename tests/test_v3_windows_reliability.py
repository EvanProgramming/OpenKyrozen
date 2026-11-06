import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'v3_windows_reliability.py'


class WindowsReliabilityDiagnosticTests(unittest.TestCase):
    def test_actual_cancellation_fixture_emits_boundary_and_persisted_evidence(self):
        self.assertTrue(SCRIPT.is_file(), 'Windows diagnostic harness is missing')
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run([sys.executable, str(SCRIPT), '--output', directory,
                                      '--iterations', '1', '--repository', str(ROOT)], cwd=ROOT, capture_output=True,
                                     text=True, timeout=40)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            path = Path(directory)
            report = json.loads((path / 'report.json').read_text())
            self.assertFalse(report['historical_cause_established'])
            self.assertEqual(report['iterations'][0]['returncode'], 0)
            events = [json.loads(line) for line in (path / '1' / 'events.jsonl').read_text().splitlines()]
            names = [event['event'] for event in events]
            for name in ('dispatch.call', 'dispatch.return', 'provider.call', 'cancel.call',
                         'cancel.return', 'release.call', 'provider.return', 'worker.return', 'future_wait.call',
                         'cleanup.call', 'cleanup.return'):
                self.assertIn(name, names)
            self.assertLess(names.index('provider.call'), names.index('cancel.call'))
            self.assertLess(names.index('cancel.return'), names.index('release.call'))
            self.assertTrue(all('monotonic_ns' in event and 'thread_id' in event for event in events))
            state = json.loads((path / '1' / 'state-before-cleanup.json').read_text())
            self.assertEqual(state['marker'], 'ok')
            self.assertTrue(state['persisted_states'])
            self.assertEqual(state['persisted_states'][-1]['status'], 'cancelled')
            self.assertTrue((path / '1' / 'threads.log').is_file())
            self.assertEqual(report['test_timeouts_seconds'], {'provider_wait': 3, 'entry_wait': 10, 'future_wait': 5})

    def test_slow_worker_completion_keeps_original_timeout_and_captures_pending_worker(self):
        import time
        from unittest.mock import patch
        from scripts.v3_windows_reliability import Trace, run_child

        original = Trace.record

        def delay_worker_completion(trace, event, **fields):
            original(trace, event, **fields)
            if event == 'worker.return':
                time.sleep(5.3)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with patch.object(Trace, 'record', delay_worker_completion):
                self.assertEqual(run_child(path), 1)
            self.assertIn('TimeoutError', (path / 'unittest.log').read_text())
            self.assertIn('Timeout (0:00:04)', (path / 'threads.log').read_text())
            self.assertTrue((path / 'state-watchdog-1.json').exists())
            state = json.loads((path / 'state-before-cleanup.json').read_text())
            self.assertEqual(state['marker'], 'ok')
            self.assertTrue(any(not run['future_done'] for run in state['runs']))
            self.assertEqual(state['persisted_states'][-1]['status'], 'cancelled')
