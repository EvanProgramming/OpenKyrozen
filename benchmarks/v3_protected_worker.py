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
base, workspace, nested, guard, source = map(pathlib.Path, sys.argv[1:6])
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
if len(sys.argv) > 6 and sys.argv[6] == 'readonly':
    attempt('source_write_denied', lambda: source.write_text('after'))
else:
    source.write_text('after')
print(json.dumps(out))
'''


def _probe_environment(home):
    """Static probes get platform paths and their own home, never parent secrets."""
    environment = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TMPDIR', 'TMP', 'TEMP')
                   if key in os.environ}
    return dict(environment, HOME=str(home), USERPROFILE=str(home), PYTHONNOUSERSITE='1')


def _probe_detach():
    """Trusted static probe only; the owning parent must already have dropped UID."""
    import time
    if os.geteuid() == 0:
        raise PermissionError('a detached probe must never run as the privileged parent')
    reader, writer = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(reader)
        os.setsid()
        os.write(writer, b'1')
        os.closerange(0, 256)
        time.sleep(120)
        os._exit(0)
    os.close(writer)
    ready = os.read(reader, 1)
    os.close(reader)
    if ready != b'1':
        raise RuntimeError('detached actor probe did not establish its new session')
    return child


_ACTOR_PROBE_COMMON = r'''
import contextlib, io, json, os, pathlib, shlex, sys, time, uuid
sys.path.insert(0, sys.argv[1])
from benchmarks.v3_process_owner import connect_actor_client
from benchmarks.v3_protected_worker import _ATTACK_SCRIPT, _probe_detach
configuration = json.loads(sys.argv[2])
workspace = pathlib.Path(configuration['workspace'])
base = pathlib.Path(configuration['base'])
client = connect_actor_client()
pathlib.Path(os.environ['TMPDIR']).joinpath('actor-temp-probe').write_text('private')
def check(value, name):
    if not value:
        raise RuntimeError('actor preflight failed: ' + name)
def denied(operation, **values):
    try:
        client.rpc(operation, **values)
    except PermissionError:
        return True
    return False
def collect(process):
    process.wait(20)
    result = {'exit': process.returncode}
    for name in ('stdout', 'stderr'):
        value = bytearray()
        while chunk := getattr(process, name).read1(4096):
            value.extend(chunk)
        result[name] = value.decode()
    return result
def raw_attacks():
    output = io.StringIO()
    previous = sys.argv
    sys.argv = ['probe', str(base), str(workspace), str(workspace/'nested'),
                str(workspace/'nested/test_guard.py'), str(workspace/'nested/source.py'), 'readonly']
    try:
        with contextlib.redirect_stdout(output):
            exec(_ATTACK_SCRIPT, {})
    finally:
        sys.argv = previous
    return json.loads(output.getvalue())
def private_read_denied(path):
    try:
        pathlib.Path(path).read_text()
    except PermissionError:
        return True
    return False
def wait_release(name):
    path = base/name
    deadline = time.monotonic()+40
    while not path.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('actor preflight parent did not release ' + name)
        time.sleep(.01)
    return json.loads(path.read_text())
def daemon_command():
    code = ('import json,os,sys,time; sys.path.insert(0,sys.argv[1]); '
            'from benchmarks.v3_protected_worker import _probe_detach; '
            '_probe_detach(); print(json.dumps({"uid":os.getuid()}),flush=True); time.sleep(120)')
    process = client.start(shlex.join([sys.executable,'-I','-c',code,sys.argv[1]]),
                           str(workspace), dict(os.environ), 120, background=True)
    value = bytearray()
    while not value.endswith(b'\n'):
        chunk = process.stdout.read1(4096)
        check(bool(chunk), 'detached command ended before its announcement')
        value.extend(chunk)
        check(len(value)<4096, 'detached command announcement')
    return process, json.loads(value)['uid']
'''


_ROOT_ACTOR_PROBE = _ACTOR_PROBE_COMMON + r'''
raw = raw_attacks()
written = client.rpc('file_mutation',action='write_file',arguments={'path':'nested/source.py','content':'helper'})
check(isinstance(written,dict) and written['error'] is None, 'root native file helper')
paths = [str(base),str(workspace),str(workspace/'nested'),str(workspace/'nested/test_guard.py'),str(workspace/'nested/source.py')]
managed = collect(client.start(shlex.join([sys.executable,'-c',_ATTACK_SCRIPT,*paths]),
                               str(workspace),dict(os.environ),10))
check(managed['exit']==0, 'managed attacks command exit')
managed_attacks = json.loads(managed['stdout'])
normal = collect(client.start('printf out; printf err >&2; exit 7',str(workspace),dict(os.environ),5))
check(normal == {'exit':7,'stdout':'out','stderr':'err'}, 'shell stdout stderr and exit')
for background, code in ((False,0),(True,7)):
    late = 'late-background' if background else 'late-foreground'
    closed = collect(client.start('exec >/dev/null 2>&1; sleep .15; printf done > '+late+'; exit '+str(code),
                                  str(workspace),dict(os.environ),5,background=background))
    check(closed['exit']==code, 'closed pipe command exit status')
    read = client.rpc('file_read',action='read_file',arguments={'path':late})
    check(read['data']['content']=='done', 'closed pipe command completed its work')
request = dict(client.identity,call_id=uuid.uuid4().hex,command='printf replay',cwd=str(workspace),
               env={'PATH':os.environ['PATH']},timeout_seconds=5,background=False)
ticket = client.rpc('ticket',request=request)
started = client.rpc('start',ticket=ticket,request=request)
from benchmarks.v3_shell_launcher import RemoteProcess
collect(RemoteProcess(client,started['id'],started['pid'],started['backend']))
replay_denied = denied('start',ticket=ticket,request=request)
environment_code = r"""
import json,os,pathlib
leaks=[key for key in os.environ if key.upper().startswith(('KYROZEN_','DYLD_','LD_','PYTHON'))
       or any(word in key.upper() for word in ('TOKEN','SECRET','PASSWORD','API_KEY'))]
fds=[]
directory='/proc/self/fd' if os.path.isdir('/proc/self/fd') else '/dev/fd'
for descriptor in map(int,os.listdir(directory)):
    if descriptor < 3: continue
    try: os.fstat(descriptor)
    except OSError: continue
    fds.append(descriptor)
pathlib.Path(os.environ['TMPDIR']).joinpath('command-temp-probe').write_text('private')
print(json.dumps({'leaks':leaks,'fds':fds}))
"""
environment = collect(client.start(shlex.join([sys.executable,'-I','-c',environment_code]),str(workspace),
    dict(os.environ,DEEPSEEK_API_KEY='fake-secret',GITHUB_TOKEN='fake-token',
         KYROZEN_BENCHMARK_BROKER_FD='123',KYROZEN_BENCHMARK_ACTOR_SOCKET='fake-socket'),5))
check(environment['exit']==0, 'command private temporary directory')
environment = json.loads(environment['stdout'])
private = pathlib.Path(os.environ['HOME'])/'root-private'
private.write_text(os.environ['KYROZEN_BENCHMARK_API_KEY'])
command, command_uid = daemon_command()
print(json.dumps({'phase':'root-ready','uid':os.getuid(),'identity':client.identity,
                  'handle':command.identifier,'command_uid':command_uid,'private':str(private)}),flush=True)
released = wait_release('release-root')
homes_isolated = private_read_denied(released['child_private'])
command.stop()
command.wait(5)
cancelled = command.poll() is not None
disconnected, disconnect_uid = daemon_command()
print(json.dumps({'phase':'disconnect-ready','uid':disconnect_uid}),flush=True)
wait_release('release-disconnect')
result = {'raw':raw,'managed':managed_attacks,'replay_denied':replay_denied,
          'shell_environment_isolated':not environment['leaks'],'shell_descriptors_isolated':not environment['fds'],
          'private_home_isolated':homes_isolated,'actor_tmpdir_writable':True,'command_cancelled':cancelled,
          'closed_pipe_exit_preserved':True}
client.stream.close()
client.connection.close()
print(json.dumps(result),flush=True)
'''


_CHILD_ACTOR_PROBE = _ACTOR_PROBE_COMMON + r'''
root = configuration['root']
request = dict(root['identity'],call_id=uuid.uuid4().hex,command='printf forbidden',cwd=str(workspace),
               env={'PATH':os.environ['PATH']},timeout_seconds=5,background=False)
forged = denied('ticket',request=request) and denied('hello',actor_id=root['identity']['actor_id'])
cross_handles = denied('poll',id=root['handle']) and denied('stop',id=root['handle'])
child_launch = denied('delegated_launch',payload={})
root_private = private_read_denied(root['private'])
check(os.environ['KYROZEN_BENCHMARK_API_KEY'] != configuration['root_key'], 'per actor relay credential')
raw = raw_attacks()
directory_shell = None
if configuration.get('directory_shell'):
    paths = [str(base),str(workspace),str(workspace/'nested'),str(workspace/'nested/test_guard.py'),str(workspace/'nested/source.py')]
    managed = collect(client.start(shlex.join([sys.executable,'-c',_ATTACK_SCRIPT,*paths]),
                                   str(workspace/'nested'),dict(os.environ),10))
    check(managed['exit']==0,'directory-scoped managed shell exit')
    directory_shell = json.loads(managed['stdout'])
read = client.rpc('file_read',action='read_file',arguments={'path':'nested/source.py'})
changed = client.rpc('file_mutation',action='edit_file',arguments={'path':'nested/source.py',
    'old_text':read['data']['content'],'new_text':'child','expected_sha256':read['data']['sha256']})
check(changed['error'] is None,'child original atomic editor')
fresh = client.rpc('file_read',action='read_file',arguments={'path':'nested/source.py'})
created = client.rpc('file_mutation',action='write_file',arguments={'path':'nested/created.py','content':'new'})
check(created['error'] is None,'child original file creation')
new = client.rpc('file_read',action='read_file',arguments={'path':'nested/created.py'})
fresh_reads = fresh['data']['content']=='child' and new['data']['content']=='new'
guard_denied = denied('file_mutation',action='write_file',arguments={'path':'nested/test_guard.py','content':'bad'})
private = pathlib.Path(os.environ['HOME'])/'child-private'
private.write_text(os.environ['KYROZEN_BENCHMARK_API_KEY'])
_probe_detach()
print(json.dumps({'phase':'child-ready','uid':os.getuid(),'identity':client.identity,'private':str(private),
    'forged_identity_denied':forged,'cross_actor_handles_denied':cross_handles,
    'child_launch_denied':child_launch,'private_home_isolated':root_private,
    'fresh_file_reads':fresh_reads,'guard_mutation_denied':guard_denied,'raw':raw,
    'actor_tmpdir_writable':True,'directory_shell':directory_shell}),flush=True)
wait_release(configuration['release'])
client.stream.close()
client.connection.close()
print(json.dumps({'finished':True}),flush=True)
'''


_NO_READ_ACTOR_PROBE = _ACTOR_PROBE_COMMON + r'''
from openkyrozen.app.bootstrap import build_application
application = build_application(surface='cli',user_id='preflight')
application.runtime._set_workspace_root(str(workspace))
application.close()
try:
    (workspace/'nested/source.py').read_text()
    denied=False
except OSError:
    denied=True
try:
    names=[path.name for path in workspace.iterdir()]
except OSError:
    names=[]
print(json.dumps({'started':True,'read_denied':denied,'names':names}),flush=True)
'''


def _verify_actor_worker_protection():
    """Exercise static hostile actors; this does not replace product-driver tests."""
    import selectors
    import time
    import uuid
    from benchmarks.v3_actor_supervisor import ActorAssignment, ActorSupervisor, _communicate
    from benchmarks.v3_process_owner import process_snapshot

    def line(process):
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(40):
                raise ValueError('benchmark actor preflight did not announce readiness')
            encoded = process.stdout.readline(65537)
        if not encoded.endswith(b'\n') or len(encoded) > 65536:
            diagnostic = ''
            try:
                os.set_blocking(process.stderr.fileno(), False)
                diagnostic = os.read(process.stderr.fileno(), 65536).decode(errors='replace')[-500:]
            except OSError:
                pass
            raise ValueError('benchmark actor preflight returned an invalid readiness message: '+diagnostic)
        return json.loads(encoded)

    def live_count(uid):
        return sum(identity == uid and not state.startswith(('Z', 'X'))
                   for identity, state in process_snapshot().values())

    def expect_uid(uid, *, minimum=0):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            count = live_count(uid)
            if (minimum and count >= minimum) or (not minimum and count == 0):
                return
            time.sleep(.01)
        raise ValueError('benchmark actor preflight kernel UID lifecycle probe failed')

    def release(base, name, value=None):
        temporary = base / (name + '.pending')
        temporary.write_text(json.dumps(value or {}), encoding='utf-8')
        temporary.chmod(0o644)
        temporary.replace(base / name)

    with tempfile.TemporaryDirectory(prefix='v3-actor-protection-', dir='/var/tmp') as directory:
        base = Path(directory).resolve()
        base.chmod(0o755)
        workspace = base / 'workspace'
        nested = workspace / 'nested'
        nested.mkdir(parents=True)
        guard, source_file = nested / 'test_guard.py', nested / 'source.py'
        guard.write_text('original')
        source_file.write_text('before')
        selected_source = Path(__file__).resolve().parents[1]
        configuration = {'workspace': str(workspace), 'base': str(base)}
        command = [sys.executable, '-I', '-c', _ROOT_ACTOR_PROBE, str(selected_source),
                   json.dumps(configuration), '--model', 'preflight-model']
        policy = ShellPolicy(workspace, {'read', 'write', 'shell', 'network'}, actor_id='preflight-root')
        issued, revoked = set(), set()
        def provider(actor_id):
            issued.add(actor_id)
            return {'provider': 'openai', 'api_key': 'preflight-' + uuid.uuid4().hex,
                    'base_url': 'http://127.0.0.1:9/v1', 'model_main': 'preflight-model',
                    'model_simple': 'preflight-model', 'model_complex': 'preflight-model'}
        environment = dict(_probe_environment(base), KYROZEN_DISABLE_VECTOR_INDEX='1')
        with ActorSupervisor(command, cwd=workspace, env=environment,
                protected_files=[guard], policy=policy, source=selected_source,
                provider_config=provider, revoke_actor=revoked.add) as supervisor:
            root = supervisor.popen(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            root_ready = line(root)
            root_actor = supervisor.root_actor
            if (root_ready['phase'] != 'root-ready' or root_ready['uid'] != root_actor['owner'].uid
                    or root_ready['identity']['actor_id'] != policy.actor_id):
                raise ValueError('benchmark root actor kernel identity probe failed')
            expect_uid(root_ready['command_uid'], minimum=2)
            with socket.socket(socket.AF_UNIX) as outsider:
                outsider.settimeout(5)
                outsider.connect(supervisor.address)
                outsider.sendall(b'{"op":"hello"}\n')
                with outsider.makefile('rb') as stream:
                    unauthenticated = json.loads(stream.readline(4097))
                if unauthenticated.get('ok') is not False:
                    raise ValueError('unregistered kernel UID obtained benchmark actor authority')
            def child_probe(name, scope, *, directory_shell=False):
                (nested / 'sym').unlink(missing_ok=True)
                payload = {'schema_version': 1, 'source': str(selected_source), 'workspace': str(workspace),
                    'review': False, 'report_repair': False, 'parent': {'model': 'preflight-model'},
                    'run': {'run_id': name, 'profile': 'coder', 'assignment': {'scope': scope}}}
                assignment = ActorAssignment(policy, payload, source=selected_source,
                                              model='preflight-model', provider='openai')
                actor = {'root': False, 'policy': assignment.shell_policy, 'assignment': assignment,
                    'connected': False, 'broker': None, 'condition': threading.Condition(),
                    'callbacks': {}, 'state': 'running', 'cancelled': False, 'disconnected': False}
                child_configuration = dict(configuration, root=root_ready, directory_shell=directory_shell,
                    root_key=root_actor['provider_config']['api_key'], release='release-'+name)
                child = supervisor._spawn([sys.executable, '-I', '-c', _CHILD_ACTOR_PROBE,
                    str(selected_source), json.dumps(child_configuration)], actor,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                ready = line(child)
                if (ready['phase'] != 'child-ready' or ready['uid'] != actor['owner'].uid
                        or ready['uid'] == root_ready['uid']
                        or ready['identity']['actor_id'] != assignment.shell_policy.actor_id):
                    raise ValueError('benchmark child actor kernel identity probe failed')
                expect_uid(ready['uid'], minimum=2)
                expect_uid(root_ready['command_uid'], minimum=2)
                release(base, 'release-'+name)
                output, error = _communicate(child, actor['owner'], timeout=20)
                if child.returncode or json.loads(output) != {'finished': True}:
                    raise ValueError('benchmark child actor preflight failed: ' + error.decode()[-500:])
                expect_uid(ready['uid'])
                return ready
            children = [child_probe('file-child', ['nested/source.py', 'nested/created.py']),
                        child_probe('directory-child', ['nested'], directory_shell=True)]
            release(base, 'release-root', {'child_private': children[0]['private']})
            disconnect_ready = line(root)
            if disconnect_ready['phase'] != 'disconnect-ready':
                raise ValueError('benchmark shell disconnect probe did not start')
            expect_uid(root_ready['command_uid'])
            expect_uid(disconnect_ready['uid'], minimum=2)
            release(base, 'release-disconnect')
            root_output, root_error = _communicate(root, root_actor['owner'], timeout=20)
            if root.returncode:
                raise ValueError('benchmark root actor preflight failed: ' + root_error.decode()[-500:])
            root_result = json.loads(root_output)
            expect_uid(disconnect_ready['uid'])
            expected = {'write', 'delete', 'replace', 'hardlink', 'symlink_write',
                        'nested_move', 'workspace_move', 'parent_move'}
            attacks = [(root_result['raw'], expected | {'source_write_denied'}),
                       (root_result['managed'], expected), (children[1]['directory_shell'], expected)]
            attacks += [(child['raw'], expected | {'source_write_denied'}) for child in children]
            for values, keys in attacks:
                if set(values) != keys or not all(value is True for value in values.values()):
                    raise ValueError('benchmark actor raw or managed attack was not denied')
            checks = [root_result[key] for key in ('replay_denied', 'shell_environment_isolated',
                'shell_descriptors_isolated', 'private_home_isolated', 'actor_tmpdir_writable', 'command_cancelled',
                'closed_pipe_exit_preserved')]
            checks += [child[key] for child in children for key in ('forged_identity_denied', 'cross_actor_handles_denied',
                'child_launch_denied', 'private_home_isolated', 'fresh_file_reads', 'guard_mutation_denied',
                'actor_tmpdir_writable')]
            if (not all(value is True for value in checks) or guard.read_text() != 'original'
                    or source_file.read_text() != 'child'):
                raise ValueError('benchmark actor authorization or native file helper probe failed')
            owners = tuple(supervisor.owners)
        owned = {owner.uid for owner in owners}
        if not all(owner.closed for owner in owners) or any(
                uid in owned and not state.startswith(('Z', 'X'))
                for uid, state in process_snapshot().values()):
            raise ValueError('benchmark actor attack writers were not certified stopped')
        alias = nested / 'sym'
        if alias.is_symlink() and alias.readlink() == guard:
            alias.unlink()
        elif alias.exists() or alias.is_symlink():
            raise ValueError('benchmark actor attack left an unexpected alias')
        for repair in (False, True):
            no_read_policy = ShellPolicy(workspace, {'read', 'write'} if repair else set())
            no_read_command = [sys.executable, '-I', '-c', _NO_READ_ACTOR_PROBE,
                               str(selected_source), json.dumps(configuration)]
            with ActorSupervisor(no_read_command, cwd=workspace, env=environment,
                    protected_files=[guard], policy=no_read_policy, source=selected_source) as no_read:
                if repair:
                    payload = {'schema_version': 1, 'source': str(selected_source), 'workspace': str(workspace),
                        'review': False, 'report_repair': True, 'parent': {'model': None},
                        'run': {'run_id': 'report-repair', 'profile': 'coder', 'assignment': {'scope': ['.']}}}
                    assignment = ActorAssignment(no_read_policy, payload, source=selected_source, model=None)
                    actor = {'root': False, 'policy': assignment.shell_policy, 'assignment': assignment,
                        'connected': False, 'broker': None, 'condition': threading.Condition(),
                        'callbacks': {}, 'state': 'running', 'cancelled': False, 'disconnected': False}
                    process = no_read._spawn(no_read_command, actor, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                else:
                    process = no_read.popen(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    actor = no_read.root_actor
                output, error = _communicate(process, actor['owner'], timeout=30)
                if process.returncode or json.loads(output) != {'started': True, 'read_denied': True, 'names': []}:
                    raise ValueError('benchmark no-read native startup or data isolation probe failed: '+error.decode()[-500:])
                owners += tuple(no_read.owners)
        live = sorted(pid for pid, (uid, state) in process_snapshot().items()
                      if uid in {owner.uid for owner in owners} and not state.startswith(('Z', 'X')))
        if live or not all(owner.closed for owner in owners) or issued != revoked:
            raise ValueError('benchmark actor preflight did not certify all owned UIDs and relay aliases closed')
        return {'authenticated_child_isolation': True, 'daemonized_process_cleanup_proven': True,
            'direct_attacks_denied': True, 'managed_shell_attacks_denied': True, 'source_write_allowed': True,
            'forged_identity_denied': True, 'cross_actor_handles_denied': True, 'ticket_replay_denied': True,
            'private_homes_isolated': True, 'fresh_file_reads': True, 'shell_environment_isolated': True,
            'shell_descriptors_isolated': True, 'actor_tmpdir_writable': True, 'command_cancel_cleanup': True,
            'actor_disconnect_cleanup': True, 'shell_disconnect_cleanup': True, 'all_owned_uids_empty': True,
            'directory_scope_guard_safe': True, 'closed_pipe_exit_preserved': True,
            'no_read_runtime_started': True, 'no_read_fixture_data_denied': True,
            'owned_uid_live_processes': live, 'checked_uid_count': len(owners),
            'probe': 'trusted-static-actors', 'backend': 'sandbox-exec' if sys.platform == 'darwin' else 'bubblewrap'}


def verify_worker_protection(*, require_authenticated_children=True):
    """Run real attacks before key lookup with a privileged owning parent.

    False selects component diagnostics only and never asserts actor isolation or
    detached process cleanup. The returned false evidence is release-blocking.
    """
    if require_authenticated_children:
        if not hasattr(os, 'geteuid') or os.geteuid() != 0:
            raise PermissionError('full benchmark protection requires a privileged parent')
        return _verify_actor_worker_protection()
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
        wrapped, env = protect_worker_command(command, cwd=workspace, env=_probe_environment(base), protected_files=[guard])
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
        env = dict(_probe_environment(base), PYTHONPATH=str(Path(__file__).resolve().parents[1]),
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
        return {'authenticated_child_isolation': False, 'daemonized_process_cleanup_proven': False,
                'direct_attacks_denied': True, 'managed_shell_attacks_denied': True,
                'source_write_allowed': True, 'backend': 'sandbox-exec' if sys.platform == 'darwin' else 'bubblewrap'}
