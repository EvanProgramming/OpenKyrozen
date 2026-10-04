#!/usr/bin/env python3
"""Repeat real concurrent gateway tasks and recovery checks in disposable state."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import io
import hashlib
from importlib import metadata
import platform
import shutil
import subprocess
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


def candidate_identity() -> dict:
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).split(b"\0")
    tracked = sorted(set(tracked) | {b"tests/test_gateway_startup_logging.py", b"scripts/production_soak.py"})
    digest = hashlib.sha256()
    for name in tracked:
        path = ROOT / os.fsdecode(name)
        if name and path.is_file():
            digest.update(name + b"\0" + path.read_bytes())
    return {"git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "candidate_source_sha256": digest.hexdigest(), "python": sys.version,
            "python_executable": sys.executable, "platform": platform.platform(),
            "dependencies": {name: metadata.version(name) for name in
                             ("psutil", "fastapi", "uvicorn", "pydantic", "chromadb")}}


def run(seconds: float, log_dir: Path) -> dict:
    gateway = DurableTaskGatewayTests()
    started = time.monotonic()
    cycles = restarts = 0
    measurements = []
    startup_seconds = []
    def start_gateway(workspace, db, skills, log_path):
        begin = time.monotonic()
        result = gateway._start_server(workspace, db, skills, log_path=log_path, startup_timeout=60)
        startup_seconds.append(round(time.monotonic() - begin, 3))
        return result
    process = None
    log_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='openkyrozen-production-soak-') as directory:
        root = Path(directory)
        workspace = root / 'workspace'
        (workspace / 'home').mkdir(parents=True)
        db = root / 'state.sqlite3'
        os.environ['KYROZEN_DB_PATH'] = str(root / 'test-driver.sqlite3')
        os.environ['KYROZEN_BROWSER_PROFILES'] = str(root / 'browser-profiles')
        try:
            process, url = start_gateway(workspace, db, root / 'skills',
                                                 log_path=log_dir / f'gateway-{restarts:03d}.log')
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
                    process, url = start_gateway(workspace, db, root / 'skills',
                                                         log_path=log_dir / f'gateway-{restarts + 1:03d}.log')
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
        except Exception as exc:
            # Retain disposable state and progress so a timeout is diagnosable.
            evidence = log_dir / 'failed-state'
            shutil.copytree(root, evidence)
            exc.soak_report = {'elapsed_seconds': round(time.monotonic()-started, 2),
                               'cycles': cycles, 'tasks': cycles * 2, 'restarts': restarts,
                               'samples': measurements, 'startup_seconds': startup_seconds, 'startup_timeout_seconds': 60, 'failed_state': str(evidence)}
            raise
        finally:
            if process is not None:
                gateway._stop_server(process)
    return {'status': 'PASS', 'elapsed_seconds': round(time.monotonic()-started, 2), 'cycles': cycles,
            'tasks': cycles * 2, 'restarts': restarts, 'initial_rss_bytes': initial_rss,
            'peak_rss_bytes': max(m['rss_bytes'] for m in measurements), 'samples': measurements,
            'startup_seconds': startup_seconds, 'startup_timeout_seconds': 60}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=float, default=1800)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--log-dir', type=Path)
    args = parser.parse_args()
    identity = candidate_identity()
    log_dir = args.log_dir or args.output.with_suffix(".logs")
    try:
        report = run(args.seconds, log_dir)
    except Exception as exc:
        args.output.write_text(json.dumps({'status': 'FAIL', 'error': str(exc),
                                          'candidate': identity, 'log_dir': str(log_dir),
                                          **getattr(exc, 'soak_report', {})}, indent=2))
        raise
    report.update(candidate=identity, log_dir=str(log_dir))
    args.output.write_text(json.dumps(report, indent=2))
