#!/usr/bin/env python3
"""Trace the unchanged cancellation unittest; passing samples do not close R260."""
from __future__ import annotations

import argparse
from contextlib import closing
import faulthandler
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
TEST = 'test_cancel_during_provider_call_prevents_late_write'
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


_CPU_LOAD = '''
import json, math, sys, time
from pathlib import Path
ready, stop, result = map(Path, sys.argv[1:4])
started, cpu = time.monotonic(), time.process_time()
deadline = started + float(sys.argv[4])
ready.write_text('ready')
value = 0.5
while not stop.exists() and time.monotonic() < deadline:
    for _ in range(20000):
        value = math.sin(value) + 0.5
result.write_text(json.dumps({'cpu_seconds': time.process_time()-cpu,
    'wall_seconds': time.monotonic()-started,
    'stopped_by': 'parent' if stop.exists() else 'deadline'}))
'''


class LoadControl:
    """Bounded diagnostic load; never writes fixture data or changes timeouts."""

    def __init__(self, control, workers, case, trace):
        self.control, self.workers, self.case, self.trace = control, workers, case, trace
        self.stopped, self.sqlite_ready = threading.Event(), threading.Event()
        self.cpu = []
        self.sqlite_thread = None
        self.acquired_transactions = 0
        self.sqlite_fatal_error = None
        self.closed = False

    def start(self):
        if self.control in {'sqlite', 'combined'}:
            self.sqlite_thread = threading.Thread(target=self._sqlite_load,
                name='diagnostic-sqlite-control', daemon=True)
            self.sqlite_thread.start()
            if not self.sqlite_ready.wait(3):
                raise RuntimeError('SQLite load control did not acquire its first bounded transaction')
        if self.control in {'cpu', 'combined'}:
            for index in range(self.workers):
                prefix = self.trace.output / f'cpu-{index}-{uuid.uuid4().hex}'
                ready, stop, result = (Path(str(prefix) + suffix) for suffix in ('.ready', '.stop', '.json'))
                process = subprocess.Popen([sys.executable, '-c', _CPU_LOAD,
                    str(ready), str(stop), str(result), '30'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.cpu.append((process, stop, result))
                deadline = time.monotonic() + 3
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                if not ready.exists():
                    raise RuntimeError('CPU load control did not become ready')
        self.trace.record('load.started', control=self.control, cpu_pids=[item[0].pid for item in self.cpu])

    def _sqlite_load(self):
        try:
            with closing(sqlite3.connect(self.case.root / 'state.sqlite3', timeout=.05,
                                         isolation_level=None)) as connection:
                while not self.stopped.is_set():
                    acquired = False
                    started = time.monotonic_ns()
                    try:
                        connection.execute('BEGIN IMMEDIATE')
                        acquired = True
                        self.acquired_transactions += 1
                        self.trace.record('sqlite.lock_acquired', wait_ns=time.monotonic_ns()-started)
                        self.sqlite_ready.set()
                        self.stopped.wait(.025)
                    except sqlite3.Error as error:
                        self.trace.record('sqlite.lock_error', error_type=type(error).__name__,
                                          wait_ns=time.monotonic_ns()-started)
                    finally:
                        if acquired:
                            connection.execute('ROLLBACK')
                            self.trace.record('sqlite.lock_released')
                    self.stopped.wait(.01)
        except sqlite3.Error as error:
            self.sqlite_fatal_error = type(error).__name__
            self.trace.record('sqlite.control_error', error_type=type(error).__name__)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stopped.set()
        cpu = []
        for process, stop, result in self.cpu:
            stop.touch()
            forced = False
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                forced = True
                process.kill()
                process.wait(timeout=3)
            metrics = json.loads(result.read_text()) if result.is_file() else {}
            cpu.append({'pid': process.pid, 'returncode': process.returncode,
                        'forced_cleanup': forced, **metrics})
        if self.sqlite_thread is not None:
            self.sqlite_thread.join(timeout=2)
        sqlite_stopped = self.sqlite_thread is None or not self.sqlite_thread.is_alive()
        write_json(self.trace.output / 'load-control.json', {'control': self.control, 'cpu': cpu,
            'sqlite': {'acquired_transactions': self.acquired_transactions, 'thread_stopped': sqlite_stopped,
                       'fatal_error': self.sqlite_fatal_error}})
        self.trace.record('load.stopped', control=self.control, cpu=cpu, sqlite_thread_stopped=sqlite_stopped)
        if (not sqlite_stopped or self.sqlite_fatal_error
                or any(item['forced_cleanup'] or item['returncode'] != 0
                       or item.get('stopped_by') != 'parent' for item in cpu)):
            raise RuntimeError('Load-control cleanup did not complete normally; inspect diagnostic evidence')


def stop_diagnostic_tree(process):
    """Stop the still-owned diagnostic group before its ancestry disappears."""
    attempt = {'parent_pid': process.pid, 'descendant_cleanup_certified': False}
    if os.name == 'nt':
        result = subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                capture_output=True, timeout=5, check=False)
        attempt.update(method='taskkill_tree', command_returncode=result.returncode)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            attempt.update(method='owned_process_group', signal_sent=True)
        except ProcessLookupError:
            attempt.update(method='owned_process_group', signal_sent=False)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
    attempt['parent_returncode'] = process.returncode
    return attempt


class Trace:
    def __init__(self, output):
        self.output = output
        self.stream = (output / 'events.jsonl').open('w', encoding='utf-8')
        self.lock = threading.Lock()
        self.case = None
        self.coordinator = None
        self.active = True

    def record(self, event, **fields):
        with self.lock:
            if not self.active:
                return
            self.stream.write(json.dumps({'event': event, 'monotonic_ns': time.monotonic_ns(),
                'thread_id': threading.get_ident(), **fields}) + '\n')
            self.stream.flush()

    def profile(self, frame, event, arg):
        if event not in {'call', 'return'}:
            return
        module = frame.f_globals.get('__name__', '')
        name = frame.f_code.co_name
        label = None
        if module == 'test_delegation' and name == 'chat' and 'SlowProvider' in frame.f_code.co_qualname:
            label = 'provider'
        elif module == 'openkyrozen.agent.delegation':
            label = {'spawn': 'dispatch', 'cancel': 'cancel', '_work': 'worker',
                     '_publish': 'persist', '_invoke': 'invoke'}.get(name)
            if label:
                self.coordinator = frame.f_locals['self']
        elif module == 'threading' and name == 'set':
            caller = frame.f_back
            if caller and caller.f_code.co_name == TEST:
                if frame.f_locals['self'] is caller.f_locals.get('release'):
                    label = 'release'
        elif module == 'concurrent.futures._base' and name == 'result':
            label = 'future_wait'
        elif module == 'openkyrozen.app.container' and name == 'close':
            label = 'application_close'
        if label:
            self.record(label + '.' + event, function=frame.f_code.co_qualname)

    def snapshot(self, filename):
        """Avoid application locks and raw copies of live SQLite/WAL files."""
        state = {'monotonic_ns': time.monotonic_ns(), 'persisted_states': [], 'runs': []}
        case, coordinator = self.case, self.coordinator
        if coordinator:
            for run_id, run in list(coordinator.runs.items()):
                future = coordinator.futures.get(run_id)
                state['runs'].append({'run_id': run_id, 'status': run.get('status'),
                    'error': run.get('error'), 'future_done': future.done() if future else None,
                    'cancelled': coordinator.cancelled[run_id].is_set()})
        if case and hasattr(case, 'root'):
            try:
                state['marker'] = (case.root / 'marker.txt').read_text(encoding='utf-8')
                database = case.root / 'state.sqlite3'
                state['sqlite_files'] = {path.name: path.stat().st_size
                    for path in case.root.glob('state.sqlite3*') if path.is_file()}
                with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=0)) as connection:
                    rows = connection.execute("SELECT payload FROM events WHERE event_type='subagent.state' ORDER BY rowid").fetchall()
                for row in rows:
                    run = json.loads(row[0])
                    state['persisted_states'].append({key: run.get(key) for key in
                        ('run_id', 'status', 'version', 'error', 'progress')})
            except (OSError, sqlite3.Error) as exc:
                state['snapshot_error'] = repr(exc)
        write_json(self.output / filename, state)


def run_child(output, control='baseline', cpu_workers=2):
    # Import the fixture before starting the four-second cancellation watchdog.
    from test_delegation import DelegationTests

    output.mkdir(parents=True, exist_ok=True)
    trace = Trace(output)

    class DiagnosticCase(DelegationTests):
        def setUp(self):
            trace.case = self
            trace.record('setup.call')
            super().setUp()
            trace.record('setup.return')
            self.load_control = LoadControl(control, cpu_workers, self, trace)
            self.addCleanup(self.load_control.close)
            self.load_control.start()

        def tearDown(self):
            trace.record('cleanup.call')
            trace.snapshot('state-before-cleanup.json')
            try:
                self.load_control.close()
            finally:
                try:
                    super().tearDown()
                finally:
                    trace.record('cleanup.return')

    stopped = threading.Event()

    def watch():
        count = 0
        while not stopped.wait(4):
            count += 1
            trace.record('watchdog', interval_seconds=4)
            trace.snapshot(f'state-watchdog-{count}.json')
            if stopped.is_set():
                return
            stacks.write('Timeout (0:00:04)!\n')
            stacks.flush()
            # The C watchdog can spin walking profiled live frames on CPython 3.12.
            faulthandler.dump_traceback(file=stacks, all_threads=True)

    previous = sys.getprofile()
    previous_thread = threading.getprofile()
    with (output / 'threads.log').open('w', encoding='utf-8') as stacks:
        watcher = threading.Thread(target=watch, name='diagnostic-state-watchdog', daemon=True)
        watcher.start()
        sys.setprofile(trace.profile)
        threading.setprofile(trace.profile)
        try:
            with (output / 'unittest.log').open('w', encoding='utf-8') as log:
                result = unittest.TextTestRunner(stream=log, verbosity=2).run(DiagnosticCase(TEST))
            trace.record('result', successful=result.wasSuccessful(), failures=len(result.failures), errors=len(result.errors))
        finally:
            sys.setprofile(previous)
            threading.setprofile(previous_thread)
            stopped.set()
            watcher.join(timeout=1)
            with trace.lock:
                trace.active = False
                trace.stream.close()
    return 0 if result.wasSuccessful() else 1


def main():
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repository', type=Path, default=ROOT, help='Exact product checkout to test; harness may live separately')
    parser.add_argument('--iterations', type=int, default=20)
    parser.add_argument('--control', choices=('baseline', 'cpu', 'sqlite', 'combined'), default='baseline')
    parser.add_argument('--cpu-workers', type=int, default=2, help='Bounded owned CPU load processes (1–4)')
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error('--iterations must be positive')
    if not 1 <= args.cpu_workers <= 4:
        parser.error('--cpu-workers must be between 1 and 4')
    ROOT = args.repository.resolve()
    sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
    output = args.output.resolve()
    if args.child:
        return run_child(output, args.control, args.cpu_workers)
    output.mkdir(parents=True, exist_ok=True)
    revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    tracked_diff = subprocess.run(['git', 'diff', 'HEAD', '--'], cwd=ROOT, capture_output=True, check=True).stdout
    report = {'tracked_product_edits': bool(tracked_diff),
        'tracked_product_diff_sha256': hashlib.sha256(tracked_diff).hexdigest(), 'schema_version': 1, 'candidate_sha': revision, 'platform': platform.platform(),
        'python': sys.version, 'fixture': 'tests.test_delegation.DelegationTests.' + TEST,
        'fixture_sha256': hashlib.sha256((ROOT / 'tests/test_delegation.py').read_bytes()).hexdigest(),
        'diagnostic_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'test_timeouts_seconds': {'provider_wait': 3, 'entry_wait': 10, 'future_wait': 5},
        'watchdog_seconds': 4, 'historical_cause_established': False,
        'control': args.control, 'cpu_workers': args.cpu_workers if args.control in {'cpu', 'combined'} else 0,
        'qualification': 'Observational instrumentation changes scheduling; passing samples do not explain the historical timeout.',
        'iterations': []}
    write_json(output / 'report.json', report)
    for index in range(1, args.iterations + 1):
        directory = output / str(index)
        directory.mkdir(exist_ok=True)
        command = [sys.executable, str(Path(__file__).resolve()), '--child', '--output', str(directory),
                   '--repository', str(ROOT), '--control', args.control, '--cpu-workers', str(args.cpu_workers)]
        started = time.monotonic_ns()
        cleanup = None
        with (directory / 'process.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=os.name != 'nt',
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
            try:
                returncode = process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                try:
                    cleanup = stop_diagnostic_tree(process)
                except Exception as error:
                    cleanup = {'parent_pid': process.pid, 'error_type': type(error).__name__,
                               'descendant_cleanup_certified': False}
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=3)
                returncode = 124
        report['iterations'].append({'iteration': index, 'returncode': returncode,
            'elapsed_ns': time.monotonic_ns() - started, 'command': command,
            'timeout_cleanup': cleanup})
        write_json(output / 'report.json', report)
        print(f'Cancellation iteration {index}: exit={returncode}', flush=True)
    return int(any(item['returncode'] != 0 for item in report['iterations']))


if __name__ == '__main__':
    raise SystemExit(main())
