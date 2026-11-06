import os
import shlex
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from openkyrozen.app.bootstrap import build_application, build_memory
from openkyrozen.tools import ToolAdapters
from openkyrozen.persistence.store import EventStore
from openkyrozen.tasks.engine import TaskManager
from openkyrozen.tasks.worker import TaskWorker


class RuntimeReliabilityTests(unittest.TestCase):
    def test_turn_cancellation_stops_its_background_process_and_reports_cancelled(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'KYROZEN_DISABLE_VECTOR_INDEX': '1',
        }):
            root = Path(directory)
            app = build_application(memory=build_memory(root / 'state.sqlite3'), tools=ToolAdapters(root))
            self.addCleanup(app.close)
            runtime = app.runtime
            ready, release = threading.Event(), threading.Event()
            handles, events = [], []

            def turn(*args, **kwargs):
                result = runtime.run_command({
                    'command': shlex.join([sys.executable, '-c',
                        'import time; print("ready",flush=True); time.sleep(30)']),
                    'background': True, 'timeout_seconds': 30,
                })
                self.assertTrue(result.success)
                handles.append(runtime.current_session.process_manager._handles[result.process_id])
                ready.set()
                release.wait(5)
                return 'Cancellation requested.'

            session = runtime.current_session
            with patch.object(runtime, '_chat_turn', side_effect=turn):
                worker = threading.Thread(target=lambda: runtime.chat(session, 'start', on_runtime_event=events.append))
                worker.start()
                try:
                    self.assertTrue(ready.wait(5))
                    self.assertTrue(runtime.cancel_turn(session.session_id, user_id=session.memory.user_id))
                    deadline = time.monotonic() + 3
                    while handles[0].process.poll() is None and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertIsNotNone(handles[0].process.poll())
                    page = session.process_manager.read(handles[0].process_id)
                    self.assertTrue(page['cancelled'])
                    self.assertFalse(page['timed_out'])
                finally:
                    release.set()
                    worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(events[-1].kind, 'turn.cancelled')

    def test_turn_cancelled_signal_controls_terminal_event_when_tool_raises(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'KYROZEN_DISABLE_VECTOR_INDEX': '1'}):
            root = Path(directory)
            app = build_application(memory=build_memory(root / 'state.sqlite3'), tools=ToolAdapters(root))
            self.addCleanup(app.close)
            runtime, events = app.runtime, []
            session = runtime.current_session
            def cancelled_turn(*args, **kwargs):
                runtime.cancel_turn(session.session_id, user_id=session.memory.user_id)
                raise RuntimeError('tool stopped after cancellation')
            with patch.object(runtime, '_chat_turn', side_effect=cancelled_turn), self.assertRaises(RuntimeError):
                runtime.chat(session, 'run', on_runtime_event=events.append)
            self.assertEqual(events[-1].kind, 'turn.cancelled')

    def test_background_cancellation_keeps_other_turn_process_running(self):
        from openkyrozen.tools.processes import SessionProcessManager
        with tempfile.TemporaryDirectory() as directory:
            manager = SessionProcessManager('user', directory, 'session')
            self.addCleanup(manager.close)
            cancelled = threading.Event()
            command = shlex.join([sys.executable, '-c', 'import time; time.sleep(30)'])
            first = manager.start(command, cwd=directory, env=None, timeout_seconds=30, cancelled=cancelled)
            other = manager.start(command, cwd=directory, env=None, timeout_seconds=30, cancelled=threading.Event())
            cancelled.set()
            deadline = time.monotonic() + 3
            while first.process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertIsNotNone(first.process.poll())
            self.assertIsNone(other.process.poll())

    def test_recovery_blocks_unknown_mutation_without_dispatching_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / 'state.sqlite3')
            manager = TaskManager(store, workspace_id='project', session_id='session')
            index = manager.add_task('append once', checkpoint={'action': 'run_cmd', 'args': 'echo duplicate >> marker'})
            manager.set_status(index, 'running')
            recovered = TaskManager(store, workspace_id='project', session_id='session')
            recovered.recover()
            self.assertEqual(recovered.tasks[0]['status'], 'blocked')
            self.assertEqual(recovered.tasks[0]['checkpoint']['recovery_effect']['status'], 'unknown')
            calls = []
            worker = TaskWorker(recovered, lambda task: calls.append(task) or {'success': True})
            self.assertIsNone(worker.run_once())
            self.assertEqual(calls, [])
            # A generic resume request cannot acknowledge or erase an unknown effect.
            self.assertIsNone(recovered.resume(recovered.tasks[0]['id']))
            self.assertIsNone(worker.run_once())
            self.assertEqual(calls, [])

    def test_common_resume_unblock_and_native_binding_cannot_replay_unknown_effect(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'KYROZEN_DISABLE_VECTOR_INDEX': '1', 'KYROZEN_WORKSPACE_ROOT': directory}):
            root = Path(directory)
            app = build_application(memory=build_memory(root / 'state.sqlite3'), tools=ToolAdapters(root))
            self.addCleanup(app.close)
            runtime, manager = app.runtime, app.runtime.tasks
            created = manager.edit_plan(expected_revision=0, operations=[
                {'op': 'add', 'ref': 'write', 'task': {'description': 'Write once', 'acceptance': ['Written'],
                    'execution': {'action': 'write_file', 'arguments': {'path': 'marker.txt', 'content': 'duplicate'}}}},
                {'op': 'start', 'task': 'write'},
            ])
            task_id = created['references']['write']
            manager.recover()
            self.assertIsNone(manager.resume(task_id))
            for operation in ({'op': 'unblock', 'task': task_id, 'reason': 'Model says retry'},
                              {'op': 'start', 'task': task_id},
                              {'op': 'replace', 'task': task_id, 'replacement': {'ref': 'retry', 'task': {
                                  'description': 'Retry same effect', 'acceptance': ['Written'],
                                  'execution': {'action': 'write_file', 'arguments': {'path': 'marker.txt', 'content': 'duplicate'}}}}}):
                with self.subTest(operation=operation), self.assertRaises(ValueError):
                    manager.edit_plan(plan_id=created['plan_id'], expected_revision=1, operations=[operation])
            # Even stale in-memory task state cannot authorize an unresolved database row.
            manager.tasks[0]['status'] = 'running'
            self.assertFalse(manager.set_status(0, 'pending'))
            manager.tasks[0]['checkpoint'].pop('recovery_effect')
            self.assertIsNone(manager.execution_task_id)
            receipt = runtime._execute_turn_action('write_file', {'path': 'marker.txt', 'content': 'duplicate'},
                operation_scope='restart', successful_operations=set(), call_id='replay', call_id_source='native',
                task_id=task_id, task_plan_id=created['plan_id'])
            self.assertFalse(receipt.success)
            self.assertFalse((root / 'marker.txt').exists())

    def test_unclassified_running_native_task_is_not_treated_as_read_only_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / 'state.sqlite3')
            manager = TaskManager(store, workspace_id='project', session_id='native')
            manager.edit_plan(expected_revision=0, operations=[
                {'op': 'add', 'ref': 'work', 'task': {'description': 'Unclassified work', 'acceptance': ['Verified']}},
                {'op': 'start', 'task': 'work'},
            ])
            manager.recover()
            self.assertEqual(manager.tasks[0]['status'], 'blocked')
            self.assertEqual(manager.tasks[0]['checkpoint']['recovery_effect']['status'], 'unknown')

    def test_completed_background_handles_do_not_consume_live_process_limit(self):
        from openkyrozen.tools.processes import SessionProcessManager
        with tempfile.TemporaryDirectory() as directory:
            manager = SessionProcessManager('user', directory, 'history')
            self.addCleanup(manager.close)
            first_id = None
            for index in range(33):
                handle = manager.start('echo retained-output', cwd=directory, env=None, timeout_seconds=5)
                first_id = first_id or handle.process_id
                handle.process.wait(timeout=3)
            deadline = time.monotonic() + 2
            page = manager.read(first_id)
            while not page['output'] and time.monotonic() < deadline:
                time.sleep(.01)
                page = manager.read(first_id)
            self.assertEqual(page['output'], 'retained-output\n')
            self.assertFalse(page['running'])

    def test_native_cancel_terminalizes_accepted_strategy_children_only(self):
        self._assert_native_cancel_terminalizes_strategy_children(narrow_scope=False)

    def test_native_cancel_after_safe_narrowing_terminalizes_original_strategy_children(self):
        self._assert_native_cancel_terminalizes_strategy_children(narrow_scope=True)

    def _assert_native_cancel_terminalizes_strategy_children(self, *, narrow_scope):
        from openkyrozen.providers.base import LLMProvider
        from openkyrozen.providers.config import ProviderConfig
        from openkyrozen.providers.models import ModelResponse, ProviderCapabilities, FinishReason
        from openkyrozen.providers.errors import ProviderError, ProviderErrorKind
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'KYROZEN_DISABLE_VECTOR_INDEX': '1', 'KYROZEN_WORKSPACE_ROOT': directory}):
            root = Path(directory)
            app = build_application(memory=build_memory(root / 'state.sqlite3'), tools=ToolAdapters(root))
            self.addCleanup(app.close)
            runtime, manager = app.runtime, app.runtime.tasks
            controller = runtime._interaction_controller
            proposal = controller.propose_plan({'title': 'Inspect', 'summary': 'Inspect assigned source',
                'bounds': {'workspace_paths': ['.'] if narrow_scope else ['marker.txt'], 'capabilities': ['read', 'write'],
                           'effects': ['read_only', 'filesystem', 'internal'], 'risk_ceiling': 'normal'},
                'steps': [{'id': 'source', 'title': 'Source', 'description': 'Inspect marker.txt',
                           'acceptance': ['Exact source verified']}]})
            accepted, _ = controller.accept_plan(manager, plan_id=proposal['plan_id'], version=proposal['version'])
            anchor = accepted['working_task_ids'][0]
            split = manager.edit_plan(plan_id=manager.working_plan.id, expected_revision=1, operations=[
                {'op': 'split', 'task': anchor, 'children': [
                    {'ref': 'inspect', 'task': {'description': 'Inspect source', 'acceptance': ['Inspected']}},
                    {'ref': 'verify', 'task': {'description': 'Verify source', 'acceptance': ['Verified']}},
                ]}, {'op': 'start', 'task': 'inspect'},
            ])
            if narrow_scope:
                import copy
                revision = copy.deepcopy(proposal)
                revision['bounds']['workspace_paths'] = ['marker.txt']
                narrowed = controller.propose_plan(revision)
                self.assertEqual(narrowed['revision_materiality'], 'within_bounds')
                self.assertEqual(controller.state()['executing_plan']['version'], accepted['version'] + 1)
                self.assertEqual(manager.tasks[0]['checkpoint']['strategic_plan']['version'], accepted['version'])
            unrelated = manager.add_task('Unrelated later work')
            unrelated_id = manager.tasks[unrelated]['id']
            class CancelProvider(LLMProvider):
                def get_capabilities(self, model=None):
                    return ProviderCapabilities(native_tools=True)
                def chat(self, messages, model=None):
                    raise AssertionError('legacy path used')
                def chat_response(self, messages, model=None, *, tools=None):
                    return ModelResponse(finish_reason=FinishReason.CANCELLED)
            config = ProviderConfig(provider='custom', model_simple='fixture', model_complex='fixture')
            provider = CancelProvider(config)
            runtime._provider_config = config
            runtime.llm_provider = runtime._default_turn.provider = provider
            with self.assertRaises(ProviderError) as cancelled:
                runtime.chat(runtime.current_session, 'Continue accepted source inspection')
            self.assertEqual(cancelled.exception.kind, ProviderErrorKind.CANCELLED)
            expected = {anchor, split['references']['inspect'], split['references']['verify']}
            self.assertEqual({task['id'] for task in manager.tasks if task['status'] == 'cancelled'}, expected)
            self.assertEqual(next(task['status'] for task in manager.tasks if task['id'] == unrelated_id), 'pending')
            self.assertEqual(manager.working_plan.status, 'cancelled')
            self.assertIsNone(controller.state()['executing_plan'])
            recovered = TaskManager(manager.store, user_id=manager.user_id, workspace_id=manager.workspace_id,
                                    session_id=manager.session_id)
            recovered.recover()
            self.assertTrue(all(task['status'] == 'cancelled' for task in recovered.tasks if task['id'] in expected))

    def test_recovery_resumes_only_declared_read_only_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / 'state.sqlite3')
            manager = TaskManager(store, workspace_id='project', session_id='session')
            index = manager.add_task('inspect', checkpoint={'action': 'read_file', 'args': 'note.txt'})
            manager.set_status(index, 'running')
            recovered = TaskManager(store, workspace_id='project', session_id='session')
            recovered.recover()
            self.assertEqual(recovered.tasks[0]['status'], 'pending')
            worker = TaskWorker(recovered, lambda task: {'success': True, 'action': 'read_file', 'result': 'note'})
            self.assertEqual(worker.run_once()['status'], 'succeeded')

    def test_jev_allow_remains_advisory_for_high_impact_operation(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'KYROZEN_DISABLE_VECTOR_INDEX': '1'}):
            root = Path(directory)
            app = build_application(memory=build_memory(root / 'state.sqlite3'), tools=ToolAdapters(root))
            self.addCleanup(app.close)
            runtime = app.runtime
            with patch.object(runtime, '_EXECUTION_SURFACE', 'tui'), \
                    patch.object(runtime, 'permission_mode', return_value='full_jev'), \
                    patch.object(runtime, '_jev_permission_check', return_value=True):
                self.assertFalse(runtime._authorize_tool_action('git_push', {'arguments': ['origin', 'main']},
                                                               approve=lambda *_: False))
                self.assertTrue(runtime._authorize_tool_action('git_push', {'arguments': ['origin', 'main']},
                                                              approve=lambda *_: True))
            with patch.object(runtime, '_EXECUTION_SURFACE', 'tui'), \
                    patch.object(runtime, 'permission_mode', return_value='full'):
                self.assertTrue(runtime._authorize_tool_action('git_push', {'arguments': ['origin', 'main']},
                                                              approve=lambda *_: False))
