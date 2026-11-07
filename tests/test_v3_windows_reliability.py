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
from unittest.mock import Mock, patch


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


class NativeVfsDiagnosticTests(unittest.TestCase):
    def test_selected_close_keeps_first_pair_when_later_connection_id_is_reused(self):
        import threading
        with tempfile.TemporaryDirectory() as directory:
            control,_=self.control(Path(directory),'wal',types.SimpleNamespace())
            control.evidence.update(connection_id=7,worker_native_tid=threading.get_native_id())
            for end_event in ('c_return','c_exception'):
                with self.subTest(end_event=end_event):
                    for key in ('close_call_monotonic_ns','close_return_monotonic_ns',
                                'close_interval_ns','close_event'):
                        control.evidence.pop(key,None)
                    control.note_close('c_return',7,at_ns=290_400_000_000)
                    control.note_close('c_call',8,at_ns=290_410_000_000)
                    control.note_close('c_call',7,at_ns=290_421_000_000)
                    control.note_close(end_event,7,at_ns=291_375_000_000)
                    control.note_close('c_call',7,at_ns=291_390_000_000)
                    control.note_close('c_return',7,at_ns=291_406_000_000)
                    self.assertEqual(control.evidence['close_call_monotonic_ns'],290_421_000_000)
                    self.assertEqual(control.evidence['close_return_monotonic_ns'],291_375_000_000)
                    self.assertEqual(control.evidence['close_interval_ns'],954_000_000)
                    self.assertEqual(control.evidence['close_event'],end_event)

    def test_owned_handle_drains_after_original_application_close_before_fixture_file_cleanup(self):
        from scripts import v3_windows_reliability as diagnostic
        order=[];held=False;holder=None;fixture_root=None
        class API:
            def open(self,path,probe=False):
                nonlocal held
                if probe:return None,32
                held=True;return 123,0
            def close(self,handle):
                nonlocal held
                order.append('handle.close');held=False
            def qpc(self):return time.perf_counter_ns()
        class Fixture(unittest.TestCase):
            def setUp(self):
                nonlocal fixture_root
                self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
                fixture_root=self.root
                (self.root/'state.sqlite3-wal').write_bytes(b'x'*100)
                cleanup=self.temp.cleanup
                def unlink():
                    order.append('fixture.unlink')
                    if held:raise PermissionError(32,'WAL still held by diagnostic')
                    cleanup()
                self.temp.cleanup=unlink
            def test_cancel_during_provider_call_prevents_late_write(self):
                holder.arm(self.root/'state.sqlite3-wal',connection_id=7,frame=sys._getframe())
                order.append('future.done')
            def tearDown(self):
                order.append('application.close.call')
                self.assertTrue(held,'intervention must still be active during original application.close')
                order.append('application.close.return')
                order.append('environment.stop')
                self.temp.cleanup()
        original=diagnostic.WalSharingControl
        def capture_holder(trace,mode):
            nonlocal holder
            holder=original(trace,mode,api=API());return holder
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(sys.modules,{'test_delegation':types.SimpleNamespace(DelegationTests=Fixture)}), \
             patch.object(diagnostic,'LoadControl') as load, \
             patch.object(diagnostic,'WalSharingControl',side_effect=capture_holder), \
             patch.object(diagnostic.faulthandler,'dump_traceback'):
            load.return_value.close.side_effect=lambda:order.append('load.close')
            result=diagnostic.run_child(Path(directory),'cpu',2,readonly_anchor=True,
                                        native_trace=True,vfs_target='wal')
            self.assertEqual(result,0,(Path(directory)/'unittest.log').read_text())
            evidence=json.loads((Path(directory)/'vfs-control.json').read_text())
            self.assertTrue(evidence['cleanup_complete'])
            self.assertGreaterEqual(evidence['release_call_performance_counter_ns']-
                                    evidence['arm_performance_counter_ns'],800_000_000)
        self.assertFalse(fixture_root.exists())
        self.assertLess(order.index('future.done'),order.index('application.close.call'))
        self.assertLess(order.index('load.close'),order.index('application.close.call'))
        self.assertLess(order.index('application.close.return'),order.index('handle.close'))
        self.assertLess(order.index('handle.close'),order.index('fixture.unlink'))

    def test_qualifying_hold_overlaps_actual_close_with_complete_consistent_integer_timestamps(self):
        from scripts.v3_windows_reliability import _vfs_condition
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            valid={'mode':'wal','hold_seconds':.8,'desired_access':0x80000000,'share_mode':3,
                'creation_disposition':3,'file_flags':0x80,'release_reason':'fixed_deadline',
                'attempted':True,'armed':True,'released':True,'cleanup_complete':True,'error':None,
                'arm_monotonic_ns':1,'release_call_monotonic_ns':800_000_001,
                'timer':'perf_counter_ns','arm_performance_counter_ns':1,
                'release_call_performance_counter_ns':800_000_001,
                'release_monotonic_ns':800_000_011,'close_call_monotonic_ns':2,
                'close_return_monotonic_ns':12,'close_interval_ns':10,'close_event':'c_return',
                'worker_pid':1,'worker_native_tid':1,'arm_qpc':1,'release_qpc':800_000_011,
                'wal':str(root/'state.sqlite3-wal'),'target':str(root/'state.sqlite3-wal')}
            path=root/'vfs-control.json';path.write_text(json.dumps(valid))
            self.assertTrue(_vfs_condition(root,'wal')[1])  # anchor/current may return before release
            coarse=dict(valid,release_call_monotonic_ns=796_875_001,release_monotonic_ns=796_875_011)
            path.write_text(json.dumps(coarse))
            self.assertTrue(_vfs_condition(root,'wal')[1])  # .8s high-resolution hold, coarse Windows clock ticks
            path.write_text(json.dumps(dict(valid,close_return_monotonic_ns=2,close_interval_ns=0)))
            self.assertTrue(_vfs_condition(root,'wal')[1])  # Windows monotonic timestamps can share one tick
            invalid=[dict(valid,close_call_monotonic_ns=900_000_001,
                          close_return_monotonic_ns=900_000_011),
                     dict(valid,close_interval_ns=11),dict(valid,close_call_monotonic_ns=True),
                     dict(valid,close_return_monotonic_ns=12.0),dict(valid,release_call_monotonic_ns=0),
                     dict(valid,release_call_performance_counter_ns=799_999_999),
                     dict(valid,close_interval_ns=False,close_return_monotonic_ns=2),
                     dict(valid,close_interval_ns=-1,close_return_monotonic_ns=1)]
            for field in ('close_call_monotonic_ns','close_return_monotonic_ns','release_call_monotonic_ns'):
                missing=dict(valid);missing.pop(field);invalid.append(missing)
            for value in invalid:
                with self.subTest(value=value):
                    path.write_text(json.dumps(value))
                    self.assertFalse(_vfs_condition(root,'wal')[1])

    def test_vfs_cli_requires_pinned_historical_environment_and_exact_six_cell_campaign(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory,patch.object(diagnostic,'ROOT',diagnostic.ROOT), \
                patch.object(diagnostic,'run_native_comparison',return_value=0) as run:
            root=Path(directory).resolve()
            base=['diagnostic','--output',str(root/'out'),'--repository',str(root/'current'),
                  '--control','native-vfs','--iterations','1']
            with patch.object(sys,'argv',base),self.assertRaises(SystemExit):diagnostic.main()
            run.assert_not_called()
            with patch.object(sys,'argv',base+['--historical-environment','--historical-repository',str(root/'old')]):
                self.assertEqual(diagnostic.main(),0)
            run.assert_called_once_with(root/'out',root/'current',root/'old',vfs=True)

    def test_only_first_worker_post_cancel_positive_wal_preclose_phase_can_arm(self):
        import sqlite3
        import threading
        from scripts.v3_windows_reliability import Trace
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            connection=sqlite3.connect(root/'state.sqlite3')
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE probe(value TEXT)');connection.commit()
            trace=Trace(root,native=True);trace.case=types.SimpleNamespace(root=root)
            holder=types.SimpleNamespace(attempted=False,arm=Mock(),note_close=Mock())
            def arm(*args,**kwargs):holder.attempted=True
            holder.arm.side_effect=arm;trace.vfs=holder
            try:
                trace.profile(sys._getframe(),'c_call',connection.close)
                holder.arm.assert_not_called()
                trace.worker_native_tids.add(threading.get_native_id())
                trace.profile(sys._getframe(),'c_call',connection.close)
                holder.arm.assert_not_called()
                trace.cancel_returned=True
                with patch.object(trace,'wal_metadata',return_value={'exists':False,'bytes':0}):
                    trace.profile(sys._getframe(),'c_call',connection.close)
                holder.arm.assert_not_called()
                trace.profile(sys._getframe(),'c_call',connection.close)
                trace.profile(sys._getframe(),'c_call',connection.close)
                holder.arm.assert_called_once()
                self.assertEqual(holder.arm.call_args.args[0],root/'state.sqlite3-wal')
            finally:
                connection.close();trace.stream.close()

    def test_close_interval_excludes_preclose_arm_and_trace_write_work(self):
        import sqlite3
        import threading
        from scripts.v3_windows_reliability import Trace
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);connection=sqlite3.connect(root/'state.sqlite3')
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE probe(value TEXT)');connection.commit()
            trace=Trace(root,native=True);trace.case=types.SimpleNamespace(root=root)
            order=[]
            holder=types.SimpleNamespace(attempted=False)
            def arm(*args,**kwargs):holder.attempted=True;order.append('arm')
            holder.arm=arm;holder.note_close=lambda event,*args,**kwargs:order.append(event)
            trace.vfs=holder;trace.cancel_returned=True
            trace.worker_native_tids.add(threading.get_native_id())
            try:
                with patch.object(trace,'record',side_effect=lambda *args,**kwargs:order.append('trace-write')):
                    trace.profile(sys._getframe(),'c_call',connection.close)
                    trace.profile(sys._getframe(),'c_return',connection.close)
                self.assertEqual(order,['arm','trace-write','c_call','c_return','trace-write'])
            finally:
                connection.close();trace.stream.close()

    def test_winapi_holder_and_delete_access_probe_never_modify_or_delete_the_file(self):
        from scripts.v3_windows_reliability import WindowsFileHandles
        kernel=types.SimpleNamespace(CreateFileW=Mock(side_effect=[123, -1]),
            CloseHandle=Mock(return_value=1),QueryPerformanceCounter=Mock())
        api=WindowsFileHandles(kernel=kernel,last_error=lambda:32)
        self.assertEqual(api.open(Path('owned-wal')), (123,0))
        self.assertEqual(api.open(Path('owned-wal'),probe=True), (None,32))
        self.assertEqual([call.args[1:6] for call in kernel.CreateFileW.call_args_list],
                         [(0x80000000,3,None,3,0x80),(0x10000,7,None,3,0x80)])
        api.close(123)
        kernel.CloseHandle.assert_called_once_with(123)

    def control(self,root,mode,api):
        from scripts.v3_windows_reliability import WalSharingControl
        trace=types.SimpleNamespace(output=root,record=Mock())
        return WalSharingControl(trace,mode,api=api),trace

    def test_wal_and_unrelated_file_controls_match_resource_flags_lifetime_and_cleanup(self):
        class API:
            def __init__(self):self.opened=[];self.closed=[]
            def open(self,path,probe=False):
                self.opened.append((Path(path),probe))
                return (None,32) if probe else (123,0)
            def close(self,handle):self.closed.append(handle)
            def qpc(self):return time.perf_counter_ns()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            wal=root/'state.sqlite3-wal';wal.write_bytes(b'x'*100)
            for mode in ('wal','control-file'):
                case=root/mode;case.mkdir();api=API()
                control,trace=self.control(case,mode,api)
                with patch('scripts.v3_windows_reliability.sqlite3.connect',
                           side_effect=AssertionError('raw handle created an SQLite participant')), \
                     patch('scripts.v3_windows_reliability.faulthandler.dump_traceback'), \
                     patch('scripts.v3_windows_reliability.time.sleep',side_effect=AssertionError('post-close sleep')):
                    control.arm(wal,connection_id=7,frame=sys._getframe())
                    control.close()
                evidence=json.loads((case/'vfs-control.json').read_text())
                target=wal if mode=='wal' else case/'vfs-control.bin'
                self.assertEqual(api.opened,[(target,False),(target,True),(target,True)])
                self.assertEqual(api.closed,[123])
                self.assertTrue(evidence['attempted'] and evidence['armed'] and evidence['released'])
                self.assertTrue(evidence['cleanup_complete'])
                self.assertEqual(evidence['timer'],'perf_counter_ns')
                self.assertGreaterEqual(evidence['release_call_performance_counter_ns']-
                                        evidence['arm_performance_counter_ns'],800_000_000)
                self.assertEqual(evidence['hold_seconds'],.8)
                self.assertEqual(evidence['share_mode'],3)
                self.assertEqual(evidence['desired_access'],0x80000000)
                self.assertTrue(all(row['error']==32 for row in evidence['delete_access_probes']))
                self.assertFalse(control.thread.is_alive())
                control.close()  # resource cleanup is idempotent
                self.assertEqual(api.closed,[123])

    def test_arm_failure_is_retained_and_never_claims_a_qualifying_condition(self):
        api=types.SimpleNamespace(open=lambda *args,**kwargs:(None,32),close=Mock(),qpc=lambda:1)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);wal=root/'state.sqlite3-wal';wal.write_bytes(b'x'*100)
            control,_=self.control(root,'wal',api)
            control.arm(wal,connection_id=7,frame=sys._getframe())
            with self.assertRaisesRegex(RuntimeError,'VFS control'):
                control.close()
            evidence=json.loads((root/'vfs-control.json').read_text())
            self.assertTrue(evidence['attempted'])
            self.assertFalse(evidence['armed'])
            self.assertIn('32',evidence['error'])
            api.close.assert_not_called()

    def test_clock_failure_cannot_skip_owned_handle_cleanup_or_claim_clean_evidence(self):
        api=types.SimpleNamespace(open=Mock(),close=Mock(),qpc=Mock(side_effect=OSError('clock unavailable')))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);control,_=self.control(root,'wal',api)
            control.handle=123
            control.release('cleanup_retry')
            with self.assertRaises(RuntimeError):control.close()
            api.close.assert_called_once_with(123)
            evidence=json.loads((root/'vfs-control.json').read_text())
            self.assertTrue(evidence['cleanup_complete'])
            self.assertIn('clock unavailable',evidence['error'])

    def test_failed_holder_and_probe_closes_retain_ownership_for_cleanup_retry(self):
        closed=[]
        def close(handle):
            closed.append(handle)
            if closed.count(handle)==1:raise OSError('close refused')
        api=types.SimpleNamespace(open=lambda path,probe=False:(999,0) if probe else (123,0),
                                 close=close,qpc=time.perf_counter_ns)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);wal=root/'state.sqlite3-wal';wal.write_bytes(b'x'*100)
            control,_=self.control(root,'wal',api)
            control.arm(wal,connection_id=7,frame=sys._getframe())
            self.assertEqual(control.handle,123)
            self.assertEqual(control.probe_handles,{999})
            with self.assertRaises(RuntimeError):control.close()
            self.assertEqual(closed,[999,123,123,999])
            evidence=json.loads((root/'vfs-control.json').read_text())
            self.assertTrue(evidence['cleanup_complete'])
            self.assertIsNotNone(evidence['error'])

    def test_live_holder_thread_cannot_be_falsely_certified_or_double_closed(self):
        api=types.SimpleNamespace(close=Mock(),qpc=time.perf_counter_ns)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);control,_=self.control(root,'wal',api)
            control.handle=123
            control.thread=types.SimpleNamespace(join=Mock(),is_alive=lambda:True)
            with self.assertRaises(RuntimeError):control.close()
            control.thread.join.assert_called_once_with(timeout=2)
            api.close.assert_not_called()
            self.assertFalse(json.loads((root/'vfs-control.json').read_text())['cleanup_complete'])


class NativeWaitDiagnosticTests(unittest.TestCase):
    @staticmethod
    def exported_profile():
        # The actual GeneralProfile+FileIO export does not list Filename.
        return '''<WindowsPerformanceRecorder Version="1.0"><Profiles>
<SystemCollector Id="exported" Name="Exported"><BufferSize Value="1024"/><Buffers Value="40"/></SystemCollector>
<SystemProvider Id="kernel"><Keywords>
<Keyword Value="ProcessThread"/><Keyword Value="Loader"/><Keyword Value="ThreadPriority"/>
<Keyword Value="CSwitch"/><Keyword Value="ReadyThread"/><Keyword Value="DiskIO"/>
<Keyword Value="DiskIOInit"/><Keyword Value="FileIO"/><Keyword Value="FileIOInit"/>
<Keyword Value="SampledProfile"/><Keyword Value="Registry"/><Keyword Value="Power"/>
</Keywords><Stacks><Stack Value="CSwitch"/></Stacks></SystemProvider>
<EventProvider Id="noise" Name="UnrelatedProvider"/>
<Profile Id="Exported.Verbose.File" Name="Exported" DetailLevel="Verbose" LoggingMode="File" Description="Exported">
<Collectors><SystemCollectorId Value="exported"><SystemProviderId Value="kernel"/></SystemCollectorId></Collectors>
</Profile></Profiles></WindowsPerformanceRecorder>'''

    def test_native_snapshot_never_opens_a_database_until_owned_futures_are_done(self):
        import sqlite3
        from contextlib import closing
        from scripts.v3_windows_reliability import Trace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with closing(sqlite3.connect(root / 'state.sqlite3')) as connection, connection:
                connection.execute('CREATE TABLE events(event_type TEXT, payload TEXT)')
                connection.execute('INSERT INTO events VALUES(?,?)',
                                   ('subagent.state', json.dumps({'run_id':'owned','status':'cancelled'})))
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')
            (root / 'marker.txt').write_text('ok')
            trace = Trace(root, native=True)
            done = False
            trace.case = types.SimpleNamespace(root=root)
            trace.coordinator = types.SimpleNamespace(runs={'owned': {'status':'cancelled'}},
                futures={'owned':types.SimpleNamespace(done=lambda: done)},
                cancelled={'owned':types.SimpleNamespace(is_set=lambda: True)})
            try:
                with patch('scripts.v3_windows_reliability.sqlite3.connect',
                           side_effect=AssertionError('measurement opened an implicit WAL client')):
                    trace.snapshot('pending.json')
                pending = json.loads((root / 'pending.json').read_text())
                self.assertTrue(pending['database_snapshot_deferred'])
                self.assertEqual(pending['persisted_states'], [])
                done = True
                trace.snapshot('complete.json')
                self.assertEqual(json.loads((root / 'complete.json').read_text())['persisted_states'][0]['status'],
                                 'cancelled')
            finally:
                trace.stream.close()

    def test_native_trace_records_joinable_identity_and_real_wal_close_metadata(self):
        import sqlite3
        from scripts.v3_windows_reliability import Trace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = sqlite3.connect(root / 'state.sqlite3')
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE probe(value TEXT)')
            connection.commit()
            trace = Trace(root, native=True)
            trace.case = types.SimpleNamespace(root=root)
            frame = types.SimpleNamespace(f_code=types.SimpleNamespace(co_qualname='owned.close'))
            try:
                trace.profile(frame, 'c_call', connection.close)
                connection.close()
                trace.profile(frame, 'c_return', connection.close)
            finally:
                trace.stream.close()
            events = [json.loads(line) for line in (root / 'events.jsonl').read_text().splitlines()]
            before, after = [row for row in events if row['event'].startswith('sqlite.close.')]
            self.assertTrue(before['wal']['exists'])
            self.assertGreater(before['wal']['bytes'], 32)
            self.assertFalse(after['wal']['exists'])
            self.assertEqual(before['connection_id'], after['connection_id'])
            self.assertEqual(before['pid'], os.getpid())
            self.assertGreater(before['native_tid'], 0)
            self.assertEqual(events[0]['event'], 'clock.mapping')
            self.assertIn('performance_counter_ns', events[0])

    def test_missing_native_tools_blocks_without_starting_or_cancelling_any_recording(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(diagnostic.platform, 'system', return_value='Windows'), \
             patch.object(diagnostic.shutil, 'which', return_value=None), \
             patch.object(diagnostic.subprocess, 'run') as run:
            capture = diagnostic.NativeCapture(Path(directory), {})
            with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                capture.preflight()
            capture.stop()
            run.assert_not_called()

    def test_native_preflight_checks_exported_wait_and_io_profiles_with_windows_xml_encoding(self):
        from scripts import v3_windows_reliability as diagnostic
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            if '-exportprofile' in command:
                Path(command[-2]).write_text(self.exported_profile(), encoding='utf-16')
            text = ('KyrozenR260' if '-profiles' in command and len(command)==3 else
                    'GeneralProfile FileIO' if '-profiles' in command else '-instancename')
            return subprocess.CompletedProcess(command, 0, text, '')
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(diagnostic.platform, 'system', return_value='Windows'), \
             patch.object(diagnostic.shutil, 'which', side_effect=lambda name:name), \
             patch.object(diagnostic.subprocess, 'run', side_effect=run):
            capture = diagnostic.NativeCapture(Path(directory), {})
            metadata = capture.preflight()
            import xml.etree.ElementTree as ET
            selected = Path(directory)/'selected-profile.wprp'
            document = ET.parse(selected).getroot()
            self.assertEqual({node.get('Value') for node in document.findall('.//SystemProvider/Keywords/Keyword')},
                {'ProcessThread','Loader','ThreadPriority','CSwitch','ReadyThread','DiskIO',
                 'DiskIOInit','FileIO','FileIOInit','Filename'})
            self.assertTrue(all(node.get('Strict')=='true' for node in document.findall('.//Keyword')))
            self.assertEqual(len(document.findall('.//SystemCollector')), 1)
            self.assertEqual(document.find('.//SystemCollector/BufferSize').get('Value'), '64')
            self.assertEqual(document.find('.//SystemCollector/Buffers').get('Value'), '40')
            self.assertFalse(any(document.findall('.//'+tag) for tag in ('Stacks','EventProvider','EventCollector')))
            self.assertEqual({node.get('LoggingMode') for node in document.findall('./Profiles/Profile')}, {'File','Memory'})
            self.assertEqual(metadata['sha256'], hashlib.sha256(selected.read_bytes()).hexdigest())
            self.assertEqual(commands[-1], ['wpr.exe','-profiles',str(selected)])
            self.assertEqual(len(commands), 4)
            self.assertFalse(any('-start' in command or '-cancel' in command for command in commands))
            selected.write_text('changed after preflight')
            with self.assertRaises(RuntimeError):capture.start(Path(directory))
            self.assertEqual(len(commands),4)

    def test_custom_profile_rejection_or_missing_system_keyword_blocks_without_fallback(self):
        from scripts import v3_windows_reliability as diagnostic
        for mode in ('missing-keyword','rejected-profile'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory, \
                 patch.object(diagnostic.platform,'system',return_value='Windows'), \
                 patch.object(diagnostic.shutil,'which',side_effect=lambda name:name):
                commands=[]
                def run(command,**kwargs):
                    commands.append(command)
                    if '-exportprofile' in command:
                        text=self.exported_profile()
                        if mode=='missing-keyword':text=text.replace('<Keyword Value="CSwitch"/>','')
                        Path(command[-2]).write_text(text)
                    custom='-profiles' in command and len(command)==3
                    text='KyrozenR260' if custom else 'GeneralProfile FileIO -instancename'
                    return subprocess.CompletedProcess(command,5 if custom else 0,text,'')
                capture=diagnostic.NativeCapture(Path(directory),{})
                with patch.object(diagnostic.subprocess,'run',side_effect=run):
                    with self.assertRaises(RuntimeError):capture.preflight()
                    with self.assertRaises(RuntimeError):capture.start(Path(directory))
                self.assertFalse(capture.active)
                self.assertFalse(any('-start' in command or '-cancel' in command for command in commands))

    def test_capture_cannot_start_without_its_unchanged_preflighted_custom_profile(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(diagnostic.shutil,'which',side_effect=lambda name:name), \
             patch.object(diagnostic.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'','')) as run:
            capture=diagnostic.NativeCapture(Path(directory),{})
            with self.assertRaises(RuntimeError):capture.start(Path(directory))
            run.assert_not_called()

    def test_native_workflow_preserves_existing_controls_and_adds_a_pinned_separate_old_checkout(self):
        import yaml
        workflow = yaml.safe_load((ROOT/'.github/workflows/v3-windows-reliability.yml').read_text())
        dispatch = workflow.get('on', workflow.get(True))['workflow_dispatch']['inputs']
        self.assertEqual(dispatch['control']['options'],
                         ['baseline','cpu','sqlite','combined','paired-anchor','native-wait','native-vfs','all'])
        steps = workflow['jobs']['cancellation']['steps']
        old = next(step for step in steps if step.get('with', {}).get('path')=='historical-product')
        self.assertEqual(old['with']['ref'], '0f172f9fe692ff8fafcc0a15a72fe51eaf67e977')
        self.assertIn("'native-vfs'",old['if'])
        validation=next(step['run'] for step in steps if step.get('name')=='Validate exact candidate identity')
        self.assertIn('native-vfs',validation)
        trace = next(step['run'] for step in steps if step.get('name','').startswith('Trace unchanged'))
        self.assertIn('--cpu-workers 2', trace)
        self.assertIn("'--historical-repository', 'historical-product'", trace)

    def test_native_capture_stops_only_its_owned_instance_even_when_start_is_uncertain(self):
        from scripts import v3_windows_reliability as diagnostic
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            if '-exportprofile' in command:
                Path(command[-2]).write_text(self.exported_profile())
            if '-profiles' in command or '-help' in command:
                return subprocess.CompletedProcess(command,0,
                    'KyrozenR260' if len(command)==3 and '-profiles' in command else
                    'GeneralProfile FileIO -instancename','')
            if '-start' in command:
                raise subprocess.TimeoutExpired(command, 10)
            if '-status' in command and 'collectors' not in command:
                return subprocess.CompletedProcess(command, 0xc5583000, 'There are no trace profiles running.', '')
            return subprocess.CompletedProcess(command, 0, 'Events Lost : 0', '')
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(diagnostic.shutil, 'which', side_effect=lambda name:name), \
             patch.object(diagnostic.subprocess, 'run', side_effect=run):
            capture = diagnostic.NativeCapture(Path(directory), {})
            with patch.object(diagnostic.platform,'system',return_value='Windows'):
                capture.preflight()
            commands.clear()
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    capture.start(Path(directory))
            finally:
                with self.assertRaisesRegex(RuntimeError, 'ETL'):
                    capture.stop()
            self.assertTrue(any('-stop' in command for command in commands))
            self.assertFalse(any('-cancel' in command for command in commands))
            self.assertTrue(all(command[-2:] == ['-instancename',capture.instance] for command in commands))

    def test_failed_save_cancels_only_owned_recording_and_verifies_inactive_without_hiding_failure(self):
        from scripts import v3_windows_reliability as diagnostic
        commands = []
        def run(command, **kwargs):
            commands.append((command, kwargs['timeout']))
            if '-stop' in command:
                Path(command[2]).write_bytes(b'partial native trace')
                raise subprocess.TimeoutExpired(command, kwargs['timeout'])
            if '-status' in command and 'collectors' not in command:
                return subprocess.CompletedProcess(command, 0xc5583000, 'There are no trace profiles running.', '')
            return subprocess.CompletedProcess(command, 0, 'Events Lost : 0', '')
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(diagnostic.shutil, 'which', side_effect=lambda name:name), \
             patch.object(diagnostic.subprocess, 'run', side_effect=run):
            capture = diagnostic.NativeCapture(Path(directory), {})
            capture.case, capture.active = Path(directory), True
            with self.assertRaises(subprocess.TimeoutExpired):
                capture.stop()
            self.assertFalse(capture.active)
            self.assertFalse(capture.cleanup_uncertified)
            self.assertTrue(any('-cancel' in command for command, _ in commands))
            self.assertTrue(all(command[-2:]==['-instancename',capture.instance] for command, _ in commands))
            self.assertTrue(all(timeout<=30 for _, timeout in commands))
            self.assertTrue(any((Path(directory)/'traces').glob('*.etl')))
            self.assertTrue((Path(directory)/'wpr-stop.json').exists())

    def test_failed_cancel_or_unknown_inactive_response_remains_active_and_uncertified(self):
        from scripts import v3_windows_reliability as diagnostic
        for cancel_code, inactive_text in ((5,'WPR recording is in progress...'), (0,'unknown status format')):
            with self.subTest(cancel_code=cancel_code), tempfile.TemporaryDirectory() as directory, \
                 patch.object(diagnostic.shutil, 'which', side_effect=lambda name:name):
                commands = []
                def run(command, **kwargs):
                    commands.append(command)
                    if '-stop' in command:
                        return subprocess.CompletedProcess(command, 5, '', 'save failed')
                    if '-cancel' in command:
                        return subprocess.CompletedProcess(command, cancel_code, '', 'cancel output')
                    text = 'Events Lost : 0' if 'collectors' in command else inactive_text
                    return subprocess.CompletedProcess(command, 0, text, '')
                capture = diagnostic.NativeCapture(Path(directory), {})
                capture.case, capture.active = Path(directory), True
                with patch.object(diagnostic.subprocess, 'run', side_effect=run), \
                     self.assertRaisesRegex(RuntimeError, 'cleanup_uncertified'):
                    capture.stop()
                self.assertTrue(capture.active)
                self.assertTrue(capture.cleanup_uncertified)
                self.assertTrue(any('-cancel' in command for command in commands))
                self.assertTrue(all(command[-2:]==['-instancename',capture.instance] for command in commands))

    def test_failed_status_with_partial_zero_loss_still_saves_but_never_qualifies_capture(self):
        from scripts import v3_windows_reliability as diagnostic
        def run(command, **kwargs):
            if '-stop' in command:
                Path(command[2]).write_bytes(b'unit-test trace stub')
            if '-status' in command:
                if 'collectors' in command:
                    return subprocess.CompletedProcess(command, 5, 'Events Lost : 0', 'query failed')
                return subprocess.CompletedProcess(command, 0, 'WPR is not recording.', '')
            return subprocess.CompletedProcess(command, 0, '', '')
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(diagnostic.shutil, 'which', side_effect=lambda name:name), \
             patch.object(diagnostic.subprocess, 'run', side_effect=run):
            capture = diagnostic.NativeCapture(Path(directory), {})
            capture.case, capture.active = Path(directory), True
            with self.assertRaisesRegex(RuntimeError, 'wpr-status'):
                capture.stop()
            self.assertFalse(capture.active)
            self.assertTrue(any((Path(directory)/'traces').glob('*.etl')))

    def test_uncertified_retries_preserve_each_partial_trace_and_tool_attempt(self):
        from scripts import v3_windows_reliability as diagnostic
        attempts = 0
        def run(command, **kwargs):
            nonlocal attempts
            if '-stop' in command:
                attempts += 1
                Path(command[2]).write_bytes(f'partial {attempts}'.encode())
                return subprocess.CompletedProcess(command, 5, '', 'failed save')
            code = 5 if '-cancel' in command else 0
            return subprocess.CompletedProcess(command, code, 'Events Lost : 0', '')
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(diagnostic.shutil, 'which', side_effect=lambda name:name), \
             patch.object(diagnostic.subprocess, 'run', side_effect=run):
            root = Path(directory)
            capture = diagnostic.NativeCapture(root, {})
            capture.case, capture.active = root, True
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, 'cleanup_uncertified'):
                    capture.stop()
            self.assertEqual(sorted(path.read_bytes() for path in (root/'traces').glob('*.etl')),
                             [b'partial 1',b'partial 2'])
            self.assertEqual(len(list(root.glob('wpr-stop*.json'))), 2)
            self.assertTrue(capture.active and capture.cleanup_uncertified)

    def comparison(self, failure=False, *, abort_reason=None, launch_error=False, saved_trace_bytes=None,
                   vfs=False, unarmed=False, infrastructure_timeout=False):
        from scripts import v3_windows_reliability as diagnostic
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name)
        old, current = output / 'old', output / 'current'
        commands = []
        activity = {'tools': [], 'workload_recording': []}
        recording = False
        capture = diagnostic.NativeCapture(output, {})
        capture.wpr, capture.decoder = 'wpr.exe', 'tracerpt.exe'

        def native_tool(command, **kwargs):
            nonlocal recording
            activity['tools'].append(command)
            if '-exportprofile' in command:
                Path(command[-2]).write_text(self.exported_profile())
            if '-profiles' in command or '-help' in command:
                return subprocess.CompletedProcess(command,0,
                    'KyrozenR260' if len(command)==3 and '-profiles' in command else
                    'GeneralProfile FileIO -instancename','')
            if '-start' in command:
                self.assertFalse(recording, 'the owned recording was already active')
                recording = True
                temporary_trace = Path(command[command.index('-recordtempto')+1]) / 'recording.etl'
                # Model the real runner's roughly 120 MiB per-recording allocation.
                with temporary_trace.open('wb') as stream:
                    stream.truncate(120*1024*1024)
            elif '-stop' in command:
                activity['checkpoint_at_stop'] = json.loads((output/'report.json').read_text())
                if saved_trace_bytes is not None:
                    with (output/'traces/temporary/recording.etl').open('r+b') as stream:
                        stream.truncate(saved_trace_bytes)
                (output/'traces/temporary/recording.etl').replace(command[2])
                recording = False
            elif '-of' in command:
                Path(command[command.index('-o')+1]).write_text('<Events/>')
                Path(command[command.index('-summary')+1]).write_text('synthetic decoder output')
            text = ('Events Lost : 0' if 'collectors' in command else 'WPR is not recording.')
            return subprocess.CompletedProcess(command, 0, text, '')

        def run(command, log_path, env, **kwargs):
            commands.append((command, kwargs))
            activity['workload_recording'].append(recording)
            if launch_error:
                raise OSError('child launch failed')
            reason = abort_reason or kwargs['abort']()
            if reason:
                return {'returncode':124, 'timeout_seconds':45, 'timeout_cleanup':None,
                        'abort_reason':reason}
            if '--child' in command:
                case = Path(command[command.index('--output')+1])
                bad = failure and len(commands)==1
                (case/'unittest.log').write_text('TimeoutError' if bad else 'OK')
                (case/'events.jsonl').write_text('\n'.join(json.dumps(row) for row in [
                    {'event':'cancel.return','monotonic_ns':1,'thread_id':1},
                    {'event':'future_wait.call','monotonic_ns':1,'thread_id':1},
                    {'event':'future_wait.return','monotonic_ns':5_000_000_001 if bad else 200_000_001,'thread_id':1}]))
                if '--vfs-target' in command:
                    mode=command[command.index('--vfs-target')+1]
                    write={'mode':mode,'hold_seconds':.8,'desired_access':0x80000000,'share_mode':3,
                           'creation_disposition':3,'file_flags':0x80,'release_reason':'fixed_deadline',
                           'attempted':not unarmed,'armed':not unarmed,'released':not unarmed,
                           'cleanup_complete':True,'error':None,'arm_monotonic_ns':1,
                           'release_monotonic_ns':800_000_001,'close_interval_ns':10,
                           'release_call_monotonic_ns':800_000_001,
                           'timer':'perf_counter_ns','arm_performance_counter_ns':1,
                           'release_call_performance_counter_ns':800_000_001,
                           'close_call_monotonic_ns':2,'close_return_monotonic_ns':12,
                           'close_event':'c_return','worker_pid':1,'worker_native_tid':1,
                           'arm_qpc':1,'release_qpc':800_000_001,'wal':str(case/'state.sqlite3-wal'),
                           'target':str(case/'state.sqlite3-wal' if mode=='wal' else case/'vfs-control.bin')}
                    (case/'vfs-control.json').write_text(json.dumps(write))
                if infrastructure_timeout and len(commands)==1:
                    return {'returncode':124,'timeout_seconds':45,'abort_reason':None,
                            'timeout_cleanup':{'descendant_cleanup_certified':False}}
                return {'returncode': int(bad), 'timeout_seconds':45, 'timeout_cleanup':None}
            return {'returncode':0, 'timeout_seconds':45, 'timeout_cleanup':None}
        with patch.object(diagnostic, 'NativeCapture', return_value=capture), \
             patch.object(diagnostic.platform, 'system', return_value='Windows'), \
             patch.object(diagnostic.subprocess, 'run', side_effect=native_tool), \
             patch.object(diagnostic, '_product_identity', side_effect=lambda path: {
                 'candidate_sha':diagnostic.HISTORICAL_PRODUCT if path==old else 'c'*40,
                 'cancel_fixture_ast_sha256':'same-original-fixture'}), \
             patch.object(diagnostic, 'historical_environment', return_value={'status':'MATCH'}), \
             patch.object(diagnostic, 'run_process', side_effect=run):
            result = diagnostic.run_native_comparison(output, current, old, **({'vfs':True} if vfs else {}))
        return result, json.loads((output/'report.json').read_text()), commands, old, current, activity

    def test_native_vfs_preserves_exact_six_cells_original_limits_reviewers_and_inconclusive_boundary(self):
        result,report,commands,old,current,_=self.comparison(vfs=True)
        self.assertEqual(result,0)
        self.assertEqual(report['status'],'INCONCLUSIVE')
        self.assertFalse(report['historical_cause_established'])
        self.assertEqual(report['active_limit_seconds'],300)
        self.assertEqual(report['child_limit_seconds'],45)
        self.assertEqual(report['test_timeouts_seconds'],{'provider_wait':3,'entry_wait':10,'future_wait':5})
        self.assertEqual([(row['vfs_target'],row['variant']) for row in report['iterations']],
            [('wal','baseline'),('wal','anchor'),('wal','current'),
             ('control-file','current'),('control-file','anchor'),('control-file','baseline')])
        self.assertEqual(len(commands),12)
        for command,kwargs in commands[::2]:
            self.assertIn('--native-trace',command)
            self.assertIn('--vfs-target',command)
            self.assertEqual(command[command.index('--control')+1],'cpu')
            self.assertEqual(command[command.index('--cpu-workers')+1],'2')
            self.assertIn('abort',kwargs)
        self.assertTrue(all(command[1]==str(old/'scripts/subagent_workflow_acceptance.py')
                            for command,_ in commands[1::2]))
        self.assertTrue(all(row['vfs_condition_valid'] for row in report['iterations']))

    def test_native_vfs_failure_keeps_all_six_cells_but_unarmed_condition_is_blocked(self):
        result,report,commands,*_=self.comparison(vfs=True,failure=True)
        self.assertEqual(result,1)
        self.assertEqual(len(report['iterations']),6)
        self.assertEqual(len(commands),12)
        self.assertEqual(report['status'],'PENDING_NATIVE_ANALYSIS')
        result,report,*_=self.comparison(vfs=True,unarmed=True)
        self.assertEqual(result,2)
        self.assertEqual(report['status'],'BLOCKED')
        self.assertFalse(report['historical_cause_established'])

    def test_native_vfs_uncertified_timeout_stops_before_reviewer_or_any_later_cell_and_saves_trace(self):
        result,report,commands,_,_,activity=self.comparison(vfs=True,infrastructure_timeout=True)
        self.assertEqual(result,2)
        self.assertEqual(len(commands),1)
        self.assertEqual(len(report['iterations']),1)
        self.assertEqual(report['status'],'BLOCKED')
        self.assertTrue(report['cleanup_uncertified'])
        self.assertFalse(any(name.startswith('reviewer-') for name in report['execution_order']))
        self.assertEqual(sum('-start' in command for command in activity['tools']),1)
        self.assertEqual(sum('-stop' in command for command in activity['tools']),1)
        self.assertEqual(report['capture']['status'],'CAPTURED')
        self.assertTrue(activity['checkpoint_at_stop']['cleanup_uncertified'])
        self.assertEqual(activity['checkpoint_at_stop']['status'],'BLOCKED')
        self.assertTrue(activity['checkpoint_at_stop']['iterations'][0]['infrastructure_failure'])

    def test_native_comparison_keeps_identical_cpu_controls_and_pinned_reviewers_and_all_fast_is_inconclusive(self):
        result, report, commands, old, current, _ = self.comparison()
        self.assertEqual(result, 0)
        self.assertEqual(report['status'], 'INCONCLUSIVE')
        self.assertFalse(report['historical_cause_established'])
        self.assertEqual(report['active_limit_seconds'], 300)
        self.assertEqual(report['trace_limit_bytes'], 512*1024*1024)
        self.assertEqual([row['variant'] for row in report['iterations']],
            ['baseline','anchor','current','current','anchor','baseline',
             'anchor','baseline','current','current','baseline','anchor'])
        self.assertEqual(len(commands), 24)
        for command, kwargs in commands[::2]:
            self.assertIn('--native-trace', command)
            self.assertEqual(command[command.index('--control')+1], 'cpu')
            self.assertEqual(command[command.index('--cpu-workers')+1], '2')
            self.assertIn('abort', kwargs)
        for command, _ in commands[1::2]:
            self.assertEqual(command[1], str(old/'scripts/subagent_workflow_acceptance.py'))
        cancellations = [command for command, _ in commands[::2]]
        self.assertEqual(['--readonly-anchor' in command for command in cancellations],
                         [row['variant']=='anchor' for row in report['iterations']])

    def test_native_failure_keeps_the_whole_control_triple_and_never_claims_attribution(self):
        result, report, commands, _, _, _ = self.comparison(failure=True)
        self.assertEqual(result, 1)
        self.assertEqual(len(commands), 6)
        self.assertEqual(report['status'], 'PENDING_NATIVE_ANALYSIS')
        self.assertFalse(report['historical_cause_established'])
        self.assertEqual(report['iterations'][0]['future_wait_seconds'], 5)

    def test_one_owned_recording_covers_all_cells_and_reviewers_with_one_shared_digest(self):
        result, report, commands, _, _, activity = self.comparison()
        self.assertEqual(result, 0)
        self.assertEqual(len(commands), 24)
        self.assertEqual(report['capture_scope'], 'experiment')
        selected=Path(report['capture_profile']['path'])
        self.assertEqual(report['capture_profile']['sha256'],hashlib.sha256(selected.read_bytes()).hexdigest())
        start=next(command for command in activity['tools'] if '-start' in command)
        self.assertEqual(start[2],str(selected)+'!KyrozenR260')
        self.assertEqual(start.count('-start'),1)
        self.assertEqual(report['capture']['bytes'], 120*1024*1024)
        self.assertTrue(all(activity['workload_recording']))
        self.assertEqual(sum('-start' in command for command in activity['tools']), 1)
        self.assertEqual(sum('-stop' in command for command in activity['tools']), 1)
        self.assertEqual(sum('-of' in command for command in activity['tools']), 1)
        self.assertEqual(len(list((Path(report['capture']['etl']).parent).glob('*.etl'))), 1)
        for row in report['iterations']:
            self.assertEqual(row['capture'], report['capture'])
            self.assertEqual(Path(row['native_events']).name, 'events.jsonl')
            self.assertTrue(Path(row['native_events']).is_file())

    def test_child_launch_failure_still_saves_the_shared_owned_recording(self):
        result, report, commands, _, _, activity = self.comparison(launch_error=True)
        self.assertEqual(result, 2)
        self.assertEqual(report['status'], 'BLOCKED')
        self.assertIn('child launch failed', report['error'])
        self.assertFalse(report['cleanup_uncertified'])
        self.assertFalse(report['historical_cause_established'])
        self.assertEqual(len(commands), 1)
        self.assertEqual(sum('-stop' in command for command in activity['tools']), 1)
        self.assertIn('capture', report)
        self.assertEqual(report['iterations'][0]['capture'], report['capture'])
        self.assertTrue(Path(report['capture']['etl']).is_file())

    def test_size_abort_preserves_the_shared_trace_and_original_limits_without_attribution(self):
        result, report, commands, _, _, activity = self.comparison(abort_reason='trace_size_limit')
        self.assertNotEqual(result, 0)
        self.assertEqual(report['limit_reached'], 'trace_size_limit')
        self.assertEqual(report['active_limit_seconds'], 300)
        self.assertEqual(report['trace_limit_bytes'], 512*1024*1024)
        self.assertEqual(report['iterations'][0]['timeout_seconds'], 45)
        self.assertFalse(report['historical_cause_established'])
        self.assertEqual(len(commands), 1)
        self.assertEqual(sum('-stop' in command for command in activity['tools']), 1)
        self.assertIn('capture', report)
        self.assertEqual(report['iterations'][0]['capture'], report['capture'])

    def test_trace_growth_during_final_save_is_reported_against_the_same_size_limit(self):
        result, report, commands, _, _, _ = self.comparison(saved_trace_bytes=536870913)
        self.assertEqual(report.get('limit_reached'), 'trace_size_limit')
        self.assertNotEqual(result, 0)
        self.assertEqual(len(commands), 24)
        self.assertEqual(report['status'], 'INCONCLUSIVE')
        self.assertFalse(report['historical_cause_established'])
        self.assertEqual(report['capture']['bytes'], 536870913)

    def test_native_time_and_trace_size_limits_are_explicit_abort_reasons(self):
        from scripts import v3_windows_reliability as diagnostic
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(diagnostic.native_limit(root, time.monotonic()-1), 'active_time_limit')
            with (root/'owned.etl').open('wb') as stream:
                stream.truncate(512*1024*1024+1)
            self.assertEqual(diagnostic.native_limit(root, time.monotonic()+300), 'trace_size_limit')
            with patch.object(Path, 'rglob', side_effect=PermissionError('unreadable trace inventory')):
                self.assertEqual(diagnostic.native_limit(root, time.monotonic()+300), 'trace_inventory_error')

    def test_native_child_runs_the_actual_fixture_without_a_watchdog_database_client(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable,str(SCRIPT),'--child','--output',directory,
                '--repository',str(ROOT),'--native-trace'], cwd=ROOT, capture_output=True, text=True, timeout=35)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            events = [json.loads(row) for row in (Path(directory)/'events.jsonl').read_text().splitlines()]
            self.assertEqual(events[0]['event'], 'clock.mapping')
            self.assertTrue(any(row['event']=='product.source' for row in events))
            self.assertTrue(any(row['event']=='sqlite.close.c_call' and 'wal' in row for row in events))
            state = json.loads((Path(directory)/'state-before-cleanup.json').read_text())
            self.assertFalse(state['database_snapshot_deferred'])
            self.assertEqual(state['persisted_states'][-1]['status'], 'cancelled')

    @unittest.skipUnless(os.name == 'posix', 'real owned child abort fixture requires POSIX')
    def test_campaign_abort_preserves_45_second_child_bound_and_cleans_the_owned_process(self):
        from scripts.v3_windows_reliability import run_process
        with tempfile.TemporaryDirectory() as directory:
            attempts = iter((None, 'active_time_limit'))
            result = run_process([sys.executable,'-c','import time; time.sleep(20)'],
                Path(directory)/'child.log', dict(os.environ), abort=lambda:next(attempts))
            self.assertEqual(result['timeout_seconds'], 45)
            self.assertEqual(result['abort_reason'], 'active_time_limit')
            self.assertEqual(result['returncode'], 124)
            self.assertIsNotNone(result['timeout_cleanup']['parent_returncode'])
