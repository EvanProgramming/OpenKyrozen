"""Real local security gates for the benchmark-only external shell launcher."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

from benchmarks import v3_protected_worker as protected
from benchmarks.v3_shell_launcher import ShellPolicy, TicketAuthority


class TicketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.policy = ShellPolicy(self.root, frozenset({'read', 'write', 'shell'}),
                                  run_id='run', actor_id='actor', session_id='session')
        self.auth = TicketAuthority(self.policy)
        self.request = {'run_id': 'run', 'actor_id': 'actor', 'session_id': 'session',
                        'call_id': 'call', 'command': 'printf hello', 'cwd': str(self.root),
                        'env': {'PATH': os.environ['PATH'], 'MESSAGE': 'hello'}, 'revision': 0, 'timeout_seconds': 1, 'background': False}

    def test_ticket_binds_every_execution_field_and_is_single_use(self):
        for field, value in [('command', 'touch hacked'), ('cwd', '/'), ('env', {'MESSAGE': 'bad'}),
                             ('actor_id', 'other'), ('session_id', 'other'), ('call_id', 'other')]:
            with self.subTest(field=field):
                ticket = self.auth.issue(self.request)
                with self.assertRaises(PermissionError):
                    self.auth.consume(ticket, dict(self.request, **{field: value}))
        ticket = self.auth.issue(self.request)
        self.auth.consume(ticket, self.request)
        with self.assertRaises(PermissionError):
            self.auth.consume(ticket, self.request)

    def test_expired_and_stale_policy_tickets_fail_closed(self):
        ticket = self.auth.issue(self.request, ttl_seconds=0.001)
        time.sleep(.01)
        with self.assertRaises(PermissionError):
            self.auth.consume(ticket, self.request)
        ticket = self.auth.issue(self.request)
        self.policy.narrow(capabilities={'read'}, scope_paths=['.'])
        with self.assertRaises(PermissionError):
            self.auth.consume(ticket, self.request)
        with self.assertRaises(PermissionError):
            self.auth.issue(dict(self.request, revision=1))

    def test_worker_claimed_approval_and_child_expansion_do_not_grant_shell(self):
        self.policy.narrow(capabilities={'read'}, scope_paths=['.'])
        with self.assertRaises(PermissionError):
            self.auth.issue(dict(self.request, revision=1, approved=True, capabilities=['shell']))
        child = self.policy.child('child', capabilities={'read', 'write', 'shell'})
        self.assertEqual(child.capabilities, frozenset({'read'}))
        with self.assertRaises(ValueError):
            self.policy.narrow(capabilities={'read', 'shell'}, scope_paths=['.'])

    def test_scope_cannot_be_widened_by_cwd_or_symlink(self):
        (self.root / 'allowed').mkdir()
        self.policy.narrow(capabilities={'read', 'shell'}, scope_paths=['allowed'])
        with self.assertRaises(PermissionError):
            self.auth.issue(dict(self.request, revision=1))
        (self.root / 'allowed' / 'escape').symlink_to(self.root)
        with self.assertRaises(PermissionError):
            self.auth.issue(dict(self.request, revision=1, cwd=str(self.root / 'allowed' / 'escape')))


@unittest.skipUnless(sys.platform == 'darwin' or sys.platform.startswith('linux'), 'Unix sandbox required')
class ProtectedWorkerTests(unittest.TestCase):
    def test_security_preflight_executes_direct_and_external_managed_shell_attacks(self):
        evidence = protected.verify_worker_protection(require_authenticated_children=False)
        self.assertTrue(evidence['direct_attacks_denied'])
        self.assertTrue(evidence['managed_shell_attacks_denied'])
        self.assertTrue(evidence['source_write_allowed'])

    def test_real_preflight_probes_receive_no_parent_provider_credentials(self):
        from unittest.mock import patch
        from benchmarks import v3_matched_live
        allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'TMPDIR', 'TMP', 'TEMP'}
        secrets = {'DEEPSEEK_API_KEY': 'fake-deepseek-secret', 'OPENAI_API_KEY': 'fake-openai-secret',
                   'ANTHROPIC_API_KEY': 'fake-anthropic-secret', 'AWS_SECRET_ACCESS_KEY': 'fake-aws-secret',
                   'KYROZEN_BENCHMARK_API_KEY': 'fake-benchmark-secret', 'GITHUB_TOKEN': 'fake-github-secret'}
        incoming, managed, launched, probe_homes = [], [], [], []
        original_protect, original_worker = protected.protect_worker_command, protected.ProtectedWorker
        def protect(command, **kwargs):
            incoming.append(dict(kwargs['env']))
            probe_homes.append(str(Path(kwargs['cwd']).resolve().parent))
            return original_protect(command, **kwargs)
        def worker(command, **kwargs):
            managed.append(dict(kwargs['env']))
            launch = original_worker(command, **kwargs)
            launched.append(dict(launch.env))
            return launch
        with tempfile.TemporaryDirectory() as home:
            environment = {key: value for key, value in os.environ.items() if key in allowed}
            environment.update(secrets, HOME=home, USERPROFILE=home, PYTHONNOUSERSITE='0',
                               UNRELATED_PARENT_STATE='must-not-forward')
            with patch.dict(os.environ, environment, clear=True), patch.object(
                    protected, 'protect_worker_command', side_effect=protect), patch.object(
                    protected, 'ProtectedWorker', side_effect=worker), patch.object(
                    v3_matched_live, '_credential', side_effect=AssertionError('credential lookup before protection')):
                evidence = protected.verify_worker_protection(require_authenticated_children=False)
        self.assertTrue(evidence['direct_attacks_denied'])
        self.assertTrue(evidence['managed_shell_attacks_denied'])
        self.assertTrue(evidence['source_write_allowed'])
        self.assertEqual(len(incoming), 2)
        self.assertEqual(len(managed), 1)
        explicit = {'HOME', 'USERPROFILE', 'PYTHONNOUSERSITE'}
        self.assertLessEqual(set(incoming[0]), allowed | explicit)
        self.assertLessEqual(set(managed[0]), allowed | explicit | {'PYTHONPATH', 'KYROZEN_SANDBOX_REQUIRED'})
        self.assertEqual(managed[0]['KYROZEN_SANDBOX_REQUIRED'], '1')
        self.assertEqual(managed[0]['PYTHONPATH'], str(Path(protected.__file__).resolve().parents[1]))
        for environment in incoming + managed + launched:
            self.assertTrue(set(secrets).isdisjoint(environment))
            self.assertNotIn('UNRELATED_PARENT_STATE', environment)
            self.assertEqual(environment['HOME'], probe_homes[0])
            self.assertEqual(environment['USERPROFILE'], probe_homes[0])
            self.assertEqual(environment['PYTHONNOUSERSITE'], '1')
            self.assertNotEqual(environment['HOME'], home)
            for value in secrets.values():
                self.assertNotIn(value, json.dumps(environment))

    def test_external_launcher_preserves_background_timeout_and_disconnect_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            guard = root / 'test_guard.py'
            guard.write_text('original')
            policy = ShellPolicy(root, frozenset({'read', 'write', 'shell'}))
            script = '''
import json, os, sys, time
from benchmarks.v3_shell_launcher import install_worker_adapter
install_worker_adapter()
from openkyrozen.tools.processes import SessionProcessManager
m = SessionProcessManager('owner', sys.argv[1], 'session')
h = m.start('printf out; printf err >&2; exit 7', cwd=sys.argv[1], env=dict(os.environ), timeout_seconds=5)
h.process.wait(10)
time.sleep(.2)
r = m.read(h.process_id)
e = m.read(h.process_id, stream='stderr')
print(json.dumps([r['exit_code'], r['output'], e['output']]), flush=True)
h = m.start('sleep 100 & wait', cwd=sys.argv[1], env=dict(os.environ), timeout_seconds=.2)
h.process.wait(10)
time.sleep(.2)
print(json.dumps([h.timed_out, h.process.poll() is not None]), flush=True)
h = m.start('sleep 100 & wait', cwd=sys.argv[1], env=dict(os.environ), timeout_seconds=100)
print('started', flush=True)
os._exit(0)
'''
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            with protected.ProtectedWorker([sys.executable, '-c', script, str(root)], cwd=root, env=env,
                                           protected_files=[guard], policy=policy) as launch:
                process = subprocess.Popen(launch.command, cwd=root, env=launch.env,
                                           pass_fds=launch.pass_fds, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                launch.attach(process)
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertEqual(json.loads(stdout.splitlines()[0]), [7, 'out', 'err'])
                self.assertEqual(json.loads(stdout.splitlines()[1]), [True, True])
                broker = launch.broker
            self.assertFalse(any(p.poll() is None for p in broker.processes.values()))

class ChildPolicyTests(unittest.TestCase):
    def test_parent_narrowing_invalidates_outstanding_child_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            parent = ShellPolicy(root, {'read', 'write', 'shell'}, run_id='run')
            child = parent.child('child', capabilities={'read', 'write', 'shell'})
            authority = TicketAuthority(child)
            request = {'run_id': 'run', 'actor_id': 'child', 'session_id': 'benchmark',
                       'call_id': 'child-call', 'command': 'true', 'cwd': str(root),
                       'env': {}, 'revision': 0, 'timeout_seconds': 1, 'background': False}
            ticket = authority.issue(request)
            parent.narrow(capabilities={'read'}, scope_paths=['.'])
            with self.assertRaises(PermissionError):
                authority.consume(ticket, request)

class ParentAuthorizationTests(unittest.TestCase):
    def test_accepted_plan_capability_and_effect_bounds_are_authoritative(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            bounds = {'workspace_paths': ['.'], 'capabilities': ['read', 'write'],
                      'effects': ['read_only', 'filesystem', 'internal'], 'risk_ceiling': 'normal'}
            policy = ShellPolicy(root, {'read', 'write', 'shell'}, accepted_bounds=bounds)
            self.assertEqual(policy.capabilities, frozenset({'read', 'write'}))
            request = {'run_id': policy.run_id, 'actor_id': policy.actor_id,
                       'session_id': policy.session_id, 'call_id': 'call', 'command': 'true',
                       'cwd': str(root), 'env': {}, 'revision': 0, 'timeout_seconds': 1, 'background': False}
            with self.assertRaises(PermissionError):
                TicketAuthority(policy).issue(request)
            bounds['capabilities'].append('shell')
            policy = ShellPolicy(root, {'read', 'write', 'shell'}, accepted_bounds=bounds)
            request['run_id'] = policy.run_id
            with self.assertRaises(PermissionError):
                TicketAuthority(policy).issue(request)

    def test_parent_independently_blocks_dangerous_commands_and_unbounded_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = ShellPolicy(Path(directory), {'read', 'write', 'shell'})
            request = {'run_id': policy.run_id, 'actor_id': policy.actor_id,
                       'session_id': policy.session_id, 'call_id': 'call', 'command': 'rm -rf /tmp/probe',
                       'cwd': str(Path(directory).resolve()), 'env': {}, 'revision': 0, 'timeout_seconds': 1, 'background': False}
            with self.assertRaises(PermissionError):
                TicketAuthority(policy).issue(request)
            for timeout in (0, True, 1801, float('nan'), float('inf')):
                with self.subTest(timeout=timeout), self.assertRaises(PermissionError):
                    TicketAuthority(policy).issue(dict(request, command='true', timeout_seconds=timeout))

@unittest.skipUnless(sys.platform == 'darwin' or sys.platform.startswith('linux'), 'Unix sandbox required')
class _BrokerCase(unittest.TestCase):
    def run_worker(self, script, *, scope_paths=('.',)):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            guard = root / 'test_guard.py'
            guard.write_text('original')
            (root / 'allowed').mkdir()
            policy = ShellPolicy(root, {'read', 'write', 'shell'}, scope_paths=scope_paths)
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            with protected.ProtectedWorker([sys.executable, '-c', script], cwd=root, env=env,
                                           protected_files=[guard], policy=policy) as launch:
                process = subprocess.Popen(launch.command, cwd=root, env=launch.env,
                                           pass_fds=launch.pass_fds, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                launch.attach(process)
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertEqual(guard.read_text(), 'original')
            return stdout.decode()

class BrokerBoundaryTests(_BrokerCase):
    def test_parent_timeout_applies_to_an_uncooperative_worker_and_returns_host_pid(self):
        output = self.run_worker('''
import json, os, time
from benchmarks.v3_shell_launcher import BrokerClient
client = BrokerClient(int(os.environ.pop('KYROZEN_BENCHMARK_BROKER_FD')),
                      json.loads(os.environ.pop('KYROZEN_BENCHMARK_BROKER_IDENTITY')))
p = client.start('sleep 100', os.getcwd(), dict(os.environ), .2)
time.sleep(.5)
print(json.dumps([p.poll() is not None, p.timed_out, type(p.pid) is int and p.pid > 0]))
''')
        self.assertEqual(json.loads(output), [True, True, True])

    def test_forged_actor_is_denied_and_real_shell_has_no_control_socket_or_credentials(self):
        output = self.run_worker('''
import json, os, shlex, sys
from benchmarks.v3_shell_launcher import BrokerClient
identity = json.loads(os.environ.pop('KYROZEN_BENCHMARK_BROKER_IDENTITY'))
client = BrokerClient(int(os.environ.pop('KYROZEN_BENCHMARK_BROKER_FD')), identity)
client.identity = dict(identity, actor_id='forged-child')
try:
    client.start('true', os.getcwd(), {}, 1)
    raise AssertionError('forged actor granted shell')
except PermissionError:
    pass
client.identity = identity
probe = "import os,socket,stat; assert not any('BROKER' in k or 'API_KEY' in k for k in os.environ); sockets=[]; " + \\
        "exec(\\\"for fd in range(3,256):\\\\n try:\\\\n  mode=os.fstat(fd).st_mode\\\\n except OSError:\\\\n  continue\\\\n if stat.S_ISSOCK(mode): sockets.append(fd)\\\"); assert not sockets, sockets; print('isolated')"
p = client.start(shlex.join([sys.executable, '-c', probe]), os.getcwd(),
                 dict(os.environ, KYROZEN_BENCHMARK_API_KEY='must-not-inherit'), 5)
p.wait(10)
assert p.returncode == 0, p.stderr.read1(4096)
print(p.stdout.read1(4096).decode().strip())
''')
        self.assertEqual(output.strip(), 'isolated')

    def test_full_campaign_gate_rejects_unproved_same_process_actor_authentication(self):
        with self.assertRaisesRegex(ValueError, 'actor isolation is unavailable'):
            protected.verify_worker_protection()

class ForegroundAdapterTests(_BrokerCase):
    def test_foreground_adapter_uses_external_shell_and_preserves_exit_output_and_cancellation(self):
        output = self.run_worker('''
import json, os, threading
from benchmarks.v3_shell_launcher import install_worker_adapter
install_worker_adapter()
from openkyrozen.tools import ToolAdapters
adapter = ToolAdapters(os.getcwd())
# A nested launch is the regression: the shell must be started by the parent.
from openkyrozen.tools import shell
def nested_forbidden(*args, **kwargs):
    raise AssertionError('nested worker sandbox invoked')
shell.sandbox_command = nested_forbidden
r = adapter.run_command({'command':'printf out; printf err >&2; exit 7'})
cancelled = threading.Event()
threading.Timer(.1, cancelled.set).start()
c = adapter.run_command({'command':'sleep 100'}, cancelled=cancelled)
t = adapter.run_command({'command':'sleep 100', 'timeout_seconds':.1})
import shlex, sys
large = adapter.run_command({'command':shlex.join([sys.executable, "-c", "print('é'*600000,end='')"])})
assert large.stdout == 'é'*600000, len(large.stdout)
print(json.dumps([r.success, r.exit_code, r.stdout, r.stderr, c.failure, t.failure, t.timed_out]))
''')
        self.assertEqual(json.loads(output), [False, 7, 'out', 'err', 'cancelled', 'timeout', True])

@unittest.skipUnless(sys.platform == 'darwin' or sys.platform.startswith('linux'), 'Unix sandbox required')
class FrozenV2Tests(unittest.TestCase):
    def test_frozen_v2_original_string_adapter_uses_broker_without_v3_process_api(self):
        import io
        import tarfile
        repository = Path(__file__).resolve().parents[1]
        archive = subprocess.run(['git', 'archive', '1bfc82c'], cwd=repository,
                                 capture_output=True, check=True).stdout
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            baseline, root = base / 'baseline', base / 'workspace'
            baseline.mkdir(); root.mkdir()
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(baseline, filter='data')
            guard = root / 'test_guard.py'
            guard.write_text('original')
            script = '''
import json, os, pathlib, runpy, sys
runpy.run_path(sys.argv[1])['install_worker_adapter']()
from openkyrozen.tools import ToolAdapters, shell
assert not hasattr(shell, '_validated_shell_options')
assert '/baseline/' in shell.__file__
a = ToolAdapters(os.getcwd())
r = a.run_command('printf out; printf err >&2; exit 7')
s = a.run_command('printf changed > test_guard.py; printf after > source.py')
d = a.run_command('rm -rf /tmp/not-run')
print(json.dumps([r.success, r.exit_code, 'stdout:\\nout' in r.output, 'stderr:\\nerr' in r.output,
                  s.success, d.failure, hasattr(a, 'new_process_manager')]))
'''
            env = dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, (baseline, repository))))
            with protected.ProtectedWorker([sys.executable, '-c', script, str(repository / 'benchmarks' / 'v3_shell_launcher.py')], cwd=root, env=env,
                                           protected_files=[guard], policy=ShellPolicy(root, {'read', 'write', 'shell'})) as launch:
                process = subprocess.Popen(launch.command, cwd=root, env=launch.env, pass_fds=launch.pass_fds,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                launch.attach(process)
                stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertEqual(json.loads(stdout), [False, 7, True, True, True, 'denied', False])
            self.assertEqual(guard.read_text(), 'original')
            self.assertEqual((root / 'source.py').read_text(), 'after')

class SequentialShellTests(_BrokerCase):
    def test_completed_foreground_calls_do_not_exhaust_background_concurrency_limit(self):
        output = self.run_worker('''
import json, os
from benchmarks.v3_shell_launcher import BrokerClient
client = BrokerClient(int(os.environ.pop('KYROZEN_BENCHMARK_BROKER_FD')),
                      json.loads(os.environ.pop('KYROZEN_BENCHMARK_BROKER_IDENTITY')))
for _ in range(33):
    p = client.start('true', os.getcwd(), {}, 1)
    assert p.wait(5) == 0
    p.stop()
print('completed')
''')
        self.assertEqual(output.strip(), 'completed')

@unittest.skipUnless(sys.platform == 'darwin', 'macOS lifecycle limitation')
class DetachedLifecycleBoundaryTests(unittest.TestCase):
    def test_same_session_cleanup_cannot_be_claimed_for_reparented_setsid_child(self):
        """Characterize the blocker; this is deliberately not cleanup assurance."""
        import signal
        import socket
        from benchmarks.v3_shell_launcher import BrokerClient, ShellBroker
        child_pid = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            parent, child = socket.socketpair()
            policy = ShellPolicy(root, {'read', 'write', 'shell'})
            broker = ShellBroker(parent, policy, [])
            identity = {k: getattr(policy, k) for k in ('run_id', 'actor_id', 'session_id', 'revision')}
            client = BrokerClient(child.detach(), identity)
            code = ('import os,time; p=os.fork(); os._exit(0) if p else None; '
                    'os.setsid(); print(os.getpid(),flush=True); os.close(1); os.close(2); time.sleep(30)')
            try:
                process = client.start(shlex.join([sys.executable, '-c', code]), str(root), {}, 2)
                process.wait(5)
                child_pid = int(process.stdout.read1(4096))
                broker.close()
                # Parent/PGID traversal loses this detached child. The full gate
                # stays closed until a kernel-owned lifecycle boundary replaces it.
                os.kill(child_pid, 0)
                with self.assertRaisesRegex(ValueError, 'Detached macOS descendants'):
                    protected.verify_worker_protection()
            finally:
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                broker.close()
                client.stream.close()
                client.connection.close()

class ApprovedScopeFilesystemTests(_BrokerCase):
    def test_shell_can_write_approved_child_directory_and_cannot_write_its_ancestor(self):
        output = self.run_worker('''
import json, os, pathlib, shlex, sys
from benchmarks.v3_shell_launcher import BrokerClient
client = BrokerClient(int(os.environ.pop('KYROZEN_BENCHMARK_BROKER_FD')),
                      json.loads(os.environ.pop('KYROZEN_BENCHMARK_BROKER_IDENTITY')))
probe = "from pathlib import Path; Path('source.txt').write_text('allowed'); " + \\
        "exec(\\\"try:\\\\n Path('../outside.txt').write_text('blocked')\\\\nexcept OSError:\\\\n print('denied')\\\")"
p = client.start(shlex.join([sys.executable, '-c', probe]), str(pathlib.Path.cwd() / 'allowed'), {}, 5)
assert p.wait(10) == 0
assert not pathlib.Path('outside.txt').exists()
assert pathlib.Path('allowed/source.txt').read_text() == 'allowed'
print(p.stdout.read1(4096).decode().strip())
''', scope_paths=('allowed',))
        self.assertEqual(output.strip(), 'denied')


class LinuxMountOrderTests(unittest.TestCase):
    def test_readonly_test_ancestor_precedes_writable_child_mount(self):
        from unittest.mock import patch
        from openkyrozen.tools.sandbox import SandboxedCommand
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            allowed = root / 'allowed'
            allowed.mkdir()
            guard = root / 'test_guard.py'
            guard.write_text('original')
            policy = ShellPolicy(root, {'read', 'write', 'shell'}, scope_paths=('allowed',))
            invocation = SandboxedCommand(['bwrap', '--ro-bind', '/', '/', '--bind', str(root), str(root),
                                           '--', '/bin/sh', '-c', 'true'], False, {}, 'bubblewrap')
            with patch('openkyrozen.tools.sandbox.sandbox_command', return_value=invocation):
                result = protected.protected_shell_command('true', policy=policy, cwd=str(allowed),
                                                           env={}, protected_files=[guard])
            mounts = []
            argv = result.command
            for index, item in enumerate(argv):
                if item in {'--bind', '--ro-bind'}:
                    mounts.append((item, argv[index + 1], argv[index + 2]))
            self.assertEqual(mounts[-2:], [('--bind', str(allowed), str(allowed)),
                                           ('--ro-bind', str(guard), str(guard))])
            self.assertTrue(all(target != str(root) for _, _, target in mounts[mounts.index(mounts[-2]) + 1:]))

@unittest.skipUnless(sys.platform == 'darwin' or sys.platform.startswith('linux'), 'Unix sandbox required')
class FullCampaignGateTests(unittest.TestCase):
    def test_unproved_actor_authentication_blocks_before_credentials(self):
        from unittest.mock import patch
        from benchmarks.v3_matched_live import run
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with patch('benchmarks.v3_matched_live._source_snapshot', return_value='candidate'), \
                    patch('benchmarks.v3_matched_live._credential', side_effect=AssertionError('credential lookup')) as credential:
                with self.assertRaisesRegex(ValueError, 'actor isolation is unavailable'):
                    run(source=repository, repo=repository, output=base / 'report.json',
                        budget_path=base / 'budget.sqlite3', prior_hold_usd='1')
            credential.assert_not_called()

@unittest.skipUnless(sys.platform == 'darwin' or sys.platform.startswith('linux'), 'Unix sandbox required')
class BrokerCancellationCompatibilityTests(unittest.TestCase):
    def test_completed_remote_handles_do_not_exhaust_the_live_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            script = '''
import os, sys
from benchmarks.v3_shell_launcher import install_worker_adapter
install_worker_adapter()
from openkyrozen.tools.processes import SessionProcessManager
manager = SessionProcessManager('owner', sys.argv[1], 'session')
try:
    first = None
    for index in range(33):
        handle = manager.start('exit 0', cwd=sys.argv[1], env=dict(os.environ), timeout_seconds=5)
        first = first or handle.process_id
        assert handle.process.wait(5) == 0
    assert manager.read(first)['exit_code'] == 0
    print('completed handles retained', flush=True)
finally:
    manager.close()
'''
            policy = ShellPolicy(root, frozenset({'read', 'write', 'shell'}))
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            with protected.ProtectedWorker([sys.executable, '-c', script, str(root)], cwd=root,
                                           env=env, protected_files=[], policy=policy) as launch:
                process = subprocess.Popen(launch.command, cwd=root, env=launch.env,
                                           pass_fds=launch.pass_fds, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                launch.attach(process)
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertIn(b'completed handles retained', stdout)

    def test_turn_cancellation_reaches_remote_background_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            script = '''
import json, os, sys, threading, time
from benchmarks.v3_shell_launcher import install_worker_adapter
install_worker_adapter()
from openkyrozen.tools.processes import SessionProcessManager
manager = SessionProcessManager('owner', sys.argv[1], 'session')
from openkyrozen.persistence.store import EventStore
manager._bind_audit(EventStore(os.path.join(sys.argv[1], 'audit.sqlite3')), 'audit-workspace')
cancelled = threading.Event()
handle = manager.start('sleep 100', cwd=sys.argv[1], env=dict(os.environ), timeout_seconds=10, cancelled=cancelled)
stopping, release = threading.Event(), threading.Event()
original_stop = handle.process.stop
def delayed_stop():
    stopping.set()
    assert release.wait(5)
    return original_stop()
handle.process.stop = delayed_stop
cancelled.set()
assert stopping.wait(5)
try:
    assert manager.read(handle.process_id)['cancelled'] is False
finally:
    release.set()
handle.process.wait(5)
time.sleep(.1)
deadline = time.monotonic() + 3
records = manager._audit_records()
while (not records or records[0]['status'] != 'cancelled') and time.monotonic() < deadline:
    time.sleep(.01)
    records = manager._audit_records()
assert records[0]['status'] == 'cancelled', records
assert records[0]['owned_process_state'] == 'exited', records
assert records[0]['descendant_state'] == 'unknown', records
print(json.dumps(manager.read(handle.process_id)['cancelled']), flush=True)
manager.close()
'''
            policy = ShellPolicy(root, frozenset({'read', 'write', 'shell'}))
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            with protected.ProtectedWorker([sys.executable, '-c', script, str(root)], cwd=root,
                                           env=env, protected_files=[], policy=policy) as launch:
                process = subprocess.Popen(launch.command, cwd=root, env=launch.env,
                                           pass_fds=launch.pass_fds, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                launch.attach(process)
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertTrue(json.loads(stdout))
