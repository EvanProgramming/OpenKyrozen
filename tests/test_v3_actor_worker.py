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
                            {'id': 'continue', 'op': 'budget_can_continue', 'values': {'cost_picos': 0}},
                            {'id': 'history', 'op': 'canonical_history', 'values': {
                                'session_id': 'run1', 'kind': 'finish', 'offset': 0, 'chunk': '', 'final': True}}]}
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
                            'child_id': 'surface:cli', 'message': {'role': 'user', 'content': 'forged'}}},
                        {'id': 'history', 'op': 'canonical_history', 'values': {
                            'session_id': 'run1', 'kind': 'finish', 'offset': 0, 'chunk': '', 'final': True}}]}
                return {'state': 'complete', 'requests': [], 'result': {
                    'session_id': 'run1', 'denied': not values['replies'][0]['ok']}}
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

    def test_native_child_history_survives_private_home_deletion_and_incremental_replay(self):
        from contextlib import ExitStack
        import threading
        from openkyrozen.app.bootstrap import build_application
        from openkyrozen.agent.conversation import ConversationHistory
        from openkyrozen.persistence.store import EventStore
        from openkyrozen.providers.base import LLMProvider
        from openkyrozen.providers.config import ProviderConfig
        from openkyrozen.providers.models import (FinishReason, ModelResponse, ProviderCapabilities,
                                                  ProviderContinuation, ToolCall)
        from openkyrozen.tools import ToolAdapters
        module = actor_module(self)
        source = Path(__file__).resolve().parents[1]
        (self.root / 'source.py').write_text(('actual source evidence ' * 20 + '\n') * 500)
        scopes = {'user_id': self.runtime.memory_bank.user_id,
                  'workspace_id': self.runtime.interaction_workspace_id()}
        phases = []
        originals = {name: getattr(ToolAdapters, name) for name in
                     ('write_file', 'edit_file', 'apply_patch', 'read_file', 'search_files',
                      'list_dir', 'list_tree', 'find_files')}
        with ExitStack() as homes:
            author_home = Path(homes.enter_context(tempfile.TemporaryDirectory()))
            for phase, review in enumerate((False, False, True, True, False)):
                home = Path(homes.enter_context(tempfile.TemporaryDirectory())) if review else author_home
                callbacks = []
                class Provider(LLMProvider):
                    _continuation_id = 'offline-history'
                    def __init__(provider):
                        super().__init__(ProviderConfig(provider='deepseek', model_simple='offline'))
                        provider._continuation_id = 'offline-history'
                        provider.calls = 0
                    def get_capabilities(provider, model=None):
                        return ProviderCapabilities(native_tools=True)
                    def chat(provider, messages, model=None):
                        raise AssertionError('native history used the legacy provider boundary')
                    def chat_response(provider, messages, model=None, *, tools=None):
                        provider.calls += 1
                        if provider.calls == 1:
                            return ModelResponse(tool_calls=(ToolCall(f'read-{phase}', 'read_file',
                                {'path': 'source.py'}),), finish_reason=FinishReason.TOOL_REQUEST,
                                usage={'prompt_tokens': 100, 'completion_tokens': 20},
                                metadata={'original_phase': phase}, continuation=ProviderContinuation(
                                    'offline-history', 'offline', 'fixture', {'padding': 'p' * 1_050_000}))
                        if phase == 4:
                            raise RuntimeError('offline native failure')
                        report = ({'verdict': 'verified', 'summary': f'Review {phase}',
                                   'findings': [], 'evidence': ['source.py']} if review else
                                  {'status': 'completed', 'summary': f'Author {phase}', 'findings': [],
                                   'evidence': ['source.py'], 'artifacts': [], 'checks': ['read'],
                                   'uncertainties': [], 'suggested_followups': []})
                        return ModelResponse(text=json.dumps(report), finish_reason=FinishReason.FINAL,
                                             usage={'prompt_tokens': 120, 'completion_tokens': 25})
                class ChildClient:
                    def rpc(client, op, **values):
                        if op == 'file_read':
                            from openkyrozen.agent.conversation import tool_result_to_dict
                            from openkyrozen.tools.models import invoke_tool_result
                            adapter = ToolAdapters(self.root)
                            return tool_result_to_dict(invoke_tool_result(
                                originals[values['action']].__get__(adapter, ToolAdapters), values['arguments']))
                        if op != 'delegated_call': raise AssertionError(op)
                        operation, data = values['operation'], values['values']
                        if operation == 'cancelled': return False
                        if operation in {'memory_recall_records', 'memory_recall', 'memory_recent'}: return []
                        if operation == 'parent_interaction_state': return {'mode': 'agent'}
                        if operation == 'parent_task_state': return {'tasks': [], 'working_plan': None}
                        if operation == 'parent_active_task': return None
                        if operation == 'access_acquire': return 'access'
                        if operation in {'canonical_history', 'receipt'}:
                            callbacks.append({'id': str(len(callbacks)), 'op': operation, 'values': data})
                            self.assertLessEqual(len(json.dumps(values, ensure_ascii=False).encode()), 1_000_000)
                            return None
                        if operation in {'access_release', 'publish', 'message', 'usage', 'usage_attempts', 'event'}:
                            return None
                        raise AssertionError(operation)
                environment = {'HOME': str(home), 'KYROZEN_DB_PATH': str(home / 'private.sqlite3'),
                    'KYROZEN_SKILLS_DIR': str(home / 'skills'), 'KYROZEN_WORKSPACE_ROOT': str(self.root),
                    'KYROZEN_DISABLE_VECTOR_INDEX': '1'}
                with patch.dict(os.environ, environment, clear=True), patch.multiple(ToolAdapters, **originals):
                    child = build_application()
                    child.runtime.get_provider = lambda config: Provider()
                    payload = {'schema_version': 1, 'source': str(source), 'workspace': str(self.root),
                        'run': dict(self.run), 'review': review, 'feedback': {'findings': ['recheck']} if phase == 1 else None,
                        'report_repair': False, 'supports_report_repair': True, 'messages': {},
                        'parent': dict(scopes, workspace_id='default', task_workspace_id=scopes['workspace_id'],
                            session_id=self.runtime.current_session.session_id,
                            capabilities=['read', 'network'], interaction_mode='agent', model='offline',
                            has_budget=False, profiles=[])}
                    with patch('openkyrozen.app.bootstrap.build_application', return_value=child):
                        config = ProviderConfig(provider='deepseek', api_key='offline-only',
                            model_simple='offline', model_complex='offline', context_window_tokens=1_000_000)
                        if phase == 4:
                            with self.assertRaisesRegex(RuntimeError, 'Native model request failed'):
                                module.run_actor(payload, ChildClient(), source=source, provider_config=config)
                            result = {'session_id': 'run1', '_test_failed': True}
                        else:
                            result = module.run_actor(payload, ChildClient(), source=source, provider_config=config)
                store = EventStore(home / 'private.sqlite3')
                try:
                    scope = dict(scopes, session_id=result['session_id'])
                    entries = store.list_conversation_entries(**scope)
                    display = ConversationHistory(store, **scope).display_messages()
                    referenced = module._artifact_references(entries)
                    for callback in callbacks:
                        if callback['op'] == 'receipt':
                            referenced.update(module._artifact_references(callback['values']['receipt']))
                    with store.connection() as connection:
                        artifacts = [dict(row) for row in connection.execute('SELECT * FROM tool_artifacts')
                                     if row['id'] in referenced]
                    phases.append((review, callbacks, result, entries, display, artifacts))
                finally:
                    store.close()
        # All source-owned private databases and artifact files are gone here.
        self.assertFalse(author_home.exists())
        self.assertGreater(len(phases[1][3]), len(phases[0][3]))
        self.assertNotEqual(phases[2][2]['session_id'], phases[3][2]['session_id'])
        outer = self
        class RootClient:
            index = -1
            stage = 0
            def rpc(client, op, **values):
                if op == 'delegated_launch':
                    client.index += 1; client.stage = 0
                    return {'id': str(client.index)}
                if op == 'delegated_stop': return None
                if op != 'delegated_poll': raise AssertionError(op)
                client.stage += 1
                if client.stage == 1:
                    return {'state': 'running', 'requests': phases[client.index][1]}
                outer.assertTrue(all(reply['ok'] for reply in values['replies']), values['replies'])
                if phases[client.index][2].get('_test_failed'):
                    return {'state': 'failed', 'requests': [], 'error': 'Native model request failed'}
                return {'state': 'complete', 'requests': [], 'result': phases[client.index][2]}
        module.install_delegated_adapter(self.runtime, RootClient(), source=source)
        self.coordinator = self.runtime.delegation()
        self.run['reviews'] = []
        self.coordinator.runs['run1'] = self.run
        self.coordinator.cancelled['run1'] = threading.Event()
        for review, _, expected, entries, display, artifacts in phases:
            if expected.get('_test_failed'):
                with self.assertRaisesRegex(RuntimeError, 'Native model request failed'):
                    self.runtime._invoke_delegated(self.run, review=review, feedback=None, coordinator=self.coordinator)
                result = expected
                self.assertTrue(any(entry['kind'] == 'model.failure' for entry in entries))
            else:
                result = self.runtime._invoke_delegated(self.run, review=review, feedback=None,
                                                       coordinator=self.coordinator)
            if review:
                self.run['reviews'].append({'session_id': result['session_id']})
            detail = self.coordinator.detail('run1')
            self.assertEqual(detail['review_messages'][result['session_id']] if review else detail['messages'], display)
            scope = dict(scopes, session_id=result['session_id'])
            self.assertEqual(self.application.store.list_conversation_entries(**scope), entries)
            self.assertTrue(any(entry['kind'] == 'tool.result' for entry in entries))
            self.assertTrue(artifacts)
            for artifact in artifacts:
                from openkyrozen.tools.models import invoke_tool_result
                owned_child = self.runtime.open_session(result['session_id'], user_id=scopes['user_id'])
                with self.runtime.use_session(owned_child):
                    page = invoke_tool_result(self.runtime._read_tool_artifact,
                        {'artifact_id': artifact['id'], 'offset': 7, 'limit': 1000})
                self.assertTrue(page.success)
                actual = page.data
                self.assertEqual(actual['content'], artifact['content'][7:1007])
                self.assertIsNone(self.application.store.artifacts.read_page(artifact['id'], 0, 1000,
                    **dict(scope, session_id='sibling')))

    def test_original_unbound_delegation_detail_reads_its_actual_native_journal_scope(self):
        import threading
        from openkyrozen.agent.conversation import ConversationHistory
        from openkyrozen.providers.base import LLMProvider
        from openkyrozen.providers.config import ProviderConfig
        from openkyrozen.providers.models import FinishReason, ModelResponse, ProviderCapabilities
        class Provider(LLMProvider):
            def __init__(provider):
                super().__init__(ProviderConfig(provider='deepseek', model_simple='offline'))
            def get_capabilities(provider, model=None):
                return ProviderCapabilities(native_tools=True)
            def chat(provider, messages, model=None):
                raise AssertionError('native fixture entered the legacy provider')
            def chat_response(provider, messages, model=None, *, tools=None):
                return ModelResponse(text=json.dumps({'status': 'completed', 'summary': 'Native evidence',
                    'findings': [], 'evidence': [], 'artifacts': [], 'checks': [],
                    'uncertainties': [], 'suggested_followups': []}), finish_reason=FinishReason.FINAL,
                    usage={'prompt_tokens': 100, 'completion_tokens': 20})
        self.runtime.get_provider = lambda config: Provider()
        self.coordinator = self.runtime.delegation()
        self.run['reviews'] = []
        self.coordinator.runs['run1'] = self.run
        self.coordinator.cancelled['run1'] = threading.Event()
        result = self.runtime._invoke_delegated(self.run, review=False, feedback=None, coordinator=self.coordinator)
        scope = dict(user_id=self.runtime.memory_bank.user_id,
                     workspace_id=self.runtime.interaction_workspace_id(), session_id=result['session_id'])
        original = ConversationHistory(self.application.store, **scope).display_messages()
        self.assertTrue(original)
        self.assertEqual(self.coordinator.detail('run1')['messages'], original,
            f"journal={scope['workspace_id']}, tasks={self.runtime.tasks.workspace_id}, "
            f"file_scope={self.runtime.memory_bank.file_scope_id}")

    def test_rejected_canonical_import_stops_before_accepting_a_completed_result(self):
        import threading
        module = actor_module(self)
        class Client:
            stopped = False
            def rpc(client, op, **values):
                if op == 'delegated_launch': return {'id': 'handle'}
                if op == 'delegated_stop': client.stopped = True; return None
                return {'state': 'running', 'requests': [{'id': 'forged', 'op': 'canonical_history',
                    'values': {'session_id': 'surface:cli', 'kind': 'finish',
                               'offset': 0, 'chunk': '', 'final': True}}]}
        client = Client()
        module.install_delegated_adapter(self.runtime, client, source=Path(__file__).resolve().parents[1])
        self.coordinator = self.runtime.delegation()
        self.coordinator.cancelled['run1'] = threading.Event()
        with self.assertRaisesRegex(RuntimeError, 'canonical history import failed'):
            self.runtime._invoke_delegated(self.run, review=False, feedback=None, coordinator=self.coordinator)
        self.assertTrue(client.stopped)

    def test_native_completion_cannot_omit_its_session_or_skip_canonical_finish(self):
        import threading
        module = actor_module(self)
        cases = [(True, {}), (True, {'session_id': None}),
                 (False, {'session_id': 'run1'}), (True, {'session_id': 'sibling'})]
        class Client:
            case = -1
            stage = 0
            stopped = 0
            def rpc(client, op, **values):
                if op == 'delegated_launch':
                    client.case += 1; client.stage = 0
                    return {'id': str(client.case)}
                if op == 'delegated_stop': client.stopped += 1; return None
                client.stage += 1
                finish, result = cases[client.case]
                if client.stage == 1 and finish:
                    return {'state': 'running', 'requests': [{'id': 'history', 'op': 'canonical_history',
                        'values': {'session_id': 'run1', 'kind': 'finish',
                                   'offset': 0, 'chunk': '', 'final': True}}]}
                return {'state': 'complete', 'requests': [], 'result': dict(result, result='claimed complete')}
        client = Client()
        module.install_delegated_adapter(self.runtime, client, source=Path(__file__).resolve().parents[1])
        self.coordinator = self.runtime.delegation()
        self.coordinator.cancelled['run1'] = threading.Event()
        for case in cases:
            with self.subTest(case=case), self.assertRaisesRegex(RuntimeError, 'without its canonical history'):
                self.runtime._invoke_delegated(self.run, review=False, feedback=None, coordinator=self.coordinator)
        self.assertEqual(client.stopped, len(cases))


class CanonicalHistoryImportTests(unittest.TestCase):
    def setUp(self):
        from openkyrozen.persistence.store import EventStore
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = EventStore(self.root / 'parent.sqlite3')
        self.module = actor_module(self)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def receiver(self, *, review=False):
        def owned(session_id):
            if not (session_id.startswith('run1_review_') if review else session_id == 'run1'):
                raise PermissionError('unowned child session')
        return self.module._CanonicalHistoryImport(self.store, user_id='alice', workspace_id='interaction',
            artifact_workspace_id='tasks', budget_id='run1', review=review, owned_session=owned)

    def entry(self, **changes):
        from openkyrozen.agent.conversation import ConversationEntry
        value = ConversationEntry(id='conv_original', kind='user.input', payload={'text': 'source input'},
            user_id='alice', workspace_id='interaction', session_id='run1', turn_id='turn-original').as_dict()
        return dict(value, **changes)

    def send(self, receiver, kind, value, *, session_id='run1'):
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        for offset in range(0, len(encoded), self.module._HISTORY_CHUNK):
            end = offset + self.module._HISTORY_CHUNK
            values = dict(session_id=session_id, kind=kind, offset=offset,
                          chunk=encoded[offset:end], final=end >= len(encoded))
            self.module._json_copy(values)
            receiver.receive(**values)

    def finish(self, receiver, session_id='run1'):
        receiver.receive(session_id=session_id, kind='finish', offset=0, chunk='', final=True)

    def test_cross_scope_malformed_chain_and_conflicting_ids_are_rejected(self):
        variants = [dict(user_id='sibling'), dict(workspace_id='memory'), dict(session_id='surface:cli'),
                    dict(parent_id='missing-head'), dict(version=2), dict(turn_id=['malformed'])]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError, PermissionError)):
                self.send(self.receiver(), 'entry', self.entry(**changes))
        entry = self.entry()
        receiver = self.receiver()
        self.send(receiver, 'entry', entry)
        with self.assertRaises((ValueError, PermissionError)):
            self.send(receiver, 'entry', entry)
        with self.assertRaisesRegex(ValueError, 'prefix'):
            self.send(self.receiver(), 'entry', dict(entry, payload={'text': 'conflicting replay'}))
        self.assertEqual(self.store.list_conversation_entries(user_id='alice', workspace_id='interaction',
                                                             session_id='run1'), [entry])

    def test_referenced_artifacts_keep_original_identity_and_separate_task_scope_in_chunks(self):
        identifier = 'artifact_' + 'a' * 32
        summary = {'artifact_id': identifier, 'total_chars': 1_260_000, 'preview': 'source-owned content'}
        entry = self.entry(kind='tool.result', payload={'call_id': 'read-source', 'name': 'read_file',
            'output': {'data': summary, 'text': json.dumps(summary), 'error': None,
                       'metadata': {'artifact_id': identifier}}, 'is_error': False})
        artifact = dict(id=identifier, user_id='alice', workspace_id='tasks', session_id='run1',
                        content='source-owned content\n' * 60_000, created_at=entry['created_at'])
        for changes in (dict(user_id='sibling'), dict(workspace_id='interaction'),
                        dict(session_id='sibling'), dict(id='unreferenced')):
            receiver = self.receiver()
            self.send(receiver, 'entry', entry)
            with self.subTest(changes=changes), self.assertRaises(PermissionError):
                self.send(receiver, 'artifact', dict(artifact, **changes))
        for _ in range(2):
            receiver = self.receiver()
            self.send(receiver, 'entry', entry)
            self.send(receiver, 'artifact', artifact)
            self.finish(receiver)
        self.assertEqual(len(self.store.list_conversation_entries(user_id='alice', workspace_id='interaction',
                                                                 session_id='run1')), 1)
        page = self.store.artifacts.read_page(identifier, 5, 1000,
            user_id='alice', workspace_id='tasks', session_id='run1')
        self.assertEqual(page['content'], artifact['content'][5:1005])
        self.assertIsNone(self.store.artifacts.read_page(identifier, 0, 1000,
            user_id='alice', workspace_id='interaction', session_id='run1'))
        receiver = self.receiver()
        self.send(receiver, 'entry', entry)
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            self.send(receiver, 'artifact', dict(artifact, content='forged'))
        with self.store.connection() as connection:
            self.assertEqual(dict(connection.execute('SELECT * FROM tool_artifacts').fetchone()), artifact)

    def test_partial_chunks_missing_artifacts_stale_heads_and_reused_review_sessions_fail_closed(self):
        receiver = self.receiver()
        receiver.receive(session_id='run1', kind='entry', offset=0, chunk='{', final=False)
        with self.assertRaisesRegex(ValueError, 'out of order'):
            receiver.receive(session_id='run1', kind='entry', offset=0, chunk='}', final=True)
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.finish(receiver)
        identifier = 'artifact_' + 'b' * 32
        entry = self.entry(kind='tool.result', payload={'call_id': 'read-source', 'name': 'read_file',
            'output': {'data': {'artifact_id': identifier, 'total_chars': 40}, 'text': 'archived',
                       'error': None, 'metadata': {'artifact_id': identifier}}, 'is_error': False})
        receiver = self.receiver()
        self.send(receiver, 'entry', entry)
        with self.assertRaisesRegex(ValueError, 'dangling'):
            self.finish(receiver)
        review_id = 'run1_review_original'
        review_entry = self.entry(id='conv_review', session_id=review_id)
        receiver = self.receiver(review=True)
        self.send(receiver, 'entry', review_entry, session_id=review_id)
        self.finish(receiver, review_id)
        with self.assertRaisesRegex(PermissionError, 'reused'):
            self.send(self.receiver(review=True), 'entry', review_entry, session_id=review_id)

    def test_concurrent_native_head_change_is_not_overwritten(self):
        entry = self.entry()
        receiver = self.receiver()
        self.send(receiver, 'entry', entry)
        other = self.entry(id='conv_concurrent', parent_id=entry['id'], payload={'text': 'concurrent native entry'})
        self.store.append_conversation_entry(other, expected_head=entry['id'])
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.finish(receiver)
        self.assertEqual(self.store.conversation_head(user_id='alice', workspace_id='interaction', session_id='run1'),
                         other['id'])

    def test_plain_json_user_source_and_opaque_provider_values_export_without_artifacts(self):
        from contextlib import closing
        from types import SimpleNamespace
        from openkyrozen.agent.conversation import ConversationHistory
        from openkyrozen.persistence.store import EventStore
        records = [('user.input', {'text': '{"artifact_id":"ordinary-user-record"}'}),
            ('tool.result', {'call_id': 'read-source', 'name': 'read_file', 'is_error': False,
                'output': {'data': {'content': '{"artifact_id":"ordinary-source-record"}'},
                           'text': 'read source JSON', 'error': None, 'metadata': {}}}),
            ('model.response', {'response': {'text': '{"artifact_id":"ordinary-model-record"}',
                'continuation': {'provider_id': 'fixture', 'request_model': 'offline', 'protocol': 'opaque',
                                 'payload': {'artifact_id': 'ordinary-provider-state'}}}}),
            ('user.input', {'text': json.dumps({'artifact_id': 'artifact_' + 'd' * 32})}),
            ('tool.result', {'call_id': 'read-json', 'name': 'read_file', 'is_error': False,
                'output': {'data': {'content': json.dumps({'artifact_id': 'artifact_' + 'd' * 32})},
                           'text': 'ordinary source JSON', 'error': None, 'metadata': {}}})]
        scope = dict(user_id='alice', workspace_id='interaction', session_id='run1')
        for index, (kind, payload) in enumerate(records):
            with self.subTest(kind=kind), closing(EventStore(self.root / f'source-{index}.sqlite3')) as store:
                history = ConversationHistory(store, **scope)
                history.append(history.entry(kind, payload))
                calls = []
                coordinator = SimpleNamespace(artifact_references={}, call=lambda op, **values: calls.append((op, values)))
                self.module._export_canonical_history(store, coordinator,
                    {'run1': (scope, dict(scope, workspace_id='tasks'))})
                self.assertEqual([values['kind'] for _, values in calls], ['entry', 'finish'])

    def test_only_matched_runtime_artifact_and_successful_page_envelopes_are_references(self):
        identifier = 'artifact_' + 'c' * 32
        summary = {'artifact_id': identifier, 'total_chars': 123}
        output = {'data': summary, 'text': 'Earlier tool result archived; use read_tool_artifact to retrieve it.',
                  'error': None, 'metadata': {'artifact_id': identifier}}
        archived = self.entry(kind='tool.result', payload={'call_id': 'read', 'name': 'read_file',
            'output': output, 'is_error': False})
        receipt = {'receipt_id': 'receipt-source', 'action': 'read_file', 'success': True,
                   'tool_result': output, 'result_data': summary, 'arguments': {'artifact_id': 'ordinary-argument'}}
        self.assertEqual(self.module._artifact_references([archived, receipt]), {identifier})
        arbitrary = self.entry(kind='tool.result', payload={'call_id': 'read', 'name': 'read_file',
            'output': dict(output, metadata={}, data={'artifact_id': identifier, 'domain_record': True}), 'is_error': False})
        self.assertEqual(self.module._artifact_references(arbitrary), set())
        page = dict(artifact_id=identifier, offset=0, limit=1000, total_chars=123, content='source page', next_offset=None)
        paged = self.entry(kind='tool.result', payload={'call_id': 'page', 'name': 'read_tool_artifact',
            'output': dict(output, data=page, metadata={}), 'is_error': False})
        self.assertEqual(self.module._artifact_references(paged), {identifier})
        failure = {'receipt_id': 'failed-page', 'action': 'read_tool_artifact', 'success': False,
                   'arguments': {'artifact_id': 'missing'}, 'result_data': None, 'tool_error': {'code': 'not_found'}}
        self.assertEqual(self.module._artifact_references(failure), set())


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
                (workspace / 'source.py').write_text(json.dumps({'artifact_id': 'artifact_' + 'd' * 32}))
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
                    synchronized = []
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
                                synchronized.append('usage')
                                if review and any(not key.startswith('run1_review_') for key in data['usage']):
                                    raise PermissionError('reviewer tried to replace author usage')
                                return None
                            if operation in {'access_release', 'publish', 'receipt', 'message', 'usage_attempts',
                                             'canonical_history'}:
                                if operation == 'usage_attempts': synchronized.append(operation)
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
                    self.assertEqual(synchronized, ['usage', 'usage_attempts'])


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
   assert 'conversation_workspace_id' not in values['payload']['run']
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
