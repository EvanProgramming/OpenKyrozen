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

    SCHEMA_VERSION = 2

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
                    workspace_id: str = "default", session_id: str | None = None,
                    user_id: str | None = None) -> list[dict[str, Any]]:
        clauses = ["workspace_id=?"]
        params: list[Any] = [workspace_id]
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
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

    def list_sessions(self, *, workspace_id: str = "default", limit: int = 100,
                      user_id: str | None = None) -> list[dict[str, Any]]:
        clauses = ["workspace_id=?", "session_id IS NOT NULL"]
        params: list[Any] = [workspace_id]
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        params.append(max(1, min(limit, 1000)))
        with self.connection() as db:
            rows = db.execute(
                "SELECT session_id, MAX(created_at) AS updated_at, COUNT(*) AS event_count FROM events "
                f"WHERE {' AND '.join(clauses)} GROUP BY session_id ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def record_usage_attempt(
            self, *, attempt_id: str, provider: str, model: str, surface: str,
            user_id: str = "local", workspace_id: str = "default",
            session_id: str | None = None, run_id: str | None = None,
            cache_status: str = "unknown", prompt_tokens: int | None = None,
            cache_hit_tokens: int | None = None, cache_miss_tokens: int | None = None,
            completion_tokens: int | None = None, reasoning_tokens: int | None = None,
            latency_ms: int | None = None, completion_state: str = "completed",
            usage_status: str = "unknown", cost_picos: int | None = None,
            pricing_snapshot: dict[str, Any] | None = None,
    ) -> bool:
        """Store one immutable provider attempt; duplicate IDs are harmless."""
        def integer(value: int | None) -> int | None:
            return None if value is None else max(0, int(value))

        values = (
            str(attempt_id)[:128], str(provider or "unknown")[:64], str(model or "unknown")[:128],
            str(surface or "unknown")[:64], str(user_id or "local")[:128],
            str(workspace_id or "default")[:256], str(session_id)[:128] if session_id else None,
            str(run_id)[:128] if run_id else None, str(cache_status or "unknown")[:32],
            integer(prompt_tokens), integer(cache_hit_tokens), integer(cache_miss_tokens),
            integer(completion_tokens), integer(reasoning_tokens), integer(latency_ms),
            str(completion_state or "unknown")[:32], str(usage_status or "unknown")[:32],
            integer(cost_picos), self._json(pricing_snapshot or {}), utc_now(),
        )
        with self._lock, self.connection() as db:
            return db.execute(
                "INSERT OR IGNORE INTO usage_attempts("
                "id,provider,model,surface,user_id,workspace_id,session_id,run_id,cache_status,"
                "prompt_tokens,cache_hit_tokens,cache_miss_tokens,completion_tokens,reasoning_tokens,"
                "latency_ms,completion_state,usage_status,cost_picos,pricing_snapshot,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            ).rowcount == 1

    def list_usage_attempts(
            self, *, workspace_id: str | None = None, session_id: str | None = None,
            user_id: str | None = None, run_id: str | None = None, limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if workspace_id is not None:
            clauses.append("workspace_id=?")
            params.append(workspace_id)
        if session_id is not None:
            clauses.append("session_id=?")
            params.append(session_id)
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(run_id)
        params.append(max(1, min(limit, 10000)))
        with self.connection() as db:
            rows = db.execute(
                f"SELECT * FROM usage_attempts WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC LIMIT ?", params,
            ).fetchall()
        return [dict(row, pricing_snapshot=self._loads(row["pricing_snapshot"], {})) for row in rows]

    def usage_totals(
            self, *, workspace_id: str | None = None, session_id: str | None = None,
            user_id: str | None = None, run_id: str | None = None, since: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate immutable usage records for one explicit durable scope."""
        clauses = ["1=1"]
        params: list[Any] = []
        for field, value in (("workspace_id", workspace_id), ("session_id", session_id),
                             ("user_id", user_id), ("run_id", run_id)):
            if value is not None:
                clauses.append(f"{field}=?")
                params.append(value)
        if since:
            clauses.append("created_at>?")
            params.append(since)
        columns = (
            "COUNT(*) AS attempts, "
            "SUM(CASE WHEN usage_status='authoritative' THEN 1 ELSE 0 END) AS authoritative_attempts, "
            "SUM(CASE WHEN usage_status='estimated' THEN 1 ELSE 0 END) AS estimated_attempts, "
            "SUM(COALESCE(prompt_tokens, 0)) AS prompt_tokens, "
            "SUM(COALESCE(cache_hit_tokens, 0)) AS cache_hit_tokens, "
            "SUM(COALESCE(cache_miss_tokens, 0)) AS cache_miss_tokens, "
            "SUM(COALESCE(completion_tokens, 0)) AS completion_tokens, "
            "SUM(COALESCE(reasoning_tokens, 0)) AS reasoning_tokens, "
            "SUM(COALESCE(cost_picos, 0)) AS cost_picos"
        )
        with self.connection() as db:
            total = db.execute(
                f"SELECT {columns} FROM usage_attempts WHERE {' AND '.join(clauses)}", params,
            ).fetchone()
            by_provider = db.execute(
                f"SELECT provider, {columns} FROM usage_attempts WHERE {' AND '.join(clauses)} "
                "GROUP BY provider ORDER BY provider", params,
            ).fetchall()
            pricing_groups = db.execute(
                "SELECT provider, pricing_snapshot, "
                "SUM(COALESCE(prompt_tokens, 0)) AS prompt_tokens, "
                "SUM(COALESCE(cache_hit_tokens, 0)) AS cache_hit_tokens, "
                "SUM(COALESCE(cache_miss_tokens, 0)) AS cache_miss_tokens, "
                "SUM(COALESCE(completion_tokens, 0)) AS completion_tokens, "
                "SUM(COALESCE(cost_picos, 0)) AS cost_picos "
                f"FROM usage_attempts WHERE {' AND '.join(clauses)} "
                "GROUP BY provider, pricing_snapshot",
                params,
            ).fetchall()

        def normalise(row: sqlite3.Row | None) -> dict[str, int]:
            names = ("attempts", "authoritative_attempts", "estimated_attempts", "prompt_tokens",
                     "cache_hit_tokens", "cache_miss_tokens", "completion_tokens", "reasoning_tokens",
                     "cost_picos")
            values = {name: int((row[name] if row else 0) or 0) for name in names}
            values["unknown_attempts"] = max(
                0, values["attempts"] - values["authoritative_attempts"] - values["estimated_attempts"],
            )
            return values

        def exact_cost(groups: list[sqlite3.Row]) -> int:
            """Round once after combining frozen per-million pricing numerators."""
            numerator = fallback = 0
            for row in groups:
                snapshot = self._loads(row["pricing_snapshot"], {})
                try:
                    output_rate = max(0, int(snapshot["output_picos_per_million"]))
                    cache_miss_rate = snapshot.get("cache_miss_picos_per_million")
                    input_rate = max(0, int(
                        cache_miss_rate if cache_miss_rate is not None else snapshot["input_picos_per_million"],
                    ))
                except (KeyError, TypeError, ValueError):
                    # Compatibility for any manually-created legacy ledger row.
                    fallback += int(row["cost_picos"] or 0)
                    continue
                prompt = int(row["prompt_tokens"] or 0)
                if "cache_hit_picos_per_million" in snapshot:
                    hit = min(prompt, max(0, int(row["cache_hit_tokens"] or 0)))
                    numerator += (hit * max(0, int(snapshot["cache_hit_picos_per_million"]))
                                  + (prompt - hit) * input_rate)
                else:
                    numerator += prompt * input_rate
                numerator += int(row["completion_tokens"] or 0) * output_rate
            return fallback + numerator // 1_000_000

        totals = normalise(total)
        totals["cost_picos"] = exact_cost(pricing_groups)
        provider_groups: dict[str, list[sqlite3.Row]] = {}
        for row in pricing_groups:
            provider_groups.setdefault(row["provider"], []).append(row)

        return {
            **totals, "currency": "USD",
            "providers": [dict(provider=row["provider"], **{
                **normalise(row), "cost_picos": exact_cost(provider_groups[row["provider"]]),
            }) for row in by_provider],
        }

    def create_usage_reset(
            self, *, scope: str, user_id: str = "local", workspace_id: str = "default",
            session_id: str | None = None,
    ) -> dict[str, str]:
        """Start a new reported usage window while retaining immutable attempts."""
        if scope not in {"workspace", "session"}:
            raise ValueError("usage reset scope must be workspace or session")
        if scope == "session" and not session_id:
            raise ValueError("session usage reset requires a session_id")
        reset_id = f"usage_reset_{uuid.uuid4().hex}"
        event_id = f"evt_{uuid.uuid4().hex}"
        created_at = utc_now()
        payload = {"reset_id": reset_id, "scope": scope}
        with self._lock, self.connection() as db:
            db.execute(
                "INSERT INTO usage_resets(id,scope,user_id,workspace_id,session_id,created_at) VALUES(?,?,?,?,?,?)",
                (reset_id, scope, user_id, workspace_id, session_id, created_at),
            )
            db.execute(
                "INSERT INTO events(id,event_type,payload,user_id,workspace_id,session_id,task_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (event_id, "usage.reset", self._json(payload), user_id, workspace_id,
                 session_id, None, created_at),
            )
        return {"reset_id": reset_id, "created_at": created_at}

    def latest_usage_reset(
            self, *, scope: str, user_id: str = "local", workspace_id: str = "default",
            session_id: str | None = None,
    ) -> dict[str, Any] | None:
        clauses = ["scope=?", "user_id=?", "workspace_id=?"]
        params: list[Any] = [scope, user_id, workspace_id]
        if scope == "session":
            clauses.append("session_id=?")
            params.append(session_id)
        with self.connection() as db:
            row = db.execute(
                f"SELECT * FROM usage_resets WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC LIMIT 1", params,
            ).fetchone()
        return dict(row) if row else None

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

    def list_memories(self, *, kind: str | None = None, status: str | None = "active", limit: int = 100,
                      workspace_id: str = "default", session_id: str | None = None,
                      user_id: str | None = None) -> list[dict[str, Any]]:
        clauses = ["workspace_id=?"]
        params: list[Any] = [workspace_id]
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        if status:
            clauses.append("status=?")
            params.append(status)
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

    def set_memory_status(self, memory_id: str, status: str, *, workspace_id: str = "default",
                          user_id: str | None = None) -> bool:
        clauses = ["id=?", "workspace_id=?"]
        params: list[Any] = [memory_id, workspace_id]
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        params[:0] = [status, utc_now()]
        with self._lock, self.connection() as db:
            return db.execute(
                f"UPDATE memories SET status=?,updated_at=? WHERE {' AND '.join(clauses)}",
                params,
            ).rowcount == 1

    def delete_memories(self, ids: list[str], *, workspace_id: str = "default",
                        user_id: str | None = None) -> int:
        ids = [str(item) for item in ids if item]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        clauses = ["workspace_id=?", f"id IN ({placeholders})"]
        params: list[Any] = [workspace_id, *ids]
        if user_id is not None:
            clauses.insert(1, "user_id=?")
            params.insert(1, user_id)
        with self._lock, self.connection() as db:
            cursor = db.execute(f"DELETE FROM memories WHERE {' AND '.join(clauses)}", params)
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

    def remove_stale_files(self, valid_paths: set[str], *, workspace_id: str = "default",
                           user_id: str | None = None) -> int:
        clauses = ["workspace_id=?"]
        params: list[Any] = [workspace_id]
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        with self._lock, self.connection() as db:
            rows = db.execute(f"SELECT id, rel_path FROM files WHERE {' AND '.join(clauses)}", params).fetchall()
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
                   statuses: set[str] | None = None, limit: int = 1000,
                   user_id: str | None = None) -> list[dict[str, Any]]:
        clauses = ["workspace_id=?"]
        params: list[Any] = [workspace_id]
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
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
            if item["status"] == "done":
                item["status"] = "succeeded"
            item["dependencies"] = self._loads(item["dependencies"], [])
            item["acceptance"] = self._loads(item["acceptance"], [])
            item["checkpoint"] = self._loads(item["checkpoint"], {})
            item["evidence"] = self._loads(item["evidence"], [])
            result.append(item)
        return result

    def update_proposal(self, proposal_id: str, *, status: str, validation: dict[str, Any] | None = None,
                        confidence: float | None = None, workspace_id: str | None = None,
                        user_id: str | None = None) -> bool:
        values = [status, self._json(validation or {}), utc_now()]
        query = "UPDATE learning_proposals SET status=?,validation=?,updated_at=?"
        if confidence is not None:
            query += ",confidence=?"
            values.append(max(0.0, min(1.0, confidence)))
        query += " WHERE id=?"
        values.append(proposal_id)
        if workspace_id is not None:
            query += " AND workspace_id=?"
            values.append(workspace_id)
        if user_id is not None:
            query += " AND user_id=?"
            values.append(user_id)
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

    def list_proposals(self, *, status: str | None = None, workspace_id: str = "default", limit: int = 100,
                       user_id: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = [workspace_id]
        clause = "workspace_id=?"
        if user_id is not None:
            clause += " AND user_id=?"
            params.append(user_id)
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
                       limit: int = 500, user_id: str | None = None) -> list[dict[str, Any]]:
        clauses = ["workspace_id=?"]
        params: list[Any] = [workspace_id]
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        if enabled is not None:
            clauses.append("enabled=?")
            params.append(int(enabled))
        params.append(max(1, min(limit, 5000)))
        with self.connection() as db:
            rows = db.execute(f"SELECT * FROM scheduled_jobs WHERE {' AND '.join(clauses)} ORDER BY next_run_at LIMIT ?", params).fetchall()
        return [dict(row, enabled=bool(row["enabled"]), payload=self._loads(row["payload"], {})) for row in rows]

    def claim_due_schedules(self, *, now: str, workspace_id: str = "default", limit: int = 20,
                            user_id: str | None = None) -> list[dict[str, Any]]:
        clauses = ["workspace_id=?"]
        params: list[Any] = [workspace_id]
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        clauses.extend(["enabled=1", "next_run_at<=?"])
        params.extend([now, max(1, min(limit, 100))])
        with self._lock, self.connection() as db:
            rows = db.execute(
                f"SELECT * FROM scheduled_jobs WHERE {' AND '.join(clauses)} ORDER BY next_run_at LIMIT ?",
                params,
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

    def set_schedule_enabled(self, job_id: str, enabled: bool, *, workspace_id: str = "default",
                             user_id: str | None = None) -> bool:
        clauses = ["id=?", "workspace_id=?"]
        params: list[Any] = [job_id, workspace_id]
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        params[:0] = [int(enabled), utc_now()]
        with self._lock, self.connection() as db:
            return db.execute(
                f"UPDATE scheduled_jobs SET enabled=?,updated_at=? WHERE {' AND '.join(clauses)}",
                params,
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
