from __future__ import annotations

import sqlite3
import stat
import tempfile
import threading
import unittest
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openkyrozen.persistence.database import SQLiteDatabase
from openkyrozen.persistence.store import EventStore
from openkyrozen.persistence.tasks import TasksRepository
from openkyrozen.security.approvals import ApprovalRequest
from openkyrozen.security.permissions import _authorize_tool_action, _confirm_tool_action, _record_permission_decision


# Version layouts mirror event_store.py at 91652a0, 4873ff1, and 3b4e3bd.
_VERSION_1 = """
CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE events(id TEXT PRIMARY KEY, event_type TEXT NOT NULL, payload TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT 'local', workspace_id TEXT NOT NULL DEFAULT 'default',
    session_id TEXT, task_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE memories(id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active', confidence REAL NOT NULL DEFAULT 0.5,
    user_id TEXT NOT NULL DEFAULT 'local', workspace_id TEXT NOT NULL DEFAULT 'default',
    session_id TEXT, content_hash TEXT NOT NULL, source_event_ids TEXT NOT NULL DEFAULT '[]',
    metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    valid_until TEXT);
CREATE TABLE files(id TEXT PRIMARY KEY, rel_path TEXT NOT NULL, content TEXT NOT NULL,
    content_hash TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT 'local',
    workspace_id TEXT NOT NULL DEFAULT 'default', updated_at TEXT NOT NULL);
CREATE TABLE tasks(id TEXT PRIMARY KEY, parent_id TEXT, description TEXT NOT NULL,
    status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
    dependencies TEXT NOT NULL DEFAULT '[]', acceptance TEXT NOT NULL DEFAULT '[]',
    attempts INTEGER NOT NULL DEFAULT 0, checkpoint TEXT NOT NULL DEFAULT '{}',
    evidence TEXT NOT NULL DEFAULT '[]', user_id TEXT NOT NULL DEFAULT 'local',
    workspace_id TEXT NOT NULL DEFAULT 'default', session_id TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE task_events(id TEXT PRIMARY KEY, task_id TEXT NOT NULL, event_type TEXT NOT NULL,
    payload TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE);
CREATE TABLE learning_proposals(id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate', confidence REAL NOT NULL DEFAULT 0.0,
    evidence TEXT NOT NULL DEFAULT '[]', validation TEXT NOT NULL DEFAULT '{}',
    user_id TEXT NOT NULL DEFAULT 'local', workspace_id TEXT NOT NULL DEFAULT 'default',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO schema_migrations VALUES (1, 'historical-v1');
"""

_VERSION_2 = """
CREATE TABLE scheduled_jobs(id TEXT PRIMARY KEY, name TEXT NOT NULL, interval_seconds REAL,
    run_at TEXT, next_run_at TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
    payload TEXT NOT NULL DEFAULT '{}', last_run_at TEXT, user_id TEXT NOT NULL DEFAULT 'local',
    workspace_id TEXT NOT NULL DEFAULT 'default', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE skills(id TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL,
    path TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', permissions TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'candidate', source TEXT NOT NULL DEFAULT 'local',
    manifest TEXT NOT NULL DEFAULT '{}', workspace_id TEXT NOT NULL DEFAULT 'default',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(name, version, workspace_id));
CREATE TABLE usage_attempts(id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,
    surface TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT 'local',
    workspace_id TEXT NOT NULL DEFAULT 'default', session_id TEXT, run_id TEXT,
    cache_status TEXT NOT NULL DEFAULT 'unknown', prompt_tokens INTEGER, cache_hit_tokens INTEGER,
    cache_miss_tokens INTEGER, completion_tokens INTEGER, reasoning_tokens INTEGER,
    latency_ms INTEGER, completion_state TEXT NOT NULL, usage_status TEXT NOT NULL,
    cost_picos INTEGER, pricing_snapshot TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
CREATE TABLE usage_resets(id TEXT PRIMARY KEY, scope TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT 'local', workspace_id TEXT NOT NULL DEFAULT 'default',
    session_id TEXT, created_at TEXT NOT NULL);
INSERT INTO schema_migrations VALUES (2, 'historical-v2');
"""

_VERSION_3 = """
CREATE TABLE learning_feature_flags(name TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT 'local',
    workspace_id TEXT NOT NULL DEFAULT 'default', enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL, PRIMARY KEY(name, user_id, workspace_id));
CREATE TABLE learning_policies(user_id TEXT NOT NULL DEFAULT 'local',
    workspace_id TEXT NOT NULL DEFAULT 'default', mode TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, workspace_id));
CREATE TABLE learning_runtime(user_id TEXT NOT NULL DEFAULT 'local',
    workspace_id TEXT NOT NULL DEFAULT 'default', mode TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, workspace_id));
CREATE TABLE history_nodes(id TEXT PRIMARY KEY, parent_id TEXT,
    kind TEXT NOT NULL DEFAULT 'turn', summary TEXT NOT NULL DEFAULT '',
    user_message TEXT NOT NULL DEFAULT '', assistant_message TEXT NOT NULL DEFAULT '',
    conversation TEXT NOT NULL DEFAULT '[]', interaction TEXT NOT NULL DEFAULT '{}',
    tasks TEXT NOT NULL DEFAULT '[]', snapshot_relpath TEXT NOT NULL,
    file_summary TEXT NOT NULL DEFAULT '{}', user_id TEXT NOT NULL DEFAULT 'local',
    workspace_id TEXT NOT NULL DEFAULT 'default', session_id TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE history_heads(user_id TEXT NOT NULL DEFAULT 'local',
    workspace_id TEXT NOT NULL DEFAULT 'default', session_id TEXT NOT NULL, head_id TEXT NOT NULL,
    updated_at TEXT NOT NULL, PRIMARY KEY(user_id, workspace_id, session_id));
INSERT INTO schema_migrations VALUES (3, 'historical-v3');
"""


@contextmanager
def _sqlite(path):
    db = sqlite3.connect(path)
    try:
        yield db
        db.commit()
    finally:
        db.close()


class SQLiteMigrationTests(unittest.TestCase):
    def _historical_database(self, path: Path, version: int, *, malformed: bool = False) -> None:
        db = sqlite3.connect(path)
        try:
            db.executescript(_VERSION_1)
            if not malformed:
                db.execute(
                    "INSERT INTO tasks(id, description, status, user_id, workspace_id, created_at, updated_at) "
                    "VALUES('legacy-task', 'preserve me', 'pending', 'alice', 'project', 'created', 'updated')"
                )
                db.execute("CREATE TABLE extension_data(value TEXT NOT NULL)")
                db.execute("INSERT INTO extension_data VALUES ('keep')")
            if version >= 2:
                db.executescript(_VERSION_2)
                db.execute(
                    "INSERT INTO usage_attempts(id, provider, model, surface, completion_state, usage_status, created_at) "
                    "VALUES('usage-1', 'provider', 'model', 'cli', 'completed', 'reported', 'created')"
                )
            if version >= 3:
                db.executescript(_VERSION_3)
                db.execute(
                    "INSERT INTO history_nodes(id, snapshot_relpath, user_id, workspace_id, session_id, created_at) "
                    "VALUES('history-1', 'snapshots/one', 'alice', 'project', 'session', 'created')"
                )
                db.execute(
                    "INSERT INTO history_heads(user_id, workspace_id, session_id, head_id, updated_at) "
                    "VALUES('alice', 'project', 'session', 'history-1', 'updated')"
                )
            if malformed:
                db.execute("DROP TABLE tasks")
            db.commit()
        finally:
            db.close()

    def test_historical_versions_upgrade_in_order_and_preserve_user_data(self):
        for version in (1, 2, 3):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.sqlite3"
                self._historical_database(path, version)

                SQLiteDatabase(path)

                with _sqlite(path) as db:
                    self.assertEqual(
                        [row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")],
                        [1, 2, 3],
                    )
                    self.assertEqual(
                        db.execute("SELECT description FROM tasks WHERE id='legacy-task'").fetchone()[0],
                        "preserve me",
                    )
                    self.assertEqual(db.execute("SELECT value FROM extension_data").fetchone()[0], "keep")
                    if version >= 2:
                        self.assertEqual(db.execute("SELECT id FROM usage_attempts").fetchone()[0], "usage-1")
                    if version >= 3:
                        self.assertEqual(db.execute("SELECT head_id FROM history_heads").fetchone()[0], "history-1")
                second_open = SQLiteDatabase(path)
                self.assertIsNone(second_open.last_backup_path)
                self.assertEqual(len(list(Path(directory).glob("*.backup.*"))), 1 if version < 3 else 0)

    def test_fresh_database_records_each_ordered_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fresh.sqlite3"

            SQLiteDatabase(path)

            with _sqlite(path) as db:
                self.assertEqual(
                    [row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")],
                    [1, 2, 3],
                )

    def test_compacted_prior_version_history_remains_valid_after_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compacted-history.sqlite3"
            self._historical_database(path, 2)
            with _sqlite(path) as db:
                db.execute("DELETE FROM schema_migrations WHERE version < 2")

            SQLiteDatabase(path)
            SQLiteDatabase(path)

            with _sqlite(path) as db:
                self.assertEqual(
                    [row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")],
                    [2, 3],
                )

    def test_sparse_legacy_history_is_valid_when_declared_schema_is_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sparse-history.sqlite3"
            self._historical_database(path, 3)
            with _sqlite(path) as db:
                db.execute("DELETE FROM schema_migrations WHERE version = 2")

            SQLiteDatabase(path)

            with _sqlite(path) as db:
                self.assertEqual(
                    [row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")],
                    [1, 3],
                )

    def test_missing_task_columns_are_rejected_before_startup_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing-task-columns.sqlite3"
            self._historical_database(path, 1)
            with _sqlite(path) as db:
                db.execute("ALTER TABLE tasks DROP COLUMN priority")
            before = path.read_bytes()

            with self.assertRaisesRegex(RuntimeError, "tasks is missing required columns"):
                SQLiteDatabase(path)

            self.assertEqual(path.read_bytes(), before)

    def test_future_version_is_rejected_without_modifying_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "future.sqlite3"
            with _sqlite(path) as db:
                db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
                db.execute("INSERT INTO schema_migrations VALUES (99, 'future')")
                db.execute("CREATE TABLE extension_data(value TEXT NOT NULL)")
                db.execute("INSERT INTO extension_data VALUES ('keep')")
            before = path.read_bytes()

            with self.assertRaisesRegex(RuntimeError, "newer schema"):
                SQLiteDatabase(path)

            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(list(Path(directory).glob("*.backup.*")))

    def test_nonpositive_or_gapped_migration_history_is_rejected(self):
        for versions in ((0,), (1, 3)):
            with self.subTest(versions=versions), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "invalid-history.sqlite3"
                with _sqlite(path) as db:
                    db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
                    db.executemany("INSERT INTO schema_migrations VALUES (?, 'historical')", [(v,) for v in versions])
                before = path.read_bytes()

                with self.assertRaisesRegex(RuntimeError, "inconsistent|migration"):
                    SQLiteDatabase(path)

                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(list(Path(directory).glob("*.backup.*")))

    def test_inconsistent_core_schema_is_rejected_without_repairing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inconsistent.sqlite3"
            self._historical_database(path, 1, malformed=True)
            before = path.read_bytes()

            with self.assertRaisesRegex(RuntimeError, "inconsistent"):
                SQLiteDatabase(path)

            self.assertEqual(path.read_bytes(), before)

    def test_upgrade_backup_is_private_and_contains_committed_wal_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            self._historical_database(path, 1)
            wal_writer = sqlite3.connect(path)
            wal_writer.execute("PRAGMA journal_mode=WAL")
            wal_writer.execute("UPDATE tasks SET description='committed WAL state' WHERE id='legacy-task'")
            wal_writer.commit()

            SQLiteDatabase(path)

            backups = list(Path(directory).glob("legacy.sqlite3.backup.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
            with _sqlite(backups[0]) as backup:
                self.assertEqual(backup.execute("SELECT description FROM tasks WHERE id='legacy-task'").fetchone()[0], "committed WAL state")
                self.assertEqual(backup.execute("SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1").fetchone()[0], 1)
            wal_writer.close()

    def test_failed_migration_rolls_back_schema_and_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollback.sqlite3"
            self._historical_database(path, 1)
            with _sqlite(path) as db:
                db.execute("CREATE VIEW usage_attempts AS SELECT 1 AS id")

            with self.assertRaises(sqlite3.OperationalError):
                SQLiteDatabase(path)

            with _sqlite(path) as db:
                self.assertEqual(db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT description FROM tasks WHERE id='legacy-task'").fetchone()[0], "preserve me")
                self.assertEqual(db.execute("SELECT type FROM sqlite_master WHERE name='usage_attempts'").fetchone()[0], "view")
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='learning_runtime'").fetchone())

    def test_same_version_additive_schema_variant_is_completed_with_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "additive.sqlite3"
            self._historical_database(path, 3)
            with _sqlite(path) as db:
                db.execute("DROP TABLE learning_runtime")

            database = SQLiteDatabase(path)

            with _sqlite(path) as db:
                self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='learning_runtime'").fetchone())
                self.assertEqual(db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 3)
            self.assertTrue(database.last_backup_path and database.last_backup_path.exists())

    def test_concurrent_initializers_apply_legacy_migrations_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "concurrent.sqlite3"
            self._historical_database(path, 1)
            barrier = threading.Barrier(2)
            errors = []

            def initialize():
                try:
                    barrier.wait()
                    SQLiteDatabase(path)
                except Exception as exc:
                    errors.append(exc)

            workers = [threading.Thread(target=initialize) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)

            self.assertFalse(any(worker.is_alive() for worker in workers))
            self.assertEqual(errors, [])
            self.assertEqual(len(list(Path(directory).glob("*.backup.*"))), 1)
            with _sqlite(path) as db:
                self.assertEqual(
                    [row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")],
                    [1, 2, 3],
                )

    def test_task_replacement_rolls_back_if_any_new_row_is_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(Path(directory) / "tasks.sqlite3")
            repository = TasksRepository(database)
            previous = {"id": "old", "description": "keep", "status": "pending"}
            repository.replace_tasks([previous], workspace_id="project")

            with self.assertRaises(KeyError):
                repository.replace_tasks(
                    [{"id": "new", "description": "partial", "status": "pending"}, {"description": "invalid"}],
                    workspace_id="project",
                )

            rows = repository.list_tasks(workspace_id="project")
            self.assertEqual([row["id"] for row in rows], ["old"])

    def test_approval_decisions_are_scoped_versioned_history_not_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            request = ApprovalRequest(
                call_id="call-1", tool_name="write_file",
                arguments={"path": "notes.txt", "api_key": "secret-value"},
                workspace_id="project", session_id="session-1", tool_version="1",
                tool_source="builtin", capability="write", risk="normal",
                side_effects="mutation",
            )

            class Runtime:
                memory_bank = SimpleNamespace(
                    store=store, user_id="alice", workspace_id="other", session_id="other-session",
                )
                _approval_request = ContextVar("approval_request", default=request)

                @staticmethod
                def permission_mode():
                    return "ask"

                @staticmethod
                def interaction_workspace_id():
                    return "other"

            _record_permission_decision(Runtime(), "write_file", "workspace_change", "user_approved")

            events = store.list_events(
                "permission.decision", workspace_id="project", session_id="session-1", user_id="alice",
            )
            self.assertEqual(len(events), 1)
            payload = events[0]["payload"]
            self.assertEqual(payload["payload_version"], 1)
            self.assertEqual(payload["request_id"], request.request_id)
            self.assertEqual(payload["call_id"], "call-1")
            self.assertEqual(payload["approval_fingerprint"], request.fingerprint)
            self.assertEqual(payload["argument_keys"], ["api_key", "path"])
            self.assertNotIn("secret-value", str(payload))
            self.assertEqual(payload["outcome"], "user_approved")
            self.assertEqual(payload["mode"], "ask")

    def test_cli_approval_is_persisted_to_the_same_scoped_event_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            request = ApprovalRequest(
                call_id="call-cli", tool_name="git_push", arguments={"remote": "origin"},
                workspace_id="project", session_id="session-cli", tool_version="1",
                tool_source="builtin", capability="git", risk="high", side_effects="external",
            )

            class Runtime:
                _EXECUTION_SURFACE = "cli"
                AVAILABLE_TOOLS = {}
                memory_bank = SimpleNamespace(store=store, user_id="alice", session_id="other")
                _approval_request = ContextVar("cli_approval_request", default=request)
                _approval_callback = ContextVar("cli_approval_callback", default=lambda action, args: True)

                @staticmethod
                def _record_permission_decision(action, category, outcome):
                    _record_permission_decision(Runtime(), action, category, outcome)

                @staticmethod
                def _record_tool_approval(action, decision, args):
                    pass

                @staticmethod
                def permission_mode():
                    return "ask"

                @staticmethod
                def interaction_workspace_id():
                    return "other"

            with patch("openkyrozen.security.tool_policy.tool_confirmation_required", return_value=True):
                self.assertTrue(_confirm_tool_action(Runtime(), "git_push", "origin"))

            events = store.list_events(
                "permission.decision", workspace_id="project", session_id="session-cli", user_id="alice",
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["payload"]["call_id"], "call-cli")

    def test_fallback_tui_approval_records_one_permission_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")

            class Runtime:
                _EXECUTION_SURFACE = "tui"
                AVAILABLE_TOOLS = {}
                memory_bank = SimpleNamespace(
                    store=store, user_id="alice", workspace_id="project", session_id="session",
                )
                _approval_request = ContextVar("fallback_approval_request", default=None)
                _approval_callback = ContextVar("fallback_approval_callback", default=None)

                @staticmethod
                def permission_mode():
                    return "ask"

                @staticmethod
                def interaction_workspace_id():
                    return "project"

                @staticmethod
                def _record_permission_decision(action, category, outcome):
                    _record_permission_decision(Runtime(), action, category, outcome)

                @staticmethod
                def _record_tool_approval(action, decision, args):
                    pass

                @staticmethod
                def _confirm_tool_action(action, args, *, force=False):
                    return _confirm_tool_action(Runtime(), action, args, force=force)

                @staticmethod
                def _emit_stream_event(event):
                    pass

            with patch("openkyrozen.security.tool_policy.tool_confirmation_required", return_value=True), \
                    patch("openkyrozen.security.permission_gate.requires_ask_approval", return_value=True), \
                    patch("openkyrozen.security.permissions.sys.stdin", SimpleNamespace(isatty=lambda: False)):
                self.assertFalse(_authorize_tool_action(Runtime(), "write_file", {"path": "notes.txt"}))

            events = store.list_events(
                "permission.decision", workspace_id="project", session_id="session", user_id="alice",
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["payload"]["outcome"], "denied_noninteractive")


if __name__ == "__main__":
    unittest.main()
