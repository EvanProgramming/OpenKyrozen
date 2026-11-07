"""Feature-owned Strategic Plan validation, approval transitions and completion.

TaskManager owns Working Plan edits; PlanningService coordinates approved
objectives with those edits through the same atomic storage contracts.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
import uuid
from copy import deepcopy
from typing import Any

from .ports import InteractionStore

class InteractionError(ValueError):
    """A bounded interaction payload failed validation."""


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise InteractionError(f"{field} must be text")
    value = value.strip()
    if not value or len(value) > limit:
        raise InteractionError(f"{field} must be 1-{limit} characters")
    return value


def validate_plan_proposal(value: Any, *, previous: dict[str, Any] | None = None,
                           original_input: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InteractionError("PlanProposal must contain a JSON object")
    steps = value.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 10:
        raise InteractionError("PlanProposal requires 1-10 steps")
    previous_version = int(previous.get("version", 0)) if previous else 0
    version = previous_version + 1
    normalised = []
    seen_ids: set[str] = set()
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise InteractionError("each plan step must be an object")
        step_id = _text(step.get("id") or f"step-{index}", "step id", 64)
        if step_id in seen_ids:
            raise InteractionError("plan step ids must be unique")
        seen_ids.add(step_id)
        execution = step.get("execution")
        if execution is not None:
            action = execution.get("action") if isinstance(execution, dict) else None
            arguments = execution.get("arguments") if isinstance(execution, dict) else None
            if (not isinstance(execution, dict) or set(execution) != {"action", "arguments"}
                    or not isinstance(action, str) or action not in {"write_file", "edit_file", "apply_patch"}
                    or not isinstance(arguments, dict)
                    or not isinstance(arguments.get("path"), str) or not arguments["path"].strip()):
                raise InteractionError("file execution contract must name write_file, edit_file, or apply_patch and an exact path")
            required = ({"path", "content"} if action == "write_file" else
                        {"path", "expected_sha256", "hunks"} if action == "apply_patch" else
                        {"path", "old_text", "new_text", "expected_sha256"})
            if set(arguments) != required or (action != "apply_patch" and any(not isinstance(item, str) for item in arguments.values())):
                raise InteractionError("file execution arguments must exactly match the selected file operation")
            if action == "edit_file" and (
                    not arguments["old_text"] or not re.fullmatch(r"[0-9a-fA-F]{64}", arguments["expected_sha256"])):
                raise InteractionError("edit_file requires nonempty old_text and a SHA-256 read hash")
            if action == "apply_patch" and (
                    not isinstance(arguments["path"], str) or not arguments["path"].strip()
                    or not isinstance(arguments["expected_sha256"], str)
                    or not re.fullmatch(r"[0-9a-fA-F]{64}", arguments["expected_sha256"])
                    or not isinstance(arguments["hunks"], list) or not arguments["hunks"]
                    or any(not isinstance(hunk, dict) or set(hunk) != {"old_text", "new_text"}
                           or not isinstance(hunk.get("old_text"), str) or not hunk["old_text"]
                           or not isinstance(hunk.get("new_text"), str) for hunk in arguments["hunks"])):
                raise InteractionError("apply_patch requires a SHA-256 read hash and exact-text hunks")
            try:
                json.dumps(arguments, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise InteractionError("file execution arguments must be JSON-safe") from exc
            execution = {"action": action, "arguments": deepcopy(arguments)}
        acceptance = step.get("acceptance")
        if isinstance(acceptance, str):
            acceptance = [acceptance]
        if not isinstance(acceptance, list) or not 1 <= len(acceptance) <= 5:
            raise InteractionError("each plan step requires 1-5 acceptance criteria")
        supplied_verifications = step.get("acceptance_verifications")
        if supplied_verifications is not None and (
                not isinstance(supplied_verifications, list) or len(supplied_verifications) != len(acceptance)):
            raise InteractionError("acceptance verifications must align with acceptance criteria")
        acceptance_verifications = []
        normalized_acceptance = []
        for criterion_index, item in enumerate(acceptance):
            verification = supplied_verifications[criterion_index] if supplied_verifications is not None else None
            if isinstance(item, dict) and set(item) == {"criterion", "verification"}:
                verification = item["verification"]
            if verification is not None:
                if (not isinstance(verification, dict)
                        or set(verification) not in ({"action", "arguments"}, {"action", "arguments", "expect"})
                        or not isinstance(verification.get("action"), str)
                        or not verification["action"].strip() or not isinstance(verification.get("arguments"), dict)):
                    raise InteractionError("acceptance verification must contain an action and object arguments")
                if verification["action"] == "read_file":
                    expect = verification.get("expect")
                    if (not isinstance(expect, dict) or len(expect) != 1
                            or set(expect) not in ({"equals"}, {"contains"})
                            or not isinstance(next(iter(expect.values())), str)
                            or ("contains" in expect and not expect["contains"])):
                        raise InteractionError("read_file verification requires a nonempty equals or contains predicate")
                elif "expect" in verification:
                    raise InteractionError("expected-result predicates are currently supported only for read_file")
                try:
                    json.dumps(verification["arguments"], ensure_ascii=False, allow_nan=False)
                except (TypeError, ValueError) as exc:
                    raise InteractionError("acceptance verification arguments must be JSON-safe") from exc
            criterion = _text(item.get("criterion") if isinstance(item, dict) else item,
                              "acceptance criterion", 300)
            normalized_acceptance.append(criterion)
            acceptance_verifications.append(verification)
        acceptance = normalized_acceptance
        acceptance_ids = []
        for criterion, verification in zip(acceptance, acceptance_verifications):
            identity = json.dumps({"criterion": criterion, "verification": verification}, sort_keys=True,
                                  ensure_ascii=False, separators=(",", ":"))
            acceptance_ids.append(f"{step_id}-ac-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:12]}")
        if len(set(acceptance_ids)) != len(acceptance_ids):
            raise InteractionError("acceptance criteria must be unique within each objective")
        normalised_step = {
            "id": step_id,
            "title": _text(step.get("title") or f"Step {index}", "step title", 120),
            "description": _text(step.get("description"), "step description", 800),
            "acceptance": acceptance,
            "acceptance_ids": acceptance_ids,
            "acceptance_verifications": acceptance_verifications,
        }
        if execution is not None:
            normalised_step["execution"] = execution
        normalised.append(normalised_step)
    assumptions = value.get("assumptions", [])
    if assumptions is None:
        assumptions = []
    if not isinstance(assumptions, list) or len(assumptions) > 10:
        raise InteractionError("assumptions must contain at most 10 items")
    raw_bounds = value.get("bounds")
    bounds_source = "proposal"
    if raw_bounds is None:
        if "bounds" in value:
            raise InteractionError("bounds must be an object")
        raw_bounds = {
            "workspace_paths": ["."], "capabilities": ["read"],
            "effects": ["read_only"], "risk_ceiling": "normal",
        }
        bounds_source = "legacy-default"
    if not isinstance(raw_bounds, dict) or set(raw_bounds) - {
        "workspace_paths", "capabilities", "effects", "risk_ceiling",
    }:
        raise InteractionError("bounds must declare workspace paths, capabilities, effects, and risk ceiling")

    def values(field: str, allowed: set[str] | None = None) -> list[str]:
        raw = raw_bounds.get(field, [])
        if not isinstance(raw, list) or len(raw) > 32:
            raise InteractionError(f"bounds.{field} must be an array with at most 32 values")
        result = []
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                raise InteractionError(f"bounds.{field} entries must be nonempty strings")
            item = item.strip()
            if allowed is not None and item not in allowed:
                raise InteractionError(f"unsupported bounds.{field} value: {item}")
            result.append(item)
        if len(set(result)) != len(result):
            raise InteractionError(f"bounds.{field} values must be unique")
        return result

    paths = values("workspace_paths")
    normalised_paths = []
    for path in paths:
        path = path.replace("\\", "/")
        if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
            raise InteractionError("workspace paths must be relative to the active workspace")
        path = posixpath.normpath(path)
        if path == ".." or path.startswith("../"):
            raise InteractionError("workspace paths cannot escape the active workspace")
        normalised_paths.append(path)
    if len(set(normalised_paths)) != len(normalised_paths):
        raise InteractionError("workspace paths must be unique after normalization")
    capabilities = values("capabilities", {
        "read", "write", "shell", "network", "git", "browser", "destructive",
    })
    effects = values("effects", {
        "read_only", "filesystem", "process", "remote", "destructive", "internal",
    })
    risk_ceiling = raw_bounds.get("risk_ceiling", "normal")
    if not isinstance(risk_ceiling, str) or risk_ceiling not in {"normal", "high"}:
        raise InteractionError("risk ceiling must be normal or high")
    bounds = {
        "workspace_paths": sorted(normalised_paths), "capabilities": sorted(capabilities),
        "effects": sorted(effects), "risk_ceiling": risk_ceiling,
    }
    contract_policies = {
        "read_file": ("read", "read_only"),
        "write_file": ("write", "filesystem"),
        "edit_file": ("write", "filesystem"),
        "apply_patch": ("write", "filesystem"),
    }
    for step in normalised:
        contracts = ([step["execution"]] if step.get("execution") else []) + [
            verification for verification in step["acceptance_verifications"] if verification
        ]
        for contract in contracts:
            policy = contract_policies.get(contract["action"])
            if policy is None:
                raise InteractionError(f"unsupported Strategic Plan contract action: {contract['action']}")
            capability, effect = policy
            if capability not in bounds["capabilities"] or effect not in bounds["effects"]:
                raise InteractionError("Strategic Plan contract exceeds its approved capability or effect bounds")
            path = contract["arguments"].get("path")
            if not isinstance(path, str) or not path.strip():
                raise InteractionError("Strategic Plan file contracts require a path")
            normalized_contract_path = posixpath.normpath(path.replace("\\", "/"))
            if (normalized_contract_path.startswith("/") or normalized_contract_path == ".."
                    or normalized_contract_path.startswith("../")):
                raise InteractionError("Strategic Plan contract path escapes the approved workspace")
            if not any(root == "." or normalized_contract_path == root
                       or normalized_contract_path.startswith(root.rstrip("/") + "/")
                       for root in bounds["workspace_paths"]):
                raise InteractionError("Strategic Plan contract path is outside its approved workspace bounds")
    plan = {
        "plan_id": _text(
            (previous or {}).get("plan_id") or f"plan_{uuid.uuid4().hex}",
            "plan_id", 100,
        ),
        "version": version,
        "title": _text(value.get("title"), "plan title", 160),
        "summary": _text(value.get("summary"), "plan summary", 1200),
        "assumptions": [_text(item, "assumption", 300) for item in assumptions],
        "steps": normalised,
        "bounds": bounds,
        "bounds_source": bounds_source,
        "original_input": str(original_input).strip()[:12_000],
    }
    fingerprint_fields = {key: plan[key] for key in (
        "plan_id", "version", "title", "summary", "assumptions", "steps", "bounds",
    )}
    plan["fingerprint"] = hashlib.sha256(json.dumps(
        fingerprint_fields, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return plan


def _revision_materiality(accepted: dict[str, Any], proposal: dict[str, Any]) -> str:
    """Allow unchanged objectives only when all revised bounds are provably narrower."""
    if any(accepted.get(field) != proposal.get(field)
           for field in ("title", "summary", "assumptions")):
        return "material"
    objectives = lambda plan: {
        step["id"]: (step["title"], step["description"], tuple(step["acceptance"]),
                     tuple(step["acceptance_verifications"]), step.get("execution"))
        for step in plan.get("steps", ())
    }
    if objectives(accepted) != objectives(proposal):
        return "material"
    old, new = accepted.get("bounds"), proposal.get("bounds")
    if not isinstance(old, dict) or not isinstance(new, dict):
        return "material"
    if (not set(new.get("capabilities", ())) <= set(old.get("capabilities", ()))
            or not set(new.get("effects", ())) <= set(old.get("effects", ()))
            or (old.get("risk_ceiling") == "normal" and new.get("risk_ceiling") == "high")):
        return "material"
    roots = [posixpath.normpath(path) for path in old.get("workspace_paths", ())]
    for path in map(posixpath.normpath, new.get("workspace_paths", ())):
        if not any(root == "." or path == root or path.startswith(root.rstrip("/") + "/")
                   for root in roots):
            return "material"
    return "within_bounds"



class PlanningService:
    """Own Strategic Plan state and its binding to a Working Plan."""

    def __init__(self, store: InteractionStore, *, user_id: str = "local", workspace_id: str = "default",
                 session_id: str):
        self.store = store
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.session_id = session_id


    def _append(self, event_type: str, payload: dict[str, Any]) -> None:
        self.store.append_event(
            event_type, payload, user_id=self.user_id, workspace_id=self.workspace_id,
            session_id=self.session_id,
        )


    def _append_auto_accepted(self, plan: dict[str, Any], previous: dict[str, Any]) -> None:
        append = getattr(self.store, "append_plan_auto_accepted", None)
        if not callable(append):
            raise InteractionError("atomic Strategic Plan revision is unavailable")
        try:
            append(plan, expected_previous=previous, user_id=self.user_id,
                   workspace_id=self.workspace_id, session_id=self.session_id)
        except ValueError as exc:
            raise InteractionError(str(exc)) from exc


    def _history(self) -> list[dict[str, Any]]:
        if callable(getattr(self.store, "list_interaction_events", None)):
            return self.store.list_interaction_events(
                workspace_id=self.workspace_id, session_id=self.session_id, user_id=self.user_id,
            )
        events = self.store.list_events(
            limit=None, workspace_id=self.workspace_id, session_id=self.session_id,
            user_id=self.user_id,
        )
        return [event for event in reversed(events)
                if str(event.get("event_type", "")).startswith("interaction.")
                or event.get("event_type") == "history.restored"]


    @staticmethod
    def _plan_state(history: list[dict[str, Any]]) -> dict[str, Any]:
        plan = None
        executing = None
        for event in history:
            event_type, payload = event["event_type"], event.get("payload", {})
            if event_type == "history.restored":
                restored = payload.get("interaction") if isinstance(payload, dict) else None
                if isinstance(restored, dict):
                    plan = restored.get("pending_plan")
                    executing = restored.get("executing_plan")
            elif event_type == "interaction.plan_proposed":
                if payload.get("revision_materiality") != "within_bounds":
                    plan = payload
            elif event_type == "interaction.plan_auto_accepted":
                if executing and payload.get("plan_id") == executing.get("plan_id"):
                    executing = dict(payload)
                    plan = None
            elif event_type == "interaction.plan_accepted":
                if plan and payload.get("plan_id") == plan.get("plan_id") and payload.get("version") == plan.get("version"):
                    executing = dict(plan)
                    for field in ("working_plan_id", "working_plan_revision", "working_task_ids"):
                        if field in payload:
                            executing[field] = payload[field]
                    plan = None
            elif event_type == "interaction.plan_cancelled":
                if plan and payload.get("plan_id") == plan.get("plan_id"):
                    plan = None
            elif event_type == "interaction.plan_completed":
                if (executing and payload.get("plan_id") == executing.get("plan_id")
                        and payload.get("version") == executing.get("version")
                        and payload.get("fingerprint") == executing.get("fingerprint")
                        and payload.get("status") in {"completed", "cancelled"}):
                    executing = None
        return {"pending_plan": plan, "executing_plan": executing}

    def state(self) -> dict[str, Any]:
        """Reconstruct Strategic Plan authority from the scoped durable history."""
        return self._plan_state(self._history())


    def propose_plan(self, value: Any, *, original_input: str = "") -> dict[str, Any]:
        state = self.state()
        previous = state.get("pending_plan") or state.get("executing_plan")
        if previous and state.get("pending_plan") is None:
            latest = [int(event.get("payload", {}).get("version", 0)) for event in self._history()
                      if event.get("event_type") in {"interaction.plan_proposed", "interaction.plan_auto_accepted"}
                      and event.get("payload", {}).get("plan_id") == previous.get("plan_id")]
            if latest:
                previous = {**previous, "version": max(latest)}
        plan = validate_plan_proposal(value, previous=previous, original_input=original_input)
        executing = state.get("executing_plan")
        if executing:
            plan["revision_materiality"] = _revision_materiality(executing, plan)
            working = self.store.get_working_plan(
                user_id=self.user_id, workspace_id=self.workspace_id, session_id=self.session_id,
            ) if callable(getattr(self.store, "get_working_plan", None)) else None
            working_revision = (working.get("revision") if working
                                and working.get("id") == executing.get("working_plan_id")
                                else executing.get("working_plan_revision"))
            plan.update({"working_plan_id": executing.get("working_plan_id"),
                         "working_plan_revision": working_revision,
                         "working_task_ids": executing.get("working_task_ids", [])})
            if plan["revision_materiality"] == "within_bounds":
                self._append_auto_accepted(plan, executing)
                return plan
        else:
            plan["revision_materiality"] = "initial"
        self._append("interaction.plan_proposed", plan)
        return plan


    def cancel_plan(self, plan_id: str | None = None, version: int | None = None) -> dict[str, Any]:
        plan = self.state().get("pending_plan")
        if not plan or (plan_id and plan_id != plan.get("plan_id")) or (version is not None and version != plan.get("version")):
            raise InteractionError("plan is not pending")
        self._append("interaction.plan_cancelled", {"plan_id": plan["plan_id"], "version": plan["version"]})
        return plan


    def accepted_plan(self, plan_id: str | None = None, version: int | None = None) -> dict[str, Any] | None:
        proposals: dict[tuple[str, int], dict[str, Any]] = {}
        accepted: dict[tuple[str, int], dict[str, Any]] = {}
        for event in self._history():
            payload = event.get("payload", {})
            key = (str(payload.get("plan_id") or ""), int(payload.get("version") or 0))
            if event["event_type"] == "interaction.plan_proposed":
                proposals[key] = payload
            elif event["event_type"] == "interaction.plan_accepted":
                accepted[key] = payload
            elif event["event_type"] == "interaction.plan_auto_accepted":
                proposals[key] = payload
                accepted[key] = payload
        matches = [proposal for key, proposal in proposals.items() if key in accepted
                   and (not plan_id or key[0] == plan_id) and (version is None or key[1] == version)]
        if not matches:
            return None
        proposal = matches[-1]
        key = (str(proposal.get("plan_id") or ""), int(proposal.get("version") or 0))
        return {**proposal, **{field: accepted[key][field] for field in (
            "working_plan_id", "working_plan_revision", "working_task_ids",
        ) if field in accepted[key]}}


    def accept_plan(self, task_manager: Any, *, plan_id: str | None = None,
                    version: int | None = None, fingerprint: str | None = None) -> tuple[dict[str, Any], bool]:
        state = self.state()
        plan = state.get("pending_plan")
        executing = state.get("executing_plan")
        if executing and not plan and (not plan_id or plan_id == executing.get("plan_id")):
            if version is not None and version != executing.get("version"):
                raise InteractionError("accepted plan version is stale")
            if fingerprint is not None and fingerprint != executing.get("fingerprint"):
                raise InteractionError("accepted plan fingerprint is stale")
            return executing, False
        if not plan:
            if executing and plan_id and plan_id != executing.get("plan_id"):
                raise InteractionError("accepted plan identity is stale")
            accepted = self.accepted_plan(plan_id, version)
            if accepted and (fingerprint is None or accepted.get("fingerprint") == fingerprint):
                return accepted, False
            if accepted:
                raise InteractionError("accepted plan fingerprint is stale")
            raise InteractionError("plan is not pending")
        if ((plan_id and plan_id != plan.get("plan_id"))
                or (version is not None and version != plan.get("version"))
                or (fingerprint is not None and fingerprint != plan.get("fingerprint"))):
            raise InteractionError("plan is not pending at the requested version")
        if executing and task_manager.working_plan is None:
            task_manager.recover()
        elif not executing and task_manager.working_plan is None:
            task_manager.recover()
        if (not executing and task_manager.working_plan is not None
                and task_manager.working_plan.status == "active"):
            raise InteractionError("cannot accept a Strategic Plan while an unrelated Working Plan is active")
        working_plan = task_manager.working_plan if executing else None
        previous_tasks = [task for task in task_manager.tasks
                          if working_plan and task.get("id") in working_plan.task_ids]
        if any(task.get("status") == "running" for task in previous_tasks):
            raise InteractionError("cannot replace a Strategic Plan while a Working Plan task is running")
        operations = []
        strategic_bindings = {}
        active_ids = set(executing.get("working_task_ids", ())) if executing else set()
        active_ids.update(task["id"] for task in previous_tasks
                          if (task.get("checkpoint") or {}).get("strategy_anchor") in active_ids)
        if executing:
            if (plan.get("revision_materiality") != "material"
                    or plan.get("plan_id") != executing.get("plan_id")
                    or plan.get("version") != executing.get("version", 0) + 1):
                raise InteractionError("Strategic Plan revision is stale or does not require approval")
            staged_revision = plan.get("working_plan_revision", executing.get("working_plan_revision"))
            if (not working_plan or working_plan.id != executing.get("working_plan_id")
                    or working_plan.revision != staged_revision):
                raise InteractionError("Working Plan revision changed; reload before accepting")

        prior = [task for task in previous_tasks if not executing or task.get("id") in active_ids]
        if any(task.get("status") == "running" for task in prior):
            raise InteractionError("cannot replace a Strategic Plan while a Working Plan task is running")
        new_refs = []
        reused: set[str] = set()
        for step in plan["steps"]:
            description = f"{step['title']}: {step['description']}"
            execution = step.get("execution") or {}
            expected_args = (json.dumps(execution.get("arguments", {}), ensure_ascii=False,
                                        allow_nan=False, sort_keys=True, separators=(",", ":"))
                             if execution else "")
            existing = next((task for task in prior
                             if task.get("status") == "succeeded"
                             and (task.get("checkpoint") or {}).get("strategic_plan", {}).get("objective_id") == step["id"]
                             and (task.get("checkpoint") or {}).get("strategic_plan", {}).get("acceptance_ids") == step["acceptance_ids"]
                             and task.get("description") == description
                             and [item.get("criterion") if isinstance(item, dict) else item
                                  for item in task.get("acceptance", [])] == step["acceptance"]
                             and (task.get("checkpoint") or {}).get("action") == execution.get("action")
                             and ((task.get("checkpoint") or {}).get("args") or "") == expected_args
                             and {criterion_id for item in task.get("evidence", [])
                                  if isinstance(item, dict) and item.get("success") is True
                                  for criterion_id in item.get("acceptance_ids", ())} >= set(step["acceptance_ids"])), None)
            if existing:
                new_refs.append(existing["id"])
                reused.add(existing["id"])
                continue
            ref = f"revision_{plan['version']}_{step['id']}" if executing else step["id"]
            new_refs.append(ref)
            operations.append({"op": "add", "ref": ref, "task": {
                "description": description, "acceptance": step["acceptance"],
                **({"execution": step["execution"]} if step.get("execution") else {}),
            }})
            strategic_bindings[ref] = {
                "plan_id": plan["plan_id"], "version": plan["version"],
                "fingerprint": plan["fingerprint"], "objective_id": step["id"],
                "acceptance_ids": step["acceptance_ids"],
                "acceptance_verifications": step["acceptance_verifications"],
                "execution": step.get("execution"),
            }
        for task in prior:
            if task["id"] not in reused and task.get("status") in {"pending", "failed", "blocked"}:
                operations.insert(0, {"op": "cancel", "task": task["id"]})
        if not operations:
            if not prior:
                raise InteractionError("Strategic Plan has no Working Plan tasks to reuse")
            operations.append({"op": "update", "task": prior[0]["id"],
                               "fields": {"priority": prior[0]["priority"]}})
        payload = {
            "plan_id": plan["plan_id"], "version": plan["version"],
            "fingerprint": plan["fingerprint"], "working_task_refs": new_refs,
            **({"revision_of": {"version": executing["version"],
                                 "fingerprint": executing["fingerprint"]}} if executing else {}),
        }
        try:
            working = task_manager.edit_plan(
                complexity=working_plan.complexity if working_plan else "complex",
                plan_id=working_plan.id if working_plan else None,
                expected_revision=working_plan.revision if working_plan else 0,
                operations=operations, strategic_bindings=strategic_bindings,
                transaction_event={"event_type": "interaction.plan_accepted", "payload": payload,
                    "expected_proposal": {"plan_id": plan["plan_id"], "version": plan["version"],
                                           "fingerprint": plan["fingerprint"]},
                    **({"expected_working_plan": {
                        "plan_id": executing["working_plan_id"],
                        "revision": executing["working_plan_revision"],
                        "strategic_plan_id": executing["plan_id"],
                        "version": executing["version"], "fingerprint": executing["fingerprint"],
                    }} if executing else {})},
            )
        except ValueError as exc:
            accepted = self.accepted_plan(plan["plan_id"], plan["version"])
            current_state = self.state()
            current_plan = current_state.get("pending_plan") or current_state.get("executing_plan")
            if current_plan and not all(
                    current_plan.get(key) == plan.get(key)
                    for key in ("plan_id", "version", "fingerprint")):
                message = str(exc)
                if "changed" not in message.lower() and "stale" not in message.lower():
                    message = f"Strategic Plan changed while acceptance was pending: {message}"
                raise InteractionError(message) from exc
            if accepted and accepted.get("fingerprint") == plan["fingerprint"]:
                return accepted, False
            raise InteractionError(str(exc)) from exc
        bound_ids = [working["references"][ref] if ref in working["references"] else ref for ref in new_refs]
        return {**plan, "working_plan_id": working["plan_id"],
                "working_plan_revision": working["revision"], "working_task_ids": bound_ids}, True


    def complete_plan(self, plan: dict[str, Any], *, status: str = "completed",
                      task_manager: Any | None = None) -> None:
        if status not in {"completed", "failed", "cancelled"}:
            raise InteractionError("Strategic Plan terminal status is invalid")
        prior_completions = [event.get("payload", {}).get("status") for event in self._history()
                             if event["event_type"] == "interaction.plan_completed"
                             and event.get("payload", {}).get("plan_id") == plan.get("plan_id")
                             and event.get("payload", {}).get("version") == plan.get("version")
                             and event.get("payload", {}).get("fingerprint") == plan.get("fingerprint")]
        if any(previous in {"completed", "cancelled"} or previous == status
               for previous in prior_completions):
            return
        executing = self.state().get("executing_plan")
        if not executing or any(executing.get(key) != plan.get(key)
                                for key in ("plan_id", "version", "fingerprint")):
            raise InteractionError("Strategic Plan changed; reload before recording completion")
        working = task_manager.working_plan if task_manager is not None else None
        task_ids = plan.get("working_task_ids")
        if task_ids is None and working and working.id == plan.get("working_plan_id"):
            task_ids = working.task_ids
        if status == "completed":
            if task_manager is None or not isinstance(task_ids, list) or not task_ids:
                raise InteractionError("Strategic Plan completion requires its linked Working Plan evidence")
            if any((task.get("checkpoint") or {}).get("strategy_anchor") in task_ids
                   and task.get("status") not in {"succeeded", "cancelled"}
                   for task in task_manager.tasks
                   if working and task.get("id") in working.task_ids):
                raise InteractionError("Strategic Plan refinement tasks are still unfinished")
            tasks = {task.get("id"): task for task in task_manager.tasks}
            objectives = {step["id"] for step in plan.get("steps", [])}
            for task_id in task_ids:
                binding = (tasks.get(task_id, {}).get("checkpoint") or {}).get("strategic_plan") or {}
                if binding.get("plan_id") != plan.get("plan_id") or binding.get("objective_id") not in objectives:
                    raise InteractionError("Working Plan contains a task not linked to the accepted Strategic Plan")
            if any(tasks.get(task_id, {}).get("status") != "succeeded"
                   or not any(isinstance(item, dict) and item.get("success") is True and item.get("acceptance")
                              for item in tasks[task_id].get("evidence", []))
                   for task_id in task_ids):
                raise InteractionError("Strategic Plan tasks have not passed their acceptance evidence gates")
            for task_id in task_ids:
                task = tasks[task_id]
                binding = (task.get("checkpoint") or {}).get("strategic_plan") or {}
                required = set(binding.get("acceptance_ids", ()))
                if required:
                    covered = {
                        criterion_id
                        for item in task.get("evidence", [])
                        if isinstance(item, dict) and item.get("success") is True
                        for criterion_id in item.get("acceptance_ids", ())
                    }
                    if not required <= covered:
                        raise InteractionError("Strategic Plan criteria have not all been verified")
        payload = {
            "plan_id": plan["plan_id"], "version": plan["version"],
            "fingerprint": plan.get("fingerprint"), "working_plan_id": plan.get("working_plan_id"),
            "working_task_ids": task_ids or [], "status": status,
        }
        append = getattr(self.store, "append_plan_completed", None)
        if callable(append):
            try:
                append(payload, user_id=self.user_id, workspace_id=self.workspace_id,
                       session_id=self.session_id)
            except ValueError as exc:
                raise InteractionError(str(exc)) from exc
        else:
            self._append("interaction.plan_completed", payload)


    def reconcile_plan_completion(self, task_manager: Any) -> None:
        plan = self.state().get("executing_plan")
        if not plan or not plan.get("working_plan_id"):
            return
        working = task_manager.working_plan
        if working is None or working.id != plan["working_plan_id"]:
            working = task_manager.restore_working_plan(plan["working_plan_id"])
        if working is not None and working.status in {"completed", "cancelled"}:
            self.complete_plan(
                plan, status=working.status, task_manager=task_manager,
            )

