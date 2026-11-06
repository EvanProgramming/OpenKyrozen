#!/usr/bin/env python3
"""Trace the unchanged cancellation unittest; passing samples do not close R260."""
from __future__ import annotations

import argparse
from contextlib import closing
import faulthandler
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEST = 'test_cancel_during_provider_call_prevents_late_write'
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


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


def run_child(output):
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

        def tearDown(self):
            trace.record('cleanup.call')
            trace.snapshot('state-before-cleanup.json')
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

    previous = sys.getprofile()
    previous_thread = threading.getprofile()
    with (output / 'threads.log').open('w', encoding='utf-8') as stacks:
        faulthandler.dump_traceback_later(4, repeat=True, file=stacks)
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
            faulthandler.cancel_dump_traceback_later()
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
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error('--iterations must be positive')
    ROOT = args.repository.resolve()
    sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
    output = args.output.resolve()
    if args.child:
        return run_child(output)
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
        'qualification': 'Observational instrumentation changes scheduling; passing samples do not explain the historical timeout.',
        'iterations': []}
    write_json(output / 'report.json', report)
    for index in range(1, args.iterations + 1):
        directory = output / str(index)
        directory.mkdir(exist_ok=True)
        command = [sys.executable, str(Path(__file__).resolve()), '--child', '--output', str(directory), '--repository', str(ROOT)]
        started = time.monotonic_ns()
        with (directory / 'process.log').open('w', encoding='utf-8') as log:
            try:
                process = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=45)
                returncode = process.returncode
            except subprocess.TimeoutExpired:
                returncode = 124
        report['iterations'].append({'iteration': index, 'returncode': returncode,
            'elapsed_ns': time.monotonic_ns() - started, 'command': command})
        write_json(output / 'report.json', report)
        print(f'Cancellation iteration {index}: exit={returncode}', flush=True)
    return int(any(item['returncode'] != 0 for item in report['iterations']))


if __name__ == '__main__':
    raise SystemExit(main())
