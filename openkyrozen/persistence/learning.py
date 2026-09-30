from __future__ import annotations

import uuid
from typing import Any

from .models import utc_now, stable_hash

class LearningRepository:
    def __init__(self, database):
        self.database = database

    def create_proposal(self, kind: str, content: str, *, confidence: float = 0.0,
                        evidence: list[str] | None = None, workspace_id: str = "default",
                        user_id: str = "local") -> str:
        proposal_id = f"proposal_{uuid.uuid4().hex}"
        now = utc_now()
        with self.database._lock, self.database.connection() as db:
            db.execute(
                "INSERT INTO learning_proposals(id,kind,content,confidence,evidence,user_id,workspace_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (proposal_id, kind, content, max(0.0, min(1.0, confidence)), self.database._json(evidence or []), user_id, workspace_id, now, now),
            )
        return proposal_id


    def update_proposal(self, proposal_id: str, *, status: str, validation: dict[str, Any] | None = None,
                        confidence: float | None = None, workspace_id: str | None = None,
                        user_id: str | None = None) -> bool:
        values = [status, self.database._json(validation or {}), utc_now()]
        query = "UPDATE learning_proposals SET status=?,validation=?,updated_at=?"
        if confidence is not None:
            query += ",confidence=?"
            values.append(max(0.0, min(1.0, confidence)))
        query += " WHERE id=?"
        values.append(proposal_id)
        if workspace_id is not None:
            query += " AND workspace_id=?"
            values.append(workspace_id)
        if user_id is not None:
            query += " AND user_id=?"
            values.append(user_id)
        with self.database._lock, self.database.connection() as db:
            return db.execute(query, values).rowcount == 1


    def add_proposal_evidence(self, proposal_id: str, evidence_id: str) -> int:
        with self.database._lock, self.database.connection() as db:
            row = db.execute("SELECT evidence FROM learning_proposals WHERE id=?", (proposal_id,)).fetchone()
            if not row:
                return 0
            evidence = self.database._loads(row["evidence"], [])
            if evidence_id not in evidence:
                evidence.append(evidence_id)
            db.execute("UPDATE learning_proposals SET evidence=?,updated_at=? WHERE id=?",
                       (self.database._json(evidence), utc_now(), proposal_id))
            return len(evidence)


    def list_learning_feature_flags(self, *, user_id: str = "local",
                                    workspace_id: str = "default") -> dict[str, bool]:
        """Return persisted self-learning feature switches for one scope."""
        with self.database.connection() as db:
            rows = db.execute(
                "SELECT name, enabled FROM learning_feature_flags "
                "WHERE user_id=? AND workspace_id=?",
                (user_id, workspace_id),
            ).fetchall()
        return {str(row["name"]): bool(row["enabled"]) for row in rows}


    def set_learning_feature_flag(self, name: str, enabled: bool, *,
                                  user_id: str = "local",
                                  workspace_id: str = "default") -> None:
        """Persist one self-learning feature switch transactionally."""
        with self.database._lock, self.database.connection() as db:
            db.execute(
                "INSERT INTO learning_feature_flags(name,user_id,workspace_id,enabled,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(name,user_id,workspace_id) DO UPDATE SET "
                "enabled=excluded.enabled,updated_at=excluded.updated_at",
                (str(name), user_id, workspace_id, int(bool(enabled)), utc_now()),
            )


    def get_learning_policy(self, *, user_id: str = "local",
                            workspace_id: str = "default") -> str:
        """Legacy policy accessor; new callers should use get_learning_runtime."""
        with self.database.connection() as db:
            row = db.execute(
                "SELECT mode FROM learning_policies WHERE user_id=? AND workspace_id=?",
                (user_id, workspace_id),
            ).fetchone()
        return str(row["mode"]) if row else "setup_required"


    def set_learning_policy(self, mode: str, *, user_id: str = "local",
                            workspace_id: str = "default") -> None:
        if mode not in {"local_only", "ollama_only", "setup_required", "local", "remote"}:
            raise ValueError("unknown learning policy")
        with self.database._lock, self.database.connection() as db:
            db.execute(
                "INSERT INTO learning_policies(user_id,workspace_id,mode,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id,workspace_id) DO UPDATE SET mode=excluded.mode,updated_at=excluded.updated_at",
                (user_id, workspace_id, mode, utc_now()),
            )


    def get_learning_runtime(self, *, user_id: str = "local",
                             workspace_id: str = "default") -> dict[str, str]:
        with self.database.connection() as db:
            row = db.execute(
                "SELECT mode,model,status,detail,updated_at FROM learning_runtime "
                "WHERE user_id=? AND workspace_id=?", (user_id, workspace_id),
            ).fetchone()
            if row:
                return dict(row)
            legacy = db.execute(
                "SELECT mode FROM learning_policies WHERE user_id=? AND workspace_id=?",
                (user_id, workspace_id),
            ).fetchone()
        detail = "Choose Local or Remote learning before semantic learning can run."
        if legacy:
            detail = "Previous learning policy requires a Local or Remote choice."
        self.set_learning_runtime("setup_required", "setup_required", detail=detail,
                                  user_id=user_id, workspace_id=workspace_id)
        return self.get_learning_runtime(user_id=user_id, workspace_id=workspace_id)


    def set_learning_runtime(self, mode: str, status: str, *, model: str = "",
                             detail: str = "", user_id: str = "local",
                             workspace_id: str = "default") -> None:
        if mode not in {"setup_required", "local", "remote"}:
            raise ValueError("learning mode must be setup_required, local, or remote")
        with self.database._lock, self.database.connection() as db:
            db.execute(
                "INSERT INTO learning_runtime(user_id,workspace_id,mode,model,status,detail,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,workspace_id) DO UPDATE SET "
                "mode=excluded.mode,model=excluded.model,status=excluded.status,detail=excluded.detail,updated_at=excluded.updated_at",
                (user_id, workspace_id, mode, model, status, detail, utc_now()),
            )


    def list_proposals(self, *, status: str | None = None, workspace_id: str = "default", limit: int = 100,
                       user_id: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = [workspace_id]
        clause = "workspace_id=?"
        if user_id is not None:
            clause += " AND user_id=?"
            params.append(user_id)
        if status:
            clause += " AND status=?"
            params.append(status)
        params.append(max(1, min(limit, 10000)))
        with self.database.connection() as db:
            rows = db.execute(f"SELECT * FROM learning_proposals WHERE {clause} ORDER BY updated_at DESC LIMIT ?", params).fetchall()
        return [dict(row, evidence=self.database._loads(row["evidence"], []), validation=self.database._loads(row["validation"], {})) for row in rows]



    def state_fingerprint(self, *, user_id: str, workspace_id: str, session_id: str | None, file_scope_id: str) -> tuple:
        with self.database.connection() as db:
            memory_clause = "user_id=? AND workspace_id=? AND status='active'"
            memory_params: list[Any] = [user_id, workspace_id]
            task_clause = "user_id=? AND workspace_id=?"
            task_params: list[Any] = [user_id, workspace_id]
            if session_id is not None:
                memory_clause += " AND (session_id IS NULL OR session_id=?)"
                memory_params.append(session_id)
                task_clause += " AND session_id=?"
                task_params.append(session_id)
            memory_row = db.execute(
                f"SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM memories WHERE {memory_clause}",
                memory_params,
            ).fetchone()
            file_row = db.execute(
                "SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM files WHERE user_id=? AND workspace_id=?",
                (user_id, file_scope_id),
            ).fetchone()
            proposal_row = db.execute(
                "SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM learning_proposals "
                "WHERE user_id=? AND workspace_id=?",
                (user_id, workspace_id),
            ).fetchone()
            task_row = db.execute(
                f"SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM tasks WHERE {task_clause}",
                task_params,
            ).fetchone()
            skill_row = db.execute(
                "SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM skills WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()
        return tuple(tuple(row or ()) for row in (memory_row, file_row, proposal_row, task_row, skill_row))
