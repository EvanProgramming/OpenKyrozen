"""Offline contract checks for the benchmark-only fresh-exec actor adapter."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def actor_module(case):
    case.assertIsNotNone(importlib.util.find_spec('benchmarks.v3_actor_worker'),
                         'fresh-exec actor adapter is missing')
    return importlib.import_module('benchmarks.v3_actor_worker')


class MutationAdapterTests(unittest.TestCase):
    def test_mutations_preserve_real_atomic_edit_results_without_local_authority(self):
        module = actor_module(self)
        from openkyrozen.tools import ToolAdapters
        from openkyrozen.tools.models import invoke_tool_result
        from openkyrozen.agent.conversation import tool_result_to_dict
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'source.py'
            target.write_text('before\n')
            original = {name: getattr(ToolAdapters, name) for name in
                        ('write_file', 'edit_file', 'apply_patch')}
            trusted = ToolAdapters(root)
            class Client:
                def rpc(self, op, **values):
                    if op != 'file_mutation':
                        raise AssertionError(op)
                    action, arguments = values['action'], values['arguments']
                    if arguments['path'] != 'source.py':
                        raise PermissionError('outside assignment')
                    return tool_result_to_dict(invoke_tool_result(
                        original[action].__get__(trusted, ToolAdapters), arguments))
            with patch.multiple(ToolAdapters, **original):
                module.install_mutation_adapter(Client())
                actor = ToolAdapters(root)
                result = invoke_tool_result(actor.edit_file, {
                    'path': 'source.py', 'old_text': 'before', 'new_text': 'after',
                    'expected_sha256': hashlib.sha256(b'before\n').hexdigest()})
                self.assertTrue(result.success)
                self.assertEqual(target.read_text(), 'after\n')
                stale = invoke_tool_result(actor.edit_file, {
                    'path': 'source.py', 'old_text': 'after', 'new_text': 'bad',
                    'expected_sha256': hashlib.sha256(b'before\n').hexdigest()})
                self.assertEqual(stale.error.code, 'stale_file')
                with self.assertRaisesRegex(PermissionError, 'outside assignment'):
                    invoke_tool_result(actor.write_file, {'path': 'other.py', 'content': 'bad'})
                self.assertFalse((root / 'other.py').exists())


class DelegationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {
            'HOME': str(self.root), 'KYROZEN_DB_PATH': str(self.root / 'state.sqlite3'),
            'KYROZEN_SKILLS_DIR': str(self.root / 'skills'),
            'KYROZEN_WORKSPACE_ROOT': str(self.root), 'KYROZEN_DISABLE_VECTOR_INDEX': '1'}, clear=True)
        self.environment.start()
        from openkyrozen.app.bootstrap import build_application
        from openkyrozen.providers.config import ProviderConfig
        self.application = build_application()
        self.runtime = self.application.runtime
        self.runtime._set_workspace_root(str(self.root))
        self.runtime._provider_config = ProviderConfig(provider='deepseek', api_key='must-not-leak',
            base_url='http://127.0.0.1:1/v1', model_simple='offline', model_complex='offline')
        self.runtime.DEEPSEEK_MODEL = 'offline'
        self.coordinator = None
        self.run = {'run_id': 'run1', 'budget_id': 'run1', 'profile': 'researcher',
                    'name': 'Author', 'reviewer': 'Reviewer', 'review_agent': {}, 'status': 'succeeded',
                    'assignment': {'objective': 'Read source', 'context': 'Offline fixture',
                                   'scope': ['source.py']}, 'usage': {}}

    def tearDown(self):
        self.application.close()
        self.environment.stop()
        self.temporary.cleanup()

    def test_adapter_services_actual_shared_budget_and_returns_original_result(self):
        module = actor_module(self)
        from openkyrozen.agent.delegation import DelegationBudget
        budget = DelegationBudget()
        budget.reserve('run1', review=False)
        expected = {'result': 'checked', 'session_id': 'run1', 'tool_records': [],
                    'evidence': [], 'metrics': {'cost_picos': 12}}
        outer = self
        class Client:
            stopped = False
            stage = 0
            def rpc(self, op, **values):
                if op == 'delegated_launch':
                    encoded = json.dumps(values['payload'])
                    outer.assertNotIn('must-not-leak', encoded)
                    outer.assertNotIn('api_key', encoded)
                    outer.assertNotIn('sibling-private', encoded)
                    return {'id': 'handle'}
                if op == 'delegated_poll':
                    self.stage += 1
                    if self.stage == 1:
                        return {'state': 'running', 'requests': [
                            {'id': 'cost', 'op': 'budget_record_cost', 'values': {'cost_picos': 12}},
                            {'id': 'continue', 'op': 'budget_can_continue', 'values': {'cost_picos': 0}}]}
                    outer.assertTrue(values['replies'][0]['ok'])
                    outer.assertIs(values['replies'][1]['value'], True)
                    return {'state': 'complete', 'requests': [], 'result': dict(expected,
                        _benchmark_request_context_records=[{'actor_id': 'author', 'kind': 'generation'}])}
                if op == 'delegated_stop':
                    self.stopped = True
                    return None
                raise AssertionError(op)
        client = Client()
        self.assertTrue(hasattr(module, 'install_delegated_adapter'), 'delegation adapter is missing')
        module.install_delegated_adapter(self.runtime, client, source=Path(__file__).resolve().parents[1])
        self.coordinator = self.runtime.delegation()
        self.coordinator.runs['run1'] = self.run
        self.coordinator.messages['sibling-run'] = [{'role': 'user', 'content': 'sibling-private'}]
        self.coordinator.budgets['run1'] = budget
        import threading
        self.coordinator.cancelled['run1'] = threading.Event()
        result = self.runtime._invoke_delegated(self.run, review=False, feedback=None,
                                                coordinator=self.coordinator)
        self.assertEqual(result, expected)
        self.assertEqual(self.runtime._benchmark_child_context_records,
                         [{'actor_id': 'author', 'kind': 'generation'}])
        self.assertEqual(budget._spent['run1'], 12)
        self.assertTrue(client.stopped)

    def test_unknown_child_callback_fails_closed_and_releases_parent_access(self):
        module = actor_module(self)
        outer = self
        class Client:
            stage = 0
            def rpc(self, op, **values):
                if op == 'delegated_launch': return {'id': 'handle'}
                if op == 'delegated_stop': return None
                self.stage += 1
                if self.stage == 1:
                    return {'state': 'running', 'requests': [
                        {'id': 'access', 'op': 'access_acquire', 'values': {
                            'path': str(outer.root / 'source.py'), 'reading': False, 'recursive': False}},
                        {'id': 'bad', 'op': 'arbitrary_python', 'values': {'code': 'bad'}}]}
                if values['replies'][1]['ok']:
                    raise AssertionError('unknown callback was granted')
                return {'state': 'failed', 'requests': [], 'error': 'unsupported callback'}
        self.assertTrue(hasattr(module, 'install_delegated_adapter'), 'delegation adapter is missing')
        module.install_delegated_adapter(self.runtime, Client(), source=Path(__file__).resolve().parents[1])
        self.coordinator = self.runtime.delegation()
        self.coordinator.runs['run1'] = self.run
        import threading
        self.coordinator.cancelled['run1'] = threading.Event()
        with self.assertRaisesRegex(RuntimeError, 'unsupported callback'):
            self.runtime._invoke_delegated(self.run, review=False, feedback=None,
                                           coordinator=self.coordinator)
        self.assertFalse(self.coordinator.access.paths)


    def test_child_cannot_append_to_the_parent_or_a_sibling_message_history(self):
        module = actor_module(self)
        class Client:
            stage = 0
            def rpc(self, op, **values):
                if op == 'delegated_launch': return {'id': 'handle'}
                if op == 'delegated_stop': return None
                self.stage += 1
                if self.stage == 1:
                    return {'state': 'running', 'requests': [
                        {'id': 'forged', 'op': 'message', 'values': {
                            'child_id': 'surface:cli', 'message': {'role': 'user', 'content': 'forged'}}}]}
                return {'state': 'complete', 'requests': [], 'result': {'denied': not values['replies'][0]['ok']}}
        module.install_delegated_adapter(self.runtime, Client(), source=Path(__file__).resolve().parents[1])
        self.coordinator = self.runtime.delegation()
        self.coordinator.runs['run1'] = self.run
        import threading
        self.coordinator.cancelled['run1'] = threading.Event()
        result = self.runtime._invoke_delegated(self.run, review=False, feedback=None, coordinator=self.coordinator)
        self.assertTrue(result['denied'])
        self.assertNotIn('surface:cli', self.coordinator.messages)


    def test_existing_learning_state_is_rejected_before_actor_dispatch(self):
        module = actor_module(self)
        class Client:
            def rpc(self, *args, **kwargs):
                raise AssertionError('actor launched despite unsupported shared learning state')
        module.install_delegated_adapter(self.runtime, Client(), source=Path(__file__).resolve().parents[1])
        self.coordinator = self.runtime.delegation()
        self.coordinator.runs['run1'] = self.run
        import threading
        self.coordinator.cancelled['run1'] = threading.Event()
        self.runtime.memory_bank.store.append_event('learning.regression_case_created',
            {'task_signature': 'prior', 'required_outcome': 'shared guidance'},
            user_id=self.runtime.memory_bank.user_id, workspace_id=self.runtime.memory_bank.workspace_id)
        with self.assertRaisesRegex(RuntimeError, 'existing skill or learning regression state'):
            self.runtime._invoke_delegated(self.run, review=False, feedback=None, coordinator=self.coordinator)


class ActorDriverTests(unittest.TestCase):
    def test_driver_keeps_real_author_and_review_scope_receipts_and_report_contract(self):
        module = actor_module(self)
        from openkyrozen.app.bootstrap import build_application
        from openkyrozen.tools import ToolAdapters
        from openkyrozen.providers.config import ProviderConfig
        source = Path(__file__).resolve().parents[1]
        for review in (False, True):
            with self.subTest(review=review), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = root / 'workspace'
                workspace.mkdir()
                (workspace / 'source.py').write_text('evidence')
                environment = {'HOME': str(root), 'KYROZEN_DB_PATH': str(root / 'private.sqlite3'),
                               'KYROZEN_WORKSPACE_ROOT': str(workspace),
                               'KYROZEN_SKILLS_DIR': str(root / 'skills'), 'KYROZEN_DISABLE_VECTOR_INDEX': '1'}
                original_mutators = {name: getattr(ToolAdapters, name) for name in
                                     ('write_file', 'edit_file', 'apply_patch', 'read_file', 'search_files',
                                      'list_dir', 'list_tree', 'find_files')}
                with patch.dict(os.environ, environment, clear=True), patch.multiple(ToolAdapters, **original_mutators):
                    app = build_application()
                    runtime = app.runtime
                    seen = []
                    class Provider:
                        def chat(self, messages, model):
                            seen.append((runtime.execution_context.child_run_id,
                                         runtime.execution_context.capability_token.capabilities))
                            if len(seen) == 1:
                                return 'Action: {"action":"read_file","args":"source.py"}', None
                            answer = ({'verdict': 'verified', 'summary': 'Independently read source',
                                       'findings': [], 'evidence': ['source.py']} if review else
                                      {'status': 'completed', 'summary': 'Read source evidence',
                                       'findings': [], 'evidence': ['source.py'], 'artifacts': [], 'checks': ['read'],
                                       'uncertainties': [], 'suggested_followups': []})
                            return json.dumps(answer), None
                    runtime.get_provider = lambda config: Provider()
                    events = []
                    class Client:
                        def rpc(self, op, **values):
                            if op == 'file_read':
                                from openkyrozen.tools.models import invoke_tool_result
                                from openkyrozen.agent.conversation import tool_result_to_dict
                                trusted = ToolAdapters(workspace)
                                method = original_mutators[values['action']].__get__(trusted, ToolAdapters)
                                return tool_result_to_dict(invoke_tool_result(method, values['arguments']))
                            if op != 'delegated_call': raise AssertionError(op)
                            operation, data = values['operation'], values['values']
                            if operation == 'cancelled': return False
                            if operation in {'memory_recall_records', 'memory_recall', 'memory_recent'}: return []
                            if operation == 'parent_interaction_state': return {'mode': 'agent'}
                            if operation == 'parent_task_state': return {'tasks': [], 'working_plan': None}
                            if operation == 'parent_active_task': return None
                            if operation == 'access_acquire': return 'access'
                            if operation == 'usage':
                                if review and any(not key.startswith('run1_review_') for key in data['usage']):
                                    raise PermissionError('reviewer tried to replace author usage')
                                return None
                            if operation in {'access_release', 'publish', 'receipt', 'message', 'usage_attempts'}:
                                return None
                            if operation == 'event': events.append(data['event']); return None
                            raise AssertionError(operation)
                    payload = {'schema_version': 1, 'source': str(source), 'workspace': str(workspace),
                        'run': {'run_id': 'run1', 'budget_id': 'run1', 'profile': 'researcher', 'name': 'Author',
                                'reviewer': 'Reviewer', 'review_agent': {},
                                'usage': {'run1': {'cost_picos': 17}},
                                'report': {'artifacts': ['source.py']},
                                'assignment': {'objective': 'Read source.py', 'context': 'Inspect actual file',
                                               'scope': ['source.py']}},
                        'review': review, 'feedback': None, 'report_repair': False,
                        'supports_report_repair': True, 'messages': {},
                        'parent': {'session_id': 'surface:cli', 'user_id': 'local', 'workspace_id': 'default',
                                   'task_workspace_id': runtime.tasks.workspace_id,
                                   'capabilities': ['read', 'write', 'shell', 'network'],
                                   'interaction_mode': 'agent', 'model': 'offline', 'has_budget': False,
                                   'profiles': []}}
                    with patch('openkyrozen.app.bootstrap.build_application', return_value=app):
                        result = module.run_actor(payload, Client(), source=source,
                            provider_config=ProviderConfig(provider='deepseek', api_key='actor-local',
                                base_url='http://127.0.0.1:1/v1', model_simple='offline', model_complex='offline'))
                    if review:
                        self.assertTrue(result['session_id'].startswith('run1_review_'))
                        self.assertEqual(json.loads(result['result'])['verdict'], 'verified')
                    else:
                        self.assertEqual(result['session_id'], 'run1')
                        self.assertEqual(json.loads(result['result'])['status'], 'completed')
                    self.assertEqual(result['tool_records'][0]['action'], 'read_file')
                    context = result['_benchmark_request_context_records']
                    self.assertEqual(len(context), len(seen))
                    self.assertGreaterEqual(len(context), 2)
                    self.assertTrue(all(item['actor_run_id'] == 'run1' and item['review'] is review
                                        and item['outcome'] == 'returned' for item in context))
                    self.assertTrue(result['tool_records'][0]['success'])
                    self.assertTrue(all(child_id == 'run1' for child_id, _ in seen))
                    self.assertTrue(all('write' not in caps and 'shell' not in caps for _, caps in seen))
                    self.assertTrue(any(event.get('event') == 'tool_receipt' for event in events))


class MutationHelperTests(unittest.TestCase):
    def test_helper_uses_selected_current_and_frozen_snapshot_without_app_startup(self):
        module = actor_module(self)
        self.assertTrue(hasattr(module, 'run_file_mutation'), 'selected-source mutation helper is missing')
        import io
        import subprocess
        import sys
        import tarfile
        repository = Path(__file__).resolve().parents[1]
        archive = subprocess.run(['git', 'archive', '1bfc82c793df49e76097061ec5adfef0e4e2e883'],
                                 cwd=repository, capture_output=True, check=True).stdout
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / 'baseline'
            baseline.mkdir()
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(baseline, filter='data')
            for name, selected in (('current', repository), ('baseline', baseline)):
                workspace = root / ('workspace-' + name)
                workspace.mkdir()
                (workspace / 'sitecustomize.py').write_text("from pathlib import Path; Path('poisoned').touch()")
                arguments = ({'path': 'source.py', 'content': 'checked'} if name == 'current'
                             else 'source.py|checked')
                env = {'PATH': '/usr/bin:/bin', 'HOME': str(root / 'home'), 'PYTHONDONTWRITEBYTECODE': '1'}
                result = subprocess.run([sys.executable, '-I', str(repository / 'benchmarks/v3_actor_worker.py'),
                    '--source', str(selected), '--file-mutation'],
                    input=json.dumps({'workspace': str(workspace), 'action': 'write_file', 'arguments': arguments}),
                    capture_output=True, text=True, cwd=workspace, env=env, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertIsInstance(output, dict if name == 'current' else str)
                self.assertEqual((workspace / 'source.py').read_text(), 'checked')
                self.assertFalse((workspace / 'poisoned').exists())
                self.assertFalse((root / 'home' / '.kyrozen').exists())
                read = subprocess.run([sys.executable, '-I', str(repository / 'benchmarks/v3_actor_worker.py'),
                    '--source', str(selected), '--file-read'],
                    input=json.dumps({'workspace': str(workspace), 'action': 'read_file',
                                      'arguments': {'path': 'source.py'} if name == 'current' else 'source.py'}),
                    capture_output=True, text=True, cwd=workspace, env=env, timeout=30)
                self.assertEqual(read.returncode, 0, read.stderr)
                content = json.loads(read.stdout)
                self.assertEqual(content['data']['content'] if name == 'current' else content, 'checked')


class SourceBindingTests(unittest.TestCase):
    def test_mutation_helper_rejects_a_different_loaded_product_source(self):
        module = actor_module(self)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(PermissionError, 'selected source'):
                module.run_file_mutation({'workspace': str(root), 'action': 'write_file',
                    'arguments': {'path': 'source.py', 'content': 'bad'}}, source=root)
            self.assertFalse((root / 'source.py').exists())


class FrozenDelegationTests(unittest.TestCase):
    def test_frozen_snapshot_keeps_its_original_delegation_and_has_no_synthetic_budget(self):
        import io
        import subprocess
        import sys
        import tarfile
        repository = Path(__file__).resolve().parents[1]
        archive = subprocess.run(['git', 'archive', '1bfc82c793df49e76097061ec5adfef0e4e2e883'],
                                 cwd=repository, capture_output=True, check=True).stdout
        script = r'''
import json,os,sys,threading
from pathlib import Path
source, apparatus, workspace, home = map(Path, sys.argv[1:])
sys.path.insert(0,str(apparatus)); sys.path.insert(0,str(source))
os.environ.update(HOME=str(home),KYROZEN_DB_PATH=str(home/'private.sqlite3'),
    KYROZEN_WORKSPACE_ROOT=str(workspace),KYROZEN_SKILLS_DIR=str(home/'skills'),KYROZEN_DISABLE_VECTOR_INDEX='1')
from openkyrozen.app.bootstrap import build_application
from openkyrozen.providers.config import ProviderConfig
from v3_actor_worker import install_delegated_adapter,run_actor
app=build_application()
runtime=app.runtime
config=ProviderConfig(provider='deepseek',api_key='offline',model_simple='offline',model_complex='offline')
runtime._provider_config=config; runtime.DEEPSEEK_MODEL='offline'
run={'run_id':'run1','profile':'researcher','name':'Author','reviewer':'Reviewer','review_agent':{},
     'status':'succeeded','assignment':{'objective':'Read source.py','context':'Inspect evidence','scope':['source.py']}}
class RootClient:
 def rpc(self,op,**values):
  if op=='delegated_launch':
   assert values['payload']['parent']['has_budget'] is False
   return {'id':'handle'}
  if op=='delegated_poll': return {'state':'complete','requests':[],'result':{'result':'transport'}}
  if op=='delegated_stop': return None
  raise AssertionError(op)
try:
 install_delegated_adapter(runtime,RootClient(),source=source)
 coordinator=runtime.delegation(); coordinator.runs['run1']=run; coordinator.cancelled['run1']=threading.Event()
 assert runtime._invoke_delegated(run,review=False,feedback=None,coordinator=coordinator)['result']=='transport'
 try: runtime._invoke_delegated(run,review=False,feedback=None,coordinator=coordinator,report_repair=True)
 except RuntimeError as exc: assert 'does not support' in str(exc)
 else: raise AssertionError('invented baseline report repair')
finally: app.close()
from openkyrozen.tools import ToolAdapters
original_read=ToolAdapters.read_file
class ChildClient:
 def rpc(self,op,**values):
  if op=='file_read': return original_read(ToolAdapters(workspace),values['arguments'])
  assert op=='delegated_call'
  operation=values['operation']
  if operation=='cancelled': return False
  if operation in {'memory_recall_records','memory_recall','memory_recent'}: return []
  if operation=='parent_interaction_state': return {'mode':'agent'}
  if operation=='parent_task_state': return {'tasks':[],'working_plan':None}
  if operation=='access_acquire': return 'access'
  if operation in {'access_release','publish','receipt','message','usage','usage_attempts','event'}: return None
  raise AssertionError(operation)
from unittest.mock import patch
app=build_application(); runtime=app.runtime
calls=[]
class Provider:
 def chat(self,messages,model):
  calls.append(messages)
  if len(calls)==1: return 'Action: {"action":"read_file","args":"source.py"}',None
  return json.dumps({'status':'completed','summary':'Checked','findings':[],'evidence':['source.py'],
    'artifacts':[],'checks':['read'],'uncertainties':[],'suggested_followups':[]}),None
runtime.get_provider=lambda config:Provider()
payload={'schema_version':1,'source':str(source),'workspace':str(workspace),'run':run,
 'review':False,'feedback':None,'report_repair':False,'supports_report_repair':False,'messages':{},
 'parent':{'session_id':'surface:cli','user_id':'local','workspace_id':'default',
           'task_workspace_id':runtime.tasks.workspace_id,'capabilities':['read','network'],
           'interaction_mode':'agent','model':'offline','has_budget':False,'profiles':[]}}
with patch('openkyrozen.app.bootstrap.build_application',return_value=app):
 result=run_actor(payload,ChildClient(),source=source,provider_config=config)
assert result['session_id']=='run1',result
assert result['tool_records'][0]['success'] is True,result
print(json.dumps({'original_child':json.loads(result['result'])['status'],'native_calls':len(calls)}))
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline, workspace, home = root / 'baseline', root / 'workspace', root / 'home'
            baseline.mkdir(); workspace.mkdir(); home.mkdir()
            (workspace / 'source.py').write_text('evidence')
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(baseline, filter='data')
            result = subprocess.run([sys.executable, '-I', '-c', script, str(baseline),
                str(repository / 'benchmarks'), str(workspace), str(home)],
                env={'PATH': '/usr/bin:/bin', 'HOME': str(home), 'PYTHONDONTWRITEBYTECODE': '1'},
                capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout.splitlines()[-1]), {'original_child': 'completed', 'native_calls': 2})


class ReadAdapterTests(unittest.TestCase):
    def test_native_reads_see_replaced_parent_inode_instead_of_stale_actor_view(self):
        module = actor_module(self)
        self.assertTrue(hasattr(module, 'install_read_adapter'), 'scoped parent read adapter is missing')
        from openkyrozen.tools import ToolAdapters
        from openkyrozen.tools.models import invoke_tool_result
        from openkyrozen.agent.conversation import tool_result_to_dict
        actions = ('read_file', 'search_files', 'list_dir', 'list_tree', 'find_files')
        originals = {action: getattr(ToolAdapters, action) for action in actions}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, actor = root / 'parent', root / 'actor'
            parent.mkdir(); actor.mkdir()
            (parent / 'source.py').write_text('before')
            (actor / 'source.py').write_text('before')
            replacement = parent / 'replacement'
            replacement.write_text('after')
            replacement.replace(parent / 'source.py')
            trusted = ToolAdapters(parent)
            class Client:
                def rpc(self, op, **values):
                    if op != 'file_read': raise AssertionError(op)
                    method = originals[values['action']].__get__(trusted, ToolAdapters)
                    return tool_result_to_dict(invoke_tool_result(method, values['arguments']))
            with patch.multiple(ToolAdapters, **originals):
                module.install_read_adapter(Client())
                isolated = ToolAdapters(actor)
                result = invoke_tool_result(isolated.read_file, {'path': 'source.py'})
                self.assertTrue(result.success)
                self.assertEqual(result.data['sha256'], hashlib.sha256(b'after').hexdigest())
                self.assertEqual((actor / 'source.py').read_text(), 'before')
                self.assertIn('after', result.text)
