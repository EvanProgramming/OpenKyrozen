from __future__ import annotations

import hashlib
import re
from typing import Any

VALID_STATUSES = {"pending", "running", "succeeded", "done", "failed", "blocked", "cancelled"}


CANONICAL_STATUSES = {"pending", "running", "succeeded", "failed", "blocked", "cancelled"}


TERMINAL_STATUSES = {"succeeded", "failed", "blocked", "cancelled"}


def _task_key(description: str) -> str:
    return hashlib.sha256(" ".join(description.lower().split()).encode("utf-8")).hexdigest()[:16]


def _scoped_task_id(scope_key: str, raw_task_id: str) -> str:
    """Return the stable, scope-bound durable id for an explicit task id."""
    raw_task_id = str(raw_task_id).strip()
    prefix = f"task_{scope_key}_"
    return raw_task_id if raw_task_id.startswith(prefix) else f"{prefix}{_task_key(raw_task_id)}"


_RECEIPT_ACTION_ALIASES = {
    "status": "git_status",
    "diff": "git_diff",
    "log": "git_log",
    "add": "git_add",
    "commit": "git_commit",
    "write": "write_file",
    "edit_file": "write_file",
    "run_command": "run_cmd",
    "execute_terminal_command": "run_cmd",
    "run_terminal": "run_cmd",
    "terminal": "run_cmd",
    "command": "run_cmd",
    "bash": "run_cmd",
    "shell": "run_cmd",
    "sh": "run_cmd",
    "cmd": "run_cmd",
}


_ORDERED_ACTION_FAMILIES = {
    "inspect": {"list_dir", "list_tree", "read_file", "find_files"},
    "write": {"write_file"},
    "execute": {"run_cmd"},
    "review": {"git_diff"},
    "status": {"git_status"},
    "stage": {"git_add"},
    "commit": {"git_commit"},
    "history": {"git_log"},
}


_ORDERED_ACTION_HINTS = {
    "inspect": ("read", "inspect", "list", "directory", "repo", "repository", "structure", "tree"),
    "write": ("write", "edit", "add", "update", "create", "modify", "append"),
    "execute": (
        "run", "test", "execute", "command", "suite", "pytest",
        "start", "serve", "verify", "check", "stop", "kill",
    ),
    "review": ("diff", "review", "compare"),
    "status": ("status", "clean"),
    "stage": ("stage", "staging"),
    "commit": ("commit", "committed"),
    "history": ("log", "history", "commits"),
}


def _receipt_action(action: Any) -> str:
    value = str(action or "").strip().lower()
    return _RECEIPT_ACTION_ALIASES.get(value, value)


def _receipt_args(args: Any) -> str:
    """Normalize transport-only line breaks before comparing receipt arguments."""
    return str(args or "").replace("\r\n", "\n").replace("\n", " ").strip()


def _ordered_plan_info(task: dict[str, Any]) -> dict[str, Any] | None:
    marker = (task.get("checkpoint") or {}).get("ordered_plan")
    if not isinstance(marker, dict) or not isinstance(marker.get("id"), str):
        return None
    if not isinstance(marker.get("index"), int) or not isinstance(marker.get("total"), int):
        return None
    if marker["index"] < 0 or marker["total"] <= 0 or marker["index"] >= marker["total"]:
        return None
    return {"id": marker["id"], "index": marker["index"], "total": marker["total"]}


def _ordered_plan_id(items: list[str]) -> str | None:
    if not items or len({_task_key(item) for item in items}) != len(items):
        return None
    return _task_key("\n".join(items))


def _ordered_action_compatible(description: str, action: str) -> bool:
    """Keep ordered inference bounded to descriptions with an execution hint."""
    action = _receipt_action(action)
    families = {family for family, actions in _ORDERED_ACTION_FAMILIES.items() if action in actions}
    description = str(description).lower()
    hinted = {
        family for family, words in _ORDERED_ACTION_HINTS.items()
        if any(re.search(rf"\b{re.escape(word)}\b", description) for word in words)
    }
    # ponytail: lexical guard is intentionally conservative; replace with a
    # provider-supplied checkpoint when plain plans gain durable action IDs.
    return bool(families & hinted)


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
