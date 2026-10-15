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

    def conversation_head(self, *, user_id: str, workspace_id: str, session_id: str) -> str | None:
        with self.database.connection() as db:
            row = db.execute(
                "SELECT id FROM events WHERE event_type='conversation.entry' AND user_id=? "
                "AND workspace_id=? AND session_id=? ORDER BY rowid DESC LIMIT 1",
                (user_id, workspace_id, session_id),
            ).fetchone()
        return row["id"] if row else None

    def append_conversation_entry(self, entry: dict[str, Any], *, expected_head=...):
        """Append private journal content atomically; generic event APIs cannot read it."""
        entry_id = entry["id"]
        payload = self.database._json(entry)
        scope = (entry["user_id"], entry["workspace_id"], entry["session_id"])
        with self.database._lock, self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT event_type,payload,user_id,workspace_id,session_id FROM events WHERE id=?",
                                  (entry_id,)).fetchone()
            if existing:
                same_scope = (existing["user_id"], existing["workspace_id"], existing["session_id"]) == scope
                if existing["event_type"] == "conversation.entry" and same_scope and existing["payload"] == payload:
                    db.commit()
                    return entry_id
                db.rollback()
                from openkyrozen.agent.conversation import ConversationHistoryError
                raise ConversationHistoryError("Conversation entry ID conflicts with an existing entry")
            head = db.execute(
                "SELECT id FROM events WHERE event_type='conversation.entry' AND user_id=? "
                "AND workspace_id=? AND session_id=? ORDER BY rowid DESC LIMIT 1", scope,
            ).fetchone()
            current_head = head["id"] if head else None
            if expected_head is not ... and expected_head != current_head:
                db.rollback()
                from openkyrozen.agent.conversation import ConversationHistoryError
                raise ConversationHistoryError("Conversation history head changed")
            if entry.get("parent_id") != current_head:
                db.rollback()
                from openkyrozen.agent.conversation import ConversationHistoryError
                raise ConversationHistoryError("Conversation entry parent is stale")
            from .models import utc_now
            db.execute(
                "INSERT INTO events(id,event_type,payload,user_id,workspace_id,session_id,task_id,created_at) "
                "VALUES(?,'conversation.entry',?,?,?,?,?,?)",
                (entry_id, payload, *scope, entry.get("task_id"), entry["created_at"] or utc_now()),
            )
            db.commit()
        return entry_id

    def list_conversation_entries(self, *, user_id: str, workspace_id: str, session_id: str):
        with self.database.connection() as db:
            rows = db.execute(
                "SELECT payload FROM events WHERE event_type='conversation.entry' AND user_id=? "
                "AND workspace_id=? AND session_id=? ORDER BY rowid",
                (user_id, workspace_id, session_id),
            ).fetchall()
        return [self.database._loads(row["payload"], {}) for row in rows]

    def conversation_checkpoint(self, node_id: str, *, user_id: str, workspace_id: str, session_id: str):
        with self.database.connection() as db:
            row = db.execute(
                "SELECT payload FROM events WHERE id=? AND event_type='conversation.checkpoint' "
                "AND user_id=? AND workspace_id=? AND session_id=?",
                (f"checkpoint_{node_id}", user_id, workspace_id, session_id),
            ).fetchone()
        return self.database._loads(row["payload"], {}) if row else None


    def list_events(self, event_type: str | None = None, *, limit: int | None = 100,
                    workspace_id: str = "default", session_id: str | None = None,
                    user_id: str | None = None) -> list[dict[str, Any]]:
        clauses = ["workspace_id=?", "event_type NOT LIKE 'conversation.%'"]
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
        if limit is not None:
            params.append(max(1, min(limit, 10000)))
        with self.database.connection() as db:
            rows = db.execute(
                f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, rowid DESC" + (" LIMIT ?" if limit is not None else ""),
                params,
            ).fetchall()
        return [dict(row, payload=self.database._loads(row["payload"], {})) for row in rows]


    def list_sessions(self, *, workspace_id: str = "default", limit: int = 100,
                      user_id: str | None = None) -> list[dict[str, Any]]:
        clauses = ["workspace_id=?", "session_id IS NOT NULL", "event_type NOT LIKE 'conversation.%'"]
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
