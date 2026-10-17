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

    def append_conversation_entries(self, entries: list[dict[str, Any]], *, expected_head=...):
        """Append one conversation import as a single head-checked transaction."""
        if not entries:
            return ()
        first = entries[0]
        scope = (first["user_id"], first["workspace_id"], first["session_id"])
        from openkyrozen.agent.conversation import ConversationHistoryError
        with self.database._lock, self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM events WHERE event_type='conversation.entry' AND user_id=? "
                "AND workspace_id=? AND session_id=? ORDER BY rowid DESC LIMIT 1", scope,
            ).fetchone()
            head = row["id"] if row else None
            if expected_head is not ... and expected_head != head:
                db.rollback()
                raise ConversationHistoryError("Conversation history head changed")
            try:
                for entry in entries:
                    if ((entry["user_id"], entry["workspace_id"], entry["session_id"]) != scope
                            or entry.get("parent_id") != head):
                        raise ConversationHistoryError("Conversation entry scope or parent is stale")
                    existing = db.execute("SELECT 1 FROM events WHERE id=?", (entry["id"],)).fetchone()
                    if existing:
                        raise ConversationHistoryError("Conversation entry ID conflicts with an existing entry")
                    db.execute(
                        "INSERT INTO events(id,event_type,payload,user_id,workspace_id,session_id,task_id,created_at) "
                        "VALUES(?,'conversation.entry',?,?,?,?,?,?)",
                        (entry["id"], self.database._json(entry), *scope, entry.get("task_id"),
                         entry.get("created_at") or utc_now()),
                    )
                    head = entry["id"]
                db.commit()
            except Exception:
                db.rollback()
                raise
        return tuple(entry["id"] for entry in entries)

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

    def append_history_rollback(self, entries: list[dict[str, Any]], events: list[dict[str, Any]], *,
                               user_id: str, workspace_id: str, session_id: str,
                               expected_head: str | None, checkpoint: dict[str, Any] | None = None) -> None:
        """Commit branch selection and its interaction events as one visible state change."""
        scope = (user_id, workspace_id, session_id)
        with self.database._lock, self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM events WHERE event_type='conversation.entry' AND user_id=? "
                "AND workspace_id=? AND session_id=? ORDER BY rowid DESC LIMIT 1", scope,
            ).fetchone()
            head = row["id"] if row else None
            if head != expected_head:
                db.rollback()
                from openkyrozen.agent.conversation import ConversationHistoryError
                raise ConversationHistoryError("Conversation history head changed")
            try:
                for entry in entries:
                    if (entry["user_id"], entry["workspace_id"], entry["session_id"]) != scope or entry.get("parent_id") != head:
                        from openkyrozen.agent.conversation import ConversationHistoryError
                        raise ConversationHistoryError("Conversation entry scope or parent is stale")
                    db.execute(
                        "INSERT INTO events(id,event_type,payload,user_id,workspace_id,session_id,task_id,created_at) "
                        "VALUES(?,'conversation.entry',?,?,?,?,?,?)",
                        (entry["id"], self.database._json(entry), *scope, entry.get("task_id"),
                         entry.get("created_at") or utc_now()),
                    )
                    head = entry["id"]
                if checkpoint is not None:
                    checkpoint_id = f"checkpoint_{checkpoint['node_id']}"
                    db.execute(
                        "INSERT INTO events(id,event_type,payload,user_id,workspace_id,session_id,task_id,created_at) "
                        "VALUES(?,'conversation.checkpoint',?,?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload "
                        "WHERE events.event_type='conversation.checkpoint' AND events.user_id=excluded.user_id "
                        "AND events.workspace_id=excluded.workspace_id AND events.session_id=excluded.session_id",
                        (checkpoint_id, self.database._json(checkpoint), *scope, None, utc_now()),
                    )
                for event in events:
                    db.execute(
                        "INSERT INTO events(id,event_type,payload,user_id,workspace_id,session_id,task_id,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (f"evt_{uuid.uuid4().hex}", event["event_type"], self.database._json(event["payload"]),
                         *scope, None, utc_now()),
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise


    def append_plan_auto_accepted(self, payload: dict[str, Any], *, expected_previous: dict[str, Any],
                                  user_id: str, workspace_id: str, session_id: str) -> str:
        """Append a narrowed-plan acceptance only if its accepted predecessor is current."""
        event_id = f"evt_{uuid.uuid4().hex}"
        accepted_types = {"interaction.plan_accepted", "interaction.plan_auto_accepted"}
        relevant_types = (*accepted_types, "interaction.plan_proposed", "interaction.plan_cancelled",
                          "interaction.plan_completed", "history.restored")
        with self.database._lock, self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                rows = db.execute(
                    "SELECT event_type,payload FROM events WHERE user_id=? AND workspace_id=? "
                    "AND session_id=? AND event_type IN (?,?,?,?,?,?) ORDER BY created_at,rowid",
                    (user_id, workspace_id, session_id, *relevant_types),
                ).fetchall()
                current = None
                pending = False
                for row in rows:
                    event_type = row["event_type"]
                    event_payload = self.database._loads(row["payload"], {})
                    if event_type == "history.restored":
                        interaction = event_payload.get("interaction", {})
                        restored = interaction.get("executing_plan") if isinstance(interaction, dict) else None
                        if isinstance(restored, dict) and restored.get("plan_id") == expected_previous.get("plan_id"):
                            current = restored
                            pending = isinstance(interaction.get("pending_plan"), dict)
                        continue
                    if event_payload.get("plan_id") != expected_previous.get("plan_id"):
                        continue
                    if event_type in accepted_types:
                        current, pending = event_payload, False
                    elif event_type == "interaction.plan_proposed":
                        pending = True
                    elif event_type == "interaction.plan_cancelled":
                        pending = False
                    elif (event_type == "interaction.plan_completed"
                          and event_payload.get("status") in {"completed", "cancelled"}):
                        current, pending = None, False
                if (pending or not current
                        or any(current.get(key) != expected_previous.get(key)
                               for key in ("plan_id", "version", "fingerprint"))):
                    raise ValueError("Strategic Plan changed; reload before accepting")
                db.execute(
                    "INSERT INTO events(id,event_type,payload,user_id,workspace_id,session_id,task_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (event_id, "interaction.plan_auto_accepted", self.database._json(payload), user_id,
                     workspace_id, session_id, None, utc_now()),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return event_id

    def append_plan_completed(self, payload: dict[str, Any], *, user_id: str,
                              workspace_id: str, session_id: str) -> str:
        event_id = f"evt_{uuid.uuid4().hex}"
        accepted_types = {"interaction.plan_accepted", "interaction.plan_auto_accepted"}
        relevant_types = (*accepted_types, "interaction.plan_proposed", "interaction.plan_cancelled",
                          "interaction.plan_completed", "history.restored")
        with self.database._lock, self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                rows = db.execute(
                    "SELECT event_type,payload FROM events WHERE user_id=? AND workspace_id=? "
                    "AND session_id=? AND event_type IN (?,?,?,?,?,?) ORDER BY created_at,rowid",
                    (user_id, workspace_id, session_id, *relevant_types),
                ).fetchall()
                current = None
                pending = False
                for row in rows:
                    event_type = row["event_type"]
                    event_payload = self.database._loads(row["payload"], {})
                    if event_type == "history.restored":
                        interaction = event_payload.get("interaction", {})
                        if isinstance(interaction, dict):
                            current = interaction.get("executing_plan")
                            pending = isinstance(interaction.get("pending_plan"), dict)
                        continue
                    if event_type in accepted_types:
                        current, pending = event_payload, False
                    elif event_type == "interaction.plan_proposed":
                        if event_payload.get("revision_materiality") != "within_bounds":
                            pending = True
                    elif event_type == "interaction.plan_cancelled":
                        pending = False
                    elif (event_type == "interaction.plan_completed"
                          and event_payload.get("status") in {"completed", "cancelled"}
                          and current and all(event_payload.get(key) == current.get(key)
                                               for key in ("plan_id", "version", "fingerprint"))):
                        current = None
                if (pending or not current
                        or any(current.get(key) != payload.get(key)
                               for key in ("plan_id", "version", "fingerprint"))):
                    raise ValueError("Strategic Plan changed; reload before recording completion")
                db.execute(
                    "INSERT INTO events(id,event_type,payload,user_id,workspace_id,session_id,task_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (event_id, "interaction.plan_completed", self.database._json(payload), user_id,
                     workspace_id, session_id, None, utc_now()),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return event_id


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

    def list_interaction_events(self, *, workspace_id: str, session_id: str,
                                user_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE workspace_id=? AND session_id=? AND user_id=? "
                "AND (event_type LIKE 'interaction.%' OR event_type='history.restored') "
                "ORDER BY created_at,rowid",
                (workspace_id, session_id, user_id),
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
