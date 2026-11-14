import copy
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from openkyrozen.app.bootstrap import build_application, build_memory
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.providers.models import ToolCall
from openkyrozen.security.capabilities import issue_capability_token
from openkyrozen.tools import ToolAdapters


class DelegatedPlanningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {'KYROZEN_DISABLE_VECTOR_INDEX': '1', 'KYROZEN_APPROVAL_MODE': 'never',
                                                   'KYROZEN_WORKSPACE_ROOT': str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.app = build_application(memory=build_memory(self.root / 'state.sqlite3'), tools=ToolAdapters(self.root))
        self.addCleanup(self.app.close)
        self.runtime = self.app.runtime
        self.runtime._provider_config = ProviderConfig(provider='deepseek', api_key='fixture-only',
                                                       model_simple='fixture', model_complex='fixture')
        self.runtime.get_provider = lambda config: object()
        self.runtime._execution_capability_token = issue_capability_token('parent', {'read', 'write'})
        self.coordinator = self.runtime.delegation()
        self.addCleanup(self.coordinator.close)
        self.run = {'run_id': 'child-local', 'budget_id': 'child-local', 'profile': 'coder', 'name': 'worker',
                    'reviewer': 'reviewer', 'review_agent': {}, 'status': 'running', 'assignment': {
                        'objective': 'Inspect and update marker.txt', 'context': 'Only assigned file',
                        'scope': ['marker.txt'], 'acceptance': ['Marker verified'], 'deliverables': ['marker.txt'],
                        'dependencies': [], 'reason': 'Independent work',
                    }}
        self.coordinator.runs[self.run['run_id']] = self.run
        self.coordinator.cancelled[self.run['run_id']] = threading.Event()

    def invoke(self, callback, *, review=False):
        with patch.object(self.runtime, 'chat', side_effect=callback):
            return self.runtime._invoke_delegated(self.run, review=review, feedback=None, coordinator=self.coordinator)

    def test_real_delegated_boundary_exposes_and_executes_only_child_local_plan(self):
        runtime = self.runtime
        parent = runtime.current_session
        proposal = parent.interaction.propose_plan({
            'title': 'Marker', 'summary': 'Update assigned marker', 'bounds': {
                'workspace_paths': ['marker.txt'], 'capabilities': ['read', 'write'],
                'effects': ['read_only', 'filesystem', 'internal'], 'risk_ceiling': 'normal'},
            'steps': [{'id': 'marker', 'title': 'Marker', 'description': 'Write marker.txt',
                       'acceptance': ['Marker verified']}],
        })
        parent.interaction.accept_plan(parent.tasks, plan_id=proposal['plan_id'], version=proposal['version'])
        parent_snapshot = copy.deepcopy(parent.tasks.working_plan_snapshot())
        parent_interaction = copy.deepcopy(parent.interaction.state())
        other = runtime.open_session('other-child')
        other.tasks.update_plan(complexity='complex', tasks=[{'description': 'Other plan', 'acceptance': ['Preserved']}])
        other_snapshot = copy.deepcopy(other.tasks.working_plan_snapshot())
        child_sessions = []

        def child_turn(child, message, **kwargs):
            child_sessions.append(child)
            self.assertIsNot(child.tasks, parent.tasks)
            self.assertIn('update_plan', runtime._permitted_tool_names())
            created = runtime.execute_call(child, ToolCall('child-plan', 'update_plan', {
                'expected_revision': 0, 'operations': [{'op': 'add', 'ref': 'inspect', 'task': {
                    'description': 'Inspect assigned marker', 'acceptance': ['Marker checked']}}],
            }))
            self.assertTrue(created.success, created.result)
            for foreign in (parent.tasks.working_plan.id, other.tasks.working_plan.id):
                denied = runtime.execute_call(child, ToolCall('foreign-plan', 'update_plan', {
                    'plan_id': foreign, 'expected_revision': 1,
                    'operations': [{'op': 'cancel', 'task': parent.tasks.tasks[0]['id']}],
                }))
                self.assertFalse(denied.success)
            denied_write = runtime.execute_call(child, ToolCall('outside', 'write_file', {
                'path': 'outside.txt', 'content': 'unauthorized'}))
            self.assertFalse(denied_write.success)
            self.assertFalse((self.root / 'outside.txt').exists())
            self.assertEqual(parent.tasks.working_plan_snapshot(), parent_snapshot)
            self.assertEqual(other.tasks.working_plan_snapshot(), other_snapshot)
            self.assertEqual(parent.interaction.state(), parent_interaction)
            return 'Child planning checked.'

        self.invoke(child_turn)
        events = runtime.memory_bank.store.list_events('execution.receipt', limit=None,
            workspace_id=parent.tasks.workspace_id, user_id=parent.memory.user_id,
            session_id=child_sessions[0].session_id)
        self.assertTrue(any(event['payload']['call_id'] == 'child-plan' for event in events))
        self.assertEqual(parent.tasks.working_plan_snapshot(), parent_snapshot)
        self.assertEqual(parent.tasks.tasks[0].get('evidence', []), [])

    def test_readonly_worker_can_plan_locally_without_staging_or_granting_writes(self):
        self.run['profile'] = 'researcher'
        parent = self.runtime.current_session
        parent_spec = self.runtime.tool_registry.get_spec('update_plan')
        def child_turn(child, message, **kwargs):
            runtime = self.runtime
            self.assertIn('update_plan', runtime._permitted_tool_names())
            spec = runtime.tool_registry.get_spec('update_plan')
            self.assertEqual(spec.capability, 'read')
            self.assertFalse(spec.parallel_safe)
            self.assertEqual(parent_spec.capability, 'write')
            self.assertNotIn('write', runtime.execution_context.capability_token.capabilities)
            created = runtime.execute_call(child, ToolCall('readonly-plan', 'update_plan', {
                'expected_revision': 0, 'operations': [{'op': 'add', 'ref': 'inspect', 'task': {
                    'description': 'Inspect assigned file', 'acceptance': ['Inspected'],
                    'execution': {'action': 'read_file', 'arguments': {'path': 'marker.txt'}}}}],
            }))
            self.assertTrue(created.success, created.result)
            revision = child.tasks.working_plan.revision
            for action, arguments in (('write_file', {'path': 'marker.txt', 'content': 'forbidden'}),
                                      ('run_cmd', {'command': 'echo unsafe'}),
                                      ('read_file', {'path': 'outside.txt'})):
                denied = runtime.execute_call(child, ToolCall('staged-denial', 'update_plan', {
                    'plan_id': child.tasks.working_plan.id, 'expected_revision': revision,
                    'operations': [{'op': 'add', 'ref': 'bad', 'task': {
                        'description': 'Forbidden execution', 'acceptance': ['Never'],
                        'execution': {'action': action, 'arguments': arguments}}}],
                }))
                self.assertFalse(denied.success, action)
                self.assertEqual(child.tasks.working_plan.revision, revision)
            denied = runtime.execute_call(child, ToolCall('write-denial', 'write_file', {
                'path': 'marker.txt', 'content': 'forbidden'}), capabilities='write')
            self.assertFalse(denied.success)
            self.assertFalse((self.root / 'marker.txt').exists())
            return 'Readonly planning validated.'
        self.invoke(child_turn)
        self.assertIs(self.runtime.tool_registry.get_spec('update_plan'), parent_spec)

    def test_simple_child_classification_ignores_runtime_role_and_report_boilerplate(self):
        from openkyrozen.providers.base import LLMProvider
        from openkyrozen.providers.models import ModelResponse, ProviderCapabilities, FinishReason, ToolReply
        self.run['profile'] = 'researcher'
        self.run['assignment']['objective'] = 'Read marker.txt'
        self.run['assignment']['context'] = 'Report its exact contents'
        (self.root / 'marker.txt').write_text('simple source', encoding='utf-8')
        observed, runtime = [], self.runtime
        class NativeRead(LLMProvider):
            def get_capabilities(self, model=None):
                return ProviderCapabilities(native_tools=True)
            def chat(self, messages, model=None):
                raise AssertionError('legacy path used')
            def chat_response(self, messages, model=None, *, tools=None):
                observed.append(set(item['name'] for item in tools))
                if not any(isinstance(item, ToolReply) for item in messages):
                    return ModelResponse(tool_calls=(ToolCall('read', 'read_file', {'path': 'marker.txt'}),),
                                         finish_reason=FinishReason.TOOL_REQUEST)
                return ModelResponse(text='The assigned file contains simple source.', finish_reason=FinishReason.FINAL)
        provider = NativeRead(runtime._provider_config)
        runtime.get_provider = lambda config: provider
        result = runtime._invoke_delegated(self.run, review=False, feedback=None, coordinator=self.coordinator)
        self.assertIn('read_file', observed[0])
        self.assertIn('simple source', result['result'])
        self.assertTrue(any(receipt['call_id'] == 'read' and receipt['success'] for receipt in result['tool_records']))

    def test_actual_complex_readonly_native_child_establishes_own_plan_without_write_authority(self):
        import json
        from openkyrozen.providers.base import LLMProvider
        from openkyrozen.providers.models import ModelResponse, ProviderCapabilities, FinishReason, ToolReply
        self.run['profile'] = 'researcher'
        self.run['assignment']['objective'] = 'Audit marker.txt comprehensively and inspect its evidence'
        (self.root / 'marker.txt').write_text('independent source', encoding='utf-8')
        parent = self.runtime.current_session
        parent_spec = self.runtime.tool_registry.get_spec('update_plan')
        runtime, observed, children = self.runtime, [], []
        class NativeAudit(LLMProvider):
            def get_capabilities(self, model=None):
                return ProviderCapabilities(native_tools=True)
            def chat(self, messages, model=None):
                raise AssertionError('legacy provider path used')
            def chat_response(self, messages, model=None, *, tools=None):
                children.append(runtime.current_session)
                replies = [item for item in messages if isinstance(item, ToolReply)]
                observed.append(set(item['name'] for item in tools))
                if not replies:
                    self_case.assertEqual(observed[-1], {'update_plan', 'read_tool_artifact'})
                    call = ToolCall('local-plan', 'update_plan', {'expected_revision': 0, 'operations': [
                        {'op': 'add', 'ref': 'inspect', 'task': {'description': 'Inspect assigned marker source',
                            'acceptance': ['Report exact checked source'],
                            'execution': {'action': 'read_file', 'arguments': {'path': 'marker.txt'}}}},
                        {'op': 'start', 'task': 'inspect'},
                    ]})
                elif not any(item.call_id == 'inspect' for item in replies):
                    call = ToolCall('inspect', 'read_file', {'path': 'marker.txt'})
                else:
                    return ModelResponse(text=json.dumps({'status': 'partial', 'summary': 'Source inspected; independent acceptance still pending',
                        'findings': [], 'evidence': ['marker.txt'], 'artifacts': [], 'checks': ['Source read'],
                        'uncertainties': ['Independent acceptance pending'], 'suggested_followups': []}), finish_reason=FinishReason.FINAL)
                return ModelResponse(tool_calls=(call,), finish_reason=FinishReason.TOOL_REQUEST)
        self_case = self
        provider = NativeAudit(self.runtime._provider_config)
        self.runtime.get_provider = lambda config: provider
        result = self.runtime._invoke_delegated(self.run, review=False, feedback=None, coordinator=self.coordinator)
        self.assertIn('Source inspected', result['result'])
        child = children[0]
        self.assertIsNotNone(child.tasks.working_plan)
        self.assertEqual(len(child.tasks.tasks), 1)
        read = next(receipt for receipt in result['tool_records'] if receipt['call_id'] == 'inspect')
        self.assertTrue(read['success'])
        self.assertEqual(read['plan_id'], child.tasks.working_plan.id)
        self.assertEqual(read['task_id'], child.tasks.tasks[0]['id'])
        self.assertEqual(read['session_id'], child.session_id)
        self.assertEqual(parent.tasks.tasks, [])
        self.assertIs(runtime.tool_registry.get_spec('update_plan'), parent_spec)
        self.assertEqual(parent_spec.capability, 'write')
        self.assertEqual((self.root / 'marker.txt').read_text(), 'independent source')

    def test_children_and_reviewers_retrieve_only_their_own_tool_artifacts(self):
        (self.root / 'marker.txt').write_text('own output ' * 1400 + 'END_OF_OWN_OUTPUT', encoding='utf-8')
        parent = self.runtime.current_session
        parent_scope = {'user_id': parent.tasks.user_id, 'workspace_id': parent.tasks.workspace_id,
                        'session_id': parent.tasks.session_id}
        parent_artifact = parent.memory.store.artifacts.create('PRIVATE_PARENT_OUTPUT', **parent_scope)
        for reviewing in (False, True):
            with self.subTest(reviewing=reviewing):
                def child_turn(child, message, **kwargs):
                    runtime = self.runtime
                    self.assertIn('read_tool_artifact', runtime._permitted_tool_names())
                    scope = {'user_id': child.tasks.user_id, 'workspace_id': child.tasks.workspace_id,
                             'session_id': child.tasks.session_id}
                    archived = runtime._agent_engine._store_tool_output('OWN_ARCHIVED_OUTPUT')
                    fetched = runtime.execute_call(child, ToolCall('own-archive', 'read_tool_artifact', {'artifact_id': archived}))
                    self.assertTrue(fetched.success, fetched.result)
                    self.assertEqual(fetched.tool_result.data['content'], 'OWN_ARCHIVED_OUTPUT')
                    read = runtime.execute_call(child, ToolCall('large-read', 'read_file', {'path': 'marker.txt'}))
                    self.assertTrue(read.success)
                    artifact = read.tool_result.data['artifact_id']
                    size = read.tool_result.data['total_chars']
                    last = runtime.execute_call(child, ToolCall('last-page', 'read_tool_artifact', {
                        'artifact_id': artifact, 'offset': max(0, size - 1000), 'limit': 1000}))
                    self.assertTrue(last.success, last.result)
                    self.assertIn('END_OF_OWN_OUTPUT', last.tool_result.data['content'])
                    foreign = [parent_artifact]
                    for alternate in ({**scope, 'user_id': 'another-user'},
                                      {**scope, 'workspace_id': 'another-workspace'},
                                      {**scope, 'session_id': 'another-child'}):
                        foreign.append(child.memory.store.artifacts.create('PRIVATE_FOREIGN_OUTPUT', **alternate))
                    for artifact_id in foreign:
                        denied = runtime.execute_call(child, ToolCall('denied', 'read_tool_artifact', {'artifact_id': artifact_id}))
                        self.assertFalse(denied.success)
                        self.assertEqual(denied.failure, 'not_found')
                        self.assertNotIn('PRIVATE_', denied.result)
                    self.assertIsNone(child.memory.store.artifacts.read_page(archived, 0, 100, **parent_scope))
                    return 'Own artifacts checked.'
                self.invoke(child_turn, review=reviewing)

    def test_parent_material_reapproval_still_blocks_child_plan_mutation(self):
        parent = self.runtime.current_session
        proposal = parent.interaction.propose_plan({
            'title': 'Marker', 'summary': 'Update marker', 'bounds': {
                'workspace_paths': ['marker.txt'], 'capabilities': ['read', 'write'],
                'effects': ['read_only', 'filesystem', 'internal'], 'risk_ceiling': 'normal'},
            'steps': [{'id': 'marker', 'title': 'Marker', 'description': 'Write marker.txt',
                       'acceptance': ['Marker verified']}],
        })
        parent.interaction.accept_plan(parent.tasks, plan_id=proposal['plan_id'], version=proposal['version'])
        parent.interaction.propose_plan({
            'title': 'Expand work', 'summary': 'Add an unrelated output', 'bounds': proposal['bounds'],
            'steps': [{'id': 'extra', 'title': 'Extra', 'description': 'Write another.txt',
                       'acceptance': ['Another output created']}],
        })
        self.assertIsNotNone(parent.interaction.state()['pending_plan'])
        before = copy.deepcopy(parent.tasks.working_plan_snapshot())
        def child_turn(child, message, **kwargs):
            denied = self.runtime.execute_call(child, ToolCall('must-wait', 'update_plan', {
                'expected_revision': 0, 'operations': [{'op': 'add', 'ref': 'new', 'task': {
                    'description': 'Must wait', 'acceptance': ['Never created']}}],
            }))
            self.assertFalse(denied.success)
            self.assertIsNone(child.tasks.working_plan)
            self.assertEqual(parent.tasks.working_plan_snapshot(), before)
            return 'Child waits for renewed approval.'
        self.invoke(child_turn)

    def test_readonly_reviewer_cannot_acquire_plan_write_permission(self):
        def review_turn(child, message, **kwargs):
            self.assertNotIn('update_plan', self.runtime._permitted_tool_names())
            denied = self.runtime.execute_call(child, ToolCall('forbidden-plan', 'update_plan', {
                'expected_revision': 0, 'operations': [{'op': 'add', 'ref': 'new', 'task': {
                    'description': 'Must not create', 'acceptance': ['Never']}}],
            }))
            self.assertFalse(denied.success)
            self.assertIsNone(child.tasks.working_plan)
            return 'Review remains read only.'
        self.invoke(review_turn, review=True)
