from __future__ import annotations

from typing import Any

from .models import utc_now, stable_hash

class HistoryRepository:
    def __init__(self, database):
        self.database = database

    def history_head(self, *, user_id: str = "local", workspace_id: str = "default",
                     session_id: str) -> str | None:
        with self.database.connection() as db:
            row = db.execute(
                "SELECT head_id FROM history_heads WHERE user_id=? AND workspace_id=? AND session_id=?",
                (user_id, workspace_id, session_id),
            ).fetchone()
        return row["head_id"] if row else None


    def history_node(self, node_id: str, *, user_id: str = "local", workspace_id: str = "default",
                     session_id: str | None = None) -> dict[str, Any] | None:
        clauses = ["id=?", "user_id=?", "workspace_id=?"]
        params: list[Any] = [node_id, user_id, workspace_id]
        if session_id is not None:
            clauses.append("session_id=?")
            params.append(session_id)
        with self.database.connection() as db:
            row = db.execute(
                f"SELECT * FROM history_nodes WHERE {' AND '.join(clauses)}", params,
            ).fetchone()
        if row is None:
            return None
        return self._decode_history_row(dict(row))


    def list_history_nodes(self, *, user_id: str = "local", workspace_id: str = "default",
                           session_id: str, include_recovery: bool = False,
                           limit: int = 10000) -> list[dict[str, Any]]:
        clauses = ["user_id=?", "workspace_id=?", "session_id=?"]
        params: list[Any] = [user_id, workspace_id, session_id]
        if not include_recovery:
            clauses.append("kind != 'recovery'")
        params.append(max(1, min(int(limit), 10000)))
        with self.database.connection() as db:
            rows = db.execute(
                f"SELECT * FROM history_nodes WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at ASC, id ASC LIMIT ?", params,
            ).fetchall()
        return [self._decode_history_row(dict(row)) for row in rows]


    def list_history_sessions(self, *, user_id: str = "local",
                              workspace_id: str = "default", limit: int = 100) -> list[dict[str, Any]]:
        """Return conversation heads without introducing a second session store."""
        with self.database.connection() as db:
            rows = db.execute(
                "SELECT h.session_id,h.updated_at,n.user_message,n.conversation "
                "FROM history_heads h JOIN history_nodes n ON n.id=h.head_id "
                "WHERE h.user_id=? AND h.workspace_id=? "
                "ORDER BY h.updated_at DESC LIMIT ?",
                (user_id, workspace_id, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [dict(row, conversation=self.database._loads(row["conversation"], [])) for row in rows]


    def _decode_history_row(self, row: dict[str, Any]) -> dict[str, Any]:
        for field, fallback in (("conversation", []), ("interaction", {}),
                                ("tasks", []), ("file_summary", {})):
            row[field] = self.database._loads(row.get(field, ""), fallback)
        return row


    def insert_history_node(self, node: dict[str, Any], *, set_head: bool = False,
                            expected_head: str | None = None) -> bool:
        fields = (
            node["id"], node.get("parent_id"), node.get("kind", "turn"), node.get("summary", ""),
            node.get("user_message", ""), node.get("assistant_message", ""), self.database._json(node.get("conversation", [])),
            self.database._json(node.get("interaction", {})), self.database._json(node.get("tasks", [])), node["snapshot_relpath"],
            self.database._json(node.get("file_summary", {})), node.get("user_id", "local"),
            node.get("workspace_id", "default"), node["session_id"], node["created_at"],
        )
        with self.database._lock, self.database.connection() as db:
            if set_head:
                current = db.execute(
                    "SELECT head_id FROM history_heads WHERE user_id=? AND workspace_id=? AND session_id=?",
                    (node.get("user_id", "local"), node.get("workspace_id", "default"), node["session_id"]),
                ).fetchone()
                current_id = current["head_id"] if current else None
                if expected_head is not None and current_id != expected_head:
                    return False
            db.execute(
                "INSERT INTO history_nodes(id,parent_id,kind,summary,user_message,assistant_message,conversation,"
                "interaction,tasks,snapshot_relpath,file_summary,user_id,workspace_id,session_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", fields,
            )
            if set_head:
                db.execute(
                    "INSERT INTO history_heads(user_id,workspace_id,session_id,head_id,updated_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(user_id,workspace_id,session_id) DO UPDATE SET head_id=excluded.head_id,updated_at=excluded.updated_at",
                    (node.get("user_id", "local"), node.get("workspace_id", "default"), node["session_id"],
                     node["id"], node["created_at"]),
                )
        return True


    def set_history_head(self, node_id: str, *, user_id: str = "local", workspace_id: str = "default",
                         session_id: str, expected_head: str | None = None) -> bool:
        now = utc_now()
        with self.database._lock, self.database.connection() as db:
            current = db.execute(
                "SELECT head_id FROM history_heads WHERE user_id=? AND workspace_id=? AND session_id=?",
                (user_id, workspace_id, session_id),
            ).fetchone()
            current_id = current["head_id"] if current else None
            if expected_head is not None and current_id != expected_head:
                return False
            db.execute(
                "INSERT INTO history_heads(user_id,workspace_id,session_id,head_id,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(user_id,workspace_id,session_id) DO UPDATE SET head_id=excluded.head_id,updated_at=excluded.updated_at",
                (user_id, workspace_id, session_id, node_id, now),
            )
        return True
