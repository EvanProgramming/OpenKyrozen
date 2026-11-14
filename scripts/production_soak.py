#!/usr/bin/env python3
"""Repeat real concurrent gateway tasks and recovery checks in disposable state."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
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

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_durable_tasks_gateway import DurableTaskGatewayTests


def _parse_process_table(output: str) -> dict[int, int]:
    parents = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            try:
                parents[int(fields[0])] = int(fields[1])
            except ValueError:
                continue
    return parents


def _powershell(script: str) -> str:
    shell = shutil.which('powershell') or shutil.which('pwsh')
    if not shell:
        raise RuntimeError('PowerShell is required for Windows process evidence')
    return subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', script],
                          capture_output=True, text=True, timeout=15, check=True).stdout


def _process_output(command: list[str]) -> tuple[str, int]:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
    return stdout, process.pid


class _WindowsProcessTable(dict):
    def __init__(self, rows, probe_pid):
        self.probe_pid = probe_pid
        self.probe = rows.pop(probe_pid, None)
        super().__init__(rows)


def _process_table() -> dict:
    if platform.system() == 'Windows':
        shell = shutil.which('powershell') or shutil.which('pwsh')
        if not shell:
            raise RuntimeError('PowerShell is required for Windows process evidence')
        output, probe_pid = _process_output([shell, '-NoProfile', '-NonInteractive', '-Command',
            "Get-CimInstance Win32_Process | Select-Object "
            "@{Name='pid';Expression={$_.ProcessId}},@{Name='ppid';Expression={$_.ParentProcessId}},"
            "@{Name='CreationDate';Expression={if ($null -ne $_.CreationDate) {"
            "$_.CreationDate.ToUniversalTime().ToString('o',[Globalization.CultureInfo]::InvariantCulture)}}},Name "
            "| ConvertTo-Json -Compress"])
        rows = json.loads(output) if output.strip() else []
        if isinstance(rows, dict):
            rows = [rows]
        parents = {}
        for row in rows:
            pid = int(row['pid'])
            if pid in parents:
                raise AssertionError(f'ambiguous Windows process identity for PID {pid}')
            stamp = row.get('CreationDate')
            parents[pid] = {'pid': pid, 'ppid': int(row['ppid']),
                           'CreationDate': stamp[:64] if isinstance(stamp, str) else None,
                           'Name': str(row.get('Name') or '')[:256]}
        return _WindowsProcessTable(parents, probe_pid)
    else:
        output, probe_pid = _process_output(['ps', '-axo', 'pid=,ppid='])
        parents = _parse_process_table(output)
    parents.pop(probe_pid, None)
    return parents


def _process_identity(parents, pid):
    row = parents[pid]
    if not isinstance(row, dict):
        return pid, None
    import datetime
    import re
    stamp = row.get('CreationDate')
    try:
        if not isinstance(stamp, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}Z', stamp):
            raise ValueError('missing or malformed timestamp')
        datetime.datetime.fromisoformat(stamp)
    except ValueError as exc:
        raise AssertionError(f'missing or invalid Windows creation identity for PID {pid}') from exc
    # Preserve all seven fractional digits; parsing above validates the calendar only.
    return pid, stamp


def _descendants(pid: int, parents: dict | None = None) -> set[int]:
    parents = _process_table() if parents is None else parents
    if isinstance(parents, _WindowsProcessTable):
        if pid not in parents:
            raise AssertionError(f'missing Windows creation identity for root PID {pid}')
        _process_identity(parents, pid)
    children = {}
    for child, parent in parents.items():
        parent = parent['ppid'] if isinstance(parent, dict) else parent
        children.setdefault(parent, []).append(child)
    found, pending = set(), [(pid, child) for child in children.get(pid, ())]
    while pending:
        parent, child = pending.pop()
        if child not in found:
            if isinstance(parents.get(parent), dict):
                # Win32 ParentProcessId can name a newer, unrelated reused PID.
                if _process_identity(parents, child)[1] < _process_identity(parents, parent)[1]:
                    continue
            found.add(child)
            pending.extend((child, descendant) for descendant in children.get(child, ()))
    return found


def _process_checkpoint(path, parents, relevant, **fields):
    relevant = sorted(relevant)
    rows = [parents[pid] if isinstance(parents.get(pid), dict)
            else {'pid': pid, 'ppid': parents.get(pid), 'CreationDate': None}
            for pid in relevant[:512]]
    path.write_text(json.dumps({'schema_version': 1, 'rows': rows, 'row_count': len(relevant),
        'rows_truncated': len(relevant) > 512,
        'identity_scheme': 'windows_pid_creation_date' if isinstance(parents, _WindowsProcessTable) else 'posix_pid',
        'excluded_probe_pid': getattr(parents, 'probe_pid', None),
        'excluded_probe': getattr(parents, 'probe', None), **fields}, indent=2) + '\n')


def _read_process_table(checkpoint):
    try:
        return _process_table()
    except Exception as exc:
        _process_checkpoint(checkpoint, {}, set(), status='capture_failed',
                            identity_scheme='unavailable', error=f'{type(exc).__name__}: {str(exc)[:300]}')
        raise


def _capture_process_tree(pid, checkpoint):
    parents = _read_process_table(checkpoint)
    raw = {key: row['ppid'] if isinstance(row, dict) else row for key, row in parents.items()}
    candidates = _descendants(pid, raw)
    fields = {'root_pid': pid, 'status': 'captured'}
    try:
        tracked = _descendants(pid, parents)
        identities = {child: _process_identity(parents, child) for child in tracked}
    except AssertionError as exc:
        _process_checkpoint(checkpoint, parents, candidates | {pid}, root_pid=pid,
                            status='identity_unavailable', error=str(exc))
        raise
    _process_checkpoint(checkpoint, parents, candidates | {pid}, **fields,
        tracked_pids=sorted(tracked)[:512], tracked_count=len(tracked),
        excluded_stale_parent_pids=sorted(candidates - tracked)[:512],
        excluded_stale_parent_count=len(candidates - tracked))
    return {'parents': parents, 'tracked': tracked, 'identities': identities}


def _assert_processes_stopped(before, checkpoint):
    after = _read_process_table(checkpoint)
    present = before['tracked'] & set(after)
    offending, reused = [], []
    try:
        for pid in sorted(present):
            if _process_identity(after, pid) == before['identities'][pid]:
                offending.append(after[pid] if isinstance(after[pid], dict)
                                 else {'pid': pid, 'ppid': after[pid], 'CreationDate': None})
            else:
                reused.append(pid)
    except AssertionError as exc:
        _process_checkpoint(checkpoint, after, before['tracked'], status='identity_unavailable',
                            error=str(exc), offending=offending[:512], reused_pids=reused[:512])
        raise
    _process_checkpoint(checkpoint, after, before['tracked'],
                        status='orphan' if offending else 'stopped',
                        offending=offending[:512], offending_count=len(offending),
                        reused_pids=reused[:512], reused_pid_count=len(reused))
    assert not offending, 'orphan gateway child'


def _assert_no_new_audit_children(pid, baseline, checkpoint):
    current = _capture_process_tree(pid, checkpoint)
    baseline_keys = set(baseline['identities'].values())
    extra = {child for child, identity in current['identities'].items() if identity not in baseline_keys}
    if extra:
        offending_pids = sorted(extra)[:512]
        offending = [current['parents'][child] if isinstance(current['parents'][child], dict)
                     else {'pid': child, 'ppid': current['parents'][child], 'CreationDate': None}
                     for child in offending_pids]
        report = json.loads(checkpoint.read_text())
        report.update(status='orphan', offending_pids=offending_pids,
                      offending=offending, offending_count=len(extra))
        checkpoint.write_text(json.dumps(report, indent=2) + '\n')
    assert not extra, 'orphan audit child'


def _process_snapshot(pid: int) -> dict[str, int]:
    if platform.system() == 'Windows':
        script = (f"$p=Get-Process -Id {int(pid)} -ErrorAction Stop; "
                  "[pscustomobject]@{rssBytes=[long]$p.WorkingSet64;threads=[int]$p.Threads.Count;"
                  "openHandles=[int]$p.HandleCount}|ConvertTo-Json -Compress")
        result = json.loads(_powershell(script))
        return {'rss_bytes': int(result['rssBytes']), 'threads': int(result['threads']),
                'open_handles': int(result['openHandles'])}
    proc = Path('/proc') / str(pid)
    if platform.system() == 'Linux' and proc.is_dir():
        status = dict(line.split(':', 1) for line in (proc / 'status').read_text().splitlines()
                      if ':' in line)
        return {'rss_bytes': int(status['VmRSS'].split()[0]) * 1024,
                'threads': int(status['Threads']), 'open_handles': len(list((proc / 'fd').iterdir()))}
    rss = int(subprocess.run(['ps', '-p', str(pid), '-o', 'rss='], capture_output=True,
                             text=True, timeout=15, check=True).stdout.strip()) * 1024
    threads = max(0, len(subprocess.run(['ps', '-M', '-p', str(pid)], capture_output=True,
                                        text=True, timeout=15, check=True).stdout.splitlines()) - 1)
    handles = max(0, len(subprocess.run(['lsof', '-nP', '-p', str(pid)], capture_output=True,
                                        text=True, timeout=15, check=True).stdout.splitlines()) - 1)
    return {'rss_bytes': rss, 'threads': threads, 'open_handles': handles}


def _handle_limit(initial_handles: int) -> int:
    # Windows starts with more runtime handles; bound growth rather than its baseline.
    return initial_handles + 100 if platform.system() == 'Windows' else 99


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
                             ("fastapi", "uvicorn", "pydantic", "chromadb")}}


def _wait_for_task_completion(gateway, url, workspace: Path, session: str,
                              cycles: int, marker: str, task_id: str) -> None:
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        tasks = gateway._request(url, f'/api/v2/tasks?session_id={session}', timeout=35)['tasks']
        current = next(t for t in tasks if t['id'] == task_id)
        if current['status'] in ('succeeded', 'failed', 'blocked'):
            assert current['status'] == 'succeeded', current
            assert (workspace / marker).read_text() == f'{session}:{cycles}'
            assert all(t['session_id'] == session for t in tasks)
            return
        time.sleep(.1)
    raise AssertionError('durable worker did not complete within 40 seconds')


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
        baseline_children = _capture_process_tree(os.getpid(), log_dir / 'process-audit-baseline.json')
        try:
            process, url = start_gateway(workspace, db, root / 'skills',
                                                 log_path=log_dir / f'gateway-{restarts:03d}.log')
            initial = _process_snapshot(process.pid)
            initial_rss = initial['rss_bytes']
            generation_rss = initial_rss
            generation_handles = initial['open_handles']
            def task(session):
                marker = f'{session}-{cycles}.txt'
                # Match SQLite's 30-second busy timeout, with a small HTTP margin.
                created = gateway._request(url, '/api/v2/tasks', method='POST', body={
                    'description': f'soak {session} {cycles}', 'session_id': session,
                    'action': 'write_file', 'args': f'{marker}|{session}:{cycles}'}, timeout=35)['task']
                _wait_for_task_completion(gateway, url, workspace, session, cycles, marker, created['id'])
            while time.monotonic() - started < seconds:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(task, ('soak-a', 'soak-b')))
                with closing(sqlite3.connect(db)) as connection:
                    with connection:
                        count, unique = connection.execute('SELECT count(*), count(DISTINCT id) FROM tasks').fetchone()
                        assert count == unique == (cycles + 1) * 2, (count, unique, cycles)
                        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
                current = _process_snapshot(process.pid)
                rss = current['rss_bytes']
                assert rss <= generation_rss + 256 * 1024 * 1024, 'gateway grew by more than 256 MiB in one generation'
                measurements.append({'cycle': cycles, 'handle_baseline': generation_handles, **current})
                assert current['threads'] < 80, 'unbounded gateway thread growth'
                assert current['open_handles'] <= _handle_limit(generation_handles), (
                    f"gateway handle growth exceeded its bound: baseline={generation_handles}, "
                    f"current={current['open_handles']}"
                )
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
                    tracked = _capture_process_tree(process.pid, log_dir / f'process-{restarts:03d}-before.json')
                    gateway._stop_server(process)
                    _assert_processes_stopped(tracked, log_dir / f'process-{restarts:03d}-after.json')
                    process, url = start_gateway(workspace, db, root / 'skills',
                                                         log_path=log_dir / f'gateway-{restarts + 1:03d}.log')
                    initial = _process_snapshot(process.pid)
                    generation_rss = initial['rss_bytes']
                    generation_handles = initial['open_handles']
                    restarts += 1
                    for session in ('soak-a', 'soak-b'):
                        tasks = gateway._request(url, f'/api/v2/tasks?session_id={session}')['tasks']
                        assert len(tasks) == cycles and all(t['status'] == 'succeeded' for t in tasks)
                print(json.dumps({'cycles': cycles, 'restarts': restarts, 'elapsed_seconds': round(time.monotonic()-started),
                                  'rss_bytes': rss}), flush=True)
                time.sleep(min(5, max(0, seconds - (time.monotonic() - started))))
            tracked = _capture_process_tree(process.pid, log_dir / 'process-final-before.json')
            gateway._stop_server(process)
            process = None
            _assert_processes_stopped(tracked, log_dir / 'process-final-after.json')
            _assert_no_new_audit_children(os.getpid(), baseline_children, log_dir / 'process-audit-final.json')
        except Exception as exc:
            # Retain disposable state and progress so a timeout is diagnosable.
            evidence = log_dir / 'failed-state'
            if process is not None:
                try:
                    gateway._stop_server(process)
                except Exception as cleanup_error:
                    exc.add_note(f'gateway cleanup failed: {type(cleanup_error).__name__}')
                else:
                    process = None
            failed_state = None
            try:
                shutil.copytree(root, evidence)
                failed_state = str(evidence)
            except OSError as copy_error:
                exc.add_note(f'failed-state copy failed: {type(copy_error).__name__}')
            exc.soak_report = {'elapsed_seconds': round(time.monotonic()-started, 2),
                               'cycles': cycles, 'tasks': cycles * 2, 'restarts': restarts,
                               'samples': measurements, 'startup_seconds': startup_seconds,
                               'startup_timeout_seconds': 60}
            if failed_state:
                exc.soak_report['failed_state'] = failed_state
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
