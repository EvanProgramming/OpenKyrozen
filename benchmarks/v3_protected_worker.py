"""Fail-closed OS protection and external managed-shell preflight for V3 runs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

from benchmarks.v3_shell_launcher import ShellBroker, ShellPolicy, _FD_ENV


def _validated_files(workspace, protected_files):
    files = []
    for item in protected_files:
        path = Path(item)
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError('benchmark protection requires unaliased regular original test files')
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(workspace):
            raise ValueError('benchmark protection escaped the workspace')
        files.append(resolved)
    return files


def _mac_denials(files):
    from openkyrozen.tools.sandbox import _sbpl_path
    if not files:
        return ''
    ancestors = sorted({parent for path in files for parent in path.parents})
    return ('(deny file-write* ' + ' '.join(f'(literal {_sbpl_path(str(p))})' for p in files) + ')'
            + '(deny file-write-unlink ' + ' '.join(f'(literal {_sbpl_path(str(p))})' for p in ancestors) + ')')


def _linux_mounts(workspace, files):
    # Mount points cannot be renamed/unlinked, even by a process which can write
    # adjacent source files. Each original test inode is read-only and unaliased.
    directories = {parent for path in files for parent in path.parents
                   if parent == workspace or parent.is_relative_to(workspace)}
    args = []
    for directory in sorted(directories, key=lambda p: len(p.parts)):
        args.extend(['--bind', str(directory), str(directory)])
    for path in files:
        args.extend(['--ro-bind', str(path), str(path)])
    return args


def protect_worker_command(command, *, cwd, env, protected_files):
    workspace = Path(cwd).resolve(strict=True)
    files = _validated_files(workspace, protected_files)
    if sys.platform == 'darwin':
        executable = shutil.which('sandbox-exec')
        if executable:
            profile = '(version 1)(allow default)' + _mac_denials(files)
            return [executable, '-p', profile, *command], dict(env)
    elif sys.platform.startswith('linux'):
        executable = shutil.which('bwrap')
        if executable:
            # Do not relax user namespace restrictions to permit nested shells:
            # managed shells run in the parent's separate sandbox instead.
            args = [executable, '--die-with-parent', '--new-session', '--unshare-pid',
                    '--unshare-user', '--disable-userns', '--ro-bind', '/', '/',
                    '--bind', str(workspace.parent), str(workspace.parent),
                    '--proc', '/proc', '--dev-bind', '/dev', '/dev']
            args += _linux_mounts(workspace, files)
            return [*args, '--chdir', str(workspace), '--', *command], dict(env)
    raise ValueError('benchmark test-file protection has no supported OS backend')


def protected_shell_command(command, *, policy, cwd, env, protected_files):
    """Extend the original shell sandbox in-place; never nest or replace it."""
    from openkyrozen.tools.sandbox import sandbox_command, _sbpl_path
    invocation = sandbox_command(command, workspace=str(policy.workspace), cwd=cwd, env=env)
    if invocation.backend == 'unavailable':
        invocation.close()
        raise PermissionError('the original managed shell sandbox is unavailable')
    try:
        files = _validated_files(policy.workspace, protected_files)
        writable = policy.scope_paths if 'write' in policy.capabilities else ()
        if invocation.backend == 'sandbox-exec':
            argv = list(invocation.command)
            profile = argv[2]
            original = f'(subpath {_sbpl_path(str(policy.workspace))})'
            allowed = ' '.join(f'(subpath {_sbpl_path(str(p))})' for p in writable)
            profile = profile.replace(original, allowed)
            argv[2] = profile + _mac_denials(files)
            invocation.command = argv
        elif invocation.backend == 'bubblewrap':
            argv = list(invocation.command)
            separator = argv.index('--')
            head, tail = argv[:separator], argv[separator:]
            workspace = str(policy.workspace)
            for index in range(len(head) - 2):
                if head[index:index + 3] == ['--bind', workspace, workspace]:
                    head[index] = '--ro-bind'
                    break
            # Apply ancestor mounts before writable children, otherwise a late
            # read-only ancestor mount hides an already-mounted writable scope.
            directories = {parent for p in files for parent in p.parents
                           if parent == policy.workspace or parent.is_relative_to(policy.workspace)}
            directories.update(writable)
            for path in sorted(directories, key=lambda p: len(p.parts)):
                mode = '--bind' if any(path.is_relative_to(p) for p in writable) else '--ro-bind'
                head.extend([mode, str(path), str(path)])
            for path in files:
                head.extend(['--ro-bind', str(path), str(path)])
            invocation.command = [head[0], '--unshare-user', '--disable-userns', *head[1:], *tail]
        else:
            raise PermissionError('unsupported managed shell sandbox backend')
        return invocation
    except Exception:
        invocation.close()
        raise


class ProtectedWorker:
    """Context wrapping one matched worker; attach immediately after Popen.

    Popen must receive command/env/pass_fds, then attach(process). The parent
    context must remain alive through communicate(); it kills broker processes
    on worker exit, socket disconnect, timeout, and context exit.
    """
    def __init__(self, command, *, cwd, env, protected_files, policy):
        self.policy = policy
        self.protected_files = _validated_files(Path(cwd).resolve(), protected_files)
        if Path(cwd).resolve() != policy.workspace:
            raise ValueError('worker workspace must match parent policy')
        parent, self.child = socket.socketpair()
        self.child.set_inheritable(True)
        self.broker = ShellBroker(parent, policy, self.protected_files)
        identity = {key: getattr(policy, key) for key in ('run_id', 'actor_id', 'session_id', 'revision')}
        worker_env = dict(env, **{_FD_ENV: str(self.child.fileno()),
                                  'KYROZEN_BENCHMARK_BROKER_IDENTITY': json.dumps(identity)})
        try:
            self.command, self.env = protect_worker_command(command, cwd=cwd, env=worker_env,
                                                             protected_files=self.protected_files)
        except Exception:
            self.child.close()
            self.broker.close()
            raise
        self.pass_fds = (self.child.fileno(),)
        self.process = None

    def __enter__(self):
        return self

    def attach(self, process):
        if self.process is not None:
            raise RuntimeError('protected launcher already attached')
        self.process = process
        self.child.close()
        def monitor():
            process.wait()
            self.broker.close()
        threading.Thread(target=monitor, daemon=True).start()

    def __exit__(self, *args):
        self.child.close()
        self.broker.close()
        if self.broker.thread is not threading.current_thread():
            self.broker.thread.join(timeout=5)


_ATTACK_SCRIPT = r'''
import json, os, pathlib, sys
base, workspace, nested, guard, source = map(pathlib.Path, sys.argv[1:])
out = {}
def attempt(name, fn):
    try:
        fn()
        out[name] = False
    except OSError:
        out[name] = True
attempt('write', lambda: guard.write_text('changed'))
attempt('delete', lambda: guard.unlink())
attempt('replace', lambda: os.replace(source, guard))
attempt('hardlink', lambda: os.link(guard, nested / 'alias'))
attempt('symlink_write', lambda: ((nested / 'sym').symlink_to(guard), (nested / 'sym').write_text('changed')))
attempt('nested_move', lambda: os.rename(nested, base / 'moved-nested'))
attempt('workspace_move', lambda: os.rename(workspace, base / 'moved-workspace'))
attempt('parent_move', lambda: os.rename(base, base.with_name(base.name + '-moved')))
source.write_text('after')
print(json.dumps(out))
'''


def verify_worker_protection(*, require_authenticated_children=True):
    """Run real attacks before key lookup; the full campaign remains blocked.

    False selects component diagnostics only and never asserts actor isolation or
    detached process cleanup. The returned false evidence is release-blocking.
    """
    with tempfile.TemporaryDirectory(prefix='v3-protection-') as directory:
        base = Path(directory).resolve()
        workspace = base / 'workspace'
        nested = workspace / 'nested'
        nested.mkdir(parents=True)
        guard, source = nested / 'test_guard.py', nested / 'source.py'
        guard.write_text('original')
        source.write_text('before')
        paths = list(map(str, (base, workspace, nested, guard, source)))
        command = [sys.executable, '-c', _ATTACK_SCRIPT, *paths]
        wrapped, env = protect_worker_command(command, cwd=workspace, env=dict(os.environ), protected_files=[guard])
        result = subprocess.run(wrapped, cwd=workspace, env=env, capture_output=True, text=True, timeout=30)
        try:
            values = json.loads(result.stdout)
        except ValueError as exc:
            raise ValueError('benchmark direct file protection probe failed: ' + result.stderr[-500:]) from exc
        expected = {'write', 'delete', 'replace', 'hardlink', 'symlink_write', 'nested_move', 'workspace_move', 'parent_move'}
        if result.returncode or set(values) != expected or not all(values.values()) or guard.read_text() != 'original' or source.read_text() != 'after':
            raise ValueError('benchmark direct file protection probe failed')
        # Remove the denied symlink-write probe's harmless alias before repeating.
        (nested / 'sym').unlink(missing_ok=True)
        source.write_text('before')
        shell_command = shlex.join(command)
        script = '''
import json, os, sys
from benchmarks.v3_shell_launcher import install_worker_adapter
install_worker_adapter()
from openkyrozen.tools.processes import SessionProcessManager
m = SessionProcessManager('owner', sys.argv[1], 'session')
h = m.start(sys.argv[2], cwd=sys.argv[1], env=dict(os.environ), timeout_seconds=10)
h.process.wait(20)
import time
deadline = time.monotonic() + 3
while not (h.process.stdout.closed and h.process.stderr.closed) and time.monotonic() < deadline:
    time.sleep(.01)
print(json.dumps({'exit': h.process.returncode, 'out': h.stdout, 'err': h.stderr}))
m.close()
'''
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                   KYROZEN_SANDBOX_REQUIRED='1')
        policy = ShellPolicy(workspace, {'read', 'write', 'shell'})
        with ProtectedWorker([sys.executable, '-c', script, str(workspace), shell_command],
                             cwd=workspace, env=env, protected_files=[guard], policy=policy) as launch:
            process = subprocess.Popen(launch.command, cwd=workspace, env=launch.env,
                                       pass_fds=launch.pass_fds, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            launch.attach(process)
            stdout, stderr = process.communicate(timeout=40)
        try:
            managed = json.loads(stdout)
            values = json.loads(managed['out'])
        except (ValueError, KeyError) as exc:
            raise ValueError('benchmark external managed shell protection probe failed: ' + stderr.decode()[-500:]) from exc
        if (process.returncode or managed['exit'] or set(values) != expected or not all(values.values())
                or guard.read_text() != 'original' or source.read_text() != 'after'):
            raise ValueError('benchmark external managed shell protection probe failed')
        if require_authenticated_children:
            raise ValueError(
                'benchmark actor isolation is unavailable: same-process runtime subagents can access '
                'the root worker channel; independently isolated per-actor dispatch is required. '
                'Detached macOS descendants also lack kernel-backed ownership and cleanup')
        return {'authenticated_child_isolation': False, 'daemonized_process_cleanup_proven': False,
                'direct_attacks_denied': True, 'managed_shell_attacks_denied': True,
                'source_write_allowed': True, 'backend': 'sandbox-exec' if sys.platform == 'darwin' else 'bubblewrap'}
