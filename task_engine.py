"""Durable, evidence-driven task orchestration for OpenKyrozen."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from event_store import EventStore, utc_now


VALID_STATUSES = {"pending", "running", "succeeded", "done", "failed", "blocked", "cancelled"}
CANONICAL_STATUSES = {"pending", "running", "succeeded", "failed", "blocked", "cancelled"}
TERMINAL_STATUSES = {"succeeded", "failed", "blocked", "cancelled"}


def _task_key(description: str) -> str:
    return hashlib.sha256(" ".join(description.lower().split()).encode("utf-8")).hexdigest()[:16]


def canonical_status(status: str) -> str:
    """Normalize the historical ``done`` spelling to the durable status."""
    status = str(status).strip().lower()
    if status == "done":
        return "succeeded"
    if status not in CANONICAL_STATUSES:
        raise ValueError(f"Unknown task status: {status}")
    return status


def is_complete(task_or_status: dict[str, Any] | str) -> bool:
    status = task_or_status.get("status") if isinstance(task_or_status, dict) else task_or_status
    return canonical_status(str(status)) == "succeeded"


def is_terminal(task_or_status: dict[str, Any] | str) -> bool:
    status = task_or_status.get("status") if isinstance(task_or_status, dict) else task_or_status
    return canonical_status(str(status)) in TERMINAL_STATUSES


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
            row = db.execute(
                "SELECT id FROM tasks WHERE id=? AND user_id=? AND workspace_id=? AND session_id IS ?",
                (task["id"], self.user_id, self.workspace_id, self.session_id),
            ).fetchone()
            values = (
                task["id"], task.get("parent_id"), task["description"], task["status"],
                int(task.get("priority", 0)), self.store._json(task.get("dependencies", [])),
                self.store._json(task.get("acceptance", [])), int(task.get("attempts", 0)),
                self.store._json(task.get("checkpoint", {})), self.store._json(task.get("evidence", [])),
                self.user_id, self.workspace_id, self.session_id, task.get("created_at", now), now,
            )
            if row:
                db.execute(
                    "UPDATE tasks SET parent_id=?,description=?,status=?,priority=?,dependencies=?,acceptance=?,attempts=?,checkpoint=?,evidence=?,updated_at=? "
                    "WHERE id=? AND user_id=? AND workspace_id=? AND session_id IS ?",
                    values[1:10] + (now, task["id"], self.user_id, self.workspace_id, self.session_id),
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
        scope_key = _task_key(f"{self.user_id}:{self.workspace_id}:{self.session_id or ''}")
        if task_id:
            raw_task_id = str(task_id).strip()
            task_id = (raw_task_id if raw_task_id.startswith(f"task_{scope_key}_")
                       else f"task_{scope_key}_{_task_key(raw_task_id)}")
        else:
            task_id = f"task_{scope_key}_{uuid.uuid4().hex}"
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
        status = canonical_status(status)
        if status == "succeeded" and not self._has_evidence(idx, evidence):
            return False
        task = self.tasks[idx]
        task["status"] = status
        if evidence:
            task.setdefault("evidence", []).append(evidence)
        from event_store import utc_now
        task["updated_at"] = utc_now()
        self._persist(task)
        self._event(task, f"task.{task['status']}", {"evidence": evidence or {}})
        return True

    def _has_evidence(self, idx: int, evidence: dict[str, Any] | None = None) -> bool:
        evidence_items = ([evidence] if evidence else []) + self.tasks[idx].get("evidence", [])
        return any(item.get("success") is True and item.get("acceptance") for item in evidence_items)

    def record_evidence(self, *, task_id: str | None = None, action: str, result: str, success: bool,
                        acceptance: str | None = None, args: str | None = None,
                        receipt_id: str | None = None) -> dict[str, Any]:
        item = {"action": action, "result": str(result)[:2000], "success": bool(success)}
        if task_id:
            item["task_id"] = str(task_id)
        if args:
            item["args"] = str(args)[:1000]
        if receipt_id:
            item["receipt_id"] = str(receipt_id)
        if acceptance:
            item["acceptance"] = acceptance
        targets = [task for task in self.tasks if task["id"] == str(task_id)] if task_id else []
        # Compatibility for the old single-task caller.  With more than one
        # task, an unaddressed receipt is intentionally not attached anywhere.
        if not task_id and len(self.tasks) == 1:
            targets = [self.tasks[0]]
            item["task_id"] = targets[0]["id"]
        for task in targets:
            task.setdefault("evidence", []).append(item)
            task["updated_at"] = utc_now()
            self._persist(task)
        self.reconcile_completions({task["id"] for task in targets})
        return item

    def request_completion(self, idx: int) -> None:
        if 0 <= idx < len(self.tasks):
            self._pending_completions.add(idx)

    def active_task_id(self) -> str | None:
        """Return the next task that may receive an execution receipt."""
        for task in self.tasks:
            if task.get("status") in {"pending", "running"}:
                return task["id"]
        return None

    def reconcile_completions(self, task_ids: set[str] | None = None) -> None:
        for idx in list(self._pending_completions):
            if task_ids is not None and self.tasks[idx]["id"] not in task_ids:
                continue
            if self._has_evidence(idx):
                self.set_status(idx, "succeeded")
                if is_complete(self.tasks[idx]):
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
                                     statuses={"pending", "running", "failed", "blocked"}, user_id=self.user_id)
        for task in rows:
            if task["status"] == "running":
                task["status"] = "pending"
                task["updated_at"] = utc_now()
                self._persist(task)
                self._event(task, "task.recovered", {"previous_status": "running"})
        self.tasks = rows
        return rows
