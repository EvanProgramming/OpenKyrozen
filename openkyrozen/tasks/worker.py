from __future__ import annotations

from typing import Any

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .engine import TaskManager
from openkyrozen.persistence.models import utc_now

class TaskWorker:
    """Execute at most a bounded number of durable task actions per call."""

    def __init__(self, manager: TaskManager, executor: Any, *, max_tasks: int = 1):
        self.manager = manager
        self.executor = executor
        self.max_tasks = max(1, min(int(max_tasks), 20))

    def recover(self) -> list[dict[str, Any]]:
        return self.manager.recover()

    def run_once(self) -> dict[str, Any] | None:
        task = self.manager.claim_next()
        if task is None:
            return None
        task_id = task["id"]
        try:
            outcome = self.executor(task)
            if not isinstance(outcome, dict):
                outcome = {"success": False, "result": str(outcome)}
        except Exception as exc:
            outcome = {"success": False, "result": f"Error: {exc}"}
        success = bool(outcome.get("success"))
        result = str(outcome.get("result", ""))[:2000]
        action = str(outcome.get("action") or task.get("checkpoint", {}).get("action") or "task.execute")
        acceptance = str(outcome.get("acceptance") or "durable task action completed") if success else None
        evidence = self.manager.record_evidence(
            task_id=task_id, action=action, args=outcome.get("args") or task.get("checkpoint", {}).get("args"),
            result=result, success=success, acceptance=acceptance,
            receipt_id=outcome.get("receipt_id"),
        )
        index = next(i for i, item in enumerate(self.manager.tasks) if item["id"] == task_id)
        status = "succeeded" if success and self.manager.set_status(index, "succeeded") else "failed"
        if status == "failed" and self.manager.tasks[index].get("status") != "failed":
            self.manager.set_status(index, "failed")
        self.manager._event(self.manager.tasks[index], "task.execution_completed", {
            "success": status == "succeeded", "result": result, "evidence": evidence,
        })
        return {"task": self.manager.tasks[index], "status": status, "result": result, "evidence": evidence}

    def run_until_idle(self, *, max_tasks: int | None = None) -> list[dict[str, Any]]:
        results = []
        for _ in range(max(1, min(int(max_tasks or self.max_tasks), 20))):
            result = self.run_once()
            if result is None:
                break
            results.append(result)
        return results
