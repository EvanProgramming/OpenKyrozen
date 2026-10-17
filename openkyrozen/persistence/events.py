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
