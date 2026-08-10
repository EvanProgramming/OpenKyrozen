"""Durable local event and state storage for OpenKyrozen v2.

SQLite is the source of truth. Vector indexes and in-memory caches are
derived data and may be rebuilt without losing the audit trail.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class EventStore:
    """Small transactional store shared by memory, tasks, and learning."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | os.PathLike[str] | None = None):
        configured = path or os.environ.get("KYROZEN_DB_PATH")
        self.path = Path(configured or Path.home() / ".kyrozen" / "v2" / "openkyrozen.sqlite3").expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialise()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        finally:
            connection.close()

    def _initialise(self) -> None:
        with self._lock, self.connection() as db:
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
                """
            )
            existing = db.execute("SELECT version FROM schema_migrations WHERE version=?", (self.SCHEMA_VERSION,)).fetchone()
            if existing is None:
                db.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
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

    def append_event(self, event_type: str, payload: Any, *, user_id: str = "local",
                     workspace_id: str = "default", session_id: str | None = None,
                     task_id: str | None = None) -> str:
        event_id = f"evt_{uuid.uuid4().hex}"
        with self._lock, self.connection() as db:
            db.execute(
                "INSERT INTO events(id,event_type,payload,user_id,workspace_id,session_id,task_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (event_id, event_type, self._json(payload), user_id, workspace_id, session_id, task_id, utc_now()),
            )
        return event_id

    def list_events(self, event_type: str | None = None, *, limit: int = 100,
                    workspace_id: str = "default", session_id: str | None = None) -> list[dict[str, Any]]:
        clauses = ["workspace_id=?"]
        params: list[Any] = [workspace_id]
        if event_type:
            clauses.append("event_type=?")
            params.append(event_type)
        if session_id is not None:
            clauses.append("session_id=?")
            params.append(session_id)
        params.append(max(1, min(limit, 10000)))
        with self.connection() as db:
            rows = db.execute(
                f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row, payload=self._loads(row["payload"], {})) for row in rows]

    def list_sessions(self, *, workspace_id: str = "default", limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT session_id, MAX(created_at) AS updated_at, COUNT(*) AS event_count FROM events "
                "WHERE workspace_id=? AND session_id IS NOT NULL GROUP BY session_id ORDER BY updated_at DESC LIMIT ?",
                (workspace_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_memory(self, content: str, *, kind: str = "episodic", status: str = "active",
                      confidence: float = 0.5, user_id: str = "local", workspace_id: str = "default",
                      session_id: str | None = None, source_event_ids: list[str] | None = None,
                      metadata: dict[str, Any] | None = None) -> str:
        now = utc_now()
        digest = stable_hash(content.strip())
        memory_id = f"mem_{uuid.uuid4().hex}"
        with self._lock, self.connection() as db:
            row = db.execute(
                "SELECT id, source_event_ids, metadata FROM memories WHERE kind=? AND user_id=? AND workspace_id=? "
                "AND session_id IS ? AND content_hash=?",
                (kind, user_id, workspace_id, session_id, digest),
            ).fetchone()
            if row:
                memory_id = row["id"]
                old_sources = self._loads(row["source_event_ids"], [])
                merged_sources = list(dict.fromkeys(old_sources + (source_event_ids or [])))
                old_meta = self._loads(row["metadata"], {})
                merged_meta = {**old_meta, **(metadata or {})}
                db.execute(
                    "UPDATE memories SET status=?,confidence=?,source_event_ids=?,metadata=?,updated_at=? WHERE id=?",
                    (status, max(0.0, min(1.0, confidence)), self._json(merged_sources), self._json(merged_meta), now, memory_id),
                )
            else:
                db.execute(
                    "INSERT INTO memories(id,kind,content,status,confidence,user_id,workspace_id,session_id,content_hash,source_event_ids,metadata,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (memory_id, kind, content, status, max(0.0, min(1.0, confidence)), user_id, workspace_id, session_id,
                     digest, self._json(source_event_ids or []), self._json(metadata or {}), now, now),
                )
        return memory_id

    def list_memories(self, *, kind: str | None = None, status: str = "active", limit: int = 100,
                      workspace_id: str = "default", session_id: str | None = None) -> list[dict[str, Any]]:
        clauses = ["status=?", "workspace_id=?"]
        params: list[Any] = [status, workspace_id]
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if session_id is not None:
            clauses.append("(session_id IS NULL OR session_id=?)")
            params.append(session_id)
        params.append(max(1, min(limit, 10000)))
        with self.connection() as db:
            rows = db.execute(
                f"SELECT * FROM memories WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row, source_event_ids=self._loads(row["source_event_ids"], []), metadata=self._loads(row["metadata"], {})) for row in rows]

    def delete_memories(self, ids: list[str], *, workspace_id: str = "default") -> int:
        ids = [str(item) for item in ids if item]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self.connection() as db:
            cursor = db.execute(f"DELETE FROM memories WHERE workspace_id=? AND id IN ({placeholders})", [workspace_id, *ids])
            return cursor.rowcount

    def upsert_file(self, rel_path: str, content: str, *, user_id: str = "local", workspace_id: str = "default") -> str:
        now = utc_now()
        digest = stable_hash(content)
        file_id = f"file_{uuid.uuid4().hex}"
        with self._lock, self.connection() as db:
            row = db.execute(
                "SELECT id FROM files WHERE rel_path=? AND user_id=? AND workspace_id=? AND content_hash=?",
                (rel_path, user_id, workspace_id, digest),
            ).fetchone()
            if row:
                db.execute("UPDATE files SET updated_at=? WHERE id=?", (now, row["id"]))
                return row["id"]
            db.execute(
                "INSERT INTO files(id,rel_path,content,content_hash,user_id,workspace_id,updated_at) VALUES(?,?,?,?,?,?,?)",
                (file_id, rel_path, content, digest, user_id, workspace_id, now),
            )
        return file_id

    def remove_stale_files(self, valid_paths: set[str], *, workspace_id: str = "default") -> int:
        with self._lock, self.connection() as db:
            rows = db.execute("SELECT id, rel_path FROM files WHERE workspace_id=?", (workspace_id,)).fetchall()
            stale = [row["id"] for row in rows if row["rel_path"] not in valid_paths]
            if stale:
                placeholders = ",".join("?" for _ in stale)
                db.execute(f"DELETE FROM files WHERE id IN ({placeholders})", stale)
            return len(stale)

    def create_proposal(self, kind: str, content: str, *, confidence: float = 0.0,
                        evidence: list[str] | None = None, workspace_id: str = "default",
                        user_id: str = "local") -> str:
        proposal_id = f"proposal_{uuid.uuid4().hex}"
        now = utc_now()
        with self._lock, self.connection() as db:
            db.execute(
                "INSERT INTO learning_proposals(id,kind,content,confidence,evidence,user_id,workspace_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (proposal_id, kind, content, max(0.0, min(1.0, confidence)), self._json(evidence or []), user_id, workspace_id, now, now),
            )
        return proposal_id

    def list_tasks(self, *, workspace_id: str = "default", session_id: str | None = None,
                   statuses: set[str] | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        clauses = ["workspace_id=?"]
        params: list[Any] = [workspace_id]
        if session_id is not None:
            clauses.append("session_id=?")
            params.append(session_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(sorted(statuses))
        params.append(max(1, min(limit, 10000)))
        with self.connection() as db:
            rows = db.execute(
                f"SELECT * FROM tasks WHERE {' AND '.join(clauses)} ORDER BY priority DESC, updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["dependencies"] = self._loads(item["dependencies"], [])
            item["acceptance"] = self._loads(item["acceptance"], [])
            item["checkpoint"] = self._loads(item["checkpoint"], {})
            item["evidence"] = self._loads(item["evidence"], [])
            result.append(item)
        return result

    def update_proposal(self, proposal_id: str, *, status: str, validation: dict[str, Any] | None = None,
                        confidence: float | None = None) -> bool:
        values = [status, self._json(validation or {}), utc_now(), proposal_id]
        query = "UPDATE learning_proposals SET status=?,validation=?,updated_at=?"
        if confidence is not None:
            query += ",confidence=?"
            values.insert(-1, max(0.0, min(1.0, confidence)))
        query += " WHERE id=?"
        with self._lock, self.connection() as db:
            return db.execute(query, values).rowcount == 1

    def add_proposal_evidence(self, proposal_id: str, evidence_id: str) -> int:
        with self._lock, self.connection() as db:
            row = db.execute("SELECT evidence FROM learning_proposals WHERE id=?", (proposal_id,)).fetchone()
            if not row:
                return 0
            evidence = self._loads(row["evidence"], [])
            if evidence_id not in evidence:
                evidence.append(evidence_id)
            db.execute("UPDATE learning_proposals SET evidence=?,updated_at=? WHERE id=?",
                       (self._json(evidence), utc_now(), proposal_id))
            return len(evidence)

    def list_proposals(self, *, status: str | None = None, workspace_id: str = "default", limit: int = 100) -> list[dict[str, Any]]:
        params: list[Any] = [workspace_id]
        clause = "workspace_id=?"
        if status:
            clause += " AND status=?"
            params.append(status)
        params.append(max(1, min(limit, 10000)))
        with self.connection() as db:
            rows = db.execute(f"SELECT * FROM learning_proposals WHERE {clause} ORDER BY updated_at DESC LIMIT ?", params).fetchall()
        return [dict(row, evidence=self._loads(row["evidence"], []), validation=self._loads(row["validation"], {})) for row in rows]

    def upsert_schedule(self, job_id: str, name: str, *, next_run_at: str,
                        interval_seconds: float | None = None, run_at: str | None = None,
                        payload: dict[str, Any] | None = None, enabled: bool = True,
                        workspace_id: str = "default", user_id: str = "local") -> str:
        now = utc_now()
        with self._lock, self.connection() as db:
            db.execute(
                "INSERT INTO scheduled_jobs(id,name,interval_seconds,run_at,next_run_at,enabled,payload,user_id,workspace_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,interval_seconds=excluded.interval_seconds,run_at=excluded.run_at,next_run_at=excluded.next_run_at,enabled=excluded.enabled,payload=excluded.payload,updated_at=excluded.updated_at",
                (job_id, name, interval_seconds, run_at, next_run_at, int(enabled), self._json(payload or {}), user_id, workspace_id, now, now),
            )
        return job_id

    def list_schedules(self, *, workspace_id: str = "default", enabled: bool | None = None,
                       limit: int = 500) -> list[dict[str, Any]]:
        clauses = ["workspace_id=?"]
        params: list[Any] = [workspace_id]
        if enabled is not None:
            clauses.append("enabled=?")
            params.append(int(enabled))
        params.append(max(1, min(limit, 5000)))
        with self.connection() as db:
            rows = db.execute(f"SELECT * FROM scheduled_jobs WHERE {' AND '.join(clauses)} ORDER BY next_run_at LIMIT ?", params).fetchall()
        return [dict(row, enabled=bool(row["enabled"]), payload=self._loads(row["payload"], {})) for row in rows]

    def claim_due_schedules(self, *, now: str, workspace_id: str = "default", limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, self.connection() as db:
            rows = db.execute(
                "SELECT * FROM scheduled_jobs WHERE workspace_id=? AND enabled=1 AND next_run_at<=? ORDER BY next_run_at LIMIT ?",
                (workspace_id, now, max(1, min(limit, 100))),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                interval = row["interval_seconds"]
                next_run = now if not interval else datetime.fromisoformat(now) + timedelta(seconds=float(interval))
                next_run_at = next_run.isoformat() if interval else row["next_run_at"]
                db.execute(
                    "UPDATE scheduled_jobs SET next_run_at=?,last_run_at=?,enabled=?,updated_at=? WHERE id=? AND enabled=1",
                    (next_run_at, now, int(bool(interval)), now, row["id"]),
                )
                claimed.append(dict(row, payload=self._loads(row["payload"], {}), next_run_at=next_run_at))
            return claimed

    def set_schedule_enabled(self, job_id: str, enabled: bool, *, workspace_id: str = "default") -> bool:
        with self._lock, self.connection() as db:
            return db.execute(
                "UPDATE scheduled_jobs SET enabled=?,updated_at=? WHERE id=? AND workspace_id=?",
                (int(enabled), utc_now(), job_id, workspace_id),
            ).rowcount == 1

    def upsert_skill(self, *, name: str, version: str, path: str, description: str,
                     permissions: list[str], status: str = "candidate", source: str = "local",
                     manifest: dict[str, Any] | None = None, workspace_id: str = "default") -> str:
        skill_id = f"skill_{stable_hash(f'{workspace_id}:{name}:{version}')[:24]}"
        now = utc_now()
        with self._lock, self.connection() as db:
            db.execute(
                "INSERT INTO skills(id,name,version,path,description,permissions,status,source,manifest,workspace_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(name,version,workspace_id) DO UPDATE SET path=excluded.path,description=excluded.description,permissions=excluded.permissions,status=excluded.status,source=excluded.source,manifest=excluded.manifest,updated_at=excluded.updated_at",
                (skill_id, name, version, path, description, self._json(sorted(set(permissions))), status, source,
                 self._json(manifest or {}), workspace_id, now, now),
            )
        return skill_id

    def list_skills(self, *, workspace_id: str = "default", status: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        clauses = ["workspace_id=?"]
        params: list[Any] = [workspace_id]
        if status:
            clauses.append("status=?")
            params.append(status)
        params.append(max(1, min(limit, 5000)))
        with self.connection() as db:
            rows = db.execute(f"SELECT * FROM skills WHERE {' AND '.join(clauses)} ORDER BY name,version LIMIT ?", params).fetchall()
        return [dict(row, permissions=self._loads(row["permissions"], []), manifest=self._loads(row["manifest"], {})) for row in rows]

    def set_skill_status(self, skill_id: str, status: str, *, workspace_id: str = "default") -> bool:
        with self._lock, self.connection() as db:
            return db.execute("UPDATE skills SET status=?,updated_at=? WHERE id=? AND workspace_id=?",
                              (status, utc_now(), skill_id, workspace_id)).rowcount == 1
