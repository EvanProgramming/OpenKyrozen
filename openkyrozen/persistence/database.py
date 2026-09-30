from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import utc_now, stable_hash

class SQLiteDatabase:
    SCHEMA_VERSION = 3
    def __init__(self, path: str | os.PathLike[str] | None = None):
        configured = path or os.environ.get("KYROZEN_DB_PATH")
        self.path = Path(configured or Path.home() / ".kyrozen" / "v2" / "openkyrozen.sqlite3").expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
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
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    user_id TEXT NOT NULL DEFAULT 'local',
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    session_id TEXT,
                    task_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_scope
                    ON events(user_id, workspace_id, session_id, created_at);
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    user_id TEXT NOT NULL DEFAULT 'local',
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    session_id TEXT,
                    content_hash TEXT NOT NULL,
                    source_event_ids TEXT NOT NULL DEFAULT '[]',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    valid_until TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_memories_dedupe
                    ON memories(kind, user_id, workspace_id, session_id, content_hash);
                CREATE INDEX IF NOT EXISTS idx_memories_lookup
                    ON memories(kind, status, user_id, workspace_id, updated_at);
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY,
                    rel_path TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    user_id TEXT NOT NULL DEFAULT 'local',
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_files_dedupe
                    ON files(rel_path, user_id, workspace_id, content_hash);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    parent_id TEXT,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    dependencies TEXT NOT NULL DEFAULT '[]',
                    acceptance TEXT NOT NULL DEFAULT '[]',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    checkpoint TEXT NOT NULL DEFAULT '{}',
                    evidence TEXT NOT NULL DEFAULT '[]',
                    user_id TEXT NOT NULL DEFAULT 'local',
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    session_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_scope_status
                    ON tasks(user_id, workspace_id, session_id, status, priority);
                CREATE TABLE IF NOT EXISTS task_events (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS learning_proposals (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'candidate',
                    confidence REAL NOT NULL DEFAULT 0.0,
                    evidence TEXT NOT NULL DEFAULT '[]',
                    validation TEXT NOT NULL DEFAULT '{}',
                    user_id TEXT NOT NULL DEFAULT 'local',
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_learning_status
                    ON learning_proposals(status, workspace_id, updated_at);
                CREATE TABLE IF NOT EXISTS learning_feature_flags (
                    name TEXT NOT NULL,
                    user_id TEXT NOT NULL DEFAULT 'local',
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(name, user_id, workspace_id)
                );
                CREATE INDEX IF NOT EXISTS idx_learning_feature_flags_scope
                    ON learning_feature_flags(user_id, workspace_id, name);
                CREATE TABLE IF NOT EXISTS learning_policies (
                    user_id TEXT NOT NULL DEFAULT 'local',
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    mode TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, workspace_id)
                );
                CREATE TABLE IF NOT EXISTS learning_runtime (
                    user_id TEXT NOT NULL DEFAULT 'local',
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    mode TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, workspace_id)
                );
                CREATE TABLE IF NOT EXISTS scheduled_jobs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    interval_seconds REAL,
                    run_at TEXT,
                    next_run_at TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL DEFAULT '{}',
                    last_run_at TEXT,
                    user_id TEXT NOT NULL DEFAULT 'local',
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_due
                    ON scheduled_jobs(enabled, next_run_at, workspace_id);
                CREATE TABLE IF NOT EXISTS skills (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    path TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    permissions TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'candidate',
                    source TEXT NOT NULL DEFAULT 'local',
                    manifest TEXT NOT NULL DEFAULT '{}',
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(name, version, workspace_id)
                );
                CREATE INDEX IF NOT EXISTS idx_skills_status
                    ON skills(workspace_id, status, name);
                CREATE TABLE IF NOT EXISTS usage_attempts (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    surface TEXT NOT NULL,
                    user_id TEXT NOT NULL DEFAULT 'local',
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    session_id TEXT,
                    run_id TEXT,
                    cache_status TEXT NOT NULL DEFAULT 'unknown',
                    prompt_tokens INTEGER,
                    cache_hit_tokens INTEGER,
                    cache_miss_tokens INTEGER,
                    completion_tokens INTEGER,
                    reasoning_tokens INTEGER,
                    latency_ms INTEGER,
                    completion_state TEXT NOT NULL,
                    usage_status TEXT NOT NULL,
                    cost_picos INTEGER,
                    pricing_snapshot TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_usage_attempts_scope
                    ON usage_attempts(user_id, workspace_id, session_id, created_at);
                CREATE TABLE IF NOT EXISTS usage_resets (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    user_id TEXT NOT NULL DEFAULT 'local',
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    session_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_usage_resets_scope
                    ON usage_resets(scope, user_id, workspace_id, session_id, created_at);
                CREATE TABLE IF NOT EXISTS history_nodes (
                    id TEXT PRIMARY KEY,
                    parent_id TEXT,
                    kind TEXT NOT NULL DEFAULT 'turn',
                    summary TEXT NOT NULL DEFAULT '',
                    user_message TEXT NOT NULL DEFAULT '',
                    assistant_message TEXT NOT NULL DEFAULT '',
                    conversation TEXT NOT NULL DEFAULT '[]',
                    interaction TEXT NOT NULL DEFAULT '{}',
                    tasks TEXT NOT NULL DEFAULT '[]',
                    snapshot_relpath TEXT NOT NULL,
                    file_summary TEXT NOT NULL DEFAULT '{}',
                    user_id TEXT NOT NULL DEFAULT 'local',
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_history_nodes_scope
                    ON history_nodes(user_id, workspace_id, session_id, created_at);
                CREATE TABLE IF NOT EXISTS history_heads (
                    user_id TEXT NOT NULL DEFAULT 'local',
                    workspace_id TEXT NOT NULL DEFAULT 'default',
                    session_id TEXT NOT NULL,
                    head_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, workspace_id, session_id)
                );
                """
            )
            existing = db.execute("SELECT version FROM schema_migrations WHERE version=?", (self.SCHEMA_VERSION,)).fetchone()
            if existing is None:
                db.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (self.SCHEMA_VERSION, utc_now()),
                )


    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


    @staticmethod
    def _loads(value: str, fallback: Any) -> Any:
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return fallback
