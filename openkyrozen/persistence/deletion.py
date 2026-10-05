from __future__ import annotations


SESSION_TABLES = (
    "events", "memories", "tasks", "usage_attempts", "usage_resets",
    "history_nodes", "history_heads",
)

WORKSPACE_TABLES = (
    "events", "memories", "files", "tasks", "learning_proposals",
    "learning_feature_flags", "learning_policies", "learning_runtime",
    "scheduled_jobs", "skills", "usage_attempts", "usage_resets",
    "history_nodes", "history_heads",
)


class DeletionRepository:
    """Delete one session or project scope without touching shared scopes."""

    def __init__(self, database):
        self.database = database

    def delete_session(self, *, user_id: str, workspace_id: str, session_id: str) -> None:
        if not session_id:
            raise ValueError("session_id is required")
        self._delete_scopes(SESSION_TABLES, user_id=user_id, workspace_id=workspace_id,
                            session_id=session_id, any_workspace=True)

    def delete_workspace(self, *, user_id: str, workspace_id: str,
                         registry_workspace_id: str) -> set[str]:
        if not workspace_id or workspace_id == registry_workspace_id:
            raise ValueError("cannot delete the global workspace scope")
        db = self.database
        with db._lock, db.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                chat_rows = connection.execute(
                    "SELECT DISTINCT session_id FROM history_heads WHERE user_id=? AND workspace_id=? "
                    "AND session_id LIKE 'chat-%'",
                    (user_id, workspace_id),
                ).fetchall()
                chat_rows += connection.execute(
                    "SELECT DISTINCT session_id FROM events WHERE user_id=? AND workspace_id=? "
                    "AND event_type='tui.chat_metadata' AND session_id LIKE 'chat-%'",
                    (user_id, workspace_id),
                ).fetchall()
                session_ids = {row["session_id"] for row in chat_rows}
                self._delete_scopes(WORKSPACE_TABLES, user_id=user_id,
                                    workspace_id=workspace_id, connection=connection)
                for session_id in session_ids:
                    self._delete_scopes(SESSION_TABLES, user_id=user_id,
                                        workspace_id=workspace_id, session_id=session_id,
                                        connection=connection, any_workspace=True)
                rows = connection.execute(
                    "SELECT id,payload FROM events WHERE user_id=? AND workspace_id=? "
                    "AND event_type='tui.project_opened'",
                    (user_id, registry_workspace_id),
                ).fetchall()
                project_events = [row["id"] for row in rows
                                  if db._loads(row["payload"], {}).get("source_scope_id") == workspace_id]
                if project_events:
                    placeholders = ",".join("?" for _ in project_events)
                    connection.execute(f"DELETE FROM events WHERE id IN ({placeholders})", project_events)
                connection.commit()
                return session_ids
            except Exception:
                connection.rollback()
                raise

    def _delete_scopes(self, tables, *, user_id: str, workspace_id: str,
                       session_id: str | None = None, connection=None,
                       any_workspace: bool = False) -> None:
        db = self.database
        if connection is None:
            with db._lock, db.connection() as active:
                active.execute("BEGIN IMMEDIATE")
                try:
                    self._delete_scopes(tables, user_id=user_id, workspace_id=workspace_id,
                                        session_id=session_id, connection=active,
                                        any_workspace=any_workspace)
                    active.commit()
                except Exception:
                    active.rollback()
                    raise
            return

        for table in tables:
            clauses = [] if any_workspace else ["workspace_id=?"]
            params: list[str] = [] if any_workspace else [workspace_id]
            if table != "skills":
                clauses.append("user_id=?")
                params.append(user_id)
            if session_id is not None:
                clauses.append("session_id=?")
                params.append(session_id)
            if not clauses:
                continue
            connection.execute(f"DELETE FROM {table} WHERE {' AND '.join(clauses)}", params)
