import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from openkyrozen.app.bootstrap import build_application, build_memory
from openkyrozen.tools import ToolAdapters
from openkyrozen.tools.processes import SessionProcessManager
from openkyrozen.tasks.engine import TaskManager


class ProcessAuditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.environment = patch.dict(os.environ, {
            'HOME': str(self.root), 'KYROZEN_WORKSPACE_ROOT': str(self.root),
            'KYROZEN_DB_PATH': str(self.root / 'state.sqlite3'),
            'KYROZEN_DISABLE_VECTOR_INDEX': '1',
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.app = build_application(memory=build_memory(self.root / 'state.sqlite3', index=None),
                                     tools=ToolAdapters(self.root))
        self.addCleanup(self.app.close)
        self.runtime = self.app.runtime
        self.session = self.runtime.current_session
        self.manager = self.session.process_manager
        self.store = self.session.memory.store
        self.scope = dict(user_id=self.session.memory.user_id,
                          workspace_id=self.session.memory.workspace_id,
                          session_id=self.session.session_id)
        self.command = shlex.join([sys.executable, '-c', 'import time; time.sleep(30)'])

    def events(self):
        return self.store.list_events('process.lifecycle', limit=None, **self.scope)

    def wait_status(self, manager, process_id, status):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            record = next((r for r in manager._audit_records() if r['process_id'] == process_id), None)
            if record and record['status'] == status:
                return record
            time.sleep(.01)
        self.fail(f'Process audit did not reach {status}')

    def reopened(self, **changes):
        scope = {**self.scope, **changes}
        manager = SessionProcessManager(scope['user_id'], str(self.root), scope['session_id'])
        from openkyrozen.persistence.store import EventStore
        manager._bind_audit(EventStore(self.store.path), scope['workspace_id'])
        return manager

    def test_real_session_launch_cancellation_and_restart_preserve_scoped_uncertainty(self):
        cancelled = threading.Event()
        handle = self.manager.start(self.command, cwd=str(self.root), env=None,
                                    timeout_seconds=30, cancelled=cancelled)
        self.assertEqual({event['payload']['status'] for event in self.events()}, {'launch_intent', 'started'})
        recovered = self.reopened()
        pending = recovered._audit_records()[0]
        self.assertEqual(pending['process_id'], handle.process_id)
        self.assertEqual(pending['owned_process_state'], 'unknown')
        self.assertEqual(pending['descendant_state'], 'unknown')
        self.assertTrue(pending['potentially_active'])
        for change in ({'user_id': 'other'}, {'workspace_id': 'other'}, {'session_id': 'other'}):
            self.assertEqual(self.reopened(**change)._audit_records(), [])
        cancelled.set()
        record = self.wait_status(recovered, handle.process_id, 'cancelled')
        self.assertEqual(record['reason'], 'cancelled')
        self.assertEqual(record['owned_process_state'], 'exited')
        self.assertEqual(record['descendant_state'], 'unknown')
        self.assertIsNotNone(handle.process.poll())

    def test_two_project_roots_with_same_global_memory_and_session_cannot_read_each_others_audit(self):
        first_root, second_root = self.root / 'first', self.root / 'second'
        first_root.mkdir()
        second_root.mkdir()
        self.runtime.configure_launch_context(project_path=first_root)
        first = self.runtime.open_session('shared-session')
        first.process_manager.start(self.command, cwd=str(first_root), env=None, timeout_seconds=30)
        self.runtime.configure_launch_context(project_path=second_root)
        second = self.runtime.open_session('shared-session')
        self.assertEqual(first.memory.workspace_id, second.memory.workspace_id)
        self.assertEqual(first.memory.user_id, second.memory.user_id)
        self.assertEqual(first.session_id, second.session_id)
        self.assertNotEqual(first.tasks.workspace_id, second.tasks.workspace_id)
        self.assertEqual(second.process_manager._audit_records(), [])
        self.assertEqual(len(first.process_manager._audit_records()), 1)

    def test_actual_restarted_native_turn_gets_only_own_bounded_unknown_process_context(self):
        from openkyrozen.workspace.context import source_scope_id
        project = self.root / 'restarted-project'
        project.mkdir()
        workspace_id, session_id = source_scope_id(project), 'restarted-chat'
        own_scope = dict(user_id=self.session.memory.user_id, workspace_id=workspace_id, session_id=session_id)
        for index in range(7):
            self.store.append_event('process.lifecycle', {
                'process_id': f'own-unknown-{index}', 'manager_id': 'previous', 'status': 'started',
                'pid': os.getpid(), 'owned_process_state': 'running', 'descendant_state': 'unknown',
                'command': 'SECRET_COMMAND_NOT_CONTEXT', 'env': {'TOKEN': 'SECRET_ENV_NOT_CONTEXT'},
            }, **own_scope)
        for field, value in (('user_id', 'other-user'), ('workspace_id', 'other-project'), ('session_id', 'other-chat')):
            self.store.append_event('process.lifecycle', {'process_id': f'foreign-{field}',
                'manager_id': 'previous', 'status': 'started', 'owned_process_state': 'unknown'},
                **{**own_scope, field: value})
        self.store.append_event('process.lifecycle', {'process_id': 'known-cancelled', 'manager_id': 'previous',
            'status': 'cancelled', 'reason': 'cancelled', 'owned_process_state': 'exited',
            'descendant_state': 'unknown'}, **own_scope)
        from openkyrozen.providers.base import LLMProvider
        from openkyrozen.providers.config import ProviderConfig
        from openkyrozen.providers.models import ModelResponse, ProviderCapabilities, FinishReason
        observed = []
        class InspectProvider(LLMProvider):
            def chat(self, messages, model=None):
                raise AssertionError('legacy path used')
            def get_capabilities(self, model=None):
                return ProviderCapabilities(native_tools=True)
            def chat_response(self, messages, model=None, *, tools=None):
                observed.append(json.dumps(messages, default=str))
                return ModelResponse(text='Unknown previous work needs inspection.', finish_reason=FinishReason.FINAL)
        # New application and session reopen the same actual SQLite journal.
        fresh = build_application(memory=build_memory(self.root / 'state.sqlite3', index=None), tools=ToolAdapters(project))
        self.addCleanup(fresh.close)
        runtime = fresh.runtime
        runtime.configure_launch_context(project_path=project)
        session = runtime.open_session(session_id)
        config = ProviderConfig(provider='custom', model_simple='fixture', model_complex='fixture')
        runtime.llm_provider = runtime._default_turn.provider = InspectProvider(config)
        runtime._provider_config = config
        session.interaction.set_mode('agent')
        with patch('openkyrozen.tools.processes.os.kill', side_effect=AssertionError('historical PID authority')), patch(
                'openkyrozen.tools.processes.os.killpg', side_effect=AssertionError('historical PID authority')), patch.object(
                runtime, '_schedule_post_turn_learning'):
            answer = runtime.chat(session, 'Hello')
        self.assertIn('needs inspection', answer)
        text = observed[0]
        self.assertIn('Prior process outcomes are unknown', text)
        self.assertIn('total=7', text)
        self.assertIn('omitted=2', text)
        self.assertEqual(text.count('own-unknown-'), 5)
        for forbidden in ('foreign-', 'known-cancelled', 'SECRET_COMMAND_NOT_CONTEXT', 'SECRET_ENV_NOT_CONTEXT'):
            self.assertNotIn(forbidden, text)
        self.assertIn('not authority', text)

    def test_audit_read_failure_retains_explicit_unknown_context(self):
        manager = SessionProcessManager(self.scope['user_id'], str(self.root), self.scope['session_id'])
        with patch.object(self.store, 'list_events', side_effect=sqlite3.OperationalError('SECRET_READ_ERROR')):
            manager._bind_audit(self.store, self.scope['workspace_id'])
        context = manager._recovery_context()
        self.assertIn('audit unavailable', context)
        self.assertIn('unknown', context)
        self.assertNotIn('SECRET_READ_ERROR', context)

    def test_intent_failure_prevents_any_process_launch(self):
        original = self.store._append_control_audit
        def fail_intent(kind, payload, **scope):
            if kind == 'process.lifecycle':
                raise sqlite3.OperationalError('audit unavailable')
            return original(kind, payload, **scope)
        with patch.object(self.store, '_append_control_audit', side_effect=fail_intent), patch(
                'openkyrozen.tools.processes.subprocess.Popen', side_effect=AssertionError('untracked launch')):
            with self.assertRaisesRegex(RuntimeError, 'audit'):
                self.manager.start(self.command, cwd=str(self.root), env=None, timeout_seconds=30)

    def test_started_record_failure_stops_only_new_owned_process_and_leaves_durable_intent(self):
        original = self.store._append_control_audit
        spawned = []
        import subprocess
        popen = subprocess.Popen
        def capture(*args, **kwargs):
            child = popen(*args, **kwargs)
            if kwargs.get('start_new_session') or kwargs.get('creationflags'):
                spawned.append(child)
            return child
        def fail_started(kind, payload, **scope):
            if kind == 'process.lifecycle' and payload['status'] == 'started':
                raise sqlite3.OperationalError('SECRET_DATABASE_ERROR')
            return original(kind, payload, **scope)
        with patch.object(self.store, '_append_control_audit', side_effect=fail_started), patch(
                'openkyrozen.tools.processes.subprocess.Popen', side_effect=capture):
            with self.assertRaisesRegex(RuntimeError, 'audit'):
                self.manager.start(self.command, cwd=str(self.root),
                    env={**os.environ, 'PRIVATE_ENV': 'SECRET_ENVIRONMENT'}, timeout_seconds=30)
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].poll())
        text = json.dumps(self.events())
        for secret in ('SECRET_DATABASE_ERROR', 'SECRET_ENVIRONMENT', self.command):
            self.assertNotIn(secret, text)
        self.assertIn('launch_intent', text)

    def test_held_sqlite_writer_cannot_delay_owned_cancellation_past_existing_deadline(self):
        cancelled, auditing, expired = threading.Event(), threading.Event(), threading.Event()
        original_expire = self.manager._expire
        def expire(*args, **kwargs):
            try:
                return original_expire(*args, **kwargs)
            finally:
                expired.set()
        with patch.object(self.manager, '_expire', side_effect=expire):
            handle = self.manager.start(self.command, cwd=str(self.root), env=None,
                                        timeout_seconds=30, cancelled=cancelled)
        original = self.manager._audit_event
        def observe(status, *args, **kwargs):
            if status == 'stop_requested':
                auditing.set()
            return original(status, *args, **kwargs)
        with self.store.database.connection() as writer, patch.object(
                self.manager, '_audit_event', side_effect=observe):
            writer.execute('BEGIN IMMEDIATE')
            try:
                cancelled.set()
                self.assertTrue(auditing.wait(2))
                handle.process.wait(timeout=3)
                deadline = time.monotonic() + 3
                while not self.manager.read(handle.process_id)['cancelled'] and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(self.manager.read(handle.process_id)['cancelled'])
                self.assertTrue(handle.audit_error)
                # Keep the writer held through the expiry's final audit attempt.
                self.assertTrue(expired.wait(3))
            finally:
                writer.rollback()
        record = self.reopened()._audit_records()[0]
        self.assertEqual(record['owned_process_state'], 'unknown')
        self.assertTrue(record['potentially_active'])

    def test_held_database_thread_lock_fails_intent_without_launch(self):
        settled, errors = threading.Event(), []
        def start():
            try:
                self.manager.start(self.command, cwd=str(self.root), env=None, timeout_seconds=30)
            except RuntimeError as error:
                errors.append(error)
            finally:
                settled.set()
        self.store.database._lock.acquire()
        worker = threading.Thread(target=start)
        worker.start()
        try:
            self.assertTrue(settled.wait(1), 'audit lock blocked launch intent indefinitely')
        finally:
            self.store.database._lock.release()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIn('audit', str(errors[0]))
        self.assertEqual(self.manager._handles, {})

    def test_lost_journal_after_intent_remains_unknown_despite_owned_cleanup(self):
        original = self.store._append_control_audit
        def fail_after_intent(kind, payload, **scope):
            if kind == 'process.lifecycle' and payload['status'] != 'launch_intent':
                raise TypeError('SECRET_SERIALIZATION_DETAIL')
            return original(kind, payload, **scope)
        with patch.object(self.store, '_append_control_audit', side_effect=fail_after_intent):
            with self.assertRaisesRegex(RuntimeError, 'audit'):
                self.manager.start(self.command, cwd=str(self.root), env=None, timeout_seconds=30)
        handle = next(iter(self.manager._handles.values()))
        self.assertIsNotNone(handle.process.poll())
        recovered = self.reopened()._audit_records()[0]
        self.assertEqual(recovered['status'], 'launch_intent')
        self.assertEqual(recovered['owned_process_state'], 'unknown')
        self.assertTrue(recovered['potentially_active'])
        self.assertNotIn('SECRET_SERIALIZATION_DETAIL', json.dumps(self.events()))

    def test_natural_exit_and_timeout_are_distinct_owned_observations_not_descendant_cleanup(self):
        command = shlex.join([sys.executable, '-c', 'pass'])
        natural = self.manager.start(command, cwd=str(self.root), env=None, timeout_seconds=30)
        exited = self.wait_status(self.reopened(), natural.process_id, 'exited')
        self.assertEqual(exited['exit_code'], 0)
        timed = self.manager.start(self.command, cwd=str(self.root), env=None, timeout_seconds=.05)
        timeout = self.wait_status(self.reopened(), timed.process_id, 'timed_out')
        self.assertEqual(timeout['reason'], 'timed_out')
        for record in (exited, timeout):
            self.assertEqual(record['owned_process_state'], 'exited')
            self.assertEqual(record['descendant_state'], 'unknown')
            self.assertTrue(record['potentially_active'])

    def test_cancel_stop_failure_is_durable_unknown_with_cancellation_reason(self):
        if os.name == 'nt':
            self.skipTest('POSIX signal fault injection')
        cancelled = threading.Event()
        handle = self.manager.start(self.command, cwd=str(self.root), env=None,
                                    timeout_seconds=30, cancelled=cancelled)
        with patch('openkyrozen.tools.processes.os.killpg', side_effect=PermissionError('blocked')):
            cancelled.set()
            record = self.wait_status(self.reopened(), handle.process_id, 'termination_failed')
            self.assertEqual(record['reason'], 'cancelled')
            self.assertEqual(record['owned_process_state'], 'unknown')
            self.assertTrue(record['potentially_active'])
            self.assertIsNone(handle.process.poll())
        temporary = handle.temporary_directory
        self.assertTrue(self.manager.stop(handle.process_id))
        if temporary is not None:
            self.assertFalse(Path(temporary.name).exists())

    def test_close_waits_for_owned_expiry_finalization_before_journal_shutdown(self):
        finalizing, release, expired, closing, closed = (threading.Event() for _ in range(5))
        original_expire = self.manager._expire
        def expire(handle, cancelled=None):
            try:
                original_expire(handle, cancelled)
                finalizing.set()
                if not release.wait(3):
                    raise AssertionError('expiry finalization was not released')
                self.manager._audit_event('exited', handle.process_id, handle=handle, owned_process_state='exited')
            finally:
                expired.set()
        with patch.object(self.manager, '_expire', side_effect=expire):
            handle = self.manager.start(shlex.join([sys.executable, '-c', 'pass']),
                cwd=str(self.root), env=None, timeout_seconds=30)
        self.assertTrue(finalizing.wait(3))
        def close():
            closing.set()
            self.manager.close()
            closed.set()
        worker = threading.Thread(target=close)
        worker.start()
        try:
            self.assertTrue(closing.wait(1))
            self.assertFalse(closed.wait(.1), 'close returned before owned audit finalization')
        finally:
            release.set()
            worker.join(3)
            self.assertTrue(expired.wait(3))
        self.assertTrue(closed.is_set())
        self.assertIsNotNone(handle.process.poll())

    def test_failed_stop_close_disables_a_late_expiry_journal_write(self):
        if os.name == 'nt':
            self.skipTest('POSIX signal fault injection')
        cancelled, finalizing, release, expired = (threading.Event() for _ in range(4))
        original_expire = self.manager._expire
        def expire(handle, token=None):
            try:
                original_expire(handle, token)
                finalizing.set()
                if not release.wait(3):
                    raise AssertionError('late expiry was not released')
                self.manager._audit_outcome(handle)
            finally:
                expired.set()
        with patch.object(self.manager, '_expire', side_effect=expire):
            handle = self.manager.start(self.command, cwd=str(self.root), env=None,
                                        timeout_seconds=30, cancelled=cancelled)
        try:
            with patch('openkyrozen.tools.processes.os.killpg', side_effect=PermissionError('blocked')):
                cancelled.set()
                self.assertTrue(finalizing.wait(3))
                self.manager.close()
                before = len(self.events())
                self.assertEqual(self.reopened()._audit_records()[0]['owned_process_state'], 'unknown')
                release.set()
                self.assertTrue(expired.wait(3))
                self.assertEqual(len(self.events()), before)
                self.assertTrue(handle.audit_error)
        finally:
            release.set()
            self.manager.stop(handle.process_id)
            self.assertTrue(expired.wait(3))

    def test_restart_never_uses_historical_pid_as_kill_authority(self):
        self.store.append_event('process.lifecycle', {
            'process_id': 'old', 'manager_id': 'previous-instance', 'status': 'started',
            'pid': os.getpid(), 'owned_process_state': 'running', 'descendant_state': 'unknown',
        }, **self.scope)
        with patch('openkyrozen.tools.processes.os.kill', side_effect=AssertionError('historical PID kill')), patch(
                'openkyrozen.tools.processes.os.killpg', side_effect=AssertionError('historical group kill')):
            recovered = self.reopened()
            self.assertEqual(recovered._audit_records()[0]['owned_process_state'], 'unknown')
            recovered.close()
        self.assertEqual(os.getpid(), self.store.list_events('process.lifecycle', **self.scope)[0]['payload']['pid'])

    def _accepted_native_cancel(self, hold):
        from openkyrozen.providers.base import LLMProvider
        from openkyrozen.providers.config import ProviderConfig
        from openkyrozen.providers.models import ModelResponse, ProviderCapabilities, FinishReason
        from openkyrozen.providers.errors import ProviderErrorKind
        controller, tasks = self.runtime._interaction_controller, self.runtime.tasks
        proposal = controller.propose_plan({'title': 'Inspect', 'summary': 'Inspect source',
            'bounds': {'workspace_paths': ['.'], 'capabilities': ['read', 'write'],
                       'effects': ['read_only', 'filesystem', 'internal'], 'risk_ceiling': 'normal'},
            'steps': [{'id': 'inspect', 'title': 'Inspect', 'description': 'Inspect marker.txt',
                       'acceptance': ['Source verified']}]})
        controller.accept_plan(tasks, plan_id=proposal['plan_id'], version=proposal['version'])
        self.assertTrue(tasks.set_status(0, 'running'))
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        errors, events = [], []
        class CancelProvider(LLMProvider):
            def chat(self, messages, model=None):
                raise AssertionError('legacy path used')
            def get_capabilities(self, model=None):
                return ProviderCapabilities(native_tools=True)
            def chat_response(self, messages, model=None, *, tools=None):
                entered.set()
                if not release.wait(5):
                    raise AssertionError('native provider was not released')
                return ModelResponse(finish_reason=FinishReason.CANCELLED)
        config = ProviderConfig(provider='custom', model_simple='fixture', model_complex='fixture')
        self.runtime.llm_provider = self.runtime._default_turn.provider = CancelProvider(config)
        self.runtime._provider_config = config
        def chat():
            try:
                self.runtime.chat(self.session, 'Continue accepted inspection', on_runtime_event=events.append)
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()
        worker = threading.Thread(target=chat)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            if hold == 'sqlite':
                with self.store.database.connection() as writer:
                    writer.execute('BEGIN IMMEDIATE')
                    try:
                        release.set()
                        self.assertTrue(finished.wait(3), 'SQLite contention delayed cancellation')
                    finally:
                        writer.rollback()
            elif hold == 'thread-lock':
                self.store.database._lock.acquire()
                try:
                    release.set()
                    self.assertTrue(finished.wait(3), 'thread-lock contention delayed cancellation')
                finally:
                    self.store.database._lock.release()
            else:
                release.set()
                self.assertTrue(finished.wait(3))
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(getattr(errors[0], 'kind', None), ProviderErrorKind.CANCELLED)
        self.assertEqual(events[-1].kind, 'turn.cancelled')
        if hold:
            self.assertTrue(any(event.kind == 'turn.audit_failed' for event in events))
            self.assertTrue(any(event.kind == 'turn.effect_uncertain' and event.payload['durable'] is False
                                for event in events))
            # No uncommitted in-memory cancellation may pretend the running task was saved.
            self.assertEqual(tasks.tasks[0]['status'], 'running')
            recovered = TaskManager(self.store, user_id=tasks.user_id, workspace_id=tasks.workspace_id,
                                    session_id=tasks.session_id)
            recovered.recover()
            self.assertEqual(recovered.tasks[0]['status'], 'blocked')
            self.assertEqual(recovered.tasks[0]['checkpoint']['recovery_effect']['status'], 'unknown')
        else:
            self.assertEqual(tasks.tasks[0]['status'], 'cancelled')
            entries = self.runtime.conversation_history.active_entries()
            self.assertTrue(any(entry.kind == 'turn.outcome' and entry.payload['status'] == 'cancelled'
                                for entry in entries))
            audit = self.store.list_events('turn.effect_uncertain', user_id=tasks.user_id,
                workspace_id=tasks.workspace_id, session_id=tasks.session_id)
            self.assertTrue(audit[0]['payload']['durable'])

    def test_accepted_native_cancel_remains_primary_with_real_sqlite_writer(self):
        self._accepted_native_cancel('sqlite')

    def test_accepted_native_cancel_remains_primary_with_real_database_thread_lock(self):
        self._accepted_native_cancel('thread-lock')

    def test_cancellation_deadline_leaves_normal_other_thread_and_other_database_waits_unchanged(self):
        from openkyrozen.persistence.store import EventStore
        other = EventStore(self.root / 'other.sqlite3')
        observed = []
        def observe_other_thread():
            with self.store.database.connection() as db:
                observed.append(db.execute('PRAGMA busy_timeout').fetchone()[0])
        with self.store._cancellation_deadline():
            with self.store.database.connection() as db:
                self.assertGreater(db.execute('PRAGMA busy_timeout').fetchone()[0], 0)
                self.assertLessEqual(db.execute('PRAGMA busy_timeout').fetchone()[0], 300)
            with other.database.connection() as db:
                self.assertEqual(db.execute('PRAGMA busy_timeout').fetchone()[0], 30000)
            worker = threading.Thread(target=observe_other_thread)
            worker.start()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(observed, [30000])
            # Ordinary RLock recursion remains valid inside an unexpired deadline.
            with self.store.database._lock:
                self.assertTrue(self.store.database._lock.acquire(blocking=False))
                self.store.database._lock.release()
        with self.store.database.connection() as db:
            self.assertEqual(db.execute('PRAGMA busy_timeout').fetchone()[0], 30000)

    def test_cancellation_deadline_keeps_canonical_history_compare_and_swap(self):
        from openkyrozen.agent.conversation import ConversationHistoryError
        history = self.runtime.conversation_history
        first = history.entry('user.input', {'text': 'first'}, parent_id=None)
        history.append(first, expected_head=None)
        stale = history.entry('turn.outcome', {'status': 'cancelled'}, parent_id=None)
        with self.store._cancellation_deadline():
            with self.assertRaises(ConversationHistoryError):
                history.append(stale, expected_head=None)
        self.assertEqual(history.head, first.id)

    def test_uncontended_native_cancel_preserves_task_cancellation_and_canonical_history(self):
        self._accepted_native_cancel(None)

    def test_native_cancellation_uncertainty_survives_restart_and_db_error_keeps_primary_cancel(self):
        from openkyrozen.providers.base import LLMProvider
        from openkyrozen.providers.config import ProviderConfig
        from openkyrozen.providers.models import ModelResponse, ProviderCapabilities, FinishReason
        from openkyrozen.providers.errors import ProviderError, ProviderErrorKind
        class CancelProvider(LLMProvider):
            def chat(self, messages, model=None):
                raise AssertionError('legacy provider path used')
            def get_capabilities(self, model=None):
                return ProviderCapabilities(native_tools=True)
            def chat_response(self, messages, model=None, *, tools=None):
                return ModelResponse(finish_reason=FinishReason.CANCELLED)
        project = self.root / 'native-project'
        project.mkdir()
        self.runtime.configure_launch_context(project_path=project)
        self.session = self.runtime.open_session('native-cancel-session')
        self.scope = dict(user_id=self.session.memory.user_id, workspace_id=self.session.tasks.workspace_id,
                          session_id=self.session.session_id)
        config = ProviderConfig(provider='custom', model_simple='fixture', model_complex='fixture')
        self.runtime.llm_provider = self.runtime._default_turn.provider = CancelProvider(config)
        self.runtime._provider_config = config
        self.runtime.set_interaction_mode('agent')
        with self.assertRaises(ProviderError) as error:
            self.runtime.chat(self.session, 'Hello')
        self.assertEqual(error.exception.kind, ProviderErrorKind.CANCELLED)
        uncertain = self.store.list_events('turn.effect_uncertain', **self.scope)
        self.assertEqual(len(uncertain), 1)
        self.assertEqual(uncertain[0]['payload']['reason'], 'cancelled_during_execution')
        original = self.store._append_control_audit
        def unavailable(kind, payload, **scope):
            if kind == 'turn.effect_uncertain':
                raise sqlite3.OperationalError('SECRET_ERROR')
            return original(kind, payload, **scope)
        events = []
        with patch.object(self.store, '_append_control_audit', side_effect=unavailable):
            with self.assertRaises(ProviderError) as error:
                self.runtime.chat(self.session, 'Hello again', on_runtime_event=events.append)
        self.assertEqual(error.exception.kind, ProviderErrorKind.CANCELLED)
        self.assertTrue(any(event.kind == 'turn.audit_failed' for event in events))
        self.assertTrue(any(event.kind == 'turn.effect_uncertain' and event.payload['durable'] is False
                            for event in events))
