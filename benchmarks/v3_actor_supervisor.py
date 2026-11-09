"""Privileged benchmark parent: kernel identities own immutable actor policies."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import selectors
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from benchmarks.v3_process_owner import UIDOwner, peer_identity, process_snapshot
from benchmarks.v3_protected_worker import _validated_files, protected_shell_command
from benchmarks.v3_shell_launcher import ShellBroker, ShellPolicy, _encoded, _MAX_MESSAGE, _safe_env

_MUTATIONS = frozenset({'write_file', 'edit_file', 'apply_patch'})
_READS = frozenset({'read_file', 'search_files', 'list_dir', 'list_tree', 'find_files'})
_PROFILES = {'researcher': frozenset({'read', 'network'}),
             'reviewer': frozenset({'read', 'network'}),
             'coder': frozenset({'read', 'write', 'shell', 'network', 'git', 'browser'})}
_CALLBACK_FIELDS = {
    'publish': {'run'}, 'receipt': {'child_id', 'receipt'}, 'message': {'child_id', 'message'},
    'usage': {'usage'}, 'cancelled': set(), 'budget_can_continue': {'cost_picos'},
    'budget_record_cost': {'cost_picos'}, 'access_acquire': {'path', 'reading', 'recursive'},
    'access_release': {'token'}, 'event': {'event'}, 'parent_interaction_state': set(),
    'parent_task_state': set(), 'parent_active_task': {'task_id'},
    'parent_mutation_matches_current_task': {'action', 'arguments'},
    'memory_recall_records': {'session_id', 'query', 'options'},
    'memory_recall': {'session_id', 'query', 'options'},
    'memory_recent': {'session_id', 'query', 'options'}, 'usage_attempts': {'attempts'},
    'canonical_history': {'session_id', 'kind', 'offset', 'chunk', 'final'}}


def _actor_socket_directory():
    directory = Path(tempfile.mkdtemp(prefix='openkyrozen-actors-', dir='/var/tmp')).resolve()
    directory.chmod(0o711)
    return directory


def _fixture_git_environment(environment, workspace):
    # Fresh actor UIDs differ from the parent's fixture owner. Trust precisely
    # that fixture, without inheriting protected-config wildcards or host files.
    return dict({key: value for key, value in environment.items() if not key.startswith('GIT_CONFIG')},
        GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_COUNT='1',
        GIT_CONFIG_KEY_0='safe.directory', GIT_CONFIG_VALUE_0=str(workspace))


def _copy(value):
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > 1_000_000:
        raise PermissionError('actor payload exceeds benchmark limit')
    return json.loads(encoded)


def _communicate(process, owner, *, input_data=None, timeout, limit=_MAX_MESSAGE):
    """Bound both pipes while preserving native tool input and exit behavior."""
    output = {'stdout': bytearray(), 'stderr': bytearray()}
    deadline = time.monotonic() + timeout
    pending = memoryview(input_data or b'')
    try:
        with selectors.DefaultSelector() as selector:
            for name in ('stdout', 'stderr'):
                stream = getattr(process, name)
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            if process.stdin:
                if pending:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
                else:
                    process.stdin.close()
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(process.args, timeout)
                for key, _ in selector.select(min(.1, remaining)):
                    if key.data == 'stdin':
                        try:
                            pending = pending[os.write(key.fd, pending[:65536]):]
                        except BrokenPipeError:
                            pending = pending[:0]
                        if not pending:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                            continue
                        output[key.data].extend(chunk)
                        if len(output[key.data]) > (limit if key.data == 'stdout' else 65536):
                            raise PermissionError('actor output exceeds its bounded channel')
            process.wait(timeout=max(.01, deadline - time.monotonic()))
        return bytes(output['stdout']), bytes(output['stderr'])
    except BaseException:
        owner.close()
        process.wait(timeout=5)
        raise
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()


def _path(workspace, value):
    if not isinstance(value, str) or not value.strip() or '\0' in value:
        raise PermissionError('invalid actor workspace path')
    candidate = workspace / value
    try:
        relative = candidate.relative_to(workspace)
    except ValueError as exc:
        raise PermissionError('actor path escaped its workspace') from exc
    cursor = workspace
    for component in relative.parts:
        cursor = cursor / component
        if cursor.is_symlink():
            raise PermissionError('actor paths cannot contain symlinks')
    path = Path(os.path.abspath(candidate))
    if not path.is_relative_to(workspace):
        raise PermissionError('actor path escaped its workspace')
    for item in (path, *path.parents):
        if item == workspace.parent:
            break
        if item.is_symlink():
            raise PermissionError('actor paths cannot contain symlinks')
        if item.exists():
            metadata = item.stat()
            if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                raise PermissionError('actor paths must be regular files or directories')
            if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
                raise PermissionError('actor paths cannot contain hardlink aliases')
    return path


def _restrict_reads(invocation, workspace, scopes, *, writable=False, protected_files=()):
    """Raw actor code cannot read fixture paths outside its assigned scopes."""
    if any(directory and path == workspace for path, directory in scopes):
        return
    if invocation.backend == 'sandbox-exec':
        from openkyrozen.tools.sandbox import _sbpl_path
        if not scopes:
            invocation.command[2] += f'(deny file-read-data (subpath {_sbpl_path(str(workspace))}))'
            return
        allowed = ' '.join(f'({"subpath" if directory else "literal"} {_sbpl_path(str(path))})'
                           for path, directory in scopes)
        ancestors = {parent for path, _ in scopes for parent in path.parents
                     if parent == workspace or parent.is_relative_to(workspace)}
        allowed_ancestors = ' '.join(f'(literal {_sbpl_path(str(path))})' for path in ancestors)
        invocation.command[2] += (
            f'(deny file-read* (require-all (subpath {_sbpl_path(str(workspace))}) '
            f'(require-not (require-any {allowed} {allowed_ancestors}))))'
            + (f'(deny file-read-data {allowed_ancestors})' if ancestors else ''))
    elif invocation.backend == 'bubblewrap':
        argv = invocation.command
        separator = argv.index('--')
        mounts = ['--tmpfs', str(workspace)]
        parents = {parent for path, _ in scopes for parent in path.parents
                   if parent != workspace and parent.is_relative_to(workspace)}
        for parent in sorted(parents, key=lambda path: len(path.parts)):
            mounts += ['--dir', str(parent)]
        for path, directory in scopes:
            if path.exists():
                mounts += ['--bind' if writable and directory else '--ro-bind', str(path), str(path)]
        for guard in protected_files:
            if any(guard == path or directory and guard.is_relative_to(path) for path, directory in scopes):
                mounts += ['--ro-bind', str(guard), str(guard)]
        mounts += ['--remount-ro', str(workspace)]
        invocation.command = argv[:separator] + mounts + argv[separator:]
    else:
        raise PermissionError('actor read isolation requires a supported OS backend')


class _ActorShellPolicy(ShellPolicy):
    def authorize(self, request):
        if self.fixture.revision != self.fixture_revision:
            raise PermissionError('actor fixture policy changed')
        super().authorize(request)
        identity = {key: getattr(self.fixture, key)
                    for key in ('run_id', 'actor_id', 'session_id', 'revision')}
        self.fixture.authorize(dict(request, **identity))


class ActorAssignment:
    """Intersect a requested assignment with the parent's frozen fixture grants."""
    def __init__(self, policy, payload, *, source, model, provider='custom', review_artifacts=()):
        self.payload = _copy(payload)
        payload = self.payload
        self.fixture, self.revision = policy, policy.revision
        self.workspace = policy.workspace
        if (payload.get('schema_version') != 1 or payload.get('source') != str(Path(source).resolve())
                or payload.get('workspace') != str(policy.workspace)
                or payload.get('parent', {}).get('model') != model
                or any(type(payload.get(key)) is not bool for key in ('review', 'report_repair'))):
            raise PermissionError('actor bootstrap does not match the parent source, workspace, or model')
        run = payload.get('run', {})
        brief = run.get('assignment', {})
        self.run_id = run.get('run_id')
        if not isinstance(self.run_id, str) or not self.run_id or len(self.run_id) > 200:
            raise PermissionError('invalid actor run identity')
        profile = run.get('profile')
        if (profile not in _PROFILES or brief.get('custom_profile') is not None
                or brief.get('provider', provider) != provider or brief.get('model', model) != model
                or brief.get('profile', profile) != profile):
            raise PermissionError('actor profile or provider override is outside the frozen fixture')
        self.review = payload['review']
        self.report_repair = payload['report_repair']
        capabilities = _PROFILES['reviewer' if self.review else profile] & policy.capabilities
        self.capabilities = frozenset() if self.report_repair else capabilities
        scopes = brief.get('scope')
        if not isinstance(scopes, list) or not scopes or len(scopes) > 128:
            raise PermissionError('actor assignment requires bounded workspace scopes')
        self.scopes = tuple((_path(self.workspace, value), _path(self.workspace, value).is_dir())
                            for value in scopes)
        if any(not any(path.is_relative_to(bound) for bound in policy.scope_paths)
               for path, _ in self.scopes):
            raise PermissionError('actor scope exceeds approved fixture bounds')
        self.read_scopes = self.scopes
        if self.review:
            extra = tuple((_path(self.workspace, str(path)), Path(path).is_dir()) for path in review_artifacts)
            if any(not any(path.is_relative_to(bound) for bound in policy.scope_paths) for path, _ in extra):
                raise PermissionError('review artifact exceeds immutable fixture bounds')
            self.read_scopes += extra
        directories = tuple(path for path, directory in self.scopes if directory)
        shell_caps = self.capabilities if directories else self.capabilities - {'write'}
        self.shell_policy = _ActorShellPolicy(self.workspace, shell_caps,
            scope_paths=directories or policy.scope_paths, run_id=policy.run_id,
            actor_id=uuid.uuid4().hex, session_id=policy.session_id, revision=policy.revision)
        self.shell_policy.fixture = policy
        self.shell_policy.fixture_revision = policy.revision
        self.shell_policy.effects, self.shell_policy.risk_ceiling = policy.effects, policy.risk_ceiling

    def validate_scopes(self):
        for path, directory in self.read_scopes:
            _path(self.workspace, str(path))
            if path.is_dir() != directory:
                raise PermissionError('actor scope changed its approved file or directory kind')

    def authorize_mutation(self, action, arguments, protected_files):
        if ('write' not in self.capabilities or self.fixture.revision != self.revision
                or action not in _MUTATIONS):
            raise PermissionError('actor has no authority for this file mutation')
        self.validate_scopes()
        from openkyrozen.tools.catalog import builtin_metadata
        specification = builtin_metadata(action)
        if (self.fixture.effects is not None and specification['side_effects'] not in self.fixture.effects
                or specification['risk'] == 'high' and self.fixture.risk_ceiling != 'high'):
            raise PermissionError('file mutation exceeds accepted plan effects or risk')
        arguments = _copy(arguments)
        path = self.mutation_target(action, arguments)
        if (path.is_dir() or not any(path == scope or directory and path.is_relative_to(scope)
                                    for scope, directory in self.scopes)
                or any(path == guard or guard.is_relative_to(path) for guard in protected_files)):
            raise PermissionError('file mutation exceeds actor scope or targets original tests')
        return arguments

    def mutation_target(self, action, arguments):
        try:
            if isinstance(arguments, dict):
                value = arguments['path']
            elif action == 'write_file':
                value, _ = arguments.split('|', 1)
                value = value.strip()
            else:
                value = json.loads(arguments)['path']
            return _path(self.workspace, value)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise PermissionError('invalid file mutation arguments') from exc

    def read_target(self, action, arguments):
        try:
            request = (json.loads(arguments) if isinstance(arguments, str)
                       and arguments.lstrip().startswith('{') else arguments)
            if action == 'find_files':
                if isinstance(request, dict):
                    value, pattern = request.get('directory', '.'), request.get('pattern', '')
                else:
                    parts = request.split('|', 1)
                    pattern, value = parts[0], parts[1] if len(parts) == 2 else '.'
                if not isinstance(pattern, str) or Path(pattern.strip()).is_absolute() or '..' in Path(pattern.strip()).parts:
                    raise PermissionError('find pattern exceeds its assigned directory')
                value = value.strip()
            else:
                value = request.get('path', '.' if action != 'read_file' else '') if isinstance(request, dict) else request
                if action in {'list_dir', 'list_tree'} or not isinstance(request, dict) and action == 'read_file':
                    value = value.strip()
            if action == 'search_files':
                pattern = request.get('glob', '*')
                if not isinstance(pattern, str) or Path(pattern).is_absolute() or '..' in Path(pattern).parts:
                    raise PermissionError('search glob exceeds its assigned scope')
            return _path(self.workspace, value or '.')
        except (ValueError, TypeError, AttributeError) as exc:
            raise PermissionError('invalid actor read arguments') from exc

    def authorize_read(self, action, arguments):
        if 'read' not in self.capabilities or self.fixture.revision != self.revision or action not in _READS:
            raise PermissionError('actor has no authority for this read')
        self.validate_scopes()
        arguments = _copy(arguments)
        path = self.read_target(action, arguments)
        if not any(path == scope or directory and path.is_relative_to(scope)
                   for scope, directory in self.read_scopes):
            raise PermissionError('read exceeds actor assigned scope')
        return arguments


class _OwnedInvocation:
    def __init__(self, invocation, owner):
        self.invocation, self.owner = invocation, owner
        self.lock = threading.Lock()

    def close(self):
        with self.lock:
            self.owner.close()
            self.invocation.close()


class _ActorBroker(ShellBroker):
    def __init__(self, connection, supervisor, actor):
        self.supervisor, self.actor = supervisor, actor
        self.pending = {}
        super().__init__(connection, actor['policy'], supervisor.protected_files)

    def _dispatch(self, request):
        if not isinstance(request, dict):
            raise PermissionError('invalid actor request')
        operation = request.get('op')
        if operation == 'hello':
            if set(request) != {'op'}:
                raise PermissionError('actor identity is kernel-bound')
            return {key: getattr(self.policy, key)
                    for key in ('run_id', 'actor_id', 'session_id', 'revision')}
        if operation == 'actor_bootstrap':
            if self.actor['root']:
                raise PermissionError('root actor has no delegated bootstrap')
            return {'payload': self.actor['assignment'].payload,
                    'provider_config': self.actor['provider_config'],
                    **({'routing_variant':self.supervisor.routing_variant}
                       if self.supervisor.routing_variant is not None else {})}
        if operation == 'routing_policy':
            if set(request) != {'op'}:
                raise PermissionError('routing policy is parent-held')
            return {'variant':self.supervisor.routing_variant}
        if operation == 'delegated_launch':
            if not self.actor['root']:
                raise PermissionError('only the root actor can launch delegated actors')
            return self.supervisor._launch_child(self.actor, request['payload'])
        if operation in {'delegated_poll', 'delegated_stop'}:
            if not self.actor['root']:
                raise PermissionError('only the root actor owns delegated handles')
            return self.supervisor._delegated(self.actor, request)
        if operation == 'delegated_call':
            if self.actor['root']:
                raise PermissionError('root actor cannot issue child callbacks')
            return self.supervisor._callback(self.actor, request)
        if operation == 'file_mutation':
            return self.supervisor._mutate(self.actor, request['action'], request['arguments'])
        if operation == 'file_read':
            return self.supervisor._mutate(self.actor, request['action'], request['arguments'], reading=True)
        if operation == 'ticket':
            with self.supervisor.execution_lock:
                self._reap_pending()
                if len(self.pending) >= 64:
                    raise PermissionError('shell ticket limit reached')
                original = _copy(request['request'])
                self.policy.authorize(original)
                owner = self.supervisor._owner()
                invocation = None
                try:
                    home = self.supervisor._home(owner)
                    environment = _fixture_git_environment(
                        dict(_safe_env(original['env']), HOME=str(home), USERPROFILE=str(home)),
                        self.supervisor.workspace)
                    invocation = protected_shell_command(original['command'], policy=self.policy,
                        cwd=original['cwd'], env=environment, protected_files=self.protected_files)
                    assignment = self.actor.get('assignment')
                    scopes = (assignment.read_scopes if assignment else
                              tuple((path, True) for path in self.policy.scope_paths))
                    _restrict_reads(invocation, self.policy.workspace,
                                    scopes if 'read' in self.policy.capabilities else (),
                                    writable='write' in self.policy.capabilities,
                                    protected_files=self.protected_files)
                    if invocation.shell:
                        raise PermissionError('managed actor shell requires an argument vector')
                    if invocation.temporary_directory:
                        self.supervisor._chown(Path(invocation.temporary_directory.name), owner.uid)
                    concrete = dict(original, env=invocation.env)
                    with self.close_lock:
                        if self.closed.is_set():
                            raise PermissionError('benchmark actor broker is closed')
                        ticket = self.authority.issue(concrete)
                        self.pending[ticket] = (original, concrete, _OwnedInvocation(invocation, owner), time.monotonic() + 5)
                    def expire():
                        time.sleep(5.01)
                        with self.supervisor.execution_lock:
                            self._reap_pending()
                    threading.Thread(target=expire, daemon=True).start()
                    return ticket
                except BaseException:
                    owner.close()
                    if invocation:
                        invocation.close()
                    raise
        if operation == 'start':
            with self.supervisor.execution_lock, self.supervisor._frozen():
                self._reap_pending()
                with self.close_lock:
                    if self.closed.is_set():
                        raise PermissionError('benchmark actor broker is closed')
                    pending = self.pending.get(request.get('ticket'))
                if pending is None:
                    raise PermissionError('invalid or expired shell ticket')
                original, concrete, owned, _ = pending
                try:
                    if _encoded(original) != _encoded(request['request']):
                        raise PermissionError('shell ticket concrete request changed')
                    assignment = self.actor.get('assignment')
                    if assignment:
                        assignment.validate_scopes()
                    else:
                        for path in self.policy.scope_paths:
                            _path(self.policy.workspace, str(path))
                            if not path.is_dir():
                                raise PermissionError('approved shell directory changed kind')
                    _validated_files(self.policy.workspace, self.protected_files)
                    self.authority.consume(request['ticket'], concrete)
                    if sum(p.poll() is None for p in self.processes.values()) >= 32:
                        raise PermissionError('benchmark shell process limit reached')
                    process = owned.owner.popen(owned.invocation.command, cwd=concrete['cwd'],
                        env=concrete['env'], group=self.supervisor.group.uid,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    with self.close_lock:
                        if self.closed.is_set():
                            raise PermissionError('benchmark actor closed during command launch')
                        identifier = uuid.uuid4().hex
                        self.processes[identifier] = process
                        self.records[identifier] = {'stdout': tempfile.TemporaryFile(), 'stderr': tempfile.TemporaryFile(),
                            'background': concrete['background'], 'eof': set(), 'timed_out': False,
                            'lock': threading.Lock(), 'invocation': owned}
                        self.pending.pop(request['ticket'], None)
                except BaseException:
                    owned.close()
                    with self.close_lock:
                        self.pending.pop(request['ticket'], None)
                    raise
                for name in ('stdout', 'stderr'):
                    threading.Thread(target=self._collect, args=(identifier, name), daemon=True).start()
                threading.Thread(target=self._expire, args=(identifier, concrete['timeout_seconds']), daemon=True).start()
                return {'id': identifier, 'pid': process.pid, 'backend': owned.invocation.backend}
        if operation == 'stop':
            record = self.records.get(request.get('id'))
            if record is None:
                raise PermissionError('process does not belong to this actor')
            record['invocation'].close()
            return self.processes[request['id']].returncode
        return super()._dispatch(request)

    def _reap_pending(self):
        for ticket, record in tuple(self.pending.items()):
            if record[3] <= time.monotonic():
                self.pending.pop(ticket)
                self.authority.tickets.pop(ticket, None)
                record[2].close()

    def _expire(self, identifier, timeout):
        process, record = self.processes[identifier], self.records[identifier]
        deadline = time.monotonic() + timeout
        try:
            process.wait(timeout=timeout)
            while len(record['eof']) != 2 and not self.closed.is_set():
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(process.args, timeout)
                time.sleep(.02)
        except subprocess.TimeoutExpired:
            record['timed_out'] = True
        record['invocation'].close()

    def _finish_collection(self, identifier):
        if self.processes[identifier].poll() is not None:
            self.records[identifier]['invocation'].close()

    def close(self, *, graceful=True):
        errors = []
        def cleanup(operation):
            try:
                operation()
            except Exception as exc:
                errors.append(exc)
        with self.close_lock:
            if getattr(self, '_cleanup_complete', False) or getattr(self, '_closing', False):
                return
            self._closing = True
            self.closed.set()
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            cleanup(self.connection.close)
            for ticket, record in tuple(self.pending.items()):
                before = len(errors)
                cleanup(record[2].close)
                self.authority.tickets.pop(ticket, None)
                if len(errors) == before:
                    self.pending.pop(ticket, None)
            for record in tuple(self.records.values()):
                cleanup(record['invocation'].close)
                with record['lock']:
                    for name in record['eof']:
                        cleanup(record[name].close)
            if not self.actor['root']:
                with self.actor['condition']:
                    self.actor['disconnected'] = True
                    self.actor['condition'].notify_all()
            self.supervisor.failures.extend(errors)
        # Never hold the broker lock while the actor stop path re-enters close.
        try:
            self.supervisor._stop_actor(self.actor, exit_grace=.5 if graceful else 0)
        except Exception as exc:
            errors.append(exc)
            self.supervisor.failures.append(exc)
        finally:
            with self.close_lock:
                self._closing = False
                self._cleanup_complete = not errors and self.actor['owner'].closed
        if errors:
            raise errors[0]


class ActorSupervisor:
    """One privileged parent and listener for a root worker and its isolated actors."""
    def __init__(self, command, *, cwd, env, protected_files, policy, source, provider_config=None,
                 revoke_actor=None, routing_variant=None, selected_actor_model=None):
        if os.geteuid() != 0:
            raise PermissionError('actor supervisor requires a privileged parent')
        self.command, self.env = list(command), dict(env)
        if routing_variant is not None:
            from benchmarks.v3_routing_supplement import VARIANTS, MODEL_VARIANTS
            if routing_variant not in VARIANTS+MODEL_VARIANTS:
                raise ValueError('invalid parent routing supplement variant')
        self.routing_variant = routing_variant
        self.env.pop('KYROZEN_BENCHMARK_ROUTING_SUPPLEMENT',None)
        if routing_variant is not None:
            self.env['KYROZEN_BENCHMARK_ROUTING_SUPPLEMENT']='1'
        self.workspace = Path(cwd).resolve(strict=True)
        if self.workspace != policy.workspace:
            raise ValueError('actor workspace must match the parent fixture')
        self.source = Path(source).resolve(strict=True)
        if self.source.is_relative_to(self.workspace) or Path(__file__).resolve().is_relative_to(self.workspace):
            raise PermissionError('trusted selected source and helpers must be outside the writable fixture')
        self.policy = policy
        self.provider_factory = provider_config if callable(provider_config) else None
        self.provider_config = {} if self.provider_factory else _copy(provider_config or {})
        self.revoke_actor = revoke_actor
        self.selected_actor_model = selected_actor_model
        self.model = (self.provider_config.get('model_main') or self.provider_config.get('model_complex')
                      or self._argument('--model'))
        self.protected_files = _validated_files(self.workspace, protected_files)
        self.execution_lock = threading.RLock()
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.owners, self.actors, self.handles, self.author_homes = [], {}, {}, {}
        self.receipt_paths = {}
        self.failures = []
        self.brokers = []
        self.handshakes = threading.BoundedSemaphore(64)
        self.process = None
        self.state = self.socket_directory = self.listener = self.thread = None
        try:
            self.group = self._owner()
            self.state = Path(tempfile.mkdtemp(prefix='openkyrozen-actors-', dir='/var/tmp')).resolve()
            self.state.chmod(0o711)
            self.socket_directory = _actor_socket_directory()
            for directory in (self.socket_directory, *self.socket_directory.parents):
                metadata = directory.stat()
                if (metadata.st_uid != 0 or metadata.st_mode & 0o022 and not metadata.st_mode & stat.S_ISVTX
                        or directory.is_relative_to(self.workspace) or directory.is_relative_to(self.state)):
                    raise PermissionError('actor socket requires immutable root-owned ancestors outside actor state')
            self.address = str(self.socket_directory / 'parent.sock')
            self.listener = socket.socket(socket.AF_UNIX)
            self.listener.bind(self.address)
            os.chmod(self.address, 0o666)
            self.listener.listen(32)
            self.listener.settimeout(.2)
            self._share_workspace()
            self.thread = threading.Thread(target=self._accept, daemon=True)
            self.thread.start()
        except BaseException:
            self.close()
            raise

    def _argument(self, name):
        return self.command[self.command.index(name) + 1] if name in self.command else None

    def _owner(self):
        owner = UIDOwner()
        self.owners.append(owner)
        return owner

    def _chown(self, root, uid):
        for path in (root, *root.rglob('*')):
            if path.is_symlink():
                raise PermissionError('private actor state cannot contain symlinks')
            os.chown(path, uid, uid)
        root.chmod(0o700)

    def _home(self, owner, key=None):
        home = self.author_homes.get(key) if key is not None else None
        if home is None:
            home = self.state / uuid.uuid4().hex
            home.mkdir(mode=0o700)
            (home / 'tmp').mkdir(mode=0o700)
            (home / 'skills').mkdir(mode=0o700)
            if key is not None:
                self.author_homes[key] = home
        self._chown(home, owner.uid)
        return home

    def _share_workspace(self):
        for path in (self.workspace, *self.workspace.rglob('*')):
            if path.is_symlink():
                raise PermissionError('benchmark workspace cannot contain symlinks')
            metadata = path.stat()
            if not (path.is_dir() or path.is_file()) or path.is_file() and metadata.st_nlink != 1:
                raise PermissionError('benchmark workspace requires unaliased regular files')
            os.chown(path, 0, self.group.uid)
            path.chmod(0o2770 if path.is_dir() else stat.S_IMODE(metadata.st_mode) | 0o660)

    def _environment(self, home):
        environment = dict(self.env, HOME=str(home), USERPROFILE=str(home), TMPDIR=str(home / 'tmp'),
            KYROZEN_DB_PATH=str(home / 'runtime.sqlite3'), KYROZEN_SKILLS_DIR=str(home / 'skills'),
            KYROZEN_BENCHMARK_ACTOR_SOCKET=self.address,
            KYROZEN_BENCHMARK_ACTOR_SOCKET_INODE=json.dumps([os.stat(self.address).st_dev, os.stat(self.address).st_ino]))
        for key in ('KYROZEN_BENCHMARK_BROKER_FD', 'KYROZEN_BENCHMARK_BROKER_IDENTITY', 'KYROZEN_SKILLS_PATH'):
            environment.pop(key, None)
        return _fixture_git_environment(environment, self.workspace)

    def _protected_actor(self, command, home, outputs=(), scopes=None):
        # Workers receive no direct workspace writes, including the root worker.
        if scopes is None:
            scopes = (tuple((path, True) for path in self.policy.scope_paths)
                      if 'read' in self.policy.capabilities else ())
        readonly = ShellPolicy(self.workspace, {'read'} if scopes else set(), run_id=self.policy.run_id)
        # Startup libraries inspect cwd; fixture access keeps its explicit workspace paths.
        invocation = protected_shell_command(shlex.join(command), policy=readonly,
            cwd=str(home), env=self._environment(home), protected_files=self.protected_files)
        try:
            if invocation.shell:
                raise PermissionError('actor protection requires a supported OS backend')
            _restrict_reads(invocation, self.workspace, scopes, protected_files=self.protected_files)
            if invocation.backend == 'sandbox-exec':
                from openkyrozen.tools.sandbox import _sbpl_path
                grants = f'(subpath {_sbpl_path(str(home))})' + ''.join(
                    f'(literal {_sbpl_path(str(path))})' for path in outputs)
                invocation.command[2] += '(allow file-write* ' + grants + ')'
            elif invocation.backend == 'bubblewrap':
                argv = invocation.command
                index = argv.index('--')
                mounts = ['--bind', str(home), str(home)]
                for path in outputs:
                    mounts += ['--bind', str(path), str(path)]
                invocation.command = argv[:index] + mounts + argv[index:]
            return invocation
        except BaseException:
            invocation.close()
            raise

    def _spawn(self, command, actor, *, stdout, stderr, stdin=None, home_key=None, outputs=()):
        with self.execution_lock:
            if self.closed.is_set():
                raise RuntimeError('actor supervisor is closed')
            if self.failures:
                raise RuntimeError('prior actor cleanup failed; no new identity may launch')
            owner = self._owner()
            actor['owner'] = owner
            actor['stop_lock'] = threading.RLock()
            actor['stopped'] = False
            actor_id = actor['policy'].actor_id
            invocation = None
            try:
                home = self._home(owner, home_key)
                actor['home'] = home
                actor['provider_config'] = _copy(self.provider_factory(actor_id) if self.provider_factory
                                                 else self.provider_config)
                for path in outputs:
                    if (path.parent != self.workspace.parent or path.is_symlink()
                            or path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
                        raise PermissionError('root actor result path exceeds the fixture')
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                    os.close(descriptor)
                    os.chown(path, owner.uid, owner.uid)
                    path.chmod(0o600)
                assignment = actor.get('assignment')
                capabilities = assignment.capabilities if assignment else self.policy.capabilities
                scopes = assignment.read_scopes if assignment else None
                invocation = self._protected_actor(command, home, outputs,
                    scopes=scopes if 'read' in capabilities else ())
                if invocation.temporary_directory:
                    self._chown(Path(invocation.temporary_directory.name), owner.uid)
                if self.provider_factory:
                    key = actor['provider_config'].get('api_key')
                    if not isinstance(key, str) or not key:
                        raise PermissionError('actor relay factory returned no bound authorization')
                    invocation.env['KYROZEN_BENCHMARK_API_KEY'] = key
                    if actor['root'] and actor['provider_config'].get('provider') == 'google':
                        invocation.env['KYROZEN_BENCHMARK_GOOGLE_POLICY'] = json.dumps({
                            name: actor['provider_config'][name] for name in
                            ('provider', 'base_url', 'model_main', 'model_simple', 'model_complex')}, sort_keys=True)
                actor['invocation'] = invocation
                self.actors[owner.uid] = actor
                process = owner.popen(invocation.command, cwd=home, env=invocation.env,
                                      group=self.group.uid, stdin=stdin, stdout=stdout, stderr=stderr)
                actor['process'] = process
                return process
            except BaseException:
                self.actors.pop(owner.uid, None)
                if self.revoke_actor:
                    self.revoke_actor(actor_id)
                if invocation:
                    invocation.close()
                owner.close()
                raise

    def popen(self, *, stdin=None, stdout=None, stderr=None):
        if self.process is not None:
            raise RuntimeError('root actor has already been launched')
        actor = {'root': True, 'policy': self.policy, 'connected': False, 'broker': None}
        result = self._argument('--result')
        self.process = self._spawn(self.command, actor, stdin=stdin, stdout=stdout, stderr=stderr,
                                   outputs=(Path(result).resolve(),) if result else ())
        self.root_actor = actor
        def monitor():
            self.process.wait()
            self._stop_actor(actor)
        threading.Thread(target=monitor, daemon=True).start()
        return self.process

    def _accept(self):
        while not self.closed.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self.handshakes.acquire(blocking=False):
                connection.close()
                continue
            def authenticate(connection=connection):
                try:
                    self._authenticate(connection)
                finally:
                    self.handshakes.release()
            threading.Thread(target=authenticate, daemon=True).start()

    def _authenticate(self, connection):
        try:
            connection.settimeout(5)
            # The first bounded message also materializes macOS peer credentials.
            hello = bytearray()
            while len(hello) <= 4096 and not hello.endswith(b'\n'):
                chunk = connection.recv(1)
                if not chunk:
                    raise PermissionError('empty actor handshake')
                hello.extend(chunk)
            if len(hello) > 4096 or json.loads(hello) != {'op': 'hello'}:
                raise PermissionError('invalid actor handshake')
            _, uid, _ = peer_identity(connection)
            with self.execution_lock, self.lock:
                actor = self.actors.get(uid)
                if (actor is None or actor['owner'].closed or actor['connected']
                        or actor.get('process') is None or actor['process'].poll() is not None):
                    raise PermissionError('unregistered or already connected actor kernel identity')
                actor['connected'] = True
                identity = {key: getattr(actor['policy'], key)
                            for key in ('run_id', 'actor_id', 'session_id', 'revision')}
                connection.sendall(_encoded({'ok': True, 'value': identity}) + b'\n')
                connection.settimeout(None)
                broker = _ActorBroker(connection, self, actor)
                actor['broker'] = broker
                self.brokers.append(broker)
                return
        except (OSError, ValueError, PermissionError):
            try:
                connection.sendall(b'{"ok":false,"message":"unregistered actor kernel identity"}\n')
            except OSError:
                pass
            connection.close()

    def _launch_child(self, parent, payload):
        with self.execution_lock:
            artifacts = []
            if payload.get('review') is True:
                prior = self.receipt_paths.get(payload.get('run', {}).get('run_id'), set())
                for item in payload.get('run', {}).get('report', {}).get('artifacts', []):
                    value = item if isinstance(item, str) else item.get('path') if isinstance(item, dict) else None
                    if value:
                        path = _path(self.workspace, value)
                        if path in prior:
                            artifacts.append(path)
            selected_model = self.model
            if parent['provider_config'].get('provider') == 'google':
                if not callable(self.selected_actor_model):
                    raise PermissionError('native Google parent model must be held by the gateway')
                selected_model = self.selected_actor_model(parent['policy'].actor_id)
            assignment = ActorAssignment(self.policy, payload, source=self.source, model=selected_model,
                                         provider=parent['provider_config'].get('provider', 'custom'),
                                         review_artifacts=artifacts)
            key = assignment.run_id
            if any(item['assignment'].run_id == key and
                   (item['state'] == 'running' or not item['owner'].closed)
                   for item in self.handles.values()):
                raise PermissionError('the prior author or reviewer invocation is still active')
            if len(self.handles) >= 256:
                raise PermissionError('delegated actor limit reached')
            identifier = uuid.uuid4().hex
            actor = {'root': False, 'policy': assignment.shell_policy, 'assignment': assignment,
                'parent': parent, 'connected': False, 'broker': None, 'condition': threading.Condition(),
                'callbacks': {}, 'state': 'running', 'cancelled': False, 'disconnected': False}
            command = [sys.executable, '-I', str(Path(__file__).with_name('v3_actor_worker.py')),
                       '--source', str(self.source)]
            process = self._spawn(command, actor, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                home_key=None if assignment.review else key)
            self.handles[identifier] = actor
            def collect():
                try:
                    output, _ = _communicate(process, actor['owner'], timeout=900)
                    self._stop_actor(actor)
                    if process.returncode or len(output) > _MAX_MESSAGE:
                        raise RuntimeError('delegated actor exited unsuccessfully')
                    result = _copy(json.loads(output))
                    with actor['condition']:
                        actor['result'], actor['state'] = result, 'complete'
                except Exception as exc:
                    self._stop_actor(actor)
                    with actor['condition']:
                        actor['error'], actor['state'] = type(exc).__name__ + ': delegated actor failed', 'failed'
                finally:
                    with actor['condition']:
                        actor['condition'].notify_all()
            threading.Thread(target=collect, daemon=True).start()
            return {'id': identifier}

    def _callback(self, actor, request):
        operation, values = request.get('operation'), request.get('values')
        if (operation not in _CALLBACK_FIELDS or not isinstance(values, dict)
                or set(values) != _CALLBACK_FIELDS[operation]):
            raise PermissionError('unsupported delegated coordinator operation')
        if operation == 'cancelled':
            # Provider/consumer threads share one RPC channel. This live,
            # parent-held leaf must not wait for another root poll/reply cycle.
            with actor['condition']:
                return bool(self.closed.is_set() or actor['cancelled'] or actor['disconnected'])
        if operation == 'access_acquire':
            if (any(type(values[key]) is not bool for key in ('reading', 'recursive'))
                    or values['path'] is not None and not isinstance(values['path'], str)):
                raise PermissionError('invalid actor access request')
            if values['path'] is not None:
                _path(self.workspace, values['path'])
        with actor['condition']:
            if len(actor['callbacks']) >= 128 or actor['cancelled'] or actor['disconnected']:
                raise PermissionError('delegated callback channel is unavailable')
            identifier = uuid.uuid4().hex
            callback = {'id': identifier, 'op': operation, 'values': _copy(values), 'sent': False}
            actor['callbacks'][identifier] = callback
            deadline = time.monotonic() + 120
            while 'reply' not in callback:
                if (self.closed.is_set() or actor['cancelled'] or actor['disconnected']
                        or time.monotonic() >= deadline):
                    actor['callbacks'].pop(identifier, None)
                    raise PermissionError('delegated callback was cancelled or expired')
                actor['condition'].wait(.1)
            actor['callbacks'].pop(identifier)
            reply = callback['reply']
            if reply['ok'] is not True:
                raise PermissionError(str(reply.get('error', 'parent rejected coordinator operation'))[:1000])
            return reply.get('value')

    def _delegated(self, parent, request):
        actor = self.handles.get(request.get('id'))
        if actor is None or actor['parent'] is not parent:
            raise PermissionError('delegated handle does not belong to this parent')
        if request['op'] == 'delegated_stop':
            self._stop_actor(actor)
            return None
        if (type(request.get('cancelled')) is not bool or not isinstance(request.get('replies'), list)
                or len(request['replies']) > 128):
            raise PermissionError('invalid delegated poll')
        if request['cancelled']:
            self._stop_actor(actor)
        with actor['condition']:
            for reply in request['replies']:
                callback = actor['callbacks'].get(reply.get('id')) if isinstance(reply, dict) else None
                if (callback is None or not callback['sent'] or 'reply' in callback
                        or type(reply.get('ok')) is not bool):
                    raise PermissionError('invalid or replayed delegated callback reply')
                callback['reply'] = _copy(reply)
            actor['condition'].notify_all()
            requests = []
            for callback in actor['callbacks'].values():
                if not callback['sent']:
                    callback['sent'] = True
                    requests.append({key: callback[key] for key in ('id', 'op', 'values')})
            result = {'state': actor['state'], 'requests': requests}
            if actor['state'] == 'complete':
                result['result'] = actor['result']
            elif actor['state'] == 'failed':
                result['error'] = actor.get('error', 'delegated actor cancelled')
            return result

    @contextmanager
    def _frozen(self):
        stopped = set()
        try:
            deadline = time.monotonic() + 5
            prior = None
            while time.monotonic() < deadline:
                uids = {owner.uid for owner in self.owners if not owner.closed}
                owned = {pid: state for pid, (uid, state) in process_snapshot().items()
                         if uid in uids and not state.startswith(('Z', 'X'))}
                if set(owned) == prior and all(state.startswith('T') for state in owned.values()):
                    yield
                    return
                prior = set(owned)
                for pid, state in owned.items():
                    if not state.startswith('T'):
                        try:
                            os.kill(pid, signal.SIGSTOP)
                            stopped.add(pid)
                        except ProcessLookupError:
                            pass
                time.sleep(.01)
            raise PermissionError('actor writers could not be frozen for scoped mutation')
        finally:
            snapshot = process_snapshot()
            uids = {owner.uid for owner in self.owners}
            for pid in stopped:
                if snapshot.get(pid, (None,))[0] in uids:
                    try:
                        os.kill(pid, signal.SIGCONT)
                    except ProcessLookupError:
                        pass

    def _mutate(self, actor, action, arguments, *, reading=False):
        with self.execution_lock, self._frozen():
            assignment = actor.get('assignment')
            if assignment is None:
                assignment = ActorAssignment(self.policy, {'schema_version': 1, 'source': str(self.source),
                    'workspace': str(self.workspace), 'review': False, 'report_repair': False,
                    'parent': {'model': self.model}, 'run': {'run_id': 'root', 'profile': 'coder',
                    'assignment': {'scope': [str(path.relative_to(self.workspace)) for path in self.policy.scope_paths]}}},
                    source=self.source, model=self.model)
            arguments = (assignment.authorize_read(action, arguments) if reading
                         else assignment.authorize_mutation(action, arguments, self.protected_files))
            _validated_files(self.workspace, self.protected_files)
            owner = self._owner()
            invocation = None
            try:
                home = self._home(owner)
                command = [sys.executable, '-I', str(Path(__file__).with_name('v3_actor_worker.py')),
                           '--source', str(self.source), '--file-read' if reading else '--file-mutation']
                environment = {'PATH': self.env.get('PATH', os.defpath), 'HOME': str(home), 'TMPDIR': str(home / 'tmp')}
                invocation = protected_shell_command(shlex.join(command), policy=self.policy,
                    cwd=str(self.workspace), env=environment, protected_files=self.protected_files)
                if reading:
                    _restrict_reads(invocation, self.workspace, assignment.read_scopes,
                                    protected_files=self.protected_files)
                    target = assignment.read_target(action, arguments)
                    if target.is_dir():
                        for path in target.rglob('*'):
                            _path(self.workspace, str(path))
                if invocation.temporary_directory:
                    self._chown(Path(invocation.temporary_directory.name), owner.uid)
                process = owner.popen(invocation.command, cwd=self.workspace, env=invocation.env,
                    group=self.group.uid, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                output, _ = _communicate(process, owner, input_data=_encoded({'workspace': str(self.workspace),
                    'action': action, 'arguments': arguments}), timeout=30, limit=1_000_000)
                if process.returncode or len(output) > 1_000_000:
                    raise RuntimeError('trusted selected-source file mutation helper failed')
                result = _copy(json.loads(output))
                success = (result.get('error') is None if isinstance(result, dict)
                           else isinstance(result, str) and not result.lower().startswith('error'))
                if success and action in _MUTATIONS | {'read_file'} and not assignment.review:
                    path = (assignment.read_target(action, arguments) if reading
                            else assignment.mutation_target(action, arguments))
                    self.receipt_paths.setdefault(assignment.run_id, set()).add(path)
                return result
            finally:
                owner.close()
                if invocation:
                    invocation.close()

    def _stop_actor(self, actor, *, exit_grace=0):
        with actor['stop_lock']:
            if actor['stopped'] or actor.get('stopping'):
                return
            actor['stopping'] = True
            errors = []
            def cleanup(operation):
                try:
                    operation()
                except Exception as exc:
                    errors.append(exc)
            process = actor.get('process')
            if exit_grace and process is not None:
                try:
                    process.wait(timeout=exit_grace)
                except subprocess.TimeoutExpired:
                    pass
            if self.revoke_actor and not actor.get('relay_revoked'):
                def revoke():
                    self.revoke_actor(actor['policy'].actor_id)
                    actor['relay_revoked'] = True
                cleanup(revoke)
            if not actor['root']:
                with actor['condition']:
                    actor['cancelled'] = True
                    actor['condition'].notify_all()
            broker = actor.get('broker')
            if broker:
                cleanup(lambda: broker.close(graceful=False))
            cleanup(actor['owner'].close)
            cleanup(actor['invocation'].close)
            if actor['root']:
                for child in tuple(self.handles.values()):
                    cleanup(lambda child=child: self._stop_actor(child))
            actor['stopping'] = False
            actor['stopped'] = not errors
            if errors:
                self.failures.extend(errors)
                raise RuntimeError('benchmark actor cleanup failed') from errors[0]

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        if self.listener:
            self.listener.close()
        for actor in tuple(self.actors.values()):
            try:
                self._stop_actor(actor)
            except Exception as exc:
                self.failures.append(exc)
        for owner in reversed(self.owners):
            if owner is self.group and any(other is not owner and not other.closed for other in self.owners):
                continue
            try:
                owner.close()
            except Exception as exc:
                self.failures.append(exc)
        if self.thread:
            self.thread.join(timeout=5)
        import shutil
        if all(owner.closed for owner in self.owners):
            for directory in (self.socket_directory, self.state):
                if directory:
                    shutil.rmtree(directory)
        if self.failures:
            raise RuntimeError('benchmark actor cleanup failed') from self.failures[0]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
