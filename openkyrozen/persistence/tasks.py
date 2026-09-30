from __future__ import annotations

from typing import Any

from .models import utc_now, stable_hash

class TasksRepository:
    def __init__(self, database):
        self.database = database

    def replace_tasks(self, tasks: list[dict[str, Any]], *, user_id: str = "local",
                      workspace_id: str = "default", session_id: str | None = None) -> None:
        with self.database._lock, self.database.connection() as db:
            clauses = ["user_id=?", "workspace_id=?", "session_id IS ?"]
            params: list[Any] = [user_id, workspace_id, session_id]
            db.execute(f"DELETE FROM tasks WHERE {' AND '.join(clauses)}", params)
            for task in tasks:
                now = task.get("updated_at") or utc_now()
                db.execute(
                    "INSERT INTO tasks(id,parent_id,description,status,priority,dependencies,acceptance,attempts,checkpoint,evidence,"
                    "user_id,workspace_id,session_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (task["id"], task.get("parent_id"), task.get("description", ""), task.get("status", "pending"),
                     int(task.get("priority", 0)), self.database._json(task.get("dependencies", [])),
                     self.database._json(task.get("acceptance", [])), int(task.get("attempts", 0)),
                     self.database._json(task.get("checkpoint", {})), self.database._json(task.get("evidence", [])),
                     user_id, workspace_id, session_id, task.get("created_at", now), now),
                )


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
        with self.database.connection() as db:
            rows = db.execute(
                f"SELECT * FROM tasks WHERE {' AND '.join(clauses)} ORDER BY priority DESC, updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if item["status"] == "done":
                item["status"] = "succeeded"
            item["dependencies"] = self.database._loads(item["dependencies"], [])
            item["acceptance"] = self.database._loads(item["acceptance"], [])
            item["checkpoint"] = self.database._loads(item["checkpoint"], {})
            item["evidence"] = self.database._loads(item["evidence"], [])
            result.append(item)
        return result



    def write_task(self, task: dict[str, Any], *, user_id: str, workspace_id: str, session_id: str | None) -> None:
        now = task.get("updated_at") or utc_now()
        with self.database._lock, self.database.connection() as db:
            row = db.execute(
                "SELECT id FROM tasks WHERE id=? AND user_id=? AND workspace_id=? AND session_id IS ?",
                (task["id"], user_id, workspace_id, session_id),
            ).fetchone()
            values = (
                task["id"], task.get("parent_id"), task["description"], task["status"],
                int(task.get("priority", 0)), self.database._json(task.get("dependencies", [])),
                self.database._json(task.get("acceptance", [])), int(task.get("attempts", 0)),
                self.database._json(task.get("checkpoint", {})), self.database._json(task.get("evidence", [])),
                user_id, workspace_id, session_id, task.get("created_at", now), now,
            )
            if row:
                db.execute(
                    "UPDATE tasks SET parent_id=?,description=?,status=?,priority=?,dependencies=?,acceptance=?,attempts=?,checkpoint=?,evidence=?,updated_at=? "
                    "WHERE id=? AND user_id=? AND workspace_id=? AND session_id IS ?",
                    values[1:10] + (now, task["id"], user_id, workspace_id, session_id),
                )
            else:
                db.execute(
                    "INSERT INTO tasks(id,parent_id,description,status,priority,dependencies,acceptance,attempts,checkpoint,evidence,user_id,workspace_id,session_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )


    def claim_task(self, *, user_id: str, workspace_id: str, session_id: str | None) -> dict[str, Any] | None:
        """Atomically claim one pending task in this exact durable scope."""
        with self.database._lock, self.database.connection() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE user_id=? AND workspace_id=? AND session_id IS ? AND status=? "
                "ORDER BY priority DESC, updated_at LIMIT 1",
                (user_id, workspace_id, session_id, "pending"),
            ).fetchone()
            if row is None:
                return None
            now = utc_now()
            claimed = db.execute(
                "UPDATE tasks SET status=?,attempts=attempts+1,updated_at=? WHERE id=? AND user_id=? "
                "AND workspace_id=? AND session_id IS ? AND status=?",
                ("running", now, row["id"], user_id, workspace_id, session_id, "pending"),
            ).rowcount
            if claimed != 1:
                return None
        task = dict(row)
        task["status"] = "running"
        task["attempts"] = int(task.get("attempts", 0)) + 1
        for key in ("dependencies", "acceptance", "checkpoint", "evidence"):
            task[key] = self.database._loads(task[key], [] if key in {"dependencies", "acceptance", "evidence"} else {})
        task["updated_at"] = now
        return task
