from __future__ import annotations

import uuid
from typing import Any

from .models import utc_now, stable_hash

class EventsRepository:
    def __init__(self, database):
        self.database = database

    def append_event(self, event_type: str, payload: Any, *, user_id: str = "local",
                     workspace_id: str = "default", session_id: str | None = None,
                     task_id: str | None = None) -> str:
        event_id = f"evt_{uuid.uuid4().hex}"
        with self.database._lock, self.database.connection() as db:
            db.execute(
                "INSERT INTO events(id,event_type,payload,user_id,workspace_id,session_id,task_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (event_id, event_type, self.database._json(payload), user_id, workspace_id, session_id, task_id, utc_now()),
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
        with self.database.connection() as db:
            rows = db.execute(
                f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, rowid DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row, payload=self.database._loads(row["payload"], {})) for row in rows]


    def list_sessions(self, *, workspace_id: str = "default", limit: int = 100,
                      user_id: str | None = None) -> list[dict[str, Any]]:
        clauses = ["workspace_id=?", "session_id IS NOT NULL"]
        params: list[Any] = [workspace_id]
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        params.append(max(1, min(limit, 1000)))
        with self.database.connection() as db:
            rows = db.execute(
                "SELECT session_id, MAX(created_at) AS updated_at, COUNT(*) AS event_count FROM events "
                f"WHERE {' AND '.join(clauses)} GROUP BY session_id ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]
