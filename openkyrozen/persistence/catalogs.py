from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .models import utc_now, stable_hash

class CatalogsRepository:
    def __init__(self, database):
        self.database = database

    def upsert_schedule(self, job_id: str, name: str, *, next_run_at: str,
                        interval_seconds: float | None = None, run_at: str | None = None,
                        payload: dict[str, Any] | None = None, enabled: bool = True,
                        workspace_id: str = "default", user_id: str = "local") -> str:
        now = utc_now()
        with self.database._lock, self.database.connection() as db:
            db.execute(
                "INSERT INTO scheduled_jobs(id,name,interval_seconds,run_at,next_run_at,enabled,payload,user_id,workspace_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,interval_seconds=excluded.interval_seconds,run_at=excluded.run_at,next_run_at=excluded.next_run_at,enabled=excluded.enabled,payload=excluded.payload,updated_at=excluded.updated_at",
                (job_id, name, interval_seconds, run_at, next_run_at, int(enabled), self.database._json(payload or {}), user_id, workspace_id, now, now),
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
        with self.database.connection() as db:
            rows = db.execute(f"SELECT * FROM scheduled_jobs WHERE {' AND '.join(clauses)} ORDER BY next_run_at LIMIT ?", params).fetchall()
        return [dict(row, enabled=bool(row["enabled"]), payload=self.database._loads(row["payload"], {})) for row in rows]


    def claim_due_schedules(self, *, now: str, workspace_id: str = "default", limit: int = 20,
                            user_id: str | None = None) -> list[dict[str, Any]]:
        clauses = ["workspace_id=?"]
        params: list[Any] = [workspace_id]
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        clauses.extend(["enabled=1", "next_run_at<=?"])
        params.extend([now, max(1, min(limit, 100))])
        with self.database._lock, self.database.connection() as db:
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
                claimed.append(dict(row, payload=self.database._loads(row["payload"], {}), next_run_at=next_run_at))
            return claimed


    def set_schedule_enabled(self, job_id: str, enabled: bool, *, workspace_id: str = "default",
                             user_id: str | None = None) -> bool:
        clauses = ["id=?", "workspace_id=?"]
        params: list[Any] = [job_id, workspace_id]
        if user_id is not None:
            clauses.append("user_id=?")
            params.append(user_id)
        params[:0] = [int(enabled), utc_now()]
        with self.database._lock, self.database.connection() as db:
            return db.execute(
                f"UPDATE scheduled_jobs SET enabled=?,updated_at=? WHERE {' AND '.join(clauses)}",
                params,
            ).rowcount == 1


    def upsert_skill(self, *, name: str, version: str, path: str, description: str,
                     permissions: list[str], status: str = "candidate", source: str = "local",
                     manifest: dict[str, Any] | None = None, workspace_id: str = "default") -> str:
        skill_id = f"skill_{stable_hash(f'{workspace_id}:{name}:{version}')[:24]}"
        now = utc_now()
        with self.database._lock, self.database.connection() as db:
            db.execute(
                "INSERT INTO skills(id,name,version,path,description,permissions,status,source,manifest,workspace_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(name,version,workspace_id) DO UPDATE SET path=excluded.path,description=excluded.description,permissions=excluded.permissions,status=excluded.status,source=excluded.source,manifest=excluded.manifest,updated_at=excluded.updated_at",
                (skill_id, name, version, path, description, self.database._json(sorted(set(permissions))), status, source,
                 self.database._json(manifest or {}), workspace_id, now, now),
            )
        return skill_id


    def list_skills(self, *, workspace_id: str = "default", status: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        clauses = ["workspace_id=?"]
        params: list[Any] = [workspace_id]
        if status:
            clauses.append("status=?")
            params.append(status)
        params.append(max(1, min(limit, 5000)))
        with self.database.connection() as db:
            rows = db.execute(f"SELECT * FROM skills WHERE {' AND '.join(clauses)} ORDER BY name,version LIMIT ?", params).fetchall()
        return [dict(row, permissions=self.database._loads(row["permissions"], []), manifest=self.database._loads(row["manifest"], {})) for row in rows]


    def set_skill_status(self, skill_id: str, status: str, *, workspace_id: str = "default") -> bool:
        with self.database._lock, self.database.connection() as db:
            return db.execute("UPDATE skills SET status=?,updated_at=? WHERE id=? AND workspace_id=?",
                              (status, utc_now(), skill_id, workspace_id)).rowcount == 1
