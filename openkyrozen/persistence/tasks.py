from __future__ import annotations

import logging
from typing import Any

from .models import utc_now, stable_hash

class TasksRepository:
    def __init__(self, database):
        self.database = database

    def replace_tasks(self, tasks: list[dict[str, Any]], *, user_id: str = "local",
                      workspace_id: str = "default", session_id: str | None = None) -> None:
        with self.database._lock, self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
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
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    logging.getLogger(__name__).exception("Rollback failed while replacing tasks")
                raise


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

    def get_working_plan(self, *, user_id: str, workspace_id: str, session_id: str) -> dict[str, Any] | None:
        with self.database.connection() as db:
            row = db.execute(
                "SELECT * FROM working_plans WHERE user_id=? AND workspace_id=? AND session_id=? AND status='active' "
                "ORDER BY updated_at DESC LIMIT 1",
                (user_id, workspace_id, session_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def set_working_plan_status(self, plan_id: str, status: str, *, user_id: str,
                                workspace_id: str, session_id: str | None,
                                expected_revision: int | None = None,
                                expected_task_ids: list[str] | tuple[str, ...] | None = None) -> bool:
        if status not in {"completed", "cancelled"}:
            raise ValueError("Working Plan terminal status must be completed or cancelled")
        with self.database._lock, self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                plan = db.execute(
                    "SELECT revision FROM working_plans WHERE id=? AND user_id=? AND workspace_id=? "
                    "AND session_id=? AND status='active'",
                    (plan_id, user_id, workspace_id, session_id or ""),
                ).fetchone()
                if plan is None or (expected_revision is not None and plan["revision"] != expected_revision):
                    db.rollback()
                    return False
                members = [row["task_id"] for row in db.execute(
                    "SELECT task_id FROM working_plan_tasks WHERE plan_id=? ORDER BY position", (plan_id,),
                )]
                if expected_task_ids is not None and members != list(expected_task_ids):
                    db.rollback()
                    return False
                tasks = db.execute(
                    "SELECT status,evidence FROM tasks JOIN working_plan_tasks ON tasks.id=working_plan_tasks.task_id "
                    "WHERE working_plan_tasks.plan_id=? ORDER BY working_plan_tasks.position", (plan_id,),
                ).fetchall()
                if not tasks or any(row["status"] not in {"succeeded", "done", "cancelled"} for row in tasks):
                    db.rollback()
                    return False
                if status == "completed" and any(
                    row["status"] in {"succeeded", "done"} and not any(
                        item.get("success") is True and item.get("acceptance")
                        for item in self.database._loads(row["evidence"], []) if isinstance(item, dict)
                    ) for row in tasks
                ):
                    db.rollback()
                    return False
                if status == "completed" and not any(row["status"] in {"succeeded", "done"} for row in tasks):
                    db.rollback()
                    return False
                cursor = db.execute(
                    "UPDATE working_plans SET status=?,updated_at=? WHERE id=? AND user_id=? "
                    "AND workspace_id=? AND session_id=? AND status='active' AND revision=?",
                    (status, utc_now(), plan_id, user_id, workspace_id, session_id or "", plan["revision"]),
                )
                db.commit()
                return cursor.rowcount == 1
            except Exception:
                db.rollback()
                raise

    def list_working_plan_tasks(self, plan_id: str, *, user_id: str, workspace_id: str,
                                session_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as db:
            rows = db.execute(
                "SELECT tasks.*, working_plan_tasks.plan_id AS plan_id, working_plan_tasks.position AS plan_position "
                "FROM working_plan_tasks JOIN tasks ON tasks.id=working_plan_tasks.task_id "
                "WHERE working_plan_tasks.plan_id=? AND tasks.user_id=? AND tasks.workspace_id=? "
                "AND tasks.session_id IS ? ORDER BY working_plan_tasks.position",
                (plan_id, user_id, workspace_id, session_id or None),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if item["status"] == "done":
                item["status"] = "succeeded"
            for key in ("dependencies", "acceptance", "checkpoint", "evidence"):
                item[key] = self.database._loads(item[key], {} if key == "checkpoint" else [])
            result.append(item)
        return result

    def write_working_plan(self, plan: dict[str, Any], tasks: list[dict[str, Any]], *,
                           user_id: str, workspace_id: str, session_id: str | None) -> None:
        scoped_session = session_id or ""
        with self.database._lock, self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute(
                    "SELECT id FROM working_plans WHERE user_id=? AND workspace_id=? AND session_id=? "
                    "AND status='active'",
                    (user_id, workspace_id, scoped_session),
                ).fetchone()
                if existing is not None and existing["id"] != plan["id"]:
                    raise ValueError("A different Working Plan already owns this session")
                db.execute(
                    "INSERT INTO working_plans(id,user_id,workspace_id,session_id,complexity,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "complexity=excluded.complexity,status=excluded.status,updated_at=excluded.updated_at "
                    "WHERE working_plans.user_id=excluded.user_id AND working_plans.workspace_id=excluded.workspace_id "
                    "AND working_plans.session_id=excluded.session_id",
                    (plan["id"], user_id, workspace_id, scoped_session, plan["complexity"],
                     plan["status"], plan["created_at"], plan["updated_at"]),
                )
                for position, task in enumerate(tasks):
                    row = db.execute(
                        "SELECT id FROM tasks WHERE id=? AND user_id=? AND workspace_id=? AND session_id IS ?",
                        (task["id"], user_id, workspace_id, session_id),
                    ).fetchone()
                    values = (
                        task["id"], task.get("parent_id"), task["description"], task["status"],
                        int(task.get("priority", 0)), self.database._json(task.get("dependencies", [])),
                        self.database._json(task.get("acceptance", [])), int(task.get("attempts", 0)),
                        self.database._json(task.get("checkpoint", {})), self.database._json(task.get("evidence", [])),
                        user_id, workspace_id, session_id, task.get("created_at", plan["created_at"]), task["updated_at"],
                    )
                    if row:
                        db.execute(
                            "UPDATE tasks SET parent_id=?,description=?,status=?,priority=?,dependencies=?,acceptance=?,attempts=?,checkpoint=?,evidence=?,updated_at=? "
                            "WHERE id=? AND user_id=? AND workspace_id=? AND session_id IS ?",
                            values[1:10] + (task["updated_at"], task["id"], user_id, workspace_id, session_id),
                        )
                    else:
                        db.execute(
                            "INSERT INTO tasks(id,parent_id,description,status,priority,dependencies,acceptance,attempts,checkpoint,evidence,user_id,workspace_id,session_id,created_at,updated_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values,
                        )
                    db.execute(
                        "INSERT INTO working_plan_tasks(plan_id,task_id,position) VALUES(?,?,?) "
                        "ON CONFLICT(plan_id,task_id) DO NOTHING",
                        (plan["id"], task["id"], position),
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise

    def apply_working_plan_edit(self, plan: dict[str, Any], tasks: list[dict[str, Any]], *,
                                expected_revision: int, expected_tasks: list[dict[str, Any]],
                                user_id: str, workspace_id: str, session_id: str | None,
                                transaction_event: dict[str, Any] | None = None) -> int:
        """Commit a validated plan snapshot only if its revision and execution state are current."""
        import uuid

        scoped_session = session_id or ""
        now = plan["updated_at"]
        with self.database._lock, self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                expected_proposal = (transaction_event or {}).get("expected_proposal")
                if expected_proposal is not None:
                    latest = db.execute(
                        "SELECT event_type,payload FROM events WHERE user_id=? AND workspace_id=? "
                        "AND session_id=? AND event_type IN ('interaction.plan_proposed', "
                        "'interaction.plan_accepted', 'interaction.plan_cancelled') "
                        "ORDER BY created_at DESC,rowid DESC LIMIT 1",
                        (user_id, workspace_id, scoped_session),
                    ).fetchone()
                    latest_payload = self.database._loads(latest["payload"], {}) if latest else {}
                    if (not latest or latest["event_type"] != "interaction.plan_proposed"
                            or any(latest_payload.get(key) != value
                                   for key, value in expected_proposal.items())):
                        raise ValueError("Strategic Plan proposal changed; reload before accepting")
                current = db.execute(
                    "SELECT * FROM working_plans WHERE user_id=? AND workspace_id=? AND session_id=? AND status='active'",
                    (user_id, workspace_id, scoped_session),
                ).fetchone()
                if current is None:
                    if plan.get("revision", 1) != 1 or expected_revision != 0:
                        raise ValueError("Working Plan revision changed; reload before editing")
                    prior_tasks = []
                    revision = 1
                    db.execute(
                        "INSERT INTO working_plans(id,user_id,workspace_id,session_id,complexity,status,created_at,updated_at,revision) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (plan["id"], user_id, workspace_id, scoped_session, plan["complexity"], plan["status"],
                         plan["created_at"], now, revision),
                    )
                else:
                    if current["id"] != plan["id"] or current["revision"] != expected_revision:
                        raise ValueError("Working Plan revision changed; reload before editing")
                    prior_rows = db.execute(
                        "SELECT tasks.* FROM working_plan_tasks JOIN tasks ON tasks.id=working_plan_tasks.task_id "
                        "WHERE working_plan_tasks.plan_id=? ORDER BY working_plan_tasks.position", (plan["id"],),
                    ).fetchall()
                    prior_tasks = []
                    for row in prior_rows:
                        task = dict(row)
                        for field in ("dependencies", "acceptance", "checkpoint", "evidence"):
                            task[field] = self.database._loads(task[field], {} if field == "checkpoint" else [])
                        prior_tasks.append(task)
                    def progress(rows):
                        return [(row["id"], "succeeded" if row["status"] == "done" else row["status"],
                                 int(row["attempts"]), row["updated_at"],
                                 self.database._json(row.get("evidence", []))) for row in rows]
                    if progress(prior_tasks) != progress(expected_tasks):
                        raise ValueError("Working Plan task progress changed; reload before editing")
                    revision = expected_revision + 1
                    db.execute(
                        "UPDATE working_plans SET complexity=?,status=?,updated_at=?,revision=? WHERE id=? AND revision=?",
                        (plan["complexity"], plan["status"], now, revision, plan["id"], expected_revision),
                    )

                db.execute("DELETE FROM working_plan_tasks WHERE plan_id=?", (plan["id"],))
                for position, task in enumerate(tasks):
                    values = (
                        task["id"], task.get("parent_id"), task["description"], task["status"],
                        int(task.get("priority", 0)), self.database._json(task.get("dependencies", [])),
                        self.database._json(task.get("acceptance", [])), int(task.get("attempts", 0)),
                        self.database._json(task.get("checkpoint", {})), self.database._json(task.get("evidence", [])),
                        user_id, workspace_id, session_id, task.get("created_at", plan["created_at"]), task["updated_at"],
                    )
                    exists = db.execute("SELECT 1 FROM tasks WHERE id=?", (task["id"],)).fetchone()
                    if exists:
                        updated = db.execute(
                            "UPDATE tasks SET parent_id=?,description=?,status=?,priority=?,dependencies=?,acceptance=?,attempts=?,checkpoint=?,evidence=?,updated_at=? "
                            "WHERE id=? AND user_id=? AND workspace_id=? AND session_id IS ?",
                            values[1:10] + (task["updated_at"], task["id"], user_id, workspace_id, session_id),
                        )
                        if updated.rowcount != 1:
                            raise ValueError("Working Plan task scope changed; reload before editing")
                    else:
                        db.execute(
                            "INSERT INTO tasks(id,parent_id,description,status,priority,dependencies,acceptance,attempts,checkpoint,evidence,user_id,workspace_id,session_id,created_at,updated_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values,
                        )
                    db.execute(
                        "INSERT INTO working_plan_tasks(plan_id,task_id,position) VALUES(?,?,?)",
                        (plan["id"], task["id"], position),
                    )

                event_payload = {"plan_id": plan["id"], "revision": revision, "status": plan["status"],
                                 "task_ids": [task["id"] for task in tasks]}
                db.execute(
                    "INSERT INTO events(id,event_type,payload,user_id,workspace_id,session_id,task_id,created_at) "
                    "VALUES(?,?,?,?,?,?,NULL,?)",
                    (f"evt_{uuid.uuid4().hex}",
                     "working_plan.cancelled" if plan["status"] == "cancelled" else "working_plan.updated",
                     self.database._json(event_payload), user_id, workspace_id, session_id, now),
                )
                if transaction_event is not None:
                    db.execute(
                        "INSERT INTO events(id,event_type,payload,user_id,workspace_id,session_id,task_id,created_at) "
                        "VALUES(?,?,?,?,?,?,NULL,?)",
                        (f"evt_{uuid.uuid4().hex}", transaction_event["event_type"],
                         self.database._json(transaction_event["payload"]),
                         user_id, workspace_id, scoped_session, now),
                    )
                db.commit()
                return revision
            except Exception:
                db.rollback()
                raise



    def write_task(self, task: dict[str, Any], *, user_id: str, workspace_id: str, session_id: str | None,
                   expected_status: str | None = None) -> bool:
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
                status_filter = " AND status=?" if expected_status is not None else ""
                cursor = db.execute(
                    "UPDATE tasks SET parent_id=?,description=?,status=?,priority=?,dependencies=?,acceptance=?,attempts=?,checkpoint=?,evidence=?,updated_at=? "
                    "WHERE id=? AND user_id=? AND workspace_id=? AND session_id IS ?" + status_filter,
                    values[1:10] + (now, task["id"], user_id, workspace_id, session_id)
                    + ((expected_status,) if expected_status is not None else ()),
                )
                return cursor.rowcount == 1
            if expected_status is not None:
                return False
            else:
                db.execute(
                    "INSERT INTO tasks(id,parent_id,description,status,priority,dependencies,acceptance,attempts,checkpoint,evidence,user_id,workspace_id,session_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
                return True


    def claim_task(self, *, user_id: str, workspace_id: str, session_id: str | None) -> dict[str, Any] | None:
        """Atomically claim one pending task in this exact durable scope."""
        with self.database._lock, self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                active_plan = db.execute(
                    "SELECT id FROM working_plans WHERE user_id=? AND workspace_id=? AND session_id=? AND status='active'",
                    (user_id, workspace_id, session_id or ""),
                ).fetchone()
                if active_plan is not None:
                    rows = db.execute(
                        "SELECT tasks.* FROM working_plan_tasks JOIN tasks ON tasks.id=working_plan_tasks.task_id "
                        "WHERE working_plan_tasks.plan_id=? ORDER BY working_plan_tasks.position",
                        (active_plan["id"],),
                    ).fetchall()
                else:
                    rows = db.execute(
                        "SELECT * FROM tasks WHERE user_id=? AND workspace_id=? AND session_id IS ? "
                        "ORDER BY priority DESC, updated_at, id", (user_id, workspace_id, session_id),
                    ).fetchall()
                statuses = {item["id"]: ("succeeded" if item["status"] == "done" else item["status"])
                            for item in db.execute(
                    "SELECT id,status FROM tasks WHERE user_id=? AND workspace_id=? AND session_id IS ?",
                    (user_id, workspace_id, session_id),
                ).fetchall()}
                row = None
                for candidate in rows:
                    if candidate["status"] != "pending":
                        continue
                    dependencies = self.database._loads(candidate["dependencies"], [])
                    if all(statuses.get(dependency) == "succeeded" for dependency in dependencies):
                        row = candidate
                        break
                if row is None:
                    db.commit()
                    return None
                now = utc_now()
                claimed = db.execute(
                    "UPDATE tasks SET status=?,attempts=attempts+1,updated_at=? WHERE id=? AND user_id=? "
                    "AND workspace_id=? AND session_id IS ? AND status=?",
                    ("running", now, row["id"], user_id, workspace_id, session_id, "pending"),
                ).rowcount
                if claimed != 1:
                    db.commit()
                    return None
                db.commit()
            except Exception:
                db.rollback()
                raise
        task = dict(row)
        task["status"] = "running"
        task["attempts"] = int(task.get("attempts", 0)) + 1
        for key in ("dependencies", "acceptance", "checkpoint", "evidence"):
            task[key] = self.database._loads(task[key], [] if key in {"dependencies", "acceptance", "evidence"} else {})
        task["updated_at"] = now
        return task
