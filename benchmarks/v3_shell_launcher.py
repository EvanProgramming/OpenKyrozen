"""Benchmark-only parent-authorized shell broker over an inherited socketpair.

The worker's approval database and capability token are deliberately not inputs
into authorization. Only the parent's ShellPolicy grants execution authority.
"""
from __future__ import annotations

import base64
import codecs
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import threading
import tempfile
import time
import uuid

_MAX_MESSAGE = 4 * 1024 * 1024
_MAX_OUTPUT = 1_000_001
_FD_ENV = 'KYROZEN_BENCHMARK_BROKER_FD'


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


class ShellPolicy:
    """Parent-owned immutable fixture bounds, with monotonic policy narrowing."""
    def __init__(self, workspace, capabilities, *, scope_paths=('.',), run_id=None,
                 actor_id='benchmark', session_id='benchmark', revision=0, accepted_bounds=None):
        self.workspace = Path(workspace).resolve(strict=True)
        self.capabilities = frozenset(capabilities)
        self.scope_paths = self._paths(scope_paths)
        self.effects = None
        self.risk_ceiling = 'high'
        if accepted_bounds is not None:
            bounds = json.loads(json.dumps(accepted_bounds))
            self.capabilities &= frozenset(bounds.get('capabilities', ()))
            accepted_paths = self._paths(bounds.get('workspace_paths', ()))
            intersections = {a if a.is_relative_to(b) else b
                             for a in self.scope_paths for b in accepted_paths
                             if a.is_relative_to(b) or b.is_relative_to(a)}
            if not intersections:
                raise ValueError('accepted plan has no approved fixture scope')
            self.scope_paths = tuple(intersections)
            self.effects = frozenset(bounds.get('effects', ()))
            self.risk_ceiling = bounds.get('risk_ceiling', 'normal')
        self.run_id = run_id or uuid.uuid4().hex
        self.actor_id = actor_id
        self.session_id = session_id
        self.revision = revision
        self.lock = threading.RLock()
        self._parent = None

    def _paths(self, values):
        paths = tuple((self.workspace / value).resolve(strict=True) for value in values)
        if not paths or any(not p.is_relative_to(self.workspace) or not p.is_dir() for p in paths):
            raise ValueError('approved scope must contain workspace directories')
        return paths

    def narrow(self, *, capabilities, scope_paths):
        with self.lock:
            paths = self._paths(scope_paths)
            capabilities = frozenset(capabilities)
            if (not capabilities <= self.capabilities
                    or any(not any(p.is_relative_to(old) for old in self.scope_paths) for p in paths)):
                raise ValueError('parent policy cannot expand approved fixture bounds')
            self.capabilities = capabilities
            self.scope_paths = paths
            self.revision += 1

    def child(self, actor_id, *, capabilities):
        with self.lock:
            child = ShellPolicy(self.workspace, self.capabilities & frozenset(capabilities),
                                scope_paths=self.scope_paths, run_id=self.run_id, actor_id=actor_id,
                                session_id=self.session_id, revision=self.revision)
            child._parent = self
            child.effects = self.effects
            child.risk_ceiling = self.risk_ceiling
            child.lock = self.lock
            return child

    def authorize(self, request):
        required = {'run_id', 'actor_id', 'session_id', 'call_id', 'command', 'cwd', 'env', 'revision', 'timeout_seconds', 'background'}
        if set(request) != required:
            raise PermissionError('invalid shell request fields')
        if (request['run_id'] != self.run_id or request['actor_id'] != self.actor_id
                or request['session_id'] != self.session_id or request['revision'] != self.revision
                or 'shell' not in self.capabilities):
            raise PermissionError('shell request is outside the effective parent policy')
        if (not isinstance(request['call_id'], str) or not request['call_id']
                or not isinstance(request['command'], str) or not request['command'].strip()
                or '\0' in request['command'] or not isinstance(request['cwd'], str)
                or not isinstance(request['env'], dict)
                or any(not isinstance(k, str) or not k or '=' in k or '\0' in k
                       or not isinstance(v, str) or '\0' in v for k, v in request['env'].items())):
            raise PermissionError('invalid shell request')
        from openkyrozen.security.command_policy import _BLOCKED_RE
        from openkyrozen.tools.catalog import builtin_metadata
        spec = builtin_metadata('run_cmd')
        if _BLOCKED_RE.search(request['command']):
            raise PermissionError('shell command is blocked by the parent command policy')
        if (self.effects is not None and spec['side_effects'] not in self.effects
                or spec['risk'] == 'high' and self.risk_ceiling != 'high'):
            raise PermissionError('shell effects or risk exceed the accepted plan')
        if type(request['background']) is not bool:
            raise PermissionError('invalid shell capture mode')
        timeout = request['timeout_seconds']
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 1800:
            raise PermissionError('shell timeout is outside the bounded parent policy')
        cwd = Path(request['cwd']).resolve(strict=True)
        if not cwd.is_dir() or not any(cwd.is_relative_to(p) for p in self.scope_paths):
            raise PermissionError('shell cwd is outside approved fixture bounds')
        if self._parent is not None:
            self._parent.authorize(dict(request, actor_id=self._parent.actor_id))


class TicketAuthority:
    """Single-use short-lived tickets bound to all concrete invocation fields."""
    def __init__(self, policy):
        self.policy = policy
        self.tickets = {}

    def issue(self, request, *, ttl_seconds=5):
        with self.policy.lock:
            self.policy.authorize(request)
            now = time.monotonic()
            self.tickets = {k: v for k, v in self.tickets.items() if v[1] > now}
            if len(self.tickets) >= 64:
                raise PermissionError('shell ticket limit reached')
            token = secrets.token_urlsafe(32)
            self.tickets[token] = (hashlib.sha256(_encoded(request)).digest(),
                                   now + min(5, max(0, ttl_seconds)))
            return token

    def consume(self, ticket, request):
        with self.policy.lock:
            record = self.tickets.pop(ticket, None)
            if (record is None or record[1] <= time.monotonic()
                    or record[0] != hashlib.sha256(_encoded(request)).digest()):
                raise PermissionError('invalid, expired, or replayed shell ticket')
            self.policy.authorize(request)


def _kill_tree(process):
    # Bounded cleanup only: detached/reparented macOS descendants are not owned.
    # The full benchmark gate remains closed until kernel-backed ownership exists.
    # Groups alone miss a command which calls setsid(). Discover descendants too.
    def snapshot():
        rows = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,pgid=,stat='], capture_output=True,
                              text=True, timeout=3, check=True).stdout.splitlines()
        return {int(pid): (int(ppid), int(group), state)
                for pid, ppid, group, state in (row.split() for row in rows if row.strip())}

    def live(record):
        return record is not None and not record[2].startswith(('Z', 'X'))

    descendants = []
    try:
        processes = snapshot()
        children = {}
        for pid, (ppid, _, _) in processes.items():
            children.setdefault(ppid, []).append(pid)
        pending = [process.pid]
        while pending:
            current = pending.pop()
            descendants.append(current)
            pending.extend(children.get(current, ()))
    except (OSError, ValueError, subprocess.SubprocessError):
        processes = {}
        descendants = [process.pid]
    # Keep the group leader alive until the group signal: Darwin can report
    # EPERM for an already-exiting group after individual leader/child kills.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        try:
            processes = snapshot()
        except (OSError, ValueError, subprocess.SubprocessError):
            raise exc
        if any(record[1] == process.pid and live(record) for record in processes.values()):
            raise
    for pid in reversed(descendants):
        record = processes.get(pid)
        if record is None or pid == process.pid or not live(record):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            try:
                still_live = live(snapshot().get(pid))
            except (OSError, ValueError, subprocess.SubprocessError):
                raise exc
            if still_live:
                raise
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def _safe_env(env):
    # Loader hooks execute before the sandbox; credentials and broker state never
    # belong in a model's shell, including when copied from the worker environment.
    denied = ('KYROZEN_', 'DYLD_', 'LD_', 'PYTHON', 'BASH_ENV', 'ENV', 'SHELLOPTS', 'BASHOPTS')
    return {k: v for k, v in env.items()
            if not k.upper().startswith(denied)
            and not any(word in k.upper() for word in ('TOKEN', 'SECRET', 'PASSWORD', 'API_KEY'))}


class ShellBroker:
    def __init__(self, connection, policy, protected_files):
        self.connection = connection
        self.policy = policy
        self.protected_files = protected_files
        self.authority = TicketAuthority(policy)
        self.processes = {}
        self.records = {}
        self.closed = threading.Event()
        self.close_lock = threading.RLock()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            with self.connection.makefile('rwb', buffering=0) as stream:
                while not self.closed.is_set():
                    line = stream.readline(_MAX_MESSAGE + 1)
                    if not line:
                        break
                    if len(line) > _MAX_MESSAGE:
                        break
                    try:
                        request = json.loads(line)
                        result = {'ok': True, 'value': self._dispatch(request)}
                    except Exception as exc:
                        result = {'ok': False, 'error': type(exc).__name__, 'message': str(exc)}
                    self.connection.sendall(_encoded(result) + b'\n')
        except (OSError, ValueError):
            pass
        finally:
            self.close()

    def _dispatch(self, request):
        operation = request.get('op')
        if operation == 'ticket':
            return self.authority.issue(request['request'])
        if operation == 'start':
            with self.policy.lock:
                self.authority.consume(request['ticket'], request['request'])
                if sum(p.poll() is None for p in self.processes.values()) >= 32:
                    raise PermissionError('benchmark shell process limit reached')
                from benchmarks.v3_protected_worker import protected_shell_command
                invocation = protected_shell_command(request['request']['command'], policy=self.policy,
                                                       cwd=request['request']['cwd'],
                                                       env=_safe_env(request['request']['env']),
                                                       protected_files=self.protected_files)
                try:
                    process = subprocess.Popen(invocation.command, shell=invocation.shell,
                                               cwd=request['request']['cwd'], env=invocation.env,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                               close_fds=True, start_new_session=True)
                except Exception:
                    invocation.close()
                    raise
                identifier = uuid.uuid4().hex
                self.processes[identifier] = process
                record = {'stdout': tempfile.TemporaryFile(), 'stderr': tempfile.TemporaryFile(),
                          'background': request['request']['background'], 'eof': set(), 'timed_out': False,
                          'lock': threading.Lock(), 'invocation': invocation}
                self.records[identifier] = record
                for name in ('stdout', 'stderr'):
                    threading.Thread(target=self._collect, args=(identifier, name), daemon=True).start()
                threading.Thread(target=self._expire,
                                 args=(identifier, request['request']['timeout_seconds']), daemon=True).start()
                return {'id': identifier, 'pid': process.pid, 'backend': invocation.backend}
        identifier = request.get('id')
        if identifier not in self.processes:
            raise PermissionError('process does not belong to this benchmark actor/session')
        process = self.processes[identifier]
        record = self.records[identifier]
        if operation == 'stop':
            _kill_tree(process)
            return process.returncode
        if operation == 'poll':
            return {'exit_code': process.poll(), 'timed_out': record['timed_out']}
        if operation == 'read':
            name, offset = request.get('stream'), request.get('offset')
            if name not in {'stdout', 'stderr'} or type(offset) is not int or offset < 0:
                raise PermissionError('invalid process output request')
            with record['lock']:
                record[name].seek(offset)
                return {'data': base64.b64encode(record[name].read(4096)).decode(),
                        'eof': name in record['eof'], 'exit_code': process.poll()}
        raise PermissionError('unsupported broker operation')

    def _expire(self, identifier, timeout):
        process, record = self.processes[identifier], self.records[identifier]
        deadline = time.monotonic() + timeout
        try:
            process.wait(timeout=timeout)
            # A shell leader may exit while its children retain the output pipes.
            while len(record['eof']) != 2 and not self.closed.is_set():
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(process.args, timeout)
                time.sleep(.02)
        except subprocess.TimeoutExpired:
            record['timed_out'] = True
            _kill_tree(process)

    def _collect(self, identifier, name):
        process, record = self.processes[identifier], self.records[identifier]
        stream = getattr(process, name)
        try:
            while chunk := stream.read1(4096):
                with record['lock']:
                    record[name].seek(0, os.SEEK_END)
                    remaining = max(0, _MAX_OUTPUT - record[name].tell()) if record['background'] else len(chunk)
                    record[name].write(chunk[:remaining])
        finally:
            stream.close()
            with record['lock']:
                record['eof'].add(name)
                if self.closed.is_set():
                    record[name].close()
            if len(record['eof']) == 2:
                self._finish_collection(identifier)

    def _finish_collection(self, identifier):
        self.records[identifier]['invocation'].close()

    def close(self):
        with self.close_lock:
            if self.closed.is_set():
                return
            self.closed.set()
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            errors = []
            for process in tuple(self.processes.values()):
                try:
                    _kill_tree(process)
                except Exception as exc:
                    errors.append(exc)
            for record in tuple(self.records.values()):
                try:
                    record['invocation'].close()
                except Exception as exc:
                    errors.append(exc)
                with record['lock']:
                    for name in record['eof']:
                        record[name].close()
            if errors:
                raise errors[0]


class BrokerClient:
    def __init__(self, fd, identity):
        self.connection = socket.socket(fileno=fd)
        self.connection.set_inheritable(False)
        self.stream = self.connection.makefile('rwb', buffering=0)
        self.lock = threading.Lock()
        self.identity = identity

    def rpc(self, op, **values):
        with self.lock:
            data = _encoded(dict(op=op, **values)) + b'\n'
            if len(data) > _MAX_MESSAGE:
                raise PermissionError('shell request exceeds benchmark limit')
            self.connection.sendall(data)
            line = self.stream.readline(_MAX_MESSAGE + 1)
            if not line:
                raise ConnectionError('benchmark shell broker disconnected')
            result = json.loads(line)
            if not result.get('ok'):
                raise PermissionError(result.get('message', 'shell broker denied request'))
            return result['value']

    def start(self, command, cwd, env, timeout_seconds, *, background=False):
        request = dict(self.identity, call_id=uuid.uuid4().hex, command=command, cwd=cwd, env=env,
                       timeout_seconds=timeout_seconds, background=background)
        ticket = self.rpc('ticket', request=request)
        value = self.rpc('start', ticket=ticket, request=request)
        return RemoteProcess(self, value['id'], value['pid'], value['backend'])


class RemoteStream:
    def __init__(self, process, name):
        self.process, self.name, self.offset = process, name, 0
        self.closed = False

    def read1(self, size):
        while not self.closed:
            result = self.process.client.rpc('read', id=self.process.identifier,
                                             stream=self.name, offset=self.offset)
            value = base64.b64decode(result['data'])[:size]
            self.offset += len(value)
            if value or result['eof']:
                return value
            time.sleep(.02)
        return b''

    def close(self):
        self.closed = True


class RemoteProcess:
    def __init__(self, client, identifier, pid, backend):
        self.client, self.identifier, self.pid, self.backend = client, identifier, pid, backend
        self.timed_out = False
        self.returncode = None
        self._benchmark_handle = None
        self.stdout = RemoteStream(self, 'stdout')
        self.stderr = RemoteStream(self, 'stderr')

    def poll(self):
        value = self.client.rpc('poll', id=self.identifier)
        if self.returncode is None:
            self.returncode = value['exit_code']
        self.timed_out = self.timed_out or value['timed_out']
        if self._benchmark_handle is not None:
            self._benchmark_handle.timed_out = self._benchmark_handle.timed_out or self.timed_out
            if self.timed_out and not self._benchmark_handle.stop_reason:
                self._benchmark_handle.stop_reason = 'timed_out'
        return self.returncode

    def wait(self, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else float('inf')
        while self.poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.identifier, timeout)
            time.sleep(.02)
        return self.returncode

    def stop(self):
        self.returncode = self.client.rpc('stop', id=self.identifier)
        return self.returncode


def _install_legacy_adapter(client, shell):
    # The frozen V2 adapter owns its original string validation, 60-second
    # timeout, and four-field result. Replace only its subprocess boundary.
    from types import SimpleNamespace

    def run(command, *, shell, capture_output, text, timeout, env, cwd):
        process = client.start(command, cwd, env, timeout)
        output = {'stdout': bytearray(), 'stderr': bytearray()}
        def collect(name):
            stream = getattr(process, name)
            while chunk := stream.read1(4096):
                output[name].extend(chunk)
            stream.close()
        collectors = [threading.Thread(target=collect, args=(name,), daemon=True)
                      for name in ('stdout', 'stderr')]
        for thread in collectors:
            thread.start()
        try:
            process.wait(timeout + 5)
            for thread in collectors:
                thread.join()
            if process.timed_out:
                raise subprocess.TimeoutExpired(command, timeout)
            return subprocess.CompletedProcess(command, process.returncode,
                                               output['stdout'].decode(errors='replace'),
                                               output['stderr'].decode(errors='replace'))
        finally:
            process.stop()
    shell.subprocess = SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)


def install_worker_adapter(client=None):
    """Install before loading the selected snapshot's adapter class."""
    raw_fd = os.environ.pop(_FD_ENV, None)
    raw_identity = os.environ.pop('KYROZEN_BENCHMARK_BROKER_IDENTITY', None)
    if client is None:
        if raw_fd is None and raw_identity is None:
            return False
        if raw_fd is None or raw_identity is None:
            raise PermissionError('incomplete benchmark shell broker context')
        client = BrokerClient(int(raw_fd), json.loads(raw_identity))
    elif raw_fd is not None or raw_identity is not None:
        raise PermissionError('actor cannot inherit a different broker context')
    from openkyrozen.tools import shell
    if not hasattr(shell, '_validated_shell_options'):
        _install_legacy_adapter(client, shell)
        return True
    from openkyrozen.tools import processes
    from openkyrozen.tools.models import CommandResult
    base_manager = processes.SessionProcessManager

    class BrokerProcessManager(base_manager):
        def start(self, command, *, cwd, env, timeout_seconds, cancelled=None):
            process_id = uuid.uuid4().hex
            audit = getattr(self, '_audit_event', None)
            if audit:
                audit('launch_intent', process_id, timeout_seconds=timeout_seconds)
            try:
                with self._lock:
                    live = sum(handle.process.poll() is None for handle in self._handles.values())
                    if self._closed or live >= 32:
                        raise RuntimeError('session process manager is closed or full')
                    if cancelled is not None and cancelled.is_set():
                        raise RuntimeError('turn was cancelled before process start')
                    process = client.start(command, cwd, env, timeout_seconds,
                                           background=not getattr(self, '_foreground', False))
                    handle = processes.ProcessHandle(process_id, process, self.owner_id,
                                                      self.workspace_id, self.session_id, timeout_seconds,
                                                      process.backend)
                    process._benchmark_handle = handle
                    self._handles[handle.process_id] = handle
            except BaseException:
                if audit:
                    try:
                        # A failed RPC can leave remote dispatch uncertain.
                        audit('launch_failed', process_id, reason='launch_not_confirmed')
                    except RuntimeError:
                        pass
                raise
            started = getattr(self, '_audit_started', None)
            if started:
                started(handle)
            for name in ('stdout', 'stderr'):
                threading.Thread(target=self._collect, args=(handle, name), daemon=True).start()
            expiry_options = {'cancelled': cancelled} if cancelled is not None else {}
            expiry = threading.Thread(target=self._expire, args=(handle,), kwargs=expiry_options, daemon=True)
            if hasattr(handle, 'expiry_thread'):
                handle.expiry_thread = expiry
            expiry.start()
            return handle

        def _collect(self, handle, name):
            if not getattr(self, '_foreground', False):
                return base_manager._collect(handle, name)
            stream = getattr(handle.process, name)
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            try:
                while chunk := stream.read1(4096):
                    with handle.lock:
                        setattr(handle, name, getattr(handle, name) + decoder.decode(chunk))
                with handle.lock:
                    setattr(handle, name, getattr(handle, name) + decoder.decode(b'', final=True))
            finally:
                stream.close()

        def _expire(self, handle, cancelled=None):
            outcome = getattr(self, '_audit_outcome', None)
            try:
                deadline = time.monotonic() + min(handle.timeout_seconds, 1800) + 5
                while (handle.process.poll() is None
                       or not (handle.process.stdout.closed and handle.process.stderr.closed)):
                    if cancelled is not None and cancelled.is_set():
                        if outcome:
                            handle.stop_reason = 'cancelled'
                        self.stop(handle.process_id)
                        handle.cancelled = True
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(handle.process.identifier, handle.timeout_seconds)
                    try:
                        if handle.process.poll() is None:
                            handle.process.wait(timeout=min(.1, remaining))
                        else:
                            time.sleep(min(.02, remaining))
                    except subprocess.TimeoutExpired:
                        continue
            except subprocess.TimeoutExpired:
                if outcome:
                    handle.stop_reason = 'timed_out'
                self.stop(handle.process_id)
                handle.timed_out = True
            finally:
                handle.timed_out = handle.timed_out or handle.process.timed_out
                if outcome:
                    if handle.timed_out and not handle.stop_reason:
                        handle.stop_reason = 'timed_out'
                    outcome(handle)
                if handle.process.poll() is not None:
                    handle.finished_at = time.monotonic()

        def stop(self, process_id):
            handle = self._handles.get(process_id)
            if handle is None:
                return None
            audit = getattr(self, '_audit_event', None)
            if audit and handle.process.poll() is None:
                handle.stop_reason = handle.stop_reason or 'stopped'
                try:
                    audit('stop_requested', process_id, handle=handle, reason=handle.stop_reason)
                except RuntimeError:
                    handle.audit_error = True
            handle.process.stop()
            stopped = handle.process.poll() is not None
            if stopped:
                handle.finished_at = time.monotonic()
            outcome = getattr(self, '_audit_outcome', None)
            if outcome:
                outcome(handle)
            return stopped

    def run_command(adapter, args, *, cancelled=None):
        validated = adapter._validated_shell_options(args)
        if isinstance(validated, CommandResult):
            return validated
        command, timeout, cwd, env, background = validated
        if background:
            return CommandResult('Error: background processes require a session runtime', False,
                                 failure='invalid_arguments')
        started = time.monotonic()
        manager = BrokerProcessManager('foreground', str(adapter._WORKSPACE_ROOT), 'foreground')
        manager._foreground = True
        handle = None
        failure = None
        try:
            handle = manager.start(command, cwd=cwd, env=env, timeout_seconds=timeout)
            while handle.process.poll() is None:
                if cancelled is not None and cancelled.is_set():
                    failure = 'cancelled'
                    manager.stop(handle.process_id)
                    break
                time.sleep(.02)
            handle.timed_out = handle.timed_out or handle.process.timed_out
            if handle.timed_out:
                failure = 'timeout'
            # Collector completion is distinct from process exit and preserves final output.
            while not (handle.process.stdout.closed and handle.process.stderr.closed):
                if cancelled is not None and cancelled.is_set() and failure is None:
                    failure = 'cancelled'
                    manager.stop(handle.process_id)
                time.sleep(.01)
            handle.process.poll()
            handle.timed_out = handle.timed_out or handle.process.timed_out
            if handle.timed_out and failure is None:
                failure = 'timeout'
            if not failure and handle.process.returncode != 0:
                failure = 'nonzero_exit'
            message = handle.stdout.strip() or '(no output)'
            if failure:
                message = f'Error: command {failure}; exit code {handle.process.returncode}\nstdout:\n{handle.stdout}\nstderr:\n{handle.stderr}'
            return CommandResult(message, failure is None, handle.process.returncode, failure,
                                 handle.stdout, handle.stderr, time.monotonic() - started,
                                 handle.timed_out, sandbox_backend=handle.sandbox_backend)
        except (PermissionError, ConnectionError, OSError) as exc:
            return CommandResult(f'Error: benchmark shell denied: {exc}', False,
                                 failure='denied', sandbox_backend='unavailable')
        finally:
            manager.close()

    processes.SessionProcessManager = BrokerProcessManager
    shell.run_command = run_command
    # Importing the tools package loads this class before the module functions
    # are replaced, so update its captured method as well.
    from openkyrozen.tools.adapters import ToolAdapters
    ToolAdapters.run_command = run_command
    return True
