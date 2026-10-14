from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import utc_now, stable_hash


_MIGRATIONS = (
    (1, """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, event_type TEXT NOT NULL, payload TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT 'local', workspace_id TEXT NOT NULL DEFAULT 'default',
            session_id TEXT, task_id TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_scope
            ON events(user_id, workspace_id, session_id, created_at);
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', confidence REAL NOT NULL DEFAULT 0.5,
            user_id TEXT NOT NULL DEFAULT 'local', workspace_id TEXT NOT NULL DEFAULT 'default',
            session_id TEXT, content_hash TEXT NOT NULL, source_event_ids TEXT NOT NULL DEFAULT '[]',
            metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, valid_until TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_memories_dedupe
            ON memories(kind, user_id, workspace_id, session_id, content_hash);
        CREATE INDEX IF NOT EXISTS idx_memories_lookup
            ON memories(kind, status, user_id, workspace_id, updated_at);
        CREATE TABLE IF NOT EXISTS files (
            id TEXT PRIMARY KEY, rel_path TEXT NOT NULL, content TEXT NOT NULL,
            content_hash TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT 'local',
            workspace_id TEXT NOT NULL DEFAULT 'default', updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_files_dedupe
            ON files(rel_path, user_id, workspace_id, content_hash);
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, parent_id TEXT, description TEXT NOT NULL, status TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0, dependencies TEXT NOT NULL DEFAULT '[]',
            acceptance TEXT NOT NULL DEFAULT '[]', attempts INTEGER NOT NULL DEFAULT 0,
            checkpoint TEXT NOT NULL DEFAULT '{}', evidence TEXT NOT NULL DEFAULT '[]',
            user_id TEXT NOT NULL DEFAULT 'local', workspace_id TEXT NOT NULL DEFAULT 'default',
            session_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_scope_status
            ON tasks(user_id, workspace_id, session_id, status, priority);
        CREATE TABLE IF NOT EXISTS task_events (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, event_type TEXT NOT NULL,
            payload TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS learning_proposals (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'candidate', confidence REAL NOT NULL DEFAULT 0.0,
            evidence TEXT NOT NULL DEFAULT '[]', validation TEXT NOT NULL DEFAULT '{}',
            user_id TEXT NOT NULL DEFAULT 'local', workspace_id TEXT NOT NULL DEFAULT 'default',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_learning_status
            ON learning_proposals(status, workspace_id, updated_at);
    """),
    (2, """
        CREATE TABLE IF NOT EXISTS scheduled_jobs (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, interval_seconds REAL, run_at TEXT,
            next_run_at TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            payload TEXT NOT NULL DEFAULT '{}', last_run_at TEXT,
            user_id TEXT NOT NULL DEFAULT 'local', workspace_id TEXT NOT NULL DEFAULT 'default',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_due
            ON scheduled_jobs(enabled, next_run_at, workspace_id);
        CREATE TABLE IF NOT EXISTS skills (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL, path TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', permissions TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'candidate', source TEXT NOT NULL DEFAULT 'local',
            manifest TEXT NOT NULL DEFAULT '{}', workspace_id TEXT NOT NULL DEFAULT 'default',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(name, version, workspace_id)
        );
        CREATE INDEX IF NOT EXISTS idx_skills_status ON skills(workspace_id, status, name);
        CREATE TABLE IF NOT EXISTS usage_attempts (
            id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL, surface TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT 'local', workspace_id TEXT NOT NULL DEFAULT 'default',
            session_id TEXT, run_id TEXT, cache_status TEXT NOT NULL DEFAULT 'unknown',
            prompt_tokens INTEGER, cache_hit_tokens INTEGER, cache_miss_tokens INTEGER,
            completion_tokens INTEGER, reasoning_tokens INTEGER, latency_ms INTEGER,
            completion_state TEXT NOT NULL, usage_status TEXT NOT NULL, cost_picos INTEGER,
            pricing_snapshot TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_usage_attempts_scope
            ON usage_attempts(user_id, workspace_id, session_id, created_at);
        CREATE TABLE IF NOT EXISTS usage_resets (
            id TEXT PRIMARY KEY, scope TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT 'local',
            workspace_id TEXT NOT NULL DEFAULT 'default', session_id TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_usage_resets_scope
            ON usage_resets(scope, user_id, workspace_id, session_id, created_at);
    """),
    (3, """
        CREATE TABLE IF NOT EXISTS learning_feature_flags (
            name TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT 'local',
            workspace_id TEXT NOT NULL DEFAULT 'default', enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL, PRIMARY KEY(name, user_id, workspace_id)
        );
        CREATE INDEX IF NOT EXISTS idx_learning_feature_flags_scope
            ON learning_feature_flags(user_id, workspace_id, name);
        CREATE TABLE IF NOT EXISTS learning_policies (
            user_id TEXT NOT NULL DEFAULT 'local', workspace_id TEXT NOT NULL DEFAULT 'default',
            mode TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(user_id, workspace_id)
        );
        CREATE TABLE IF NOT EXISTS learning_runtime (
            user_id TEXT NOT NULL DEFAULT 'local', workspace_id TEXT NOT NULL DEFAULT 'default',
            mode TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
            PRIMARY KEY(user_id, workspace_id)
        );
        CREATE TABLE IF NOT EXISTS history_nodes (
            id TEXT PRIMARY KEY, parent_id TEXT, kind TEXT NOT NULL DEFAULT 'turn',
            summary TEXT NOT NULL DEFAULT '', user_message TEXT NOT NULL DEFAULT '',
            assistant_message TEXT NOT NULL DEFAULT '', conversation TEXT NOT NULL DEFAULT '[]',
            interaction TEXT NOT NULL DEFAULT '{}', tasks TEXT NOT NULL DEFAULT '[]',
            snapshot_relpath TEXT NOT NULL, file_summary TEXT NOT NULL DEFAULT '{}',
            user_id TEXT NOT NULL DEFAULT 'local', workspace_id TEXT NOT NULL DEFAULT 'default',
            session_id TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_history_nodes_scope
            ON history_nodes(user_id, workspace_id, session_id, created_at);
        CREATE TABLE IF NOT EXISTS history_heads (
            user_id TEXT NOT NULL DEFAULT 'local', workspace_id TEXT NOT NULL DEFAULT 'default',
            session_id TEXT NOT NULL, head_id TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(user_id, workspace_id, session_id)
        );
    """),
)

_REQUIRED_COLUMNS = {
    "schema_migrations": {"version", "applied_at"},
    "events": {"id", "event_type", "payload", "user_id", "workspace_id", "session_id", "created_at"},
    "memories": {"id", "kind", "content", "user_id", "workspace_id", "content_hash", "metadata"},
    "files": {"id", "rel_path", "content", "content_hash", "user_id", "workspace_id"},
    "tasks": {"id", "description", "status", "dependencies", "acceptance", "user_id", "workspace_id", "session_id"},
    "task_events": {"id", "task_id", "event_type", "payload", "created_at"},
    "learning_proposals": {"id", "kind", "content", "status", "workspace_id"},
    "scheduled_jobs": {"id", "name", "next_run_at", "payload", "user_id", "workspace_id"},
    "skills": {"id", "name", "version", "path", "permissions", "workspace_id"},
    "usage_attempts": {"id", "provider", "model", "surface", "completion_state", "usage_status"},
    "usage_resets": {"id", "scope", "workspace_id", "created_at"},
    "history_nodes": {"id", "snapshot_relpath", "conversation", "interaction", "tasks", "session_id"},
    "history_heads": {"user_id", "workspace_id", "session_id", "head_id"},
    "learning_feature_flags": {"name", "user_id", "workspace_id", "enabled", "updated_at"},
    "learning_policies": {"user_id", "workspace_id", "mode", "updated_at"},
    "learning_runtime": {"user_id", "workspace_id", "mode", "status", "updated_at"},
}

class SQLiteDatabase:
    SCHEMA_VERSION = _MIGRATIONS[-1][0]
    def __init__(self, path: str | os.PathLike[str] | None = None):
        configured = path or os.environ.get("KYROZEN_DB_PATH")
        self.path = Path(configured or Path.home() / ".kyrozen" / "v2" / "openkyrozen.sqlite3").expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.last_backup_path: Path | None = None
        self._initialise()


    @contextmanager
    def connection(self, *, timeout: float = 30, busy_timeout_ms: int | None = None) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=timeout, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms if busy_timeout_ms is not None else 30000}")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        finally:
            connection.close()


    def _initialise(self) -> None:
        # WAL mode changes take an exclusive lock.  Multiple supported entry
        # points can import the store at the same time on a fresh HOME, so
        # retry the idempotent schema bootstrap instead of surfacing a raw
        # sqlite "database is locked" traceback.
        delay = 0.05
        for attempt in range(8):
            try:
                self._initialise_once()
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                if attempt == 7:
                    raise RuntimeError(
                        "OpenKyrozen state database is busy during startup; retry the command."
                    ) from None
                time.sleep(delay)
                delay = min(delay * 2, 1.0)


    def _initialise_once(self) -> None:
        with self._lock, self.connection(timeout=1, busy_timeout_ms=1000) as db:
            version = self._schema_version(db)
            self._validate_schema(db, version)
            db.execute("BEGIN IMMEDIATE")
            backup_path = None
            try:
                version = self._schema_version(db)
                self._validate_schema(db, version)
                if self._needs_upgrade(db, version):
                    backup_path = self._backup_database()
                for migration_version, script in _MIGRATIONS:
                    if migration_version > version:
                        self._execute_script(db, script)
                        db.execute(
                            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                            (migration_version, utc_now()),
                        )
                if version == self.SCHEMA_VERSION:
                    self._execute_script(db, _MIGRATIONS[-1][1])
                db.commit()
            except Exception:
                db.rollback()
                if backup_path is not None:
                    warnings.warn(
                        f"SQLite migration did not complete; original state is unchanged. Backup: {backup_path}",
                        RuntimeWarning, stacklevel=2,
                    )
                raise
            if backup_path is not None:
                self.last_backup_path = backup_path
                warnings.warn(
                    f"OpenKyrozen SQLite state was upgraded; backup: {backup_path}",
                    RuntimeWarning, stacklevel=2,
                )
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")

    def _schema_version(self, db: sqlite3.Connection) -> int:
        objects = {row["name"]: row["type"] for row in db.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')"
        )}
        if objects.get("schema_migrations") not in (None, "table"):
            raise RuntimeError("OpenKyrozen SQLite schema is inconsistent: schema_migrations is not a table")
        if "schema_migrations" not in objects:
            if set(objects) - {"sqlite_sequence"}:
                raise RuntimeError("OpenKyrozen SQLite schema is inconsistent: unversioned application tables found")
            return 0
        columns = {row["name"] for row in db.execute("PRAGMA table_info(schema_migrations)")}
        if not _REQUIRED_COLUMNS["schema_migrations"].issubset(columns):
            raise RuntimeError("OpenKyrozen SQLite schema is inconsistent: invalid schema_migrations table")
        rows = [row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")]
        if any(type(value) is not int or value < 1 for value in rows):
            raise RuntimeError("OpenKyrozen SQLite schema is inconsistent: migration versions must be positive integers")
        if rows and rows[-1] > self.SCHEMA_VERSION:
            raise RuntimeError(
                f"OpenKyrozen state uses newer schema {rows[-1]}; supported schema is {self.SCHEMA_VERSION}."
            )
        if rows and rows not in (list(range(1, rows[-1] + 1)), [rows[-1]]):
            raise RuntimeError("OpenKyrozen SQLite schema is inconsistent: migration history has gaps")
        version = rows[-1] if rows else 0
        if version == 0 and set(objects) - {"schema_migrations", "sqlite_sequence"}:
            raise RuntimeError("OpenKyrozen SQLite schema is inconsistent: unversioned application tables found")
        return version

    def _validate_schema(self, db: sqlite3.Connection, version: int) -> None:
        if version == 0:
            return
        required = {1: ("events", "memories", "files", "tasks", "task_events", "learning_proposals"),
                    2: ("scheduled_jobs", "skills", "usage_attempts", "usage_resets"),
                    3: ("history_nodes", "history_heads")}
        tables = {row["name"] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        for stage in range(1, version + 1):
            for table in required[stage]:
                columns = {row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')} if table in tables else set()
                if not _REQUIRED_COLUMNS[table].issubset(columns):
                    raise RuntimeError(
                        f"OpenKyrozen SQLite schema is inconsistent: {table} is missing required columns"
                    )
        tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("learning_feature_flags", "learning_policies", "learning_runtime"):
            if table in tables:
                columns = {row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')}
                if not _REQUIRED_COLUMNS[table].issubset(columns):
                    raise RuntimeError(
                        f"OpenKyrozen SQLite schema is inconsistent: {table} is missing required columns"
                    )

    def _needs_upgrade(self, db: sqlite3.Connection, version: int) -> bool:
        if version < self.SCHEMA_VERSION:
            return True
        tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return not {"learning_feature_flags", "learning_policies", "learning_runtime"}.issubset(tables)

    @staticmethod
    def _execute_script(db: sqlite3.Connection, script: str) -> None:
        statement = ""
        for line in script.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                if statement.strip():
                    db.execute(statement)
                statement = ""
        if statement.strip():
            raise RuntimeError("OpenKyrozen contains an incomplete SQLite migration")

    def _backup_database(self) -> Path | None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        backup_path = self.path.with_name(f"{self.path.name}.backup.{time.time_ns()}")
        descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)
        try:
            source = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            os.chmod(backup_path, stat.S_IRUSR | stat.S_IWUSR)
            return backup_path
        except Exception:
            backup_path.unlink(missing_ok=True)
            raise


    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


    @staticmethod
    def _loads(value: str, fallback: Any) -> Any:
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return fallback
