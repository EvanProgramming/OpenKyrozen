from __future__ import annotations

from typing import Any


def _execute_durable_task(self, task: dict[str, Any]) -> dict[str, Any]:
    """Run one explicitly stored task action through the normal CLI gates."""
    checkpoint = task.get("checkpoint", {})
    action = self.TOOL_ALIASES.get(str(checkpoint.get("action", "")).strip(),
                              str(checkpoint.get("action", "")).strip())
    args = checkpoint.get("args", "")
    if not action:
        return {"success": False,
                "result": "No executable action stored; create the task with action and string args."}
    if not isinstance(args, str):
        return {"success": False, "action": action, "result": "Task args must be a plain string."}
    receipt = self.execute(self.current_session, action, args, operation_scope=task["id"])
    result, success = receipt.result, receipt.success
    return {"success": success, "action": action, "args": args, "result": result,
            "acceptance": str(checkpoint.get("acceptance") or "durable task action completed")}
