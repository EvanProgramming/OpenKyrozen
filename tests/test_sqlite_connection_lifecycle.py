"""Application-owned WAL lifetime, using real SQLite without agent/SDK startup."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from contextvars import copy_context
from pathlib import Path
import sqlite3
import os
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openkyrozen.app.container import Application
from openkyrozen.app.bootstrap import build_memory
from openkyrozen.memory.service import MemoryBank
from openkyrozen.persistence.store import EventStore
from openkyrozen.providers import usage


class SQLiteConnectionLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='kyrozen-sqlite-lifetime-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    @staticmethod
    def _cleanup_store(store):
        close = getattr(store, 'close', None)
        if callable(close):
            close()

    def _store(self, name='state.sqlite3'):
        store = EventStore(self.root / name)
        self.addCleanup(self._cleanup_store, store)
        return store

    def _close(self, store):
        close = getattr(store, 'close', None)
        self.assertTrue(callable(close), 'EventStore.close() must own explicit SQLite shutdown')
        return close()

    @contextmanager
    def _observed_store(self):
        """Observe real handle lifetime without depending on a private anchor name."""
        opened = []
        real_connect = sqlite3.connect

        class ObservedConnection(sqlite3.Connection):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.close_started = self.closed = False
                opened.append(self)

            def close(self):
                self.close_started = True
                super().close()
                self.closed = True

        def connect(*args, **kwargs):
            kwargs.setdefault('factory', ObservedConnection)
            return real_connect(*args, **kwargs)

        with patch.object(sqlite3, 'connect', side_effect=connect):
            yield self._store(), opened

    def _application(self, store):
        shutdown_started = threading.Event()

        class Executor(ThreadPoolExecutor):
            def shutdown(self, *args, **kwargs):
                shutdown_started.set()
                return super().shutdown(*args, **kwargs)

        executor = Executor(max_workers=1)
        self.addCleanup(executor.shutdown, wait=True, cancel_futures=True)
        memory = MemoryBank(store=store)
        session = SimpleNamespace(subagents=SimpleNamespace(coordinator=None),
                                  process_manager=SimpleNamespace(close=lambda: None))
        runtime = SimpleNamespace(subagent_manager=SimpleNamespace(coordinator=None),
            _default_session=session, _sessions={}, _technology_executor=executor,
            _workspaces={}, memory_bank=memory)
        return Application(runtime, None, store), executor, shutdown_started

    def test_ordinary_leases_keep_wal_materialized_while_owner_is_alive(self):
        store = self._store()
        wal = Path(str(store.path) + '-wal')
        for index in range(3):
            store.append_event('lifetime.probe', {'index': index})
            self.assertTrue(wal.is_file(), 'An ordinary lease performed last-client WAL cleanup')
            self.assertGreater(wal.stat().st_size, 32)
            with store.connection() as connection:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM events').fetchone()[0], index + 1)
            self.assertTrue(wal.is_file(), 'A finished read must not tear down the owned WAL client')

    def test_idle_owner_holds_neither_writer_lock_nor_read_transaction(self):
        store = self._store()
        store.append_event('lifetime.probe', {})
        with closing(sqlite3.connect(store.path, timeout=.05, isolation_level=None)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("INSERT INTO events(id,event_type,payload,created_at) VALUES('external','probe','{}','now')")
            writer.execute('COMMIT')
            checkpoint = writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            self.assertEqual(checkpoint[0], 0, 'An idle owner retained a read transaction')
        with store.connection() as connection:
            self.assertFalse(connection.in_transaction)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events WHERE id='external'").fetchone()[0], 1)
        self.assertTrue(Path(str(store.path) + '-wal').exists(), 'The idle owner must remain materialized')

    def test_explicit_owner_close_checkpoints_idempotently_and_rejects_new_leases(self):
        with self._observed_store() as (store, opened):
            store.append_event('lifetime.probe', {'committed': True})
            # A failed open must release its reserved lease before owner shutdown.
            with patch.object(sqlite3, 'connect', side_effect=sqlite3.OperationalError('unavailable database')):
                with self.assertRaisesRegex(sqlite3.OperationalError, 'unavailable database'):
                    with store.connection():
                        self.fail('Unavailable SQLite unexpectedly admitted a lease')
            self._close(store)
            self.assertTrue(all(connection.closed for connection in opened))
            self.assertFalse(Path(str(store.path) + '-wal').exists(), 'Final writable owner close must checkpoint WAL')
            count = len(opened)
            self._close(store)
            with self.assertRaisesRegex(RuntimeError, 'clos'):
                with store.connection():
                    self.fail('A closed store admitted a new connection lease')
            self.assertEqual(len(opened), count, 'Closed storage reopened SQLite before rejecting the lease')
            with closing(sqlite3.connect(store.path)) as persisted:
                self.assertEqual(persisted.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)

    def test_owner_close_defers_handle_close_until_existing_writer_lease_drains(self):
        with self._observed_store() as (store, opened):
            self.assertTrue(callable(getattr(store, 'close', None)), 'EventStore.close() is required')
            idle = [connection for connection in opened if not connection.closed]
            self.assertTrue(idle, 'The application must own a materialized idle SQLite client')
            entered, release, closing_started, rejected = (threading.Event() for _ in range(4))
            errors, leases = [], []

            def writer():
                try:
                    with store.connection() as connection:
                        leases.append(connection)
                        connection.execute('BEGIN IMMEDIATE')
                        connection.execute("INSERT INTO events(id,event_type,payload,created_at) VALUES('leased','probe','{}','now')")
                        entered.set()
                        if not release.wait(5):
                            raise AssertionError('Test did not release the writer lease')
                        connection.execute('COMMIT')
                except BaseException as error:
                    errors.append(error)

            def close_owner():
                closing_started.set()
                try:
                    store.close()
                except BaseException as error:
                    errors.append(error)

            def late_lease():
                try:
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        try:
                            with store.connection() as connection:
                                connection.execute('SELECT 1')
                        except RuntimeError as error:
                            if 'clos' not in str(error).lower():
                                raise
                            rejected.set()
                            return
                        time.sleep(.005)
                    raise AssertionError('Closing storage kept admitting new leases')
                except BaseException as error:
                    errors.append(error)

            worker = threading.Thread(target=writer, daemon=True)
            closer = threading.Thread(target=close_owner, daemon=True)
            late = threading.Thread(target=late_lease, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                closer.start()
                self.assertTrue(closing_started.wait(3))
                late.start()
                self.assertTrue(rejected.wait(3), errors)
                self.assertTrue(all(not connection.close_started for connection in idle),
                                'Owner close began before an existing lease drained')
                self.assertFalse(leases[0].close_started)
            finally:
                release.set()
                for thread in (worker, closer, late):
                    if thread.ident is not None:
                        thread.join(3)
                        self.assertFalse(thread.is_alive(), 'SQLite shutdown left an owned thread alive')
            self.assertEqual(errors, [])
            self.assertTrue(all(connection.closed for connection in idle + leases))
            self.assertFalse(Path(str(store.path) + '-wal').exists())
            self._close(store)
            with closing(sqlite3.connect(store.path)) as persisted:
                self.assertEqual(persisted.execute("SELECT COUNT(*) FROM events WHERE id='leased'").fetchone()[0], 1)

    def test_closed_store_rejects_late_background_writes_without_reopening(self):
        with self._observed_store() as (store, opened):
            self._close(store)
            count = len(opened)
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(store.append_event, 'late.background', {})
                with self.assertRaisesRegex(RuntimeError, 'clos'):
                    future.result(timeout=3)
            self.assertEqual(len(opened), count)
            self.assertFalse(Path(str(store.path) + '-wal').exists())

    def test_unscoped_usage_helpers_close_temporary_stores(self):
        operations = (
            lambda: usage._track_cost('ollama', {'prompt_tokens': 1}),
            usage.get_cost_report,
            usage.reset_cost_tracker,
        )
        for operation in operations:
            with self.subTest(operation=operation), self._observed_store() as (store, opened), closing(store):
                with patch.object(usage, 'EventStore', return_value=store):
                    operation()
                self.assertTrue(all(connection.closed for connection in opened),
                                'A temporary usage-store owner retained its SQLite anchor')
                self.assertFalse(Path(str(store.path) + '-wal').exists())
                with self.assertRaisesRegex(RuntimeError, 'clos'):
                    store.append_event('after.usage.close', {})

    def test_unscoped_usage_helpers_close_temporary_stores_when_operation_raises(self):
        operations = (
            (lambda: usage._track_cost('ollama', {'prompt_tokens': 1}), 'record_usage_attempt'),
            (usage.get_cost_report, 'usage_totals'),
            (usage.reset_cost_tracker, 'create_usage_reset'),
        )
        for operation, method in operations:
            with self.subTest(method=method), self._observed_store() as (store, opened), closing(store):
                with patch.object(usage, 'EventStore', return_value=store), \
                     patch.object(store, method, side_effect=RuntimeError('usage failure')):
                    with self.assertRaisesRegex(RuntimeError, 'usage failure'):
                        operation()
                self.assertTrue(all(connection.closed for connection in opened),
                                'A failed usage operation leaked its temporary store')
                with self.assertRaisesRegex(RuntimeError, 'clos'):
                    store.append_event('after.usage.failure', {})

    def test_usage_helpers_leave_borrowed_store_open_on_success_and_error(self):
        store = self._store()
        operations = (
            (lambda: usage._track_cost('ollama', {'prompt_tokens': 1}), 'record_usage_attempt'),
            (lambda: usage.get_cost_report(store=store), 'usage_totals'),
            (lambda: usage.reset_cost_tracker(store=store), 'create_usage_reset'),
        )
        with usage.usage_scope(store=store):
            for operation, method in operations:
                with self.subTest(method=method):
                    operation()
                    store.append_event('borrowed.usage.success', {})
                    with patch.object(store, method, side_effect=RuntimeError('usage failure')):
                        with self.assertRaisesRegex(RuntimeError, 'usage failure'):
                            operation()
                    store.append_event('borrowed.usage.failure', {})
        self.assertEqual(sum(event['event_type'].startswith('borrowed.usage.')
                             for event in store.list_events()), 6)

    def test_usage_pricing_error_does_not_create_a_fallback_store(self):
        with patch.object(usage, '_legacy_pricing_snapshot', side_effect=ValueError('pricing failure')), \
             patch.object(usage, 'EventStore') as constructor:
            with self.assertRaisesRegex(ValueError, 'pricing failure'):
                usage._track_cost('ollama', {'prompt_tokens': 1})
        constructor.assert_not_called()

    def test_scoped_memory_close_leaves_shared_store_usable(self):
        with patch.dict(os.environ, {'KYROZEN_DISABLE_VECTOR_INDEX': '1'}):
            memory = build_memory(self.root / 'memory.sqlite3')
        store = memory.store
        self.addCleanup(self._cleanup_store, store)
        first = memory.scoped(session_id='first')
        second = memory.scoped(session_id='second')
        first.close()
        store.append_event('lifetime.first_closed', {})
        second.close()
        store.append_event('lifetime.second_closed', {})
        self.assertIs(first.store, second.store)
        self.assertEqual(len(store.list_events()), 2)
        memory.close()
        with self.assertRaisesRegex(RuntimeError, 'clos'):
            store.append_event('after.owner.close', {})

    def test_standalone_memory_close_releases_its_created_database(self):
        with patch.dict(os.environ, {'KYROZEN_DISABLE_VECTOR_INDEX': '1'}):
            memory = build_memory(self.root / 'memory.sqlite3')
        self.addCleanup(self._cleanup_store, memory.store)
        memory.store.append_event('owned.memory', {})
        memory.close()
        memory.close()
        with self.assertRaisesRegex(RuntimeError, 'clos'):
            memory.store.append_event('after.owner.close', {})
        self.assertFalse(Path(str(memory.store.path) + '-wal').exists())

    def test_memory_with_injected_store_borrows_database_ownership(self):
        store = self._store()
        with patch.dict(os.environ, {'KYROZEN_DISABLE_VECTOR_INDEX': '1'}):
            memory = build_memory(store=store)
        memory.close()
        store.append_event('borrowed.store', {})
        self.assertEqual(len(store.list_events()), 1)

    def test_failed_memory_construction_closes_only_a_created_store(self):
        for owned in (True, False):
            with self.subTest(owned=owned), self._observed_store() as (store, opened), closing(store):
                with patch.dict(os.environ, {'KYROZEN_DISABLE_VECTOR_INDEX': '1'}), \
                     patch('openkyrozen.persistence.store.EventStore', return_value=store), \
                     patch('openkyrozen.memory.service.MemoryBank', side_effect=ValueError('memory initialization failed')):
                    with self.assertRaisesRegex(ValueError, 'memory initialization failed'):
                        build_memory(self.root / 'memory.sqlite3', store=None if owned else store)
                if owned:
                    self.assertTrue(all(connection.closed for connection in opened),
                                    'Failed memory construction retained its new SQLite owner')
                    with self.assertRaisesRegex(RuntimeError, 'clos'):
                        store.append_event('after.failed.startup', {})
                else:
                    store.append_event('borrowed.after.failed.startup', {})

    def test_one_shot_v1_migration_closes_owned_memory_on_success_and_error(self):
        from openkyrozen.persistence import migration
        for failing in (False, True):
            with self.subTest(failing=failing), patch.dict(os.environ, {'KYROZEN_DISABLE_VECTOR_INDEX': '1'}):
                source = self.root / f'legacy-{failing}'
                source.mkdir()
                memory = build_memory(self.root / f'migrated-{failing}.sqlite3')
                self.addCleanup(memory.close)

                class Client:
                    def get_collection(self, name):
                        if failing:
                            def fail(**kwargs):
                                raise ValueError('legacy collection failed')
                            return SimpleNamespace(get=fail)
                        raise KeyError(name)

                modules = {
                    'chromadb': SimpleNamespace(PersistentClient=lambda **kwargs: Client()),
                    'chromadb.config': SimpleNamespace(Settings=lambda **kwargs: None),
                }
                with patch.dict(sys.modules, modules), patch.object(migration, 'MemoryBank', return_value=memory):
                    if failing:
                        with self.assertRaisesRegex(ValueError, 'legacy collection failed'):
                            migration.migrate_v1_chroma(source, memory.db_path)
                    else:
                        report = migration.migrate_v1_chroma(source, memory.db_path)
                        self.assertEqual(report['imported_logs'], 0)
                        self.assertEqual(report['imported_files'], 0)
                with self.assertRaisesRegex(RuntimeError, 'clos'):
                    memory.store.append_event('after.migration', {})
                self.assertFalse(Path(str(memory.db_path) + '-wal').exists())

    def test_scoped_memory_close_does_not_close_shared_index(self):
        calls = []
        index = SimpleNamespace(memories=None, files=None, close=lambda: calls.append('closed'))
        memory = MemoryBank(store=self._store(), index=index)
        memory.scoped(session_id='borrowed').close()
        self.assertEqual(calls, [])
        memory.close()
        memory.close()
        self.assertEqual(calls, ['closed'])

    def test_application_is_the_explicit_store_shutdown_owner(self):
        store = self._store()
        app, _, _ = self._application(store)
        store.append_event('lifetime.probe', {})
        app.close()
        with self.assertRaisesRegex(RuntimeError, 'clos'):
            store.append_event('after.application.close', {})
        self.assertFalse(Path(str(store.path) + '-wal').exists())

    def test_application_close_uses_default_memory_owner_when_active_view_is_borrowed(self):
        store = self._store()
        calls = []
        index = SimpleNamespace(memories=None, files=None, close=lambda: calls.append('closed'))
        owner = MemoryBank(store=store, index=index)
        app, _, _ = self._application(store)
        app.runtime._default_session.memory = owner
        app.runtime.memory_bank = owner.scoped(session_id='borrowed')
        app.close()
        self.assertEqual(calls, ['closed'])

    def test_application_shutdown_preserves_an_existing_background_writer(self):
        with self._observed_store() as (store, opened):
            app, executor, shutdown_started = self._application(store)
            idle = [connection for connection in opened if not connection.closed]
            entered, release = threading.Event(), threading.Event()
            errors, leases = [], []

            def write():
                with store.connection() as connection:
                    leases.append(connection)
                    connection.execute('BEGIN IMMEDIATE')
                    connection.execute("INSERT INTO events(id,event_type,payload,created_at) VALUES('background','probe','{}','now')")
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('Test did not release the background writer')
                    connection.execute('COMMIT')

            def close():
                try:
                    app.close()
                except BaseException as error:
                    errors.append(error)

            future = executor.submit(write)
            closer = threading.Thread(target=close, daemon=True)
            try:
                self.assertTrue(entered.wait(3))
                closer.start()
                self.assertTrue(shutdown_started.wait(3))
                self.assertFalse(leases[0].close_started)
                self.assertTrue(all(not connection.close_started for connection in idle))
            finally:
                release.set()
                if closer.ident is not None:
                    closer.join(3)
                    self.assertFalse(closer.is_alive())
            future.result(timeout=3)
            self.assertEqual(errors, [])
            with self.assertRaisesRegex(RuntimeError, 'clos'):
                store.append_event('late.background', {})
            self.assertFalse(Path(str(store.path) + '-wal').exists())
            with closing(sqlite3.connect(store.path)) as persisted:
                self.assertEqual(persisted.execute("SELECT COUNT(*) FROM events WHERE id='background'").fetchone()[0], 1)

    def test_lease_lifetime_preserves_per_database_and_per_thread_cancellation_budget(self):
        store, other = self._store(), self._store('other.sqlite3')
        observed = []
        with store._cancellation_deadline():
            with store.connection() as connection:
                budget = connection.execute('PRAGMA busy_timeout').fetchone()[0]
                self.assertGreater(budget, 0)
                self.assertLessEqual(budget, 300)
            with other.connection() as connection:
                self.assertEqual(connection.execute('PRAGMA busy_timeout').fetchone()[0], 30000)
            context = copy_context()

        def read_budget():
            with store.connection() as connection:
                observed.append(connection.execute('PRAGMA busy_timeout').fetchone()[0])

        thread = threading.Thread(target=context.run, args=(read_budget,), daemon=True)
        thread.start()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(observed, [30000])
        with store.connection() as connection:
            self.assertEqual(connection.execute('PRAGMA busy_timeout').fetchone()[0], 30000)

    def test_exception_in_lease_rolls_back_without_retaining_a_writer_transaction(self):
        store = self._store()
        with self.assertRaisesRegex(RuntimeError, 'rollback probe'):
            with store.connection() as connection:
                connection.execute('BEGIN IMMEDIATE')
                connection.execute("INSERT INTO events(id,event_type,payload,created_at) VALUES('rolled-back','probe','{}','now')")
                raise RuntimeError('rollback probe')
        with store.connection() as connection:
            self.assertFalse(connection.in_transaction)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events WHERE id='rolled-back'").fetchone()[0], 0)
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('ROLLBACK')
