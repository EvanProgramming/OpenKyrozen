from __future__ import annotations

import uuid

from .models import utc_now


class ToolArtifactsRepository:
    def __init__(self, database):
        self.database = database

    def create(self, content: str, *, user_id: str, workspace_id: str, session_id: str) -> str:
        if not all(isinstance(value, str) and value for value in (user_id, workspace_id, session_id)):
            raise ValueError("Tool artifacts require a complete session scope")
        artifact_id = f"artifact_{uuid.uuid4().hex}"
        with self.database._lock, self.database.connection() as db:
            db.execute(
                "INSERT INTO tool_artifacts(id,user_id,workspace_id,session_id,content,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (artifact_id, user_id, workspace_id, session_id, content, utc_now()),
            )
        return artifact_id

    def read_page(self, artifact_id: str, offset: int, limit: int, *,
                  user_id: str, workspace_id: str, session_id: str) -> dict | None:
        with self.database.connection() as db:
            row = db.execute(
                "SELECT content FROM tool_artifacts WHERE id=? AND user_id=? AND workspace_id=? AND session_id=?",
                (artifact_id, user_id, workspace_id, session_id),
            ).fetchone()
        if row is None:
            return None
        content = row["content"]
        return {"artifact_id": artifact_id, "offset": offset, "limit": limit,
                "total_chars": len(content), "content": content[offset:offset + limit],
                "next_offset": min(offset + limit, len(content)) if offset + limit < len(content) else None}
