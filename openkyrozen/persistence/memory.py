from __future__ import annotations

import uuid
from typing import Any

from .models import utc_now, stable_hash

class MemoryRepository:
    def __init__(self, database):
        self.database = database

    def upsert_memory(self, content: str, *, kind: str = "episodic", status: str = "active",
                      confidence: float = 0.5, user_id: str = "local", workspace_id: str = "default",
                      session_id: str | None = None, source_event_ids: list[str] | None = None,
                      metadata: dict[str, Any] | None = None) -> str:
        now = utc_now()
        digest = stable_hash(content.strip())
        memory_id = f"mem_{uuid.uuid4().hex}"
        with self.database._lock, self.database.connection() as db:
            row = db.execute(
                "SELECT id, source_event_ids, metadata FROM memories WHERE kind=? AND user_id=? AND workspace_id=? "
                "AND session_id IS ? AND content_hash=?",
                (kind, user_id, workspace_id, session_id, digest),
            ).fetchone()
            if row:
                memory_id = row["id"]
                old_sources = self.database._loads(row["source_event_ids"], [])
                merged_sources = list(dict.fromkeys(old_sources + (source_event_ids or [])))
                old_meta = self.database._loads(row["metadata"], {})
                merged_meta = {**old_meta, **(metadata or {})}
                db.execute(
                    "UPDATE memories SET status=?,confidence=?,source_event_ids=?,metadata=?,updated_at=? WHERE id=?",
                    (status, max(0.0, min(1.0, confidence)), self.database._json(merged_sources), self.database._json(merged_meta), now, memory_id),
                )
            else:
                db.execute(
                    "INSERT INTO memories(id,kind,content,status,confidence,user_id,workspace_id,session_id,content_hash,source_event_ids,metadata,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (memory_id, kind, content, status, max(0.0, min(1.0, confidence)), user_id, workspace_id, session_id,
                     digest, self.database._json(source_event_ids or []), self.database._json(metadata or {}), now, now),
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
        with self.database.connection() as db:
            rows = db.execute(
                f"SELECT * FROM memories WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row, source_event_ids=self.database._loads(row["source_event_ids"], []), metadata=self.database._loads(row["metadata"], {})) for row in rows]


    def set_memory_status(self, memory_id: str, status: str, *, workspace_id: str = "default",
                          user_id: str | None = None) -> bool:
        clauses = ["id=?", "workspace_id=?"]
        params: list[Any] = [memory_id, workspace_id]
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        params[:0] = [status, utc_now()]
        with self.database._lock, self.database.connection() as db:
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
        with self.database._lock, self.database.connection() as db:
            cursor = db.execute(f"DELETE FROM memories WHERE {' AND '.join(clauses)}", params)
            return cursor.rowcount


    def upsert_file(self, rel_path: str, content: str, *, user_id: str = "local", workspace_id: str = "default") -> str:
        now = utc_now()
        digest = stable_hash(content)
        file_id = f"file_{uuid.uuid4().hex}"
        with self.database._lock, self.database.connection() as db:
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
        with self.database._lock, self.database.connection() as db:
            rows = db.execute(f"SELECT id, rel_path FROM files WHERE {' AND '.join(clauses)}", params).fetchall()
            stale = [row["id"] for row in rows if row["rel_path"] not in valid_paths]
            if stale:
                placeholders = ",".join("?" for _ in stale)
                db.execute(f"DELETE FROM files WHERE id IN ({placeholders})", stale)
            return len(stale)
