"""Durable, evidence-driven task orchestration for OpenKyrozen."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from event_store import EventStore, utc_now


VALID_STATUSES = {"pending", "running", "succeeded", "done", "failed", "blocked", "cancelled"}


def _task_key(description: str) -> str:
    return hashlib.sha256(" ".join(description.lower().split()).encode("utf-8")).hexdigest()[:16]


class TaskManager:
    """Compatibility facade backed by SQLite and guarded by evidence."""

    def __init__(self, store: EventStore | None = None, *, workspace_id: str = "default",
                 session_id: str | None = None, user_id: str = "local"):
        self.store = store or EventStore()
        self.workspace_id = workspace_id
        self.session_id = session_id
        self.user_id = user_id
        self.tasks: list[dict[str, Any]] = []
        self._pending_completions: set[int] = set()
        self._evidence: list[dict[str, Any]] = []

    def _persist(self, task: dict[str, Any]) -> None:
        now = task.get("updated_at") or utc_now()
        # The EventStore intentionally keeps SQL details private; task writes
        # are performed through its transactional connection here so the
        # compatibility facade remains small and atomic.
        with self.store._lock, self.store.connection() as db:
            row = db.execute("SELECT id FROM tasks WHERE id=?", (task["id"],)).fetchone()
            values = (
                task["id"], task.get("parent_id"), task["description"], task["status"],
                int(task.get("priority", 0)), self.store._json(task.get("dependencies", [])),
                self.store._json(task.get("acceptance", [])), int(task.get("attempts", 0)),
                self.store._json(task.get("checkpoint", {})), self.store._json(task.get("evidence", [])),
                self.user_id, self.workspace_id, self.session_id, task.get("created_at", now), now,
            )
            if row:
                db.execute(
                    "UPDATE tasks SET parent_id=?,description=?,status=?,priority=?,dependencies=?,acceptance=?,attempts=?,checkpoint=?,evidence=?,updated_at=? WHERE id=?",
                    values[1:10] + (now, task["id"]),
                )
            else:
                db.execute(
                    "INSERT INTO tasks(id,parent_id,description,status,priority,dependencies,acceptance,attempts,checkpoint,evidence,user_id,workspace_id,session_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )

    def _event(self, task: dict[str, Any], event_type: str, payload: Any) -> None:
        self.store.append_event(event_type, payload, user_id=self.user_id, workspace_id=self.workspace_id,
                                session_id=self.session_id, task_id=task["id"])

    def add_task(self, description: str, *, dependencies: list[str] | None = None,
                 acceptance: list[dict[str, Any]] | None = None, priority: int = 0,
                 task_id: str | None = None) -> int:
        description = str(description).strip()
        if not description:
            raise ValueError("Task description cannot be empty")
        task_id = task_id or f"task_{_task_key(description)}"
        existing = next((item for item in self.tasks if item["id"] == task_id), None)
        if existing:
            return self.tasks.index(existing)
        task = {
            "id": task_id, "description": description, "status": "pending", "priority": priority,
            "dependencies": dependencies or [], "acceptance": acceptance or [], "attempts": 0,
            "checkpoint": {}, "evidence": [], "created_at": utc_now(), "updated_at": utc_now(),
        }
        self.tasks.append(task)
        self._persist(task)
        self._event(task, "task.created", {"description": description})
        return len(self.tasks) - 1

    def set_status(self, idx: int, status: str, *, evidence: dict[str, Any] | None = None) -> bool:
        if not 0 <= idx < len(self.tasks):
            return False
        if status not in VALID_STATUSES:
            raise ValueError(f"Unknown task status: {status}")
        if status in {"succeeded", "done"} and not self._has_evidence(idx, evidence):
            return False
        task = self.tasks[idx]
        task["status"] = "succeeded" if status == "done" else status
        if evidence:
            task.setdefault("evidence", []).append(evidence)
        from event_store import utc_now
        task["updated_at"] = utc_now()
        self._persist(task)
        self._event(task, f"task.{task['status']}", {"evidence": evidence or {}})
        return True

    def _has_evidence(self, idx: int, evidence: dict[str, Any] | None = None) -> bool:
        if evidence and evidence.get("success") is True:
            return True
        evidence_items = self._evidence + self.tasks[idx].get("evidence", [])
        return any(item.get("success") is True and item.get("acceptance") for item in evidence_items)

    def record_evidence(self, *, action: str, result: str, success: bool,
                        acceptance: str | None = None) -> None:
        item = {"action": action, "result": str(result)[:2000], "success": bool(success)}
        if acceptance:
            item["acceptance"] = acceptance
        self._evidence.append(item)
        for task in self.tasks:
            if success and acceptance:
                task.setdefault("evidence", []).append(item)
        self.reconcile_completions()

    def request_completion(self, idx: int) -> None:
        if 0 <= idx < len(self.tasks):
            self._pending_completions.add(idx)

    def reconcile_completions(self) -> None:
        for idx in list(self._pending_completions):
            if self._has_evidence(idx):
                self.set_status(idx, "succeeded", evidence=self._evidence[-1] if self._evidence else None)
                self._pending_completions.discard(idx)

    def mark_done(self, idx: int, *, evidence: dict[str, Any] | None = None) -> bool:
        return self.set_status(idx, "succeeded", evidence=evidence)

    def mark_first_pending_done(self) -> bool:
        """Deprecated safety-net API; never declares work complete without evidence."""
        for idx, task in enumerate(self.tasks):
            if task["status"] == "pending":
                return self.mark_done(idx)
        return False

    def clear(self) -> None:
        self.tasks.clear()
        self._pending_completions.clear()
        self._evidence.clear()

    def format(self) -> str:
        if not self.tasks:
            return "No tasks."
        icons = {"pending": "○", "running": "◷", "in_progress": "◷", "succeeded": "✓", "done": "✓",
                 "failed": "!", "blocked": "!", "cancelled": "×"}
        lines = []
        for task in self.tasks:
            desc = task["description"].replace("{", "{{").replace("}", "}}")
            lines.append(f"  {icons.get(task['status'], '?')}  {desc}")
        return "\n".join(lines)

    def from_llm_block(self, text: str) -> None:
        """Merge an LLM TaskList without resetting durable status."""
        import re
        match = re.search(r"TaskList:\s*```(?:json)?\s*([\s\S]*?)\s*```", text)
        if not match:
            return
        try:
            raw_tasks = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            return
        if not isinstance(raw_tasks, list):
            return
        existing_by_key = {_task_key(item["description"]): item for item in self.tasks}
        for item in raw_tasks:
            if isinstance(item, str):
                description, task_id, acceptance = item, None, None
                dependencies = None
            elif isinstance(item, dict) and item.get("description"):
                description = str(item["description"])
                task_id = str(item.get("id")) if item.get("id") else None
                dependencies = item.get("dependencies") if isinstance(item.get("dependencies"), list) else None
                acceptance = item.get("acceptance") if isinstance(item.get("acceptance"), list) else None
            else:
                continue
            key = _task_key(description)
            if key in existing_by_key:
                task = existing_by_key[key]
                if dependencies:
                    task["dependencies"] = dependencies
                if acceptance:
                    task["acceptance"] = acceptance
                self._persist(task)
            else:
                idx = self.add_task(description, dependencies=dependencies, acceptance=acceptance, task_id=task_id)
                existing_by_key[key] = self.tasks[idx]

    def mark_done_from_text(self, text: str) -> None:
        """Treat TaskDone as a completion request; evidence performs the transition."""
        import re
        for match in re.finditer(r"TaskDone:\s*(\d+)", text):
            self.request_completion(int(match.group(1)))

    def recover(self) -> list[dict[str, Any]]:
        """Load unfinished tasks from SQLite and make running tasks recoverable."""
        rows = self.store.list_tasks(workspace_id=self.workspace_id, session_id=self.session_id,
                                     statuses={"pending", "running", "failed", "blocked"})
        for task in rows:
            if task["status"] == "running":
                task["status"] = "pending"
        self.tasks = rows
        return rows
