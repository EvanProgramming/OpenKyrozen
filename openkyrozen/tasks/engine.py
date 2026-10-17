from __future__ import annotations

import json
import hashlib
import re
import uuid
from copy import deepcopy
from contextvars import ContextVar
from typing import Any
from openkyrozen.persistence.models import utc_now

from .models import CANONICAL_STATUSES, TERMINAL_STATUSES, VALID_STATUSES, WorkingPlan, _ORDERED_ACTION_FAMILIES, _ORDERED_ACTION_HINTS, _RECEIPT_ACTION_ALIASES, _ordered_action_compatible, _ordered_plan_id, _ordered_plan_info, _receipt_action, _receipt_args, _scoped_task_id, _task_key, canonical_status, is_complete, is_terminal, valid_task_transition
from .ports import TaskStore

class TaskManager:
    """Compatibility facade backed by SQLite and guarded by evidence."""

    def __init__(self, store: TaskStore, *, workspace_id: str = "default",
                 session_id: str | None = None, user_id: str = "local"):
        self.store = store
        self.workspace_id = workspace_id
        self.session_id = session_id
        self.user_id = user_id
        self.tasks: list[dict[str, Any]] = []
        self._pending_completions: set[int] = set()
        self._evidence: list[dict[str, Any]] = []
        self.working_plan: WorkingPlan | None = None
        self._runtime_event_callback: ContextVar[Any] = ContextVar("task_runtime_event_callback", default=None)

    def _persist(self, task: dict[str, Any], *, expected_status: str | None = None) -> bool:
        return self.store.write_task(task, user_id=self.user_id, workspace_id=self.workspace_id,
                                     session_id=self.session_id, expected_status=expected_status)

    def _reload_task(self, task_id: str) -> dict[str, Any] | None:
        return next((item for item in self.store.list_tasks(
            user_id=self.user_id, workspace_id=self.workspace_id, session_id=self.session_id, limit=10000,
        ) if item["id"] == task_id), None)

    @staticmethod
    def _dependencies_succeeded(task: dict[str, Any], tasks: list[dict[str, Any]]) -> bool:
        statuses = {item["id"]: canonical_status(item.get("status", "pending")) for item in tasks}
        return all(statuses.get(dependency) == "succeeded" for dependency in task.get("dependencies", []))
    def _event(self, task: dict[str, Any], event_type: str, payload: Any) -> None:
        self.store.append_event(event_type, payload, user_id=self.user_id, workspace_id=self.workspace_id,
                                session_id=self.session_id, task_id=task["id"])
        callback = self._runtime_event_callback.get()
        if callable(callback):
            try:
                callback(event_type, payload, task["id"])
            except Exception:
                pass

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

    def set_status(self, idx: int, status: str, *, evidence: dict[str, Any] | None = None,
                   _terminalize_plan: bool = True) -> bool:
        if not 0 <= idx < len(self.tasks):
            return False
        status = canonical_status(status)
        current = canonical_status(self.tasks[idx].get("status", "pending"))
        if not valid_task_transition(current, status):
            return False
        if current == status:
            return True
        if status == "succeeded" and not self._has_evidence(idx, evidence):
            return False
        task = deepcopy(self.tasks[idx])
        expected_status = self.tasks[idx].get("status", "pending")
        task["status"] = status
        if evidence:
            task.setdefault("evidence", []).append(evidence)
        from openkyrozen.persistence.models import utc_now
        task["updated_at"] = utc_now()
        if not self._persist(task, expected_status=expected_status):
            latest = self._reload_task(task["id"])
            if latest is not None:
                self.tasks[idx] = latest
            return False
        self.tasks[idx] = task
        self._event(task, f"task.{task['status']}", {"evidence": evidence or {}})
        if _terminalize_plan and status in {"succeeded", "cancelled"} and self.working_plan is not None:
            plan_tasks = [item for item in self.tasks if item["id"] in self.working_plan.task_ids]
            terminal = len(plan_tasks) == len(self.working_plan.task_ids) and all(
                canonical_status(item["status"]) in {"succeeded", "cancelled"} for item in plan_tasks
            )
            plan_status = "completed" if terminal and any(
                canonical_status(item["status"]) == "succeeded" for item in plan_tasks
            ) and all(
                canonical_status(item["status"]) != "succeeded" or any(
                    evidence.get("success") is True and evidence.get("acceptance")
                    for evidence in item.get("evidence", []) if isinstance(evidence, dict)
                ) for item in plan_tasks
            ) else "cancelled" if terminal else None
            if (plan_status is not None and self.store.set_working_plan_status(
                        self.working_plan.id, plan_status, user_id=self.user_id,
                        workspace_id=self.workspace_id, session_id=self.session_id,
                        expected_revision=self.working_plan.revision,
                        expected_task_ids=self.working_plan.task_ids,
                    )):
                self.working_plan = WorkingPlan(
                    id=self.working_plan.id, complexity=self.working_plan.complexity,
                    status=plan_status, task_ids=self.working_plan.task_ids,
                    created_at=self.working_plan.created_at, updated_at=utc_now(),
                    revision=self.working_plan.revision,
                )
                if plan_status == "completed":
                    self._event(task, "working_plan.completed", {"plan_id": self.working_plan.id})
                else:
                    self._event(task, "working_plan.cancelled", {"plan_id": self.working_plan.id})
        return True

    @staticmethod
    def _plan_task_fields(task: Any) -> dict[str, Any]:
        if not isinstance(task, dict):
            raise ValueError("task must be an object")
        allowed = {"description", "acceptance", "dependencies", "priority", "execution"}
        if set(task) - allowed:
            raise ValueError("task contains unsupported fields")
        description = task.get("description")
        acceptance = task.get("acceptance")
        dependencies = task.get("dependencies", [])
        priority = task.get("priority", 0)
        if not isinstance(description, str) or not description.strip():
            raise ValueError("task description must be a nonempty string")
        if not isinstance(acceptance, list) or any(not isinstance(value, str) or not value.strip() for value in acceptance):
            raise ValueError("task acceptance must be an array of nonempty strings")
        if not isinstance(dependencies, list) or any(not isinstance(value, str) or not value for value in dependencies):
            raise ValueError("task dependencies must be an array of task references")
        if type(priority) is not int:
            raise ValueError("task priority must be an integer")
        checkpoint: dict[str, Any] = {}
        execution = task.get("execution")
        if execution is not None:
            if (not isinstance(execution, dict) or set(execution) != {"action", "arguments"}
                    or not isinstance(execution.get("action"), str) or not execution["action"].strip()
                    or not isinstance(execution.get("arguments"), dict)):
                raise ValueError("execution must contain an action and object arguments")
            checkpoint = {"action": execution["action"].strip(),
                          "args": json.dumps(execution["arguments"], ensure_ascii=False, allow_nan=False,
                                             sort_keys=True, separators=(",", ":"))}
        return {"description": description.strip(), "acceptance": list(acceptance),
                "dependencies": list(dependencies), "priority": priority, "checkpoint": checkpoint}

    def edit_plan(self, *, expected_revision: int, operations: list[dict[str, Any]],
                  complexity: str | None = None, plan_id: str | None = None,
                  strategic_bindings: dict[str, dict[str, Any]] | None = None,
                  transaction_event: dict[str, Any] | None = None) -> dict[str, Any]:
        """Apply a validated ordered operation batch as one revision-checked transaction."""
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        if not isinstance(operations, list):
            raise ValueError("operations must be an array")
        if not operations:
            raise ValueError("operations cannot be empty")
        if plan_id is not None and (not isinstance(plan_id, str) or not plan_id.strip()):
            raise ValueError("plan_id must be a nonempty string")
        if strategic_bindings is not None and not isinstance(strategic_bindings, dict):
            raise ValueError("strategic_bindings must be an object")
        if transaction_event is not None and (
                not isinstance(transaction_event, dict)
                or set(transaction_event) not in (
                    {"event_type", "payload"}, {"event_type", "payload", "expected_proposal"},
                )
                or transaction_event.get("event_type") != "interaction.plan_accepted"
                or not isinstance(transaction_event.get("payload"), dict)
                or ("expected_proposal" in transaction_event and (
                    not isinstance(transaction_event["expected_proposal"], dict)
                    or set(transaction_event["expected_proposal"]) != {"plan_id", "version", "fingerprint"}
                ))):
            raise ValueError("only a structured plan-acceptance transaction event is supported")

        saved = self.store.get_working_plan(
            user_id=self.user_id, workspace_id=self.workspace_id, session_id=self.session_id or "",
        )
        complexity = str(complexity or (saved.get("complexity") if saved else "complex")).strip().lower()
        if complexity not in {"simple", "medium", "complex"}:
            raise ValueError("complexity must be simple, medium, or complex")
        if plan_id is None:
            if saved is not None or expected_revision != 0:
                raise ValueError("plan_id and the current revision are required to edit an existing Working Plan")
            previous_tasks: list[dict[str, Any]] = []
            plan_id = f"plan_{_task_key(f'{self.user_id}:{self.workspace_id}:{self.session_id or ''}')}_{uuid.uuid4().hex}"
            created_at = utc_now()
        else:
            if saved is None or saved["id"] != plan_id:
                raise ValueError("plan_id does not identify the active Working Plan")
            if saved.get("revision", 0) != expected_revision:
                raise ValueError("Working Plan revision changed; reload before editing")
            previous_tasks = self.store.list_working_plan_tasks(
                plan_id, user_id=self.user_id, workspace_id=self.workspace_id, session_id=self.session_id or "",
            )
            created_at = saved["created_at"]

        original_ids = {task["id"] for task in previous_tasks}
        tasks = deepcopy(previous_tasks)
        by_id = {task["id"]: task for task in tasks}
        has_strategic_tasks = any(
            isinstance(task.get("checkpoint"), dict) and isinstance(task["checkpoint"].get("strategic_plan"), dict)
            for task in previous_tasks
        )
        is_strategic_acceptance = bool(
            transaction_event and transaction_event.get("event_type") == "interaction.plan_accepted"
            and transaction_event.get("expected_proposal")
        )
        references: dict[str, str] = {}
        used_strategic_bindings: set[str] = set()
        now = utc_now()
        scope_key = _task_key(f"{self.user_id}:{self.workspace_id}:{self.session_id or ''}")

        def resolve(value: Any) -> str:
            if not isinstance(value, str) or not value:
                raise ValueError("task reference must be a nonempty string")
            task_id = references.get(value, value)
            if task_id not in by_id:
                raise ValueError(f"Unknown task or dependency reference: {value}")
            return task_id

        allowed_by_op = {
            "add": {"op", "ref", "task"}, "update": {"op", "task", "fields"},
            "cancel": {"op", "task"}, "reorder": {"op", "tasks"},
            "add_dependency": {"op", "task", "depends_on"},
            "remove_dependency": {"op", "task", "depends_on"},
        }
        if saved is None and not any(isinstance(item, dict) and item.get("op") == "add" for item in operations):
            raise ValueError("a new Working Plan requires at least one add operation")
        for operation in operations:
            if not isinstance(operation, dict) or operation.get("op") not in allowed_by_op:
                raise ValueError("each plan operation requires a supported op")
            kind = operation["op"]
            if set(operation) != allowed_by_op[kind]:
                raise ValueError(f"{kind} operation has missing or unsupported fields")
            if kind == "add":
                ref = operation["ref"]
                if not isinstance(ref, str) or not ref.strip() or ref in references or ref in by_id:
                    raise ValueError("task references must be unique nonempty strings")
                fields = self._plan_task_fields(operation["task"])
                if has_strategic_tasks and (not is_strategic_acceptance or not strategic_bindings or ref not in strategic_bindings):
                    raise ValueError("new work in a Strategic Plan requires an approved strategic binding")
                task_id = f"task_{scope_key}_{uuid.uuid4().hex}"
                task = {"id": task_id, **fields, "status": "pending", "attempts": 0,
                        "checkpoint": fields["checkpoint"], "evidence": [], "created_at": now,
                        "updated_at": now, "plan_id": plan_id}
                if strategic_bindings and ref in strategic_bindings:
                    binding = strategic_bindings[ref]
                    if (not isinstance(binding, dict)
                            or set(binding) not in ({"plan_id", "version", "fingerprint", "objective_id", "acceptance_ids",
                                                     "acceptance_verifications"},
                                                    {"plan_id", "version", "fingerprint", "objective_id", "acceptance_ids",
                                                     "acceptance_verifications", "execution"})
                            or not isinstance(binding.get("version"), int)
                            or not isinstance(binding.get("acceptance_ids"), list)
                            or not isinstance(binding.get("acceptance_verifications"), list)):
                        raise ValueError("strategic task binding is invalid")
                    accepted_payload = (transaction_event or {}).get("payload", {})
                    if (not is_strategic_acceptance
                            or any(binding.get(key) != accepted_payload.get(key)
                                   for key in ("plan_id", "version", "fingerprint"))):
                        raise ValueError("strategic task binding must match the approved plan")
                    execution = binding.get("execution")
                    if execution is not None:
                        if (not isinstance(execution, dict) or set(execution) != {"action", "arguments"}
                                or fields["checkpoint"].get("action") != _receipt_action(execution.get("action"))
                                or fields["checkpoint"].get("args") != json.dumps(
                                    execution.get("arguments"), ensure_ascii=False, allow_nan=False,
                                    sort_keys=True, separators=(",", ":"))):
                            raise ValueError("strategic task execution must match its approved contract")
                    elif fields["checkpoint"].get("action"):
                        raise ValueError("strategic task execution is missing from its approved binding")
                    task["checkpoint"] = {**task["checkpoint"], "strategic_plan": deepcopy(binding)}
                    verifications = binding["acceptance_verifications"]
                    if len(verifications) != len(fields["acceptance"]):
                        raise ValueError("strategic acceptance verifications do not match the task")
                    task["acceptance"] = []
                    for criterion_id, criterion, verification in zip(
                            binding["acceptance_ids"], fields["acceptance"], verifications):
                        item = {"id": criterion_id, "criterion": criterion}
                        if verification is not None:
                            if (not isinstance(verification, dict) or set(verification) != {"action", "arguments"}
                                    or not isinstance(verification.get("action"), str)
                                    or not isinstance(verification.get("arguments"), dict)):
                                raise ValueError("strategic acceptance verification is invalid")
                            item["action"] = _receipt_action(verification["action"])
                            item["args"] = json.dumps(verification["arguments"], ensure_ascii=False,
                                                       allow_nan=False, sort_keys=True, separators=(",", ":"))
                        task["acceptance"].append(item)
                    if len(task["acceptance"]) != len(fields["acceptance"]):
                        raise ValueError("strategic acceptance criteria do not match the task")
                    used_strategic_bindings.add(ref)
                references[ref] = task_id
                tasks.append(task)
                by_id[task_id] = task
            elif kind == "update":
                task_id = resolve(operation["task"])
                fields = operation["fields"]
                if not isinstance(fields, dict) or not fields or set(fields) - {"description", "acceptance", "dependencies", "priority", "execution"}:
                    raise ValueError("update fields are empty or unsupported")
                current = by_id[task_id]
                if (has_strategic_tasks and isinstance(current.get("checkpoint"), dict)
                        and isinstance(current["checkpoint"].get("strategic_plan"), dict)
                        and set(fields) - {"priority", "dependencies"}):
                    raise ValueError("Strategic Plan task intent cannot change without approval")
                validated = self._plan_task_fields({
                    "description": fields.get("description", current["description"]),
                    "acceptance": fields.get("acceptance", []),
                    **{key: fields[key] for key in ("dependencies", "priority", "execution") if key in fields},
                })
                normalized = {key: validated[key] for key in fields if key != "execution"}
                if "execution" in fields:
                    checkpoint = current.get("checkpoint", {})
                    unchanged_execution = all(
                        checkpoint.get(key) == validated["checkpoint"].get(key)
                        for key in ("action", "args")
                    )
                    excluded = {"action", "args"} if unchanged_execution else {
                        "action", "args", "args_sha256", "ordered_receipt",
                    }
                    base = {key: value for key, value in checkpoint.items() if key not in excluded}
                    normalized["checkpoint"] = {**base, **validated["checkpoint"]}
                changed = any(current.get(key) != value for key, value in normalized.items())
                if changed and (current["status"] != "pending" or current.get("attempts", 0)
                                or current.get("evidence")):
                    raise ValueError("executed task changes require cancellation and a new task")
                current.update(normalized, updated_at=now)
            elif kind == "cancel":
                task_id = resolve(operation["task"])
                current = by_id[task_id]
                if not valid_task_transition(current["status"], "cancelled"):
                    raise ValueError("task cannot be cancelled from its current state")
                current.update(status="cancelled", updated_at=now)
            elif kind == "reorder":
                requested = operation["tasks"]
                if not isinstance(requested, list):
                    raise ValueError("reorder tasks must be an array")
                ordered = [resolve(value) for value in requested]
                if len(ordered) != len(tasks) or len(set(ordered)) != len(tasks) or set(ordered) != set(by_id):
                    raise ValueError("reorder must list every plan task exactly once")
                tasks = [by_id[task_id] for task_id in ordered]
            else:
                task_id = resolve(operation["task"])
                dependency_id = resolve(operation["depends_on"])
                if task_id == dependency_id:
                    raise ValueError("a task cannot depend on itself")
                current = by_id[task_id]
                dependencies = list(current.get("dependencies", []))
                if kind == "add_dependency":
                    if dependency_id in dependencies:
                        raise ValueError("duplicate dependency")
                    if current["status"] != "pending" or current.get("attempts", 0) or current.get("evidence"):
                        raise ValueError("executed task dependencies require cancellation and a new task")
                    dependencies.append(dependency_id)
                else:
                    if dependency_id not in dependencies:
                        raise ValueError("dependency does not exist")
                    if current["status"] != "pending" or current.get("attempts", 0) or current.get("evidence"):
                        raise ValueError("executed task dependencies require cancellation and a new task")
                    dependencies.remove(dependency_id)
                current["dependencies"] = dependencies

        for task in tasks:
            task["dependencies"] = [resolve(value) for value in task.get("dependencies", [])]
            if len(set(task["dependencies"])) != len(task["dependencies"]):
                raise ValueError("task dependencies must be unique")
        dependents: dict[str, list[str]] = {task["id"]: [] for task in tasks}
        remaining: dict[str, int] = {}
        for task in tasks:
            dependencies = task["dependencies"]
            remaining[task["id"]] = len(dependencies)
            for dependency in dependencies:
                dependents[dependency].append(task["id"])
        ready = [task_id for task_id, count in remaining.items() if count == 0]
        resolved = 0
        while ready:
            task_id = ready.pop()
            resolved += 1
            for dependent in dependents[task_id]:
                remaining[dependent] -= 1
                if remaining[dependent] == 0:
                    ready.append(dependent)
        if resolved != len(tasks):
            raise ValueError("dependency cycle detected")
        if used_strategic_bindings != set(strategic_bindings or {}):
            raise ValueError("strategic bindings must identify added task references exactly")

        for task in tasks:
            task["plan_id"] = plan_id
        terminal = bool(tasks) and all(canonical_status(task["status"]) in {"succeeded", "cancelled"}
                                       for task in tasks)
        completed = terminal and any(canonical_status(task["status"]) == "succeeded" for task in tasks) and all(
            canonical_status(task["status"]) != "succeeded" or any(
                evidence.get("success") is True and evidence.get("acceptance")
                for evidence in task.get("evidence", []) if isinstance(evidence, dict)
            ) for task in tasks
        )
        plan_status = "completed" if completed else "cancelled" if terminal else "active"
        plan = {"id": plan_id, "complexity": complexity, "status": plan_status,
                "created_at": created_at, "updated_at": now, "revision": expected_revision + 1}
        revision = self.store.apply_working_plan_edit(
            plan, tasks, expected_revision=expected_revision, expected_tasks=previous_tasks,
            user_id=self.user_id, workspace_id=self.workspace_id, session_id=self.session_id,
            transaction_event=(
                {"event_type": transaction_event["event_type"], **(
                    {"expected_proposal": transaction_event["expected_proposal"]}
                    if "expected_proposal" in transaction_event else {}
                ), "payload": {
                    **transaction_event["payload"], "working_plan_id": plan_id,
                    "working_plan_revision": expected_revision + 1,
                    "working_task_ids": [task["id"] for task in tasks],
                }} if transaction_event else None
            ),
        )
        working_plan = WorkingPlan(
            id=plan_id, complexity=complexity, status=plan_status, task_ids=tuple(task["id"] for task in tasks),
            created_at=created_at, updated_at=now, revision=revision,
        )
        self.working_plan = working_plan
        self.tasks = tasks + [task for task in self.tasks if task["id"] not in original_ids]
        payload = {"plan_id": plan_id, "revision": revision, "task_ids": list(working_plan.task_ids),
                   "status": plan_status}
        callback = self._runtime_event_callback.get()
        if callable(callback):
            try:
                event_type = ("working_plan.completed" if plan_status == "completed"
                              else "working_plan.cancelled" if plan_status == "cancelled"
                              else "working_plan.updated")
                callback(event_type, payload, tasks[-1]["id"] if tasks else None)
            except Exception:
                pass
        return {**payload, "complexity": complexity, "tasks": deepcopy(tasks),
                "references": dict(references)}

    def update_plan(self, *, complexity: str, tasks: list[dict[str, Any]] | None = None,
                    plan_id: str | None = None, expected_revision: int | None = None,
                    operations: list[dict[str, Any]] | None = None) -> WorkingPlan | dict[str, Any]:
        """Apply canonical operations or adapt the historical complexity/tasks shape."""
        if operations is not None:
            if expected_revision is None:
                raise ValueError("expected_revision is required for plan edits")
            return self.edit_plan(complexity=complexity, plan_id=plan_id,
                                  expected_revision=expected_revision, operations=operations)
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("a Working Plan requires at least one task")
        saved = self.store.get_working_plan(
            user_id=self.user_id, workspace_id=self.workspace_id, session_id=self.session_id or "",
        )
        expected_revision = int(saved.get("revision", 0)) if saved else 0
        operations: list[dict[str, Any]] = []
        previous = self.store.list_working_plan_tasks(
            saved["id"], user_id=self.user_id, workspace_id=self.workspace_id,
            session_id=self.session_id or "",
        ) if saved else []
        by_id = {task["id"]: task for task in previous}
        for item in tasks:
            if not isinstance(item, dict):
                raise ValueError("each plan task must be an object")
            fields = {key: value for key, value in item.items() if key != "id"}
            task_id = item.get("id")
            if task_id is not None:
                if task_id not in by_id:
                    raise ValueError("task id must identify a task in the active Working Plan")
                operations.append({"op": "update", "task": task_id, "fields": fields})
            else:
                ref = f"legacy_{len(operations)}"
                operations.append({"op": "add", "ref": ref, "task": fields})
        if saved is None:
            result = self.edit_plan(complexity=complexity, expected_revision=0, operations=operations)
        else:
            result = self.edit_plan(complexity=complexity, plan_id=saved["id"],
                                    expected_revision=expected_revision, operations=operations)
        return self.working_plan

    def adopt_legacy_plan(self, *, complexity: str = "complex") -> WorkingPlan:
        """Persist tasks created by the legacy textual adapter without changing their IDs."""
        if complexity not in {"simple", "medium", "complex"}:
            raise ValueError("Unknown task complexity")
        if self.working_plan is not None:
            return self.working_plan
        if not self.tasks:
            raise ValueError("cannot create a Working Plan without tasks")
        now = utc_now()
        scope_key = _task_key(f"{self.user_id}:{self.workspace_id}:{self.session_id or ''}")
        plan_id = f"plan_{scope_key}_{uuid.uuid4().hex}"
        plan_tasks = [dict(task, plan_id=plan_id) for task in self.tasks]
        plan = {
            "id": plan_id, "complexity": complexity, "status": "active",
            "created_at": now, "updated_at": now,
        }
        self.store.write_working_plan(
            plan, plan_tasks, user_id=self.user_id, workspace_id=self.workspace_id,
            session_id=self.session_id,
        )
        self.working_plan = WorkingPlan(
            id=plan_id, complexity=complexity, status="active",
            task_ids=tuple(task["id"] for task in plan_tasks),
            created_at=now, updated_at=now,
        )
        self._event(plan_tasks[0], "working_plan.migrated", {
            "plan_id": plan_id, "complexity": complexity, "task_ids": list(self.working_plan.task_ids),
        })
        return self.working_plan

    def _has_evidence(self, idx: int, evidence: dict[str, Any] | None = None) -> bool:
        evidence_items = ([evidence] if evidence else []) + self.tasks[idx].get("evidence", [])
        return any(item.get("success") is True and item.get("acceptance") for item in evidence_items)

    def _receipt_match_score(self, task: dict[str, Any], action: str, args: str,
                            args_fingerprint: str | None = None) -> int:
        """Match only an explicit action/argument contract on the task."""
        action = _receipt_action(action)
        args = _receipt_args(args)
        checkpoint = task.get("checkpoint") or {}
        checkpoint_action = _receipt_action(checkpoint.get("action"))
        checkpoint_args = _receipt_args(checkpoint.get("args"))
        if checkpoint_action:
            if checkpoint_action != action:
                return -1
            if checkpoint_args:
                checkpoint_fingerprint = (checkpoint.get("args_sha256")
                                          or hashlib.sha256(checkpoint_args.encode("utf-8")).hexdigest())
                if args_fingerprint:
                    if checkpoint_fingerprint != args_fingerprint:
                        return -1
                elif checkpoint_args != args:
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
            if criterion_args:
                criterion_fingerprint = hashlib.sha256(criterion_args.encode("utf-8")).hexdigest()
                if args_fingerprint:
                    if criterion_fingerprint != args_fingerprint:
                        continue
                elif criterion_args != args:
                    continue
            score = max(score, 120 if criterion_args else 100)
        return score or -1

    def _match_receipt_task(self, action: str, args: str,
                            args_fingerprint: str | None = None) -> dict[str, Any] | None:
        candidates = [
            task for task in self.tasks
            if (self.working_plan is None or task["id"] in self.working_plan.task_ids)
            and canonical_status(task.get("status", "pending")) in {"pending", "running"}
            and self._dependencies_succeeded(task, self.tasks)
        ]
        scored = [(self._receipt_match_score(task, action, args, args_fingerprint), task)
                  for task in candidates]
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
        pending = [task for task in ordered_tasks
                   if canonical_status(task.get("status", "pending")) in {"pending", "running"}
                   and self._dependencies_succeeded(task, self.tasks)]
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

    def mutation_matches_current_task(self, action: str, args: str) -> tuple[bool, str]:
        """Authorize a mutation only when it belongs to the next ordered task."""
        current = next((
            task for task in self.tasks
            if (self.working_plan is None or task["id"] in self.working_plan.task_ids)
            and canonical_status(task.get("status", "pending")) in {"pending", "running"}
            and self._dependencies_succeeded(task, self.tasks)
        ), None)
        if current is None:
            return False, "the accepted plan has no pending task"
        if self._receipt_match_score(current, action, args) > 0:
            return True, ""
        if _ordered_plan_info(current) is None or not _ordered_action_compatible(
                current.get("description", ""), action):
            return False, f"the next accepted step is: {current.get('description', '')}"
        if _receipt_action(action) == "write_file":
            try:
                structured = json.loads(args)
            except (TypeError, json.JSONDecodeError):
                structured = None
            raw_target = (structured.get("path") if isinstance(structured, dict)
                          else str(args).split("|", 1)[0])
            target = str(raw_target or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
            if not target or target.lower() not in str(current.get("description", "")).lower():
                return False, f"the next accepted step does not name {target or 'that file'}"
        return True, ""

    def record_evidence(self, *, task_id: str | None = None, action: str, result: str, success: bool,
                        acceptance: str | None = None, args: str | None = None,
                        args_projection: str | None = None,
                        args_fingerprint: str | None = None, receipt_id: str | None = None,
                        verification_status: str | None = None) -> dict[str, Any]:
        canonical = _receipt_action(action)
        normalized_args = _receipt_args(args)
        persisted_args = _receipt_args(args_projection) if args_projection is not None else normalized_args
        item = {"action": canonical, "result": str(result)[:2000], "success": bool(success)}
        requested_task_id = str(task_id) if task_id else None
        if args:
            item["args"] = persisted_args[:1000]
        if args_fingerprint:
            item["args_sha256"] = args_fingerprint
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
                    candidate, canonical, normalized_args, args_fingerprint) >= 0):
                targets = [candidate]
                item["task_id"] = requested_task_id
        if not task_id:
            matched = self._match_receipt_task(canonical, normalized_args, args_fingerprint)
            if matched is not None:
                targets = [matched]
                inferred = True
                ordered_inferred = _ordered_plan_info(matched) is not None
                item["task_id"] = matched["id"]
                if success and not acceptance:
                    item["acceptance"] = f"verified receipt for planned action {canonical}"
        persisted_targets = []
        for task in targets:
            candidate = deepcopy(task)
            if success and verification_status in {"verified", "not_applicable"}:
                verified_ids = [criterion["id"] for criterion in candidate.get("acceptance", [])
                                if isinstance(criterion, dict) and criterion.get("id")
                                and criterion.get("action") == canonical
                                and criterion.get("args") == normalized_args]
                if verified_ids:
                    item["acceptance_ids"] = verified_ids
                    item.setdefault("acceptance", "verified strategic criterion")
            if ordered_inferred:
                marker = _ordered_plan_info(task)
                if marker:
                    item["ordered_plan"] = marker
                candidate["checkpoint"] = {
                    **(task.get("checkpoint") or {}), "action": canonical,
                    "args": persisted_args, "ordered_receipt": True,
                    **({"args_sha256": args_fingerprint} if args_fingerprint else {}),
                }
            elif inferred and not (task.get("checkpoint") or {}).get("action"):
                candidate["checkpoint"] = {
                    **(task.get("checkpoint") or {}),
                    "action": canonical, "args": persisted_args,
                    **({"args_sha256": args_fingerprint} if args_fingerprint else {}),
                }
            candidate.setdefault("evidence", []).append(item)
            candidate["updated_at"] = utc_now()
            if self._persist(candidate, expected_status=task.get("status", "pending")):
                task.update(candidate)
                persisted_targets.append(task)
            else:
                latest = self._reload_task(task["id"])
                if latest is not None:
                    self._replace_task(latest)
                item["unmatched_reason"] = "Task status changed before evidence was recorded."
        targets = persisted_targets
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
            if (task.get("status") in {"pending", "running"}
                    and self._dependencies_succeeded(task, self.tasks)):
                return task["id"]
        return None

    def active_working_task(self, task_id: str) -> dict[str, Any] | None:
        """Return a task only when it is pending in this active durable plan."""
        plan = self.working_plan
        if plan is None or plan.status != "active" or task_id not in plan.task_ids:
            return None
        task = next((item for item in self.tasks if item.get("id") == task_id), None)
        if (task is None or task.get("plan_id") != plan.id
                or canonical_status(task.get("status", "pending")) not in {"pending", "running"}
                or not self._dependencies_succeeded(task, self.tasks)):
            return None
        return task

    def _replace_task(self, task: dict[str, Any]) -> int:
        for index, existing in enumerate(self.tasks):
            if existing["id"] == task["id"]:
                self.tasks[index] = task
                return index
        self.tasks.append(task)
        return len(self.tasks) - 1

    def claim_next(self) -> dict[str, Any] | None:
        """Atomically claim one pending task in this exact durable scope."""
        task = self.store.claim_task(user_id=self.user_id, workspace_id=self.workspace_id, session_id=self.session_id)
        if task is None:
            return None
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
        task = deepcopy(self.tasks[index])
        expected_status = self.tasks[index].get("status", "pending")
        task["checkpoint"] = {**task.get("checkpoint", {}), **checkpoint}
        task["updated_at"] = utc_now()
        if not self._persist(task, expected_status=expected_status):
            latest = self._reload_task(task["id"])
            if latest is not None:
                self.tasks[index] = latest
            return False
        self.tasks[index] = task
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
        updated = deepcopy(task)
        updated["status"] = "pending"
        updated["updated_at"] = utc_now()
        if not self._persist(updated, expected_status=previous):
            latest = self._reload_task(task["id"])
            if latest is not None:
                self.tasks[index] = latest
            return None
        self.tasks[index] = updated
        task = updated
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
        if self.working_plan is None:
            saved_plan = self.store.get_working_plan(
                user_id=self.user_id, workspace_id=self.workspace_id, session_id=self.session_id or "",
            )
            if saved_plan is not None:
                self.recover()
        if self.working_plan is not None:
            plan = self.working_plan
            plan_ids = set(plan.task_ids)
            for index, task in enumerate(self.tasks):
                if task["id"] in plan_ids and task["status"] not in {"succeeded", "cancelled"}:
                    if not self.set_status(index, "cancelled", _terminalize_plan=False):
                        return
            plan = self.working_plan or plan
            if plan.status == "active" and self.store.set_working_plan_status(
                plan.id, "cancelled", user_id=self.user_id, workspace_id=self.workspace_id,
                session_id=self.session_id, expected_revision=plan.revision,
                expected_task_ids=plan.task_ids,
            ):
                plan_task = next((task for task in self.tasks if task["id"] in plan_ids), None)
                if plan_task is not None:
                    self._event(plan_task, "working_plan.cancelled", {"plan_id": plan.id})
            elif plan.status == "active":
                return
            self.working_plan = None
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
                if self._persist(task, expected_status="running"):
                    self._event(task, "task.recovered", {"previous_status": "running"})
                else:
                    latest = self._reload_task(task["id"])
                    if latest is not None:
                        task.update(latest)
        saved_plan = self.store.get_working_plan(
            user_id=self.user_id, workspace_id=self.workspace_id, session_id=self.session_id or "",
        )
        if saved_plan:
            plan_tasks = self.store.list_working_plan_tasks(
                saved_plan["id"], user_id=self.user_id, workspace_id=self.workspace_id,
                session_id=self.session_id or "",
            )
            for task in plan_tasks:
                if task["status"] == "running":
                    task["status"] = "pending"
                    task["updated_at"] = utc_now()
                    if self._persist(task, expected_status="running"):
                        self._event(task, "task.recovered", {"previous_status": "running"})
                    else:
                        latest = self._reload_task(task["id"])
                        if latest is not None:
                            task.update(latest)
            terminal = bool(plan_tasks) and all(
                task["status"] in {"succeeded", "done", "cancelled"} for task in plan_tasks
            )
            completed = terminal and any(task["status"] in {"succeeded", "done"} for task in plan_tasks) and all(
                task["status"] not in {"succeeded", "done"} or any(
                    item.get("success") is True and item.get("acceptance")
                    for item in task.get("evidence", []) if isinstance(item, dict)
                ) for task in plan_tasks
            )
            plan_status = "completed" if completed else "cancelled" if terminal else "active"
            if (plan_status != "active" and saved_plan["status"] == "active"
                    and self.store.set_working_plan_status(
                        saved_plan["id"], plan_status, user_id=self.user_id,
                        workspace_id=self.workspace_id, session_id=self.session_id,
                        expected_revision=int(saved_plan.get("revision", 0)),
                        expected_task_ids=[task["id"] for task in plan_tasks],
                    )):
                saved_plan["status"] = plan_status
                saved_plan["updated_at"] = utc_now()
                event_type = "working_plan.completed" if plan_status == "completed" else "working_plan.cancelled"
                self._event(plan_tasks[-1], event_type, {"plan_id": saved_plan["id"]})
            self.working_plan = WorkingPlan(
                id=saved_plan["id"], complexity=saved_plan["complexity"], status=saved_plan["status"],
                task_ids=tuple(task["id"] for task in plan_tasks),
                created_at=saved_plan["created_at"], updated_at=saved_plan["updated_at"],
                revision=int(saved_plan.get("revision", 0)),
            )
            plan_ids = set(self.working_plan.task_ids)
            self.tasks = plan_tasks + [task for task in unfinished if task["id"] not in plan_ids]
            return unfinished
        self.working_plan = None
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
            ) + [task for task in unfinished if _ordered_plan_info(task) is None]
        else:
            self.tasks = unfinished
        return unfinished

    def restore_working_plan(self, plan_id: str) -> WorkingPlan | None:
        saved = self.store.get_working_plan_by_id(
            plan_id, user_id=self.user_id, workspace_id=self.workspace_id,
            session_id=self.session_id or "",
        )
        if saved is None:
            return None
        tasks = self.store.list_working_plan_tasks(
            plan_id, user_id=self.user_id, workspace_id=self.workspace_id,
            session_id=self.session_id or "",
        )
        self.working_plan = WorkingPlan(
            id=saved["id"], complexity=saved["complexity"], status=saved["status"],
            task_ids=tuple(task["id"] for task in tasks), created_at=saved["created_at"],
            updated_at=saved["updated_at"], revision=int(saved.get("revision", 0)),
        )
        self.tasks = tasks + [task for task in self.tasks if task["id"] not in self.working_plan.task_ids]
        return self.working_plan

from .worker import TaskWorker
