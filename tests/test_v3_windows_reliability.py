import json
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
import zipfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'v3_windows_reliability.py'


class WindowsReliabilityDiagnosticTests(unittest.TestCase):
    @staticmethod
    def _historical_archive(revision):
        return subprocess.run(['git', '-c', 'core.autocrlf=false', '-c', 'core.eol=lf',
                               'archive', '--format=zip', revision], cwd=ROOT,
                              capture_output=True, check=True, timeout=10).stdout

    def test_historical_archive_ignores_inherited_windows_line_endings(self):
        with patch.dict(os.environ, {'GIT_CONFIG_COUNT': '1',
                                     'GIT_CONFIG_KEY_0': 'core.autocrlf', 'GIT_CONFIG_VALUE_0': 'true'}):
            archive = self._historical_archive('0f172f9fe692ff8fafcc0a15a72fe51eaf67e977')
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            fixture = source.read('tests/test_delegation.py')
        self.assertEqual(hashlib.sha256(fixture).hexdigest(),
                         '7484131880e91a328961ec0b8b99fe2bcaad8ded19b2ebe8efd944028f2aa3a7')

    def test_readonly_anchor_materializes_wal_without_a_transaction_and_final_writer_close_checkpoints(self):
        import sqlite3
        from scripts.v3_windows_reliability import LoadControl, Trace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'state.sqlite3'
            wal = Path(str(database) + '-wal')
            connection = sqlite3.connect(database)
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE probe(value TEXT)')
            connection.commit()
            connection.close()
            self.assertFalse(wal.exists())
            output = root / 'evidence'
            output.mkdir()
            trace = Trace(output)
            control = LoadControl('baseline', 0, types.SimpleNamespace(root=root), trace,
                                  readonly_anchor=True)
            try:
                control.start()
                anchor = control.anchor
                self.assertFalse(anchor.in_transaction)
                writer = sqlite3.connect(database, timeout=.05)
                try:
                    writer.execute("INSERT INTO probe VALUES('committed')")
                    writer.commit()
                finally:
                    writer.close()
                self.assertTrue(wal.is_file(), 'Materialized anchor must prevent last-client WAL cleanup')
                self.assertEqual(anchor.execute('SELECT value FROM probe').fetchall(), [('committed',)])
                self.assertFalse(anchor.in_transaction)
                with self.assertRaises(sqlite3.OperationalError):
                    anchor.execute("INSERT INTO probe VALUES('forbidden')")
                self.assertEqual(anchor.total_changes, 0)
            finally:
                control.close()
                trace.stream.close()
            self.assertTrue(wal.exists(), 'The final read-only client cannot checkpoint the WAL')
            with self.assertRaises(sqlite3.ProgrammingError):
                anchor.execute('SELECT 1')
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(connection.execute('SELECT value FROM probe').fetchall(), [('committed',)])
            finally:
                connection.close()
            self.assertFalse(wal.exists(), 'A final writable client must checkpoint the committed WAL')

    def test_paired_anchor_reports_counterbalanced_order_and_identical_bounded_cpu_load(self):
        from scripts import v3_windows_reliability as diagnostic
        commands = []
        def run(command, log_path, env):
            commands.append(command)
            return {'returncode': 0, 'elapsed_ns': 1, 'command': command, 'timeout_cleanup': None}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(sys, 'argv', [str(SCRIPT), '--output', directory, '--iterations', '2',
                 '--control', 'paired-anchor', '--interleave-reviewer']), \
             patch.object(diagnostic, 'run_process', side_effect=run):
            self.assertEqual(diagnostic.main(), 0)
            report = json.loads((Path(directory) / 'report.json').read_text())
            self.assertEqual(report['execution_order'], [
                'cancellation-1-baseline', 'reviewer-1-baseline',
                'cancellation-1-anchor', 'reviewer-1-anchor',
                'cancellation-2-anchor', 'reviewer-2-anchor',
                'cancellation-2-baseline', 'reviewer-2-baseline'])
            self.assertEqual([row['variant'] for row in report['iterations']],
                             ['baseline', 'anchor', 'anchor', 'baseline'])
            cancellations = commands[::2]
            self.assertTrue(all(command[command.index('--control') + 1] == 'cpu' for command in cancellations))
            self.assertEqual(['--readonly-anchor' in command for command in cancellations],
                             [False, True, True, False])
            with patch.object(sys, 'argv', [str(SCRIPT), '--output', directory,
                    '--control', 'paired-anchor', '--iterations', '11']):
                with self.assertRaises(SystemExit) as error:
                    diagnostic.main()
                self.assertEqual(error.exception.code, 2)

    def test_last_client_checkpoint_latency_hits_original_deadline_until_readonly_anchor(self):
        from scripts.v3_windows_reliability import isolated_environment, run_process
        code = """
from pathlib import Path
import sqlite3, sys, threading, time
from unittest.mock import patch
import scripts.v3_windows_reliability as harness
repository = Path(sys.argv[3]).resolve()
harness.ROOT = repository
sys.path[:0] = [str(repository), str(repository / 'tests')]
from scripts.v3_windows_reliability import Trace, run_child
import openkyrozen
assert Path(openkyrozen.__file__).resolve().is_relative_to(repository)
print('Fixture product source:', openkyrozen.__file__, flush=True)

active, injected = threading.Event(), threading.Event()
state = {}
original_record, original_connect = Trace.record, sqlite3.connect

def record(trace, event, **fields):
    state['trace'] = trace
    if event == 'cancel.return':
        active.set()
    original_record(trace, event, **fields)

class CheckpointLatency(sqlite3.Connection):
    def __init__(self, database, *args, **kwargs):
        super().__init__(database, *args, **kwargs)
        self.fixture = str(database).endswith(('state.sqlite3', 'state.sqlite3?mode=ro'))

    def close(self):
        trace = state.get('trace')
        wal = trace.case.root / 'state.sqlite3-wal' if trace and active.is_set() and self.fixture else None
        size = wal.stat().st_size if wal and wal.is_file() else 0
        super().close()
        # A dirty WAL disappearing proves this close performed real last-client checkpoint cleanup.
        if size > 32 and not wal.exists() and not injected.is_set():
            injected.set()
            trace.record('injected_checkpoint_close.call', wal_bytes=size)
            time.sleep(5.3)
            trace.record('injected_checkpoint_close.return')

def connect(*args, **kwargs):
    kwargs['factory'] = CheckpointLatency
    return original_connect(*args, **kwargs)

with patch.object(Trace, 'record', record), patch.object(sqlite3, 'connect', connect):
    raise SystemExit(run_child(Path(sys.argv[1]), readonly_anchor=sys.argv[2] == 'anchor'))
"""
        with tempfile.TemporaryDirectory() as directory:
            revision = '0f172f9fe692ff8fafcc0a15a72fe51eaf67e977'
            legacy = Path(directory) / 'historical-product'
            archive = self._historical_archive(revision)
            with zipfile.ZipFile(io.BytesIO(archive)) as source:
                self.assertTrue(all(not Path(name).is_absolute() and '..' not in Path(name).parts
                                    for name in source.namelist()))
                source.extractall(legacy)
            self.assertEqual(hashlib.sha256((legacy / 'tests/test_delegation.py').read_bytes()).hexdigest(),
                             '7484131880e91a328961ec0b8b99fe2bcaad8ded19b2ebe8efd944028f2aa3a7')
            evidence = Path(os.environ.get('KYROZEN_DIAGNOSTIC_TEST_OUTPUT', directory))
            evidence = evidence / f'checkpoint-close-{time.time_ns()}'
            env = isolated_environment(Path(directory) / 'home')
            for variant, expected in (('baseline', 1), ('anchor', 0), ('current', 0)):
                repository = ROOT if variant == 'current' else legacy
                output = evidence / variant
                output.mkdir(parents=True)
                process = run_process([sys.executable, '-c', code, str(output), variant, str(repository)],
                                      output / 'process.log', env)
                (output / 'child-process-result.json').write_text(json.dumps(
                    {**process, 'expected_returncode': expected, 'readonly_anchor': variant == 'anchor',
                     'product_revision': 'current-checkout' if variant == 'current' else revision,
                     'fixture_sha256': hashlib.sha256((repository / 'tests/test_delegation.py').read_bytes()).hexdigest(),
                     'database_source_sha256': hashlib.sha256((repository / 'openkyrozen/persistence/database.py').read_bytes()).hexdigest()}, indent=2))
                self.assertEqual(process['returncode'], expected, (output / 'process.log').read_text())
                log = (output / 'unittest.log').read_text()
                events = [json.loads(line)['event'] for line in (output / 'events.jsonl').read_text().splitlines()]
                state = json.loads((output / 'state-before-cleanup.json').read_text())
                self.assertEqual(state['marker'], 'ok')
                self.assertEqual(state['persisted_states'][-1]['status'], 'cancelled')
                if variant == 'baseline':
                    self.assertIn('TimeoutError', log)
                    self.assertIn('injected_checkpoint_close.call', events)
                    self.assertLess(events.index('injected_checkpoint_close.call'), events.index('future_wait.return'))
                elif variant == 'anchor':
                    self.assertNotIn('TimeoutError', log)
                    self.assertNotIn('injected_checkpoint_close.call', events)
                    self.assertLess(events.index('future_wait.return'), events.index('anchor.close.call'))
                else:
                    self.assertNotIn('TimeoutError', log)
                    for index, event in enumerate(events):
                        if event == 'injected_checkpoint_close.call':
                            self.assertLess(events.index('future_wait.return'), index)

    def test_detailed_profiler_starts_after_fixture_setup(self):
        import sqlite3
        from scripts.v3_windows_reliability import run_child

        class Fixture(unittest.TestCase):
            def setUp(self):
                self.root = root
                connection = sqlite3.connect(':memory:')
                connection.close()

            def test_cancel_during_provider_call_prevents_late_write(self):
                connection = sqlite3.connect(':memory:')
                connection.close()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = types.SimpleNamespace(DelegationTests=Fixture)
            with patch.dict(sys.modules, {'test_delegation': fixture}):
                self.assertEqual(run_child(root), 0)
            names = [json.loads(line)['event'] for line in (root / 'events.jsonl').read_text().splitlines()]
            start = names.index('setup.return')
            self.assertFalse(any(name.startswith('sqlite.') for name in names[:start]))
            self.assertIn('sqlite.connect.c_call', names[start:names.index('cleanup.call')])

    def test_historical_pin_export_includes_preinstalled_dependencies_and_excludes_product(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'requirements.txt'
            process = subprocess.run([sys.executable, str(SCRIPT), '--write-historical-requirements', str(path)],
                                     capture_output=True, text=True, timeout=5)
            self.assertEqual(process.returncode, 0, process.stderr)
            requirements = path.read_text().splitlines()
            for requirement in ('openai==3.42.1', 'anthropic==2.3.0', 'packaging==26.3',
                                'colorama==0.4.6', 'click==8.5.0', 'filelock==4.2.4'):
                self.assertIn(requirement, requirements)
            self.assertFalse(any(item.startswith('openkyrozen==') for item in requirements))

    def test_historical_environment_mismatch_stops_before_running_any_fixture(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sys, 'argv', [str(SCRIPT), '--output', directory,
                    '--iterations', '1', '--historical-environment']), \
                 patch.object(diagnostic.platform, 'python_version', return_value='3.12.99'):
                self.assertEqual(diagnostic.main(), 2)
            root = Path(directory)
            environment = json.loads((root / 'historical-environment.json').read_text())
            self.assertEqual(environment['status'], 'INFRASTRUCTURE_FAILURE')
            self.assertFalse((root / '1').exists())

    def test_child_environment_uses_private_windows_home_and_scrubs_credentials(self):
        from scripts.v3_windows_reliability import isolated_environment
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'USERPROFILE': '/original-user', 'HOME': '/original-home',
                'DEEPSEEK_API_KEY': 'must-not-inherit', 'GH_TOKEN': 'must-not-inherit',
                'KYROZEN_DB_PATH': '/original.sqlite3'}):
            env = isolated_environment(Path(directory))
            self.assertEqual(env['HOME'], directory)
            self.assertEqual(env['USERPROFILE'], directory)
            self.assertEqual(env['KYROZEN_DB_PATH'], str(Path(directory) / 'runtime.sqlite3'))
            self.assertTrue(Path(env['KYROZEN_WORKSPACE_ROOT']).is_dir())
            self.assertNotIn('DEEPSEEK_API_KEY', env)
            self.assertNotIn('GH_TOKEN', env)

    def test_historical_environment_rejects_runtime_and_dependency_substitutions(self):
        from scripts.v3_windows_reliability import historical_environment
        with patch('scripts.v3_windows_reliability.platform.python_version', return_value='3.12.10'), \
             patch('scripts.v3_windows_reliability.platform.system', return_value='Windows'), \
             patch('scripts.v3_windows_reliability.metadata.version', return_value='wrong-version'):
            report = historical_environment()
        self.assertEqual(report['status'], 'INFRASTRUCTURE_FAILURE')
        self.assertIn('openai', report['dependency_mismatches'])
        self.assertEqual(report['dependency_mismatches']['openai']['expected'], '3.42.1')
        self.assertEqual(report['dependency_mismatches']['anthropic']['expected'], '2.3.0')
        with patch('scripts.v3_windows_reliability.platform.python_version', return_value='3.12.13'):
            report = historical_environment()
        self.assertEqual(report['status'], 'INFRASTRUCTURE_FAILURE')
        self.assertEqual(report['python'], '3.12.13')

    def test_interleaved_run_keeps_original_order_and_traces_post_cancel_phases(self):
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run([sys.executable, str(SCRIPT), '--output', directory,
                '--iterations', '2', '--repository', str(ROOT), '--interleave-reviewer'],
                cwd=ROOT, capture_output=True, text=True, timeout=45)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            root = Path(directory)
            report = json.loads((root / 'report.json').read_text())
            self.assertFalse(report['historical_cause_established'])
            self.assertEqual(report['execution_order'], [
                'cancellation-1', 'reviewer-1', 'cancellation-2', 'reviewer-2'])
            for index in (1, 2):
                result = report['iterations'][index - 1]
                self.assertEqual(result['reviewer']['returncode'], 0)
                self.assertIn('Sub-agent workflow acceptance passed.',
                              (root / str(index) / 'reviewer.log').read_text())
                events = [json.loads(line) for line in
                          (root / str(index) / 'events.jsonl').read_text().splitlines()]
                names = [item['event'] for item in events]
                for name in ('provider_wait.call', 'provider_check.call', 'usage_persist.call', 'usage_summary.call',
                             'sqlite.execute.c_call', 'sqlite.close.c_return'):
                    self.assertIn(name, names)
                self.assertTrue(all('thread_cpu_ns' in item for item in events))

    @unittest.skipUnless(os.name == 'posix', 'real owned process-group cleanup requires POSIX')
    def test_diagnostic_timeout_cleanup_stops_owned_descendant(self):
        from scripts.v3_windows_reliability import stop_diagnostic_tree
        code = '''
import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(30)
'''
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'child-pid'
            process = subprocess.Popen([sys.executable, '-c', code, str(marker)],
                                       start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 3
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(marker.exists())
                child_pid = int(marker.read_text())
                cleanup = stop_diagnostic_tree(process)
                process.communicate(timeout=3)
                state = subprocess.run(['ps', '-p', str(child_pid), '-o', 'stat='],
                                       capture_output=True, text=True, timeout=3).stdout.strip()
                self.assertTrue(not state or state.startswith('Z'), state)
                self.assertFalse(cleanup['descendant_cleanup_certified'])
                self.assertIsNotNone(process.returncode)
            finally:
                stop_diagnostic_tree(process)
                process.communicate(timeout=3)

    def test_cpu_control_exits_at_its_own_deadline_without_parent_stop(self):
        from scripts.v3_windows_reliability import _CPU_LOAD
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = subprocess.run([sys.executable, '-c', _CPU_LOAD,
                str(root / 'ready'), str(root / 'never-stopped'), str(root / 'result.json'), '.15'],
                capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)
            metrics = json.loads((root / 'result.json').read_text())
            self.assertEqual(metrics['stopped_by'], 'deadline')
            self.assertLess(metrics['wall_seconds'], 2)

    def test_explicit_load_control_records_real_contention_and_owned_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run([sys.executable, str(SCRIPT), '--output', directory,
                '--iterations', '1', '--repository', str(ROOT), '--control', 'combined',
                '--cpu-workers', '1'], cwd=ROOT, capture_output=True, text=True, timeout=40)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            report = json.loads((Path(directory) / 'report.json').read_text())
            self.assertEqual(report['control'], 'combined')
            self.assertFalse(report['historical_cause_established'])
            result = json.loads((Path(directory) / '1' / 'load-control.json').read_text())
            self.assertEqual(result['control'], 'combined')
            self.assertGreaterEqual(result['sqlite']['acquired_transactions'], 1)
            self.assertTrue(result['sqlite']['thread_stopped'])
            self.assertEqual(len(result['cpu']), 1)
            self.assertEqual(result['cpu'][0]['returncode'], 0)
            self.assertGreater(result['cpu'][0]['cpu_seconds'], 0)
            self.assertGreater(result['cpu'][0]['wall_seconds'], 0)
            events = [json.loads(line)['event'] for line in
                      (Path(directory) / '1' / 'events.jsonl').read_text().splitlines()]
            self.assertIn('sqlite.lock_acquired', events)
            self.assertIn('sqlite.lock_released', events)
            self.assertIn('load.started', events)
            self.assertIn('load.stopped', events)
            state = json.loads((Path(directory) / '1' / 'state-before-cleanup.json').read_text())
            self.assertEqual(state['marker'], 'ok')

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
        code = """
from pathlib import Path
import faulthandler
import sys
import time
from unittest.mock import patch
from scripts.v3_windows_reliability import Trace, run_child

original = Trace.record

def delay_worker_completion(trace, event, **fields):
    original(trace, event, **fields)
    if event == 'worker.return':
        time.sleep(5.3)

with patch.object(Trace, 'record', delay_worker_completion), \
     patch.object(faulthandler, 'dump_traceback_later', side_effect=AssertionError('native watchdog forbidden')), \
     patch.object(faulthandler, 'cancel_dump_traceback_later', side_effect=AssertionError('native watchdog forbidden')):
    result = run_child(Path(sys.argv[1]))
print('native watchdog unused', flush=True)
raise SystemExit(result)
"""
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(os.environ.get('KYROZEN_DIAGNOSTIC_TEST_OUTPUT', directory))
            evidence = evidence / f'native-watchdog-{time.time_ns()}'
            for iteration in range(3):
                path = evidence / str(iteration)
                path.mkdir(parents=True)
                with (path / 'process.log').open('w') as log:
                    process = subprocess.run([sys.executable, '-c', code, str(path)], cwd=ROOT,
                                             stdout=log, stderr=subprocess.STDOUT, timeout=20)
                output = (path / 'process.log').read_text()
                self.assertIn('native watchdog unused', output)
                self.assertEqual(process.returncode, 1, output)
                self.assertIn('TimeoutError', (path / 'unittest.log').read_text())
                self.assertIn('Timeout (0:00:04)', (path / 'threads.log').read_text())
                self.assertTrue((path / 'state-watchdog-1.json').exists())
                state = json.loads((path / 'state-before-cleanup.json').read_text())
                self.assertEqual(state['marker'], 'ok')
                self.assertTrue(any(not run['future_done'] for run in state['runs']))
                self.assertEqual(state['persisted_states'][-1]['status'], 'cancelled')
