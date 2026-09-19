"""Durable, evidence-driven task orchestration for OpenKyrozen."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from event_store import EventStore, utc_now


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
    "execute": ("run", "test", "execute", "command", "suite", "pytest"),
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
                 task_id: str | None = None, checkpoint: dict[str, Any] | None = None) -> int:
        description = str(description).strip()
        if not description:
            raise ValueError("Task description cannot be empty")
        scope_key = _task_key(f"{self.user_id}:{self.workspace_id}:{self.session_id or ''}")
        if task_id:
            task_id = _scoped_task_id(scope_key, task_id)
        else:
            task_id = f"task_{scope_key}_{uuid.uuid4().hex}"
        existing = next((item for item in self.tasks if item["id"] == task_id), None)
        if existing:
            return self.tasks.index(existing)
        task = {
            "id": task_id, "description": description, "status": "pending", "priority": priority,
            "dependencies": dependencies or [], "acceptance": acceptance or [], "attempts": 0,
            "checkpoint": checkpoint or {}, "evidence": [], "created_at": utc_now(), "updated_at": utc_now(),
        }
        self.tasks.append(task)
        self._persist(task)
        self._event(task, "task.created", {"description": description})
        return len(self.tasks) - 1

    def add_ordered_plan(self, descriptions: list[str], *, task_id_prefix: str | None = None,
                         acceptance: list[list[dict[str, Any]]] | None = None) -> None:
        """Add a numbered plan with the same durable ordering as a plain TaskList."""
        items = [str(description).strip() for description in descriptions if str(description).strip()]
        plan_id = _ordered_plan_id(items)
        for position, description in enumerate(items):
            checkpoint = None
            if plan_id is not None:
                checkpoint = {"ordered_plan": {
                    "id": plan_id, "index": position, "total": len(items),
                }}
            task_id = f"{task_id_prefix}-{position + 1}" if task_id_prefix else None
            criteria = acceptance[position] if acceptance and position < len(acceptance) else None
            self.add_task(description, task_id=task_id, checkpoint=checkpoint, acceptance=criteria)

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

    def _receipt_match_score(self, task: dict[str, Any], action: str, args: str) -> int:
        """Match only an explicit action/argument contract on the task."""
        action = _receipt_action(action)
        args = _receipt_args(args)
        checkpoint = task.get("checkpoint") or {}
        checkpoint_action = _receipt_action(checkpoint.get("action"))
        checkpoint_args = _receipt_args(checkpoint.get("args"))
        if checkpoint_action:
            if checkpoint_action != action:
                return -1
            if checkpoint_args and checkpoint_args != args:
                return -1
            return 200 if checkpoint_args else 150

        score = 0
        for criterion in task.get("acceptance") or []:
            if not isinstance(criterion, dict):
                continue
            criterion_action = _receipt_action(criterion.get("action"))
            if not criterion_action or criterion_action != action:
                continue
            criterion_args = _receipt_args(criterion.get("args"))
            if criterion_args and criterion_args != args:
                continue
            score = max(score, 120 if criterion_args else 100)
        return score or -1

    def _match_receipt_task(self, action: str, args: str) -> dict[str, Any] | None:
        candidates = [
            task for task in self.tasks
            if canonical_status(task.get("status", "pending")) in {"pending", "running"}
        ]
        scored = [(self._receipt_match_score(task, action, args), task) for task in candidates]
        scored = [(score, task) for score, task in scored if score > 0]
        if scored:
            highest = max(score for score, _task in scored)
            matches = [task for score, task in scored if score == highest]
            if len(matches) == 1:
                return matches[0]
            return None

        # Plain-string TaskLists have no action contract.  They may still be
        # reconciled, but only as one complete, uniquely identified ordered
        # plan; a neighboring pending task without this marker is never used.
        ordered_groups: dict[str, list[dict[str, Any]]] = {}
        for task in self.tasks:
            marker = _ordered_plan_info(task)
            if marker is not None:
                ordered_groups.setdefault(marker["id"], []).append(task)
        valid_groups = []
        for plan_id, group in ordered_groups.items():
            markers = [_ordered_plan_info(task) for task in group]
            total = markers[0]["total"] if markers else 0
            if len(group) != total or {item["index"] for item in markers} != set(range(total)):
                continue
            if any(
                (any(isinstance(item, dict) and item.get("action") for item in task.get("acceptance", []))
                 and not (task.get("checkpoint") or {}).get("ordered_receipt"))
                or ((task.get("checkpoint") or {}).get("action")
                    and not (task.get("checkpoint") or {}).get("ordered_receipt"))
                for task in group
            ):
                continue
            valid_groups.append((plan_id, sorted(group, key=lambda item: _ordered_plan_info(item)["index"])))
        if len(valid_groups) != 1:
            return None
        _plan_id, ordered_tasks = valid_groups[0]
        pending = [task for task in ordered_tasks if canonical_status(task.get("status", "pending")) in {"pending", "running"}]
        if not pending:
            return None
        candidate = pending[0]
        if _ordered_action_compatible(candidate["description"], action):
            return candidate
        # One ordered plan item can legitimately cover several observations
        # (for example listing a repository and reading two files).  Keep the
        # receipt on the most recent verified item rather than assigning it to
        # the next neighboring item whose description conflicts with the action.
        candidate_index = _ordered_plan_info(candidate)["index"]
        for previous in reversed(ordered_tasks[:candidate_index]):
            if (is_complete(previous)
                    and _ordered_action_compatible(previous["description"], action)):
                return previous
        return None

    def record_evidence(self, *, task_id: str | None = None, action: str, result: str, success: bool,
                        acceptance: str | None = None, args: str | None = None,
                        receipt_id: str | None = None) -> dict[str, Any]:
        canonical = _receipt_action(action)
        normalized_args = _receipt_args(args)
        item = {"action": canonical, "result": str(result)[:2000], "success": bool(success)}
        requested_task_id = str(task_id) if task_id else None
        if args:
            item["args"] = normalized_args[:1000]
        if receipt_id:
            item["receipt_id"] = str(receipt_id)
        if acceptance:
            item["acceptance"] = acceptance
        inferred = False
        ordered_inferred = False
        targets = []
        if task_id:
            candidate = next((task for task in self.tasks if task["id"] == str(task_id)), None)
            # An explicit identity is authoritative, but a stored action
            # contract still has to agree before successful evidence can be
            # attached to that task.
            has_contract = bool(((candidate or {}).get("checkpoint") or {}).get("action")) or any(
                isinstance(item, dict) and item.get("action")
                for item in (candidate or {}).get("acceptance", [])
            )
            if candidate is not None and (not has_contract or self._receipt_match_score(
                    candidate, canonical, normalized_args) >= 0):
                targets = [candidate]
                item["task_id"] = requested_task_id
        if not task_id:
            matched = self._match_receipt_task(canonical, normalized_args)
            if matched is not None:
                targets = [matched]
                inferred = True
                ordered_inferred = _ordered_plan_info(matched) is not None
                item["task_id"] = matched["id"]
                if success and not acceptance:
                    item["acceptance"] = f"verified receipt for planned action {canonical}"
        for task in targets:
            if ordered_inferred:
                marker = _ordered_plan_info(task)
                if marker:
                    item["ordered_plan"] = marker
                task["checkpoint"] = {
                    **(task.get("checkpoint") or {}), "action": canonical,
                    "args": normalized_args, "ordered_receipt": True,
                }
            elif inferred and not (task.get("checkpoint") or {}).get("action"):
                task["checkpoint"] = {"action": canonical, "args": normalized_args}
            task.setdefault("evidence", []).append(item)
            task["updated_at"] = utc_now()
            self._persist(task)
        if ordered_inferred and success:
            index = next((index for index, task in enumerate(self.tasks) if targets and task["id"] == targets[0]["id"]), None)
            if index is not None and not is_complete(self.tasks[index]):
                self.set_status(index, "succeeded")
        if not targets:
            item["unmatched_reason"] = (
                "No unique ordered or contracted task matched this receipt; no task was marked complete."
            )
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

    def _replace_task(self, task: dict[str, Any]) -> int:
        for index, existing in enumerate(self.tasks):
            if existing["id"] == task["id"]:
                self.tasks[index] = task
                return index
        self.tasks.append(task)
        return len(self.tasks) - 1

    def claim_next(self) -> dict[str, Any] | None:
        """Atomically claim one pending task in this exact durable scope."""
        with self.store._lock, self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE user_id=? AND workspace_id=? AND session_id IS ? AND status=? "
                "ORDER BY priority DESC, updated_at LIMIT 1",
                (self.user_id, self.workspace_id, self.session_id, "pending"),
            ).fetchone()
            if row is None:
                return None
            now = utc_now()
            claimed = db.execute(
                "UPDATE tasks SET status=?,attempts=attempts+1,updated_at=? WHERE id=? AND user_id=? "
                "AND workspace_id=? AND session_id IS ? AND status=?",
                ("running", now, row["id"], self.user_id, self.workspace_id, self.session_id, "pending"),
            ).rowcount
            if claimed != 1:
                return None
        task = dict(row)
        task["status"] = "running"
        task["attempts"] = int(task.get("attempts", 0)) + 1
        for key in ("dependencies", "acceptance", "checkpoint", "evidence"):
            task[key] = self.store._loads(task[key], [] if key in {"dependencies", "acceptance", "evidence"} else {})
        task["updated_at"] = now
        self._replace_task(task)
        self._event(task, "task.claimed", {"attempt": task["attempts"]})
        return task

    def update_checkpoint(self, task_id: str, checkpoint: dict[str, Any]) -> bool:
        """Persist a bounded resumable checkpoint for one task."""
        if not isinstance(checkpoint, dict):
            raise ValueError("checkpoint must be an object")
        if not self.tasks:
            self.recover()
        index = next((i for i, task in enumerate(self.tasks) if task["id"] == str(task_id)), None)
        if index is None:
            return False
        task = self.tasks[index]
        task["checkpoint"] = {**task.get("checkpoint", {}), **checkpoint}
        task["updated_at"] = utc_now()
        self._persist(task)
        self._event(task, "task.checkpoint", {"checkpoint": task["checkpoint"]})
        return True

    def resume(self, task_id: str) -> dict[str, Any] | None:
        """Move one failed or blocked task back to pending in this scope."""
        if not self.tasks:
            self.recover()
        index = next((i for i, task in enumerate(self.tasks) if task["id"] == str(task_id)), None)
        if index is None:
            self.recover()
            index = next((i for i, task in enumerate(self.tasks) if task["id"] == str(task_id)), None)
        if index is None:
            return None
        task = self.tasks[index]
        if task.get("status") not in {"failed", "blocked"}:
            return task if task.get("status") == "pending" else None
        previous = task["status"]
        task["status"] = "pending"
        task["updated_at"] = utc_now()
        self._persist(task)
        self._event(task, "task.resumed", {"previous_status": previous})
        return task

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
            status = canonical_status(task.get("status", "pending"))
            lines.append(f"  {icons.get(status, '?')}  {desc}")
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
        existing_by_id = {item["id"]: item for item in self.tasks}
        existing_by_key = {_task_key(item["description"]): item for item in self.tasks}
        plain_items = [item for item in raw_tasks if isinstance(item, str) and item.strip()]
        ordered_plan_id = None
        if len(plain_items) == len(raw_tasks):
            ordered_plan_id = _ordered_plan_id([item.strip() for item in plain_items])
        for position, item in enumerate(raw_tasks):
            checkpoint = None
            if isinstance(item, str):
                description, task_id, acceptance = item, None, None
                dependencies = None
                if ordered_plan_id is not None:
                    checkpoint = {"ordered_plan": {
                        "id": ordered_plan_id, "index": position,
                        "total": len(raw_tasks),
                    }}
            elif isinstance(item, dict) and item.get("description"):
                description = str(item["description"])
                task_id = str(item.get("id")) if item.get("id") else None
                dependencies = item.get("dependencies") if isinstance(item.get("dependencies"), list) else None
                acceptance = item.get("acceptance") if isinstance(item.get("acceptance"), list) else None
                checkpoint = item.get("checkpoint") if isinstance(item.get("checkpoint"), dict) else None
                if checkpoint is None and item.get("action"):
                    checkpoint = {"action": item.get("action"), "args": item.get("args", "")}
            else:
                continue
            key = _task_key(description)
            explicit_id = task_id is not None
            if explicit_id:
                scope_key = _task_key(f"{self.user_id}:{self.workspace_id}:{self.session_id or ''}")
                stable_id = _scoped_task_id(scope_key, task_id)
                task = existing_by_id.get(stable_id)
            else:
                stable_id = None
                task = existing_by_key.get(key)
            if task is not None:
                if explicit_id:
                    task["description"] = description
                if dependencies:
                    task["dependencies"] = dependencies
                if acceptance:
                    task["acceptance"] = acceptance
                if checkpoint:
                    task["checkpoint"] = {**task.get("checkpoint", {}), **checkpoint}
                self._persist(task)
            else:
                idx = self.add_task(description, dependencies=dependencies, acceptance=acceptance,
                                    task_id=task_id, checkpoint=checkpoint)
                existing_by_key[key] = self.tasks[idx]
                existing_by_id[self.tasks[idx]["id"]] = self.tasks[idx]

    def mark_done_from_text(self, text: str) -> None:
        """Treat TaskDone as a completion request; evidence performs the transition."""
        import re
        for match in re.finditer(r"TaskDone:\s*(\d+)", text):
            self.request_completion(int(match.group(1)))

    def recover(self) -> list[dict[str, Any]]:
        """Load unfinished tasks from SQLite and make running tasks recoverable."""
        unfinished = self.store.list_tasks(workspace_id=self.workspace_id, session_id=self.session_id,
                                           statuses={"pending", "running", "failed", "blocked"}, user_id=self.user_id)
        for task in unfinished:
            if task["status"] == "running":
                task["status"] = "pending"
                task["updated_at"] = utc_now()
                self._persist(task)
                self._event(task, "task.recovered", {"previous_status": "running"})
        ordered_plan_ids = {
            marker["id"] for task in unfinished
            if (marker := _ordered_plan_info(task)) is not None
        }
        if ordered_plan_ids:
            all_rows = self.store.list_tasks(
                workspace_id=self.workspace_id, session_id=self.session_id,
                user_id=self.user_id,
            )
            self.tasks = sorted(
                (
                    task for task in all_rows
                    if (marker := _ordered_plan_info(task)) is not None
                    and marker["id"] in ordered_plan_ids
                ),
                key=lambda task: _ordered_plan_info(task)["index"],
            )
        else:
            self.tasks = unfinished
        return unfinished


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
