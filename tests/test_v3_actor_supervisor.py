"""Parent policy and authenticated actor boundary regressions, without providers."""
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from benchmarks.v3_shell_launcher import ShellPolicy


class ActorPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.workspace = self.base / 'workspace'
        self.workspace.mkdir()
        self.source = self.base / 'selected'
        self.source.mkdir()
        (self.workspace / 'source.py').write_text('before')
        (self.workspace / 'other.py').write_text('other')
        self.guard = self.workspace / 'test_guard.py'
        self.guard.write_text('original')
        self.policy = ShellPolicy(self.workspace, {'read', 'write', 'shell', 'network'},
                                  run_id='fixture', actor_id='root', session_id='parent')

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('benchmarks.v3_actor_supervisor'),
                             'trusted actor supervisor is missing')
        return importlib.import_module('benchmarks.v3_actor_supervisor')

    def payload(self, scope=None, **overrides):
        value = {'schema_version': 1, 'source': str(self.source), 'workspace': str(self.workspace),
                 'review': False, 'report_repair': False, 'supports_report_repair': True,
                 'feedback': None, 'messages': {},
                 'run': {'run_id': 'subagent_author', 'budget_id': 'subagent_author', 'profile': 'coder',
                         'assignment': {'scope': scope or ['source.py'], 'objective': 'fix'}},
                 'parent': {'model': 'fixture-model', 'capabilities': ['read', 'write', 'shell'],
                            'profiles': []}}
        value.update(overrides)
        return value

    def assignment(self, payload=None):
        return self.module().ActorAssignment(self.policy, payload or self.payload(),
                                             source=self.source, model='fixture-model')

    def test_exact_file_author_can_mutate_only_its_assigned_file(self):
        assignment = self.assignment()
        request = assignment.authorize_mutation('write_file', {'path': 'source.py', 'content': 'after'}, [self.guard])
        self.assertEqual(request, {'path': 'source.py', 'content': 'after'})
        for target in ('other.py', 'test_guard.py', '../outside'):
            with self.subTest(target=target), self.assertRaises(PermissionError):
                assignment.authorize_mutation('write_file', {'path': target, 'content': 'changed'}, [self.guard])

    def test_file_scope_never_grants_shell_write_to_the_containing_directory(self):
        assignment = self.assignment()
        self.assertNotIn('write', assignment.shell_policy.capabilities)
        (self.workspace / 'package').mkdir()
        directory = self.assignment(self.payload(['package']))
        self.assertIn('write', directory.shell_policy.capabilities)
        self.assertEqual(directory.shell_policy.scope_paths, (self.workspace / 'package',))

    def test_reviewer_and_report_repair_cannot_mutate_even_with_forged_parent_grants(self):
        for flags in ({'review': True}, {'report_repair': True}):
            with self.subTest(flags=flags):
                assignment = self.assignment(self.payload(**flags))
                self.assertNotIn('write', assignment.shell_policy.capabilities)
                with self.assertRaises(PermissionError):
                    assignment.authorize_mutation('write_file', {'path': 'source.py', 'content': 'bad'}, [self.guard])

    def test_parent_claims_and_unknown_profiles_do_not_expand_fixture_authority(self):
        self.policy.narrow(capabilities={'read'}, scope_paths=['.'])
        assignment = self.assignment()
        self.assertEqual(assignment.capabilities, frozenset({'read'}))
        with self.assertRaises(PermissionError):
            assignment.authorize_mutation('write_file', {'path': 'source.py', 'content': 'bad'}, [])
        payload = self.payload()
        payload['run']['profile'] = 'custom-root'
        with self.assertRaises(PermissionError):
            self.assignment(payload)

    def test_source_workspace_model_and_custom_profile_claims_are_checked(self):
        payloads = [self.payload(source=str(self.base)), self.payload(workspace=str(self.base))]
        for key, value in (('model', 'other-model'), ('provider', 'other-provider'), ('custom_profile', {'capabilities': 'full'})):
            payload = self.payload()
            payload['run']['assignment'][key] = value
            payloads.append(payload)
        payload = self.payload()
        payload['parent']['model'] = 'other-model'
        payloads.append(payload)
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(PermissionError):
                self.assignment(payload)

    def test_explicit_canonical_relay_provider_and_model_do_not_change_authority(self):
        module = self.module()
        payload = self.payload()
        payload['run']['assignment'].update(provider='openai', model='fixture-model')
        assignment = module.ActorAssignment(self.policy, payload, source=self.source,
                                             model='fixture-model', provider='openai')
        self.assertEqual(assignment.capabilities, frozenset({'read', 'write', 'shell', 'network'}))
        for provider in ('custom', 'deepseek'):
            payload['run']['assignment']['provider'] = provider
            with self.subTest(provider=provider), self.assertRaises(PermissionError):
                module.ActorAssignment(self.policy, payload, source=self.source,
                                       model='fixture-model', provider='openai')

    def test_symlink_alias_and_new_symlink_parent_are_rejected(self):
        (self.workspace / 'alias.py').symlink_to('source.py')
        with self.assertRaises(PermissionError):
            self.assignment(self.payload(['alias.py']))
        assignment = self.assignment()
        (self.workspace / 'source.py').unlink()
        (self.workspace / 'source.py').symlink_to('other.py')
        with self.assertRaises(PermissionError):
            assignment.authorize_mutation('write_file', {'path': 'source.py', 'content': 'bad'}, [])

    def test_parent_traversal_cannot_erase_a_symlink_before_scope_validation(self):
        (self.workspace / 'nested' / 'deeper').mkdir(parents=True)
        (self.workspace / 'alias').symlink_to('nested/deeper')
        assignment = self.assignment()
        with self.assertRaises(PermissionError):
            assignment.authorize_mutation('write_file', {'path': 'alias/../source.py', 'content': 'bad'}, [])

    def test_hardlink_and_protected_test_are_denied_even_inside_directory_scope(self):
        assignment = self.assignment(self.payload(['.']))
        os.link(self.workspace / 'source.py', self.workspace / 'alias.py')
        for path in ('source.py', 'alias.py', 'test_guard.py'):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                assignment.authorize_mutation('write_file', {'path': path, 'content': 'bad'}, [self.guard])

    def test_legacy_and_structured_mutation_paths_keep_original_argument_shape(self):
        assignment = self.assignment()
        value = ' source.py |left|right'
        self.assertEqual(assignment.authorize_mutation('write_file', value, []), value)
        edit = json.dumps({'path': 'source.py', 'old_text': 'before', 'new_text': 'after', 'expected_sha256': 'a' * 64})
        self.assertEqual(assignment.authorize_mutation('edit_file', edit, []), edit)
        with self.assertRaises(PermissionError):
            assignment.authorize_mutation('__getattribute__', {'path': 'source.py'}, [])

    def test_structured_paths_do_not_gain_scope_by_whitespace_normalization(self):
        assignment = self.assignment()
        for path in ('source.py ', ' source.py'):
            with self.subTest(path=path), self.assertRaises(PermissionError):
                assignment.authorize_mutation('write_file', {'path': path, 'content': 'bad'}, [])
            with self.subTest(read=path), self.assertRaises(PermissionError):
                assignment.authorize_read('read_file', {'path': path})

    def test_start_revalidates_every_scope_before_spawning_the_ticketed_command(self):
        from contextlib import nullcontext
        module = self.module()
        for kind in ('symlink', 'directory'):
            with self.subTest(kind=kind):
                target = self.workspace / f'second-{kind}.py'
                target.write_text('before')
                assignment = self.assignment(self.payload(['source.py', target.name]))
                policy = assignment.shell_policy
                request = {key: getattr(policy, key) for key in ('run_id', 'actor_id', 'session_id', 'revision')}
                request.update(call_id='call', command='printf ready', cwd=str(self.workspace),
                               env={}, timeout_seconds=1, background=False)
                from benchmarks.v3_shell_launcher import TicketAuthority
                authority = TicketAuthority(policy)
                ticket = authority.issue(request)
                target.unlink()
                if kind == 'symlink':
                    target.symlink_to('source.py')
                else:
                    target.mkdir()
                broker = module._ActorBroker.__new__(module._ActorBroker)
                broker.actor, broker.policy, broker.authority = {'assignment': assignment}, policy, authority
                broker.closed, broker.close_lock = threading.Event(), threading.RLock()
                broker.processes = {}
                owner = SimpleNamespace(popen=lambda *args, **kwargs: self.fail('invalid scope reached process execution'))
                owned = SimpleNamespace(owner=owner, close=lambda: None, invocation=SimpleNamespace(
                    command=[sys.executable], env={}, backend='test'))
                broker.pending = {ticket: (request, request, owned, time.monotonic()+5)}
                broker.supervisor = SimpleNamespace(execution_lock=threading.RLock(),
                    _frozen=nullcontext, protected_files=[], group=SimpleNamespace(uid=12345))
                with self.assertRaises(PermissionError):
                    broker._dispatch({'op':'start', 'ticket':ticket, 'request':request})

    def test_narrowed_fixture_invalidates_existing_assignment(self):
        assignment = self.assignment()
        self.policy.narrow(capabilities={'read'}, scope_paths=['.'])
        with self.assertRaises(PermissionError):
            assignment.authorize_mutation('write_file', {'path': 'source.py', 'content': 'bad'}, [])

    def test_assignment_snapshot_cannot_be_modified_after_authorization(self):
        payload = self.payload()
        assignment = self.assignment(payload)
        payload['run']['assignment']['scope'] = ['.']
        with self.assertRaises(PermissionError):
            assignment.authorize_mutation('write_file', {'path': 'other.py', 'content': 'bad'}, [])

    def test_mutation_effects_must_fit_the_accepted_plan(self):
        self.policy.effects = frozenset({'read'})
        assignment = self.assignment()
        with self.assertRaises(PermissionError):
            assignment.authorize_mutation('write_file', {'path': 'source.py', 'content': 'bad'}, [])

    def test_parent_read_scope_blocks_sibling_and_traversal_patterns(self):
        assignment = self.assignment()
        self.assertEqual(assignment.authorize_read('read_file', 'source.py'), 'source.py')
        for action, arguments in (('read_file', 'other.py'), ('list_dir', '.'),
                                  ('find_files', {'pattern': '../*', 'directory': '.'}),
                                  ('search_files', {'query': 'x', 'glob': '../*', 'path': 'source.py'})):
            with self.subTest(action=action), self.assertRaises(PermissionError):
                assignment.authorize_read(action, arguments)

    def test_review_extra_reads_require_parent_authorized_artifacts(self):
        module = self.module()
        assignment = module.ActorAssignment(self.policy, self.payload(review=True),
            source=self.source, model='fixture-model', review_artifacts=[self.workspace / 'other.py'])
        self.assertEqual(assignment.authorize_read('read_file', 'other.py'), 'other.py')
        with self.assertRaises(PermissionError):
            self.assignment(self.payload(review=True)).authorize_read('read_file', 'other.py')
        with self.assertRaises(PermissionError):
            assignment.authorize_mutation('write_file', {'path': 'other.py', 'content': 'bad'}, [])

    def test_supervisor_requires_privileged_parent(self):
        with patch.object(self.module().os, 'geteuid', return_value=501):
            with self.assertRaisesRegex(PermissionError, 'privileged'):
                self.module().ActorSupervisor([sys.executable, '-c', 'pass'], cwd=self.workspace,
                    env={}, protected_files=[self.guard], policy=self.policy, source=self.source)

    def test_actor_environment_pins_the_native_skills_directory_to_its_private_home(self):
        supervisor = self.module().ActorSupervisor.__new__(self.module().ActorSupervisor)
        supervisor.env = {'KYROZEN_SKILLS_DIR': '/shared-skills', 'KYROZEN_SKILLS_PATH': '/stale-path'}
        with socket.socket(socket.AF_UNIX) as listener:
            supervisor.address = str(self.base / 'listener')
            listener.bind(supervisor.address)
            environment = supervisor._environment(self.base / 'private-home')
        self.assertEqual(environment['KYROZEN_SKILLS_DIR'], str(self.base / 'private-home' / 'skills'))
        self.assertNotIn('KYROZEN_SKILLS_PATH', environment)

    @unittest.skipUnless(os.name == 'posix', 'Unix actor listener directory')
    def test_socket_factory_uses_a_private_directory_under_root_owned_sticky_var_tmp(self):
        module=self.module()
        self.assertTrue(hasattr(module,'_actor_socket_directory'), 'portable actor socket factory is missing')
        directory=module._actor_socket_directory()
        try:
            self.assertEqual(directory.parent,Path('/var/tmp').resolve())
            self.assertEqual(directory.stat().st_mode & 0o777,0o711)
            self.assertEqual(directory.stat().st_uid,os.geteuid())
            self.assertEqual(directory.parent.stat().st_uid,0)
            self.assertTrue(directory.parent.stat().st_mode & 0o1000)
            self.assertFalse(directory.is_relative_to(self.base))
        finally:
            directory.rmdir()

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'uses unprivileged directories to force rejection')
    def test_failed_constructor_releases_unexecuted_identity_and_private_directories(self):
        module = self.module()
        state, channel = self.base / 'state', self.base / 'channel'
        state.mkdir()
        channel.mkdir()
        owner = SimpleNamespace(uid=1_234_567_890, closed=False)
        owner.close = lambda: setattr(owner, 'closed', True)
        with patch.object(module.os, 'geteuid', return_value=0), patch.object(module, 'UIDOwner', return_value=owner), \
                patch.object(module.tempfile, 'mkdtemp', side_effect=[str(state), str(channel)]):
            with self.assertRaisesRegex(PermissionError, 'root-owned'):
                module.ActorSupervisor([sys.executable, '-c', 'pass'], cwd=self.workspace,
                    env={}, protected_files=[self.guard], policy=self.policy, source=self.source)
        self.assertTrue(owner.closed)
        self.assertFalse(state.exists())
        self.assertFalse(channel.exists())


@unittest.skipUnless(hasattr(os, 'geteuid') and os.geteuid() == 0,
                     'privileged disposable benchmark runner required')
class PrivilegedActorSupervisorTests(unittest.TestCase):
    def test_narrowed_root_policy_bounds_raw_and_managed_shell_reads(self):
        from benchmarks.v3_actor_supervisor import ActorSupervisor
        with tempfile.TemporaryDirectory(dir='/var/tmp') as directory:
            base = Path(directory).resolve()
            base.chmod(0o755)
            workspace = base/'workspace'
            allowed = workspace/'allowed'
            allowed.mkdir(parents=True)
            (allowed/'source.py').write_text('visible')
            (workspace/'sibling.py').write_text('private')
            source = Path(__file__).resolve().parents[1]
            probe = '''import json,pathlib,sys
p=pathlib.Path(sys.argv[1])
out=[(p/'allowed/source.py').read_text()]
try:
    (p/'sibling.py').read_text()
    out.append(False)
except OSError:
    out.append(True)
try:
    names=[item.name for item in p.iterdir()]
except OSError:
    names=[]
out.append('sibling.py' not in names)
print(json.dumps(out))
'''
            code = '''import json,os,shlex,sys
sys.path.insert(0,sys.argv[1])
from benchmarks.v3_process_owner import connect_actor_client
c=connect_actor_client()
args=sys.argv
sys.argv=['probe',args[2]]
exec(compile(args[3],'<raw-root-probe>','exec'))
sys.argv=args
p=c.start(shlex.join([sys.executable,'-I','-c',sys.argv[3],sys.argv[2]]),
          os.path.join(sys.argv[2],'allowed'),{'PATH':os.environ['PATH']},5)
p.wait(10)
out=bytearray()
while chunk:=p.stdout.read1(4096): out.extend(chunk)
while p.stderr.read1(4096): pass
assert p.returncode==0
print(out.decode().strip())
'''
            policy = ShellPolicy(workspace, {'read','write','shell'}, scope_paths=['allowed'])
            with ActorSupervisor([sys.executable,'-I','-c',code,str(source),str(workspace),probe],
                    cwd=workspace, env={'PATH':os.environ['PATH']}, protected_files=[],
                    policy=policy, source=source) as supervisor:
                process = supervisor.popen(stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                output,error=process.communicate(timeout=20)
                self.assertEqual(process.returncode,0,error)
                self.assertEqual([json.loads(line) for line in output.splitlines()],
                                 [['visible',True,True],['visible',True,True]])

    def test_directory_child_shell_cannot_hide_protected_test_mounts(self):
        from benchmarks.v3_actor_supervisor import ActorAssignment, ActorSupervisor
        with tempfile.TemporaryDirectory(dir='/var/tmp') as directory:
            base = Path(directory).resolve()
            base.chmod(0o755)
            workspace = base / 'workspace'
            package = workspace / 'package'
            package.mkdir(parents=True)
            guard = package / 'test_guard.py'
            guard.write_text('original')
            (package / 'source.py').write_text('before')
            source = Path(__file__).resolve().parents[1]
            policy = ShellPolicy(workspace, {'read', 'write', 'shell'})
            payload = {'schema_version': 1, 'source': str(source), 'workspace': str(workspace),
                'review': False, 'report_repair': False, 'parent': {'model': 'fixture-model'},
                'run': {'run_id': 'directory-author', 'profile': 'coder', 'assignment': {'scope': ['package']}}}
            assignment = ActorAssignment(policy, payload, source=source, model='fixture-model')
            code = '''import json,os,sys
sys.path.insert(0,sys.argv[1])
from benchmarks.v3_process_owner import connect_actor_client
c=connect_actor_client()
p=c.start('printf changed > test_guard.py; printf after > source.py',sys.argv[2],{'PATH':os.environ['PATH']},5)
p.wait(10)
while p.stdout.read1(4096): pass
while p.stderr.read1(4096): pass
print(json.dumps(p.returncode))
'''
            with ActorSupervisor([sys.executable, '-c', 'pass'], cwd=workspace,
                    env={'PATH': os.environ['PATH']}, protected_files=[guard], policy=policy, source=source) as supervisor:
                actor = {'root': False, 'policy': assignment.shell_policy, 'assignment': assignment,
                    'connected': False, 'broker': None, 'condition': threading.Condition(),
                    'callbacks': {}, 'cancelled': False, 'disconnected': False, 'state': 'running'}
                process = supervisor._spawn([sys.executable, '-I', '-c', code, str(source), str(package)],
                                           actor, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                output, error = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, error)
                self.assertEqual(json.loads(output), 0)
                self.assertEqual(guard.read_text(), 'original')
                self.assertEqual((package / 'source.py').read_text(), 'after')

    def test_no_read_actor_starts_native_runtime_without_access_to_fixture_data(self):
        from benchmarks.v3_actor_supervisor import ActorAssignment, ActorSupervisor
        for repair in (False, True):
            with self.subTest(report_repair=repair), tempfile.TemporaryDirectory(dir='/var/tmp') as directory:
                base = Path(directory).resolve()
                base.chmod(0o755)
                workspace = base / 'workspace'
                workspace.mkdir()
                (workspace / 'secret.py').write_text('private-fixture')
                source = Path(__file__).resolve().parents[1]
                policy = ShellPolicy(workspace, {'read', 'write'} if repair else set())
                payload = {'schema_version': 1, 'source': str(source), 'workspace': str(workspace),
                    'review': False, 'report_repair': repair, 'parent': {'model': 'fixture-model'},
                    'run': {'run_id': 'repair-author', 'profile': 'coder', 'assignment': {'scope': ['.']}}}
                assignment = ActorAssignment(policy, payload, source=source, model='fixture-model')
                code = '''import json,pathlib,sys
sys.path.insert(0,sys.argv[1])
from benchmarks.v3_process_owner import connect_actor_client
c=connect_actor_client()
from openkyrozen.app.bootstrap import build_application
app=build_application()
app.runtime._set_workspace_root(sys.argv[2])
app.close()
try:
    pathlib.Path(sys.argv[2],'secret.py').read_text()
    denied=False
except OSError:
    denied=True
try:
    names=[p.name for p in pathlib.Path(sys.argv[2]).iterdir()]
except OSError:
    names=[]
print(json.dumps([denied,names]))
'''
                with ActorSupervisor([sys.executable, '-I', '-c', code, str(source), str(workspace)],
                        cwd=workspace, env={'PATH': os.environ['PATH'], 'KYROZEN_DISABLE_VECTOR_INDEX': '1'},
                        protected_files=[], policy=policy, source=source) as supervisor:
                    if repair:
                        actor = {'root': False, 'policy': assignment.shell_policy, 'assignment': assignment,
                            'connected': False, 'broker': None, 'condition': threading.Condition(),
                            'callbacks': {}, 'cancelled': False, 'disconnected': False, 'state': 'running'}
                        process = supervisor._spawn(supervisor.command, actor, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    else:
                        process = supervisor.popen(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    output, error = process.communicate(timeout=30)
                    self.assertEqual(process.returncode, 0, error)
                    self.assertEqual(json.loads(output), [True, []])

    def test_scoped_child_helpers_see_atomic_replacement_and_new_files(self):
        from benchmarks.v3_actor_supervisor import ActorAssignment, ActorSupervisor
        with tempfile.TemporaryDirectory(dir='/var/tmp') as directory:
            base = Path(directory).resolve()
            base.chmod(0o755)
            workspace = base / 'workspace'
            workspace.mkdir()
            (workspace / 'source.py').write_text('before')
            (workspace / 'sibling.py').write_text('private')
            guard = workspace / 'test_guard.py'
            guard.write_text('original')
            source = Path(__file__).resolve().parents[1]
            policy = ShellPolicy(workspace, {'read', 'write', 'shell'})
            payload = {'schema_version': 1, 'source': str(source), 'workspace': str(workspace),
                'review': False, 'report_repair': False, 'parent': {'model': 'fixture-model'},
                'run': {'run_id': 'subagent_author', 'profile': 'coder',
                        'assignment': {'scope': ['source.py', 'created.py']}}}
            assignment = ActorAssignment(policy, payload, source=source, model='fixture-model')
            code = '''import json,pathlib,sys
import os
pathlib.Path(os.environ['TMPDIR']).joinpath('child-temp-probe').write_text('private')
sys.path.insert(0,sys.argv[1])
from benchmarks.v3_process_owner import connect_actor_client
c=connect_actor_client()
original=c.rpc('file_read',action='read_file',arguments={'path':'source.py'})
digest=original['data']['sha256']
c.rpc('file_mutation',action='edit_file',arguments={'path':'source.py','old_text':'before','new_text':'after','expected_sha256':digest})
updated=c.rpc('file_read',action='read_file',arguments={'path':'source.py'})
c.rpc('file_mutation',action='write_file',arguments={'path':'created.py','content':'new'})
created=c.rpc('file_read',action='read_file',arguments={'path':'created.py'})
out=[updated['data']['content'],created['data']['content']]
for action,target in [('file_read','sibling.py'),('file_mutation','test_guard.py')]:
    try:
        c.rpc(action,action='read_file' if action=='file_read' else 'write_file',arguments={'path':target,'content':'bad'})
        out.append(False)
    except PermissionError:
        out.append(True)
print(json.dumps(out))
'''
            with ActorSupervisor([sys.executable, '-c', 'pass'], cwd=workspace,
                    env={'PATH': os.environ['PATH']}, protected_files=[guard], policy=policy,
                    source=source, provider_config={'model_main': 'fixture-model'}) as supervisor:
                actor = {'root': False, 'policy': assignment.shell_policy, 'assignment': assignment,
                    'connected': False, 'broker': None, 'condition': threading.Condition(),
                    'callbacks': {}, 'cancelled': False, 'disconnected': False, 'state': 'running'}
                process = supervisor._spawn([sys.executable, '-I', '-c', code, str(source)], actor,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                output, error = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, error)
                self.assertEqual(json.loads(output), ['after', 'new', True, True])
                self.assertEqual((workspace / 'source.py').read_text(), 'after')
                self.assertEqual(guard.read_text(), 'original')

    def test_registered_uid_bootstrap_is_canonical_and_unregistered_uid_is_denied(self):
        from benchmarks.v3_actor_supervisor import ActorSupervisor
        with tempfile.TemporaryDirectory(dir='/var/tmp') as directory:
            base = Path(directory)
            base.chmod(0o755)
            workspace = base / 'workspace'
            workspace.mkdir()
            source = Path(__file__).resolve().parents[1]
            script = ('import json,sys,os,pathlib; '
                      'pathlib.Path(os.environ["TMPDIR"]).joinpath("root-temp-probe").write_text("private"); '
                      'sys.path.insert(0,sys.argv[1]); '
                      'from benchmarks.v3_process_owner import connect_actor_client; '
                      'c=connect_actor_client(); print(json.dumps(c.identity),flush=True)')
            policy = ShellPolicy(workspace, {'read'}, actor_id='canonical-root')
            with ActorSupervisor([sys.executable, '-I', '-c', script, str(source)], cwd=workspace,
                                 env={'PATH': os.environ['PATH']}, protected_files=[], policy=policy,
                                 source=source) as supervisor:
                process = supervisor.popen(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                output, error = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, error)
                self.assertEqual(json.loads(output)['actor_id'], 'canonical-root')
                with socket.socket(socket.AF_UNIX) as outsider:
                    outsider.settimeout(5)
                    outsider.connect(supervisor.address)
                    outsider.sendall(b'{"op":"hello","actor_id":"canonical-root"}\n')
                    self.assertFalse(json.loads(outsider.makefile('rb').readline())['ok'])


class ActorChannelTests(unittest.TestCase):
    def supervisor(self):
        from benchmarks.v3_actor_supervisor import ActorSupervisor
        supervisor = ActorSupervisor.__new__(ActorSupervisor)
        supervisor.closed = threading.Event()
        supervisor.handles = {}
        return supervisor

    def actor(self, parent):
        return {'root': False, 'parent': parent, 'condition': threading.Condition(),
                'callbacks': {}, 'cancelled': False, 'disconnected': False, 'state': 'running'}

    def test_callback_only_its_parent_can_reply_and_replays_fail(self):
        supervisor = self.supervisor()
        parent = object()
        actor = self.actor(parent)
        supervisor.handles['owned'] = actor
        result = []
        thread = threading.Thread(target=lambda: result.append(supervisor._callback(actor,
            {'operation': 'budget_can_continue', 'values': {'cost_picos': 7}})))
        thread.start()
        try:
            deadline = time.monotonic() + 2
            while not actor['callbacks'] and time.monotonic() < deadline:
                time.sleep(.001)
            with self.assertRaises(PermissionError):
                supervisor._delegated(object(), {'op': 'delegated_poll', 'id': 'owned', 'cancelled': False, 'replies': []})
            poll = supervisor._delegated(parent, {'op': 'delegated_poll', 'id': 'owned', 'cancelled': False, 'replies': []})
            self.assertEqual(poll['requests'][0]['op'], 'budget_can_continue')
            reply = {'id': poll['requests'][0]['id'], 'ok': True, 'value': False}
            supervisor._delegated(parent, {'op': 'delegated_poll', 'id': 'owned', 'cancelled': False, 'replies': [reply]})
            thread.join(timeout=2)
            self.assertEqual(result, [False])
            with self.assertRaises(PermissionError):
                supervisor._delegated(parent, {'op': 'delegated_poll', 'id': 'owned', 'cancelled': False, 'replies': [reply]})
        finally:
            supervisor.closed.set()
            thread.join(timeout=2)

    def test_callback_shape_is_finite_and_cannot_choose_arbitrary_service(self):
        supervisor = self.supervisor()
        actor = self.actor(object())
        for operation, values in (('__getattribute__', {}), ('budget_can_continue', {'cost_picos': 1, 'run_id': 'sibling'}),
                                  ('cancelled', {'object': 'root'}), ('access_acquire', {'path': '/', 'reading': 'yes'})):
            with self.subTest(operation=operation, values=values), patch.object(actor['condition'], 'wait',
                    side_effect=AssertionError('invalid callback reached the parent queue')), self.assertRaises(PermissionError):
                supervisor._callback(actor, {'operation': operation, 'values': values})

    def test_freeze_resumes_only_processes_it_stopped(self):
        from benchmarks import v3_actor_supervisor as module
        supervisor = self.supervisor()
        supervisor.owners = [SimpleNamespace(uid=12345, closed=False)]
        snapshots = [{20: (12345, 'S'), 21: (12345, 'T'), 99: (501, 'S')},
                     {20: (12345, 'T'), 21: (12345, 'T'), 99: (501, 'S')},
                     {20: (12345, 'T'), 21: (12345, 'T'), 99: (501, 'S')}]
        with patch.object(module, 'process_snapshot', side_effect=snapshots), patch.object(module.os, 'kill') as kill:
            with supervisor._frozen():
                pass
        import signal
        self.assertEqual([(call.args[0], call.args[1]) for call in kill.call_args_list],
                         [(20, signal.SIGSTOP), (20, signal.SIGCONT)])

    def test_bounded_actor_collection_preserves_stdin_stdout_and_stderr(self):
        from benchmarks import v3_actor_supervisor as module
        self.assertTrue(hasattr(module, '_communicate'), 'bounded actor output collection is missing')
        process = subprocess.Popen([sys.executable, '-I', '-c',
            'import sys; print(sys.stdin.read()); print("diagnostic",file=sys.stderr)'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        owner = SimpleNamespace(close=lambda: process.kill() if process.poll() is None else None)
        try:
            output, error = module._communicate(process, owner, input_data=b'hello', timeout=5)
            self.assertEqual((output, error, process.returncode), (b'hello\n', b'diagnostic\n', 0))
        finally:
            owner.close()
            process.wait(timeout=5)

    def test_output_overflow_stops_the_owned_process_instead_of_buffering_forever(self):
        from benchmarks import v3_actor_supervisor as module
        self.assertTrue(hasattr(module, '_communicate'), 'bounded actor output collection is missing')
        process = subprocess.Popen([sys.executable, '-I', '-c',
            'import os,time; os.write(1,b"x"*128); time.sleep(120)'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        owner = SimpleNamespace(close=lambda: process.kill() if process.poll() is None else None)
        try:
            with self.assertRaisesRegex(PermissionError, 'output'):
                module._communicate(process, owner, timeout=5, limit=64)
            self.assertIsNotNone(process.poll())
        finally:
            owner.close()
            process.wait(timeout=5)

    def test_relay_revocation_failure_cannot_skip_kernel_identity_cleanup(self):
        supervisor = self.supervisor()
        supervisor.failures = []
        def revoke(_):
            raise RuntimeError('revocation failed')
        supervisor.revoke_actor = revoke
        owner = SimpleNamespace(closed=False)
        owner.close = lambda: setattr(owner, 'closed', True)
        invocation = SimpleNamespace(closed=False)
        invocation.close = lambda: setattr(invocation, 'closed', True)
        actor = {'root': True, 'stop_lock': threading.RLock(), 'stopped': False,
                 'owner': owner, 'invocation': invocation, 'broker': None,
                 'policy': SimpleNamespace(actor_id='root')}
        with self.assertRaises(RuntimeError):
            supervisor._stop_actor(actor)
        self.assertTrue(owner.closed)
        self.assertTrue(invocation.closed)

    def test_normal_channel_eof_allows_python_shutdown_before_uid_drain(self):
        from benchmarks.v3_actor_supervisor import _ActorBroker
        supervisor = self.supervisor()
        supervisor.failures, supervisor.revoke_actor, supervisor.protected_files = [], None, []
        parent, child = socket.socketpair()
        process = subprocess.Popen([sys.executable, '-I', '-c',
            'import os,sys,time; os.close(int(sys.argv[1])); time.sleep(.05); print("complete")',
            str(child.fileno())], pass_fds=(child.fileno(),), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        child.close()
        owner = SimpleNamespace(closed=False)
        def close():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            owner.closed = True
        owner.close = close
        with tempfile.TemporaryDirectory() as directory:
            actor = {'root': True, 'policy': ShellPolicy(directory, {'read'}),
                     'stop_lock': threading.RLock(), 'stopped': False, 'owner': owner,
                     'process': process, 'invocation': SimpleNamespace(close=lambda: None), 'broker': None}
            broker = _ActorBroker(parent, supervisor, actor)
            actor['broker'] = broker
            try:
                output, error = process.communicate(timeout=5)
                broker.thread.join(timeout=5)
                self.assertEqual(process.returncode, 0, error)
                self.assertEqual(output, b'complete\n')
                self.assertTrue(owner.closed)
            finally:
                broker.close()
                owner.close()

    def test_failed_identity_drain_remains_retryable_and_never_certifies_stopped(self):
        supervisor = self.supervisor()
        supervisor.failures, supervisor.revoke_actor = [], None
        owner = SimpleNamespace(closed=False)
        def fail():
            raise RuntimeError('kernel cleanup failed')
        owner.close = fail
        actor = {'root': True, 'stop_lock': threading.RLock(), 'stopped': False,
                 'owner': owner, 'invocation': SimpleNamespace(close=lambda: None),
                 'broker': None, 'policy': SimpleNamespace(actor_id='root')}
        with self.assertRaises(RuntimeError):
            supervisor._stop_actor(actor)
        self.assertFalse(actor['stopped'])
        owner.close = lambda: setattr(owner, 'closed', True)
        supervisor._stop_actor(actor)
        self.assertTrue(owner.closed)

    def test_shared_group_reservation_survives_any_failed_actor_drain(self):
        supervisor = self.supervisor()
        group = SimpleNamespace(closed=False)
        group.close = lambda: setattr(group, 'closed', True)
        owner = SimpleNamespace(closed=False)
        def fail():
            raise RuntimeError('actor remained alive')
        owner.close = fail
        supervisor.group, supervisor.owners = group, [group, owner]
        supervisor.actors, supervisor.failures = {}, []
        supervisor.listener = supervisor.thread = supervisor.state = supervisor.socket_directory = None
        with self.assertRaises(RuntimeError):
            supervisor.close()
        self.assertFalse(group.closed)


class ActorBrokerCleanupTests(unittest.TestCase):
    def broker(self, command_invocations, pending=()):
        from benchmarks.v3_actor_supervisor import ActorSupervisor, _ActorBroker
        supervisor = ActorSupervisor.__new__(ActorSupervisor)
        supervisor.failures, supervisor.revoke_actor, supervisor.handles = [], None, {}
        owner = SimpleNamespace(closed=False)
        owner.close = lambda: setattr(owner, 'closed', True)
        actor = {'root': True, 'owner': owner, 'stopped': False, 'stop_lock': threading.RLock(),
                 'invocation': SimpleNamespace(close=lambda: None), 'policy': SimpleNamespace(actor_id='root')}
        broker = _ActorBroker.__new__(_ActorBroker)
        broker.actor, broker.supervisor = actor, supervisor
        broker.closed, broker.close_lock = threading.Event(), threading.RLock()
        broker.connection = SimpleNamespace(shutdown=lambda how: None, close=lambda: None)
        broker.pending = {str(index): ({}, {}, invocation, 0) for index, invocation in enumerate(pending)}
        broker.authority = SimpleNamespace(tickets={})
        broker.processes = {str(index): SimpleNamespace(pid=12345) for index, _ in enumerate(command_invocations)}
        broker.records = {str(index): {'invocation': invocation, 'lock': threading.Lock(), 'eof': set()}
                          for index, invocation in enumerate(command_invocations)}
        actor['broker'] = broker
        return broker, actor, supervisor

    def test_uid_broker_never_uses_historical_pid_or_process_group_authority(self):
        from benchmarks import v3_shell_launcher as launcher
        drained = []
        invocation = SimpleNamespace(close=lambda: drained.append('command'))
        broker, actor, _ = self.broker([invocation])
        with patch.object(launcher, '_kill_tree', side_effect=AssertionError('historical PID used')), patch.object(
                launcher.os, 'killpg', side_effect=AssertionError('historical group used')):
            broker.close()
        self.assertEqual(drained, ['command'])
        self.assertTrue(actor['owner'].closed)

    def test_actor_cancel_owns_a_command_during_popen_and_rejects_late_publication(self):
        from contextlib import nullcontext
        from benchmarks.v3_actor_supervisor import _OwnedInvocation
        from benchmarks.v3_shell_launcher import TicketAuthority
        entered, release, close_entered, finished = (threading.Event() for _ in range(4))
        process = SimpleNamespace(pid=12345, returncode=None, stdout=io.BytesIO(), stderr=io.BytesIO())
        process.poll = lambda: process.returncode
        def wait(timeout):
            if not finished.wait(timeout):
                raise subprocess.TimeoutExpired('benign barrier', timeout)
            return process.returncode
        process.wait = wait
        owner = SimpleNamespace(closed=False, lock=threading.RLock())
        def popen(*args, **kwargs):
            with owner.lock:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test did not release popen')
                return process
        def close():
            close_entered.set()
            with owner.lock:
                process.returncode = -9
                owner.closed = True
                finished.set()
        owner.popen, owner.close = popen, close
        invocation = SimpleNamespace(command=['benign'], env={}, backend='test', close=lambda: None)
        owned = _OwnedInvocation(invocation, owner)
        broker, actor, supervisor = self.broker([])
        with tempfile.TemporaryDirectory() as directory:
            policy = ShellPolicy(directory, {'read','shell'})
            broker.policy = actor['policy'] = policy
            broker.protected_files = []
            actor.update(root=False, condition=threading.Condition(), callbacks={}, disconnected=False)
            supervisor.execution_lock, supervisor._frozen = threading.RLock(), nullcontext
            supervisor.group = SimpleNamespace(uid=12345)
            broker.authority = TicketAuthority(policy)
            request = {key:getattr(policy,key) for key in ('run_id','actor_id','session_id','revision')}
            request.update(call_id='barrier',command='printf benign',cwd=directory,env={},
                           timeout_seconds=1800,background=False)
            ticket = broker.authority.issue(request)
            broker.pending[ticket] = (request,request,owned,time.monotonic()+5)
            results, errors, cancel_errors = [], [], []
            cancelled = threading.Event()
            def start():
                try:
                    results.append(broker._dispatch({'op':'start','ticket':ticket,'request':request}))
                except Exception as exc:
                    errors.append(exc)
            def cancel():
                try:
                    supervisor._stop_actor(actor)
                except Exception as exc:
                    cancel_errors.append(exc)
                finally:
                    cancelled.set()
            starter = threading.Thread(target=start)
            stopper = threading.Thread(target=cancel)
            starter.start()
            try:
                self.assertTrue(entered.wait(3))
                stopper.start()
                deadline = time.monotonic()+3
                while not (close_entered.is_set() or cancelled.is_set()) and time.monotonic()<deadline:
                    time.sleep(.001)
                release.set()
                starter.join(timeout=3)
                stopper.join(timeout=3)
                self.assertFalse(starter.is_alive() or stopper.is_alive(), 'cancel and start deadlocked')
                self.assertFalse(cancel_errors)
                self.assertTrue(owner.closed, 'cancel missed the in-flight command UID')
                self.assertEqual(broker.records, {})
                self.assertEqual(results, [])
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], PermissionError)
            finally:
                release.set()
                close()
                starter.join(timeout=5)
                if stopper.ident is not None:
                    stopper.join(timeout=5)
                broker.close()

    def test_failed_uid_cleanup_drains_other_handles_and_actor_then_remains_retryable(self):
        attempted = []
        state = {'fail': True}
        def pending():
            attempted.append('pending')
            if state['fail']:
                raise RuntimeError('pending UID remained live')
        failed = SimpleNamespace(close=pending)
        complete = SimpleNamespace(close=lambda: attempted.append('command'))
        broker, actor, supervisor = self.broker([complete], pending=[failed])
        with self.assertRaisesRegex(RuntimeError, 'pending UID remained live'):
            broker.close()
        self.assertEqual(attempted, ['pending', 'command'])
        self.assertTrue(actor['owner'].closed)
        self.assertTrue(supervisor.failures)
        state['fail'] = False
        broker.close()
        self.assertEqual(attempted, ['pending', 'command', 'pending', 'command'])
        broker.close()
        self.assertEqual(attempted, ['pending', 'command', 'pending', 'command'])


class ActorOutputLifecycleTests(unittest.TestCase):
    def test_closed_output_pipes_do_not_cancel_running_foreground_or_background_commands(self):
        from benchmarks.v3_actor_supervisor import _ActorBroker
        for background, code in ((False, 0), (True, 7)):
            with self.subTest(background=background), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'finished'
                command = f'exec >/dev/null 2>&1; sleep .15; printf done > "{target}"; exit {code}'
                process = subprocess.Popen(['/bin/sh', '-c', command], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                drained = []
                def close():
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                    drained.append(True)
                broker = _ActorBroker.__new__(_ActorBroker)
                broker.closed = threading.Event()
                broker.processes = {'command': process}
                record = {'stdout': tempfile.TemporaryFile(), 'stderr': tempfile.TemporaryFile(),
                    'background': background, 'eof': set(), 'timed_out': False, 'lock': threading.Lock(),
                    'invocation': SimpleNamespace(close=close)}
                broker.records = {'command': record}
                collectors = [threading.Thread(target=broker._collect, args=('command', name))
                              for name in ('stdout', 'stderr')]
                expiry = threading.Thread(target=broker._expire, args=('command', 5))
                for thread in collectors:
                    thread.start()
                expiry.start()
                try:
                    self.assertEqual(process.wait(timeout=5), code)
                    self.assertEqual(target.read_text(), 'done')
                    expiry.join(timeout=5)
                    self.assertTrue(drained)
                    self.assertFalse(record['timed_out'])
                finally:
                    close()
                    for thread in collectors:
                        thread.join(timeout=5)
                    expiry.join(timeout=5)
                    record['stdout'].close()
                    record['stderr'].close()


@unittest.skipUnless(sys.platform == 'darwin' or sys.platform.startswith('linux'), 'Unix sandbox required')
class ActorReadSandboxTests(unittest.TestCase):
    def test_root_read_view_defaults_to_parent_approved_directories(self):
        from benchmarks.v3_actor_supervisor import ActorSupervisor
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory).resolve()
            workspace=base/'workspace'
            (workspace/'allowed').mkdir(parents=True)
            (workspace/'allowed/source.py').write_text('visible')
            (workspace/'sibling.py').write_text('private')
            home=base/'home'
            home.mkdir()
            supervisor=ActorSupervisor.__new__(ActorSupervisor)
            supervisor.workspace=workspace
            supervisor.policy=ShellPolicy(workspace,{'read'},scope_paths=['allowed'])
            supervisor.protected_files=[]
            supervisor._environment=lambda home:{'PATH':os.environ['PATH'],'HOME':str(home)}
            code='''import json,pathlib,sys
p=pathlib.Path(sys.argv[1])
out=[(p/'allowed/source.py').read_text()]
try:
    (p/'sibling.py').read_text()
    out.append(False)
except OSError:
    out.append(True)
print(json.dumps(out))
'''
            invocation=supervisor._protected_actor([sys.executable,'-I','-c',code,str(workspace)],home)
            try:
                result=subprocess.run(invocation.command,cwd=workspace,env=invocation.env,
                                      capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(json.loads(result.stdout),['visible',True])
            finally:
                invocation.close()

    def test_empty_read_scope_denies_file_data_and_directory_listing(self):
        from benchmarks import v3_actor_supervisor as module
        from benchmarks.v3_protected_worker import protected_shell_command
        import shlex
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            (workspace / 'secret').write_text('private')
            code = '''import json,pathlib,sys
p=pathlib.Path(sys.argv[1])
try:
    (p/'secret').read_text()
    denied=False
except OSError:
    denied=True
try:
    names=[item.name for item in p.iterdir()]
except OSError:
    names=[]
print(json.dumps([denied,names]))
'''
            invocation = protected_shell_command(shlex.join([sys.executable,'-I','-c',code,str(workspace)]),
                policy=ShellPolicy(workspace, {'read'}), cwd=str(workspace),
                env={'PATH': os.environ['PATH']}, protected_files=[])
            try:
                module._restrict_reads(invocation, workspace, ())
                result = subprocess.run(invocation.command, cwd=workspace, env=invocation.env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), [True, []])
            finally:
                invocation.close()

    def test_raw_python_reads_only_assigned_file_and_cannot_discover_siblings(self):
        from benchmarks import v3_actor_supervisor as module
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            allowed = workspace / 'source.py'
            allowed.write_text('allowed')
            (workspace / 'sibling.py').write_text('private')
            policy = ShellPolicy(workspace, {'read'})
            code = '''import json,pathlib,sys
p=pathlib.Path(sys.argv[1])
out=[(p/"source.py").read_text()]
try:
    (p/"sibling.py").read_text()
    out.append(False)
except OSError:
    out.append(True)
try:
    out.append(sorted(path.name for path in p.iterdir()))
except OSError:
    out.append(True)
print(json.dumps(out))
'''
            from benchmarks.v3_protected_worker import protected_shell_command
            import shlex
            invocation = protected_shell_command(shlex.join([sys.executable, '-I', '-c', code, str(workspace)]),
                policy=policy, cwd=str(workspace), env={'PATH': os.environ['PATH']}, protected_files=[])
            try:
                self.assertTrue(hasattr(module, '_restrict_reads'), 'actor read sandbox is missing')
                module._restrict_reads(invocation, workspace, ((allowed, False),))
                result = subprocess.run(invocation.command, env=invocation.env, cwd=workspace,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                values = json.loads(result.stdout)
                self.assertEqual(values[:2], ['allowed', True])
                self.assertIn(values[2], (True, ['source.py']))
            finally:
                invocation.close()


if __name__ == '__main__':
    unittest.main()
