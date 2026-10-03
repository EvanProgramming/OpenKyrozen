#!/usr/bin/env python3
"""Repeat real concurrent gateway tasks and recovery checks in disposable state."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_durable_tasks_gateway import DurableTaskGatewayTests


def run(seconds: float) -> dict:
    gateway = DurableTaskGatewayTests()
    started = time.monotonic()
    cycles = restarts = 0
    measurements = []
    process = None
    with tempfile.TemporaryDirectory(prefix='openkyrozen-production-soak-') as directory:
        root = Path(directory)
        workspace = root / 'workspace'
        (workspace / 'home').mkdir(parents=True)
        db = root / 'state.sqlite3'
        os.environ['KYROZEN_DB_PATH'] = str(root / 'test-driver.sqlite3')
        try:
            process, url = gateway._start_server(workspace, db, root / 'skills')
            baseline_children = {p.pid for p in psutil.Process().children(recursive=True)}
            initial_rss = psutil.Process(process.pid).memory_info().rss
            generation_rss = initial_rss
            def task(session):
                marker = f'{session}-{cycles}.txt'
                created = gateway._request(url, '/api/v2/tasks', method='POST', body={
                    'description': f'soak {session} {cycles}', 'session_id': session,
                    'action': 'write_file', 'args': f'{marker}|{session}:{cycles}'})['task']
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    tasks = gateway._request(url, f'/api/v2/tasks?session_id={session}')['tasks']
                    current = next(t for t in tasks if t['id'] == created['id'])
                    if current['status'] in ('succeeded', 'failed', 'blocked'):
                        assert current['status'] == 'succeeded', current
                        assert (workspace / marker).read_text() == f'{session}:{cycles}'
                        assert all(t['session_id'] == session for t in tasks)
                        return
                    time.sleep(.1)
                raise AssertionError('durable worker did not complete within 15 seconds')
            while time.monotonic() - started < seconds:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(task, ('soak-a', 'soak-b')))
                with sqlite3.connect(db) as connection:
                    count, unique = connection.execute('SELECT count(*), count(DISTINCT id) FROM tasks').fetchone()
                    assert count == unique == (cycles + 1) * 2, (count, unique, cycles)
                    assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                    assert not connection.execute('PRAGMA foreign_key_check').fetchall()
                current = psutil.Process(process.pid)
                rss = current.memory_info().rss
                assert rss <= generation_rss + 256 * 1024 * 1024, 'gateway grew by more than 256 MiB in one generation'
                measurements.append({'cycle': cycles, 'rss_bytes': rss, 'threads': current.num_threads(),
                                     'open_files': len(current.open_files())})
                assert current.num_threads() < 80, 'unbounded gateway thread growth'
                assert len(current.open_files()) < 100, 'unbounded gateway file descriptor growth'
                cycles += 1
                if cycles % 6 == 0:
                    suite = unittest.defaultTestLoader.loadTestsFromNames([
                        'test_delegation.DelegationTests.test_cancel_during_provider_call_prevents_late_write',
                        'test_delegation.DelegationTests.test_cancellation_and_interruption_never_replay_work',
                        'test_learning_worker.LearningWorkerTests.test_worker_runs_a_cycle_after_cli_heartbeat_is_stale',
                    ])
                    result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
                    assert result.wasSuccessful(), (result.errors, result.failures)
                if cycles % 12 == 0:
                    tracked = current.children(recursive=True)
                    gateway._stop_server(process)
                    assert all(not child.is_running() for child in tracked), 'orphan gateway child'
                    process, url = gateway._start_server(workspace, db, root / 'skills')
                    generation_rss = psutil.Process(process.pid).memory_info().rss
                    restarts += 1
                    for session in ('soak-a', 'soak-b'):
                        tasks = gateway._request(url, f'/api/v2/tasks?session_id={session}')['tasks']
                        assert len(tasks) == cycles and all(t['status'] == 'succeeded' for t in tasks)
                print(json.dumps({'cycles': cycles, 'restarts': restarts, 'elapsed_seconds': round(time.monotonic()-started),
                                  'rss_bytes': rss}), flush=True)
                time.sleep(min(5, max(0, seconds - (time.monotonic() - started))))
            gateway._stop_server(process)
            process = None
            children = psutil.Process().children(recursive=True)
            assert not [p.pid for p in children if p.pid not in baseline_children], 'orphan audit child'
        finally:
            if process is not None:
                gateway._stop_server(process)
    return {'status': 'PASS', 'elapsed_seconds': round(time.monotonic()-started, 2), 'cycles': cycles,
            'tasks': cycles * 2, 'restarts': restarts, 'initial_rss_bytes': initial_rss,
            'peak_rss_bytes': max(m['rss_bytes'] for m in measurements), 'samples': measurements}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=float, default=1800)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run(args.seconds)
    except Exception as exc:
        args.output.write_text(json.dumps({'status': 'FAIL', 'error': str(exc)}))
        raise
    args.output.write_text(json.dumps(report, indent=2))
