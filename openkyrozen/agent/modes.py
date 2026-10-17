"""Durable interaction modes, questions, and plan proposals."""

from __future__ import annotations

import json
import hashlib
import posixpath
import re
import shlex
import uuid
from copy import deepcopy
from typing import Any

from .ports import InteractionStore


INTERACTION_MODES = frozenset({"auto", "ask", "plan", "agent"})
SYSTEM_ONE_BACKENDS = frozenset({"off", "jev", "kev"})
PERMISSION_MODES = frozenset({"ask", "full_jev", "full"})
# Compatibility name for plugins and clients written before the rename.
FAST_BACKENDS = SYSTEM_ONE_BACKENDS
READ_ONLY_CAPABILITIES = frozenset({"read", "network"})
PLAN_ACCEPT_PHRASES = frozenset({
    "accept plan", "execute plan",
    "接受计划", "执行计划", "接受方案", "执行方案",
})
_ACTION_RE = re.compile(
    r"\b(implement|build|create|write|edit|change|fix|refactor|install|delete|remove|"
    r"commit|push|deploy|run|execute|send|publish|update|migrate|configure)\b|"
    r"(实现|创建|编写|修改|修复|重构|安装|删除|提交|推送|部署|运行|执行|发送|发布|更新|迁移|配置)",
    re.IGNORECASE,
)
_INFORMATIONAL_RE = re.compile(
    r"^\s*(what|why|how|when|where|who|which|is|are|does|do|should)\b|"
    r"^\s*(什么|为什么|为何|如何|怎么|何时|哪里|谁|哪一个)",
    re.IGNORECASE,
)
_READ_ACTION_RE = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+|would you\s+)?"
    r"(read|list|inspect|review|analy[sz]e|search|find|check|open)\b|"
    r"^\s*(?:请)?(读取|列出|检查|审查|分析|搜索|查找|打开)",
    re.IGNORECASE,
)
_SENSITIVE_RE = re.compile(
    r"\b(api[ _-]?key|password|passcode|credential|secret|access[ _-]?token|"
    r"private[ _-]?key|ssh[ _-]?key|one[ _-]?time[ _-]?(?:code|password)|2fa|mfa)\b|"
    r"密码|密钥|私钥|凭据|验证码",
    re.IGNORECASE,
)
_AUTHORIZATION_RE = re.compile(
    r"\b(?:may|can|should)\s+(?:i|we)\s+(?:run|execute|write|delete|remove|commit|push|"
    r"deploy|install|send|publish)\b|\b(?:approve|authorize|permission)\b.{0,40}"
    r"\b(?:command|tool|write|delete|commit|push|deploy)\b|"
    r"(?:是否允许|可以|能否).{0,20}(?:运行|执行|写入|删除|提交|推送|部署)",
    re.IGNORECASE,
)

_INLINE_COMMAND_MODES = frozenset({"auto", "ask", "plan", "agent"})
_INLINE_COMMAND_BACKENDS = frozenset({"off", "jev", "kev"})
_INLINE_COMMAND_PROFILES = frozenset({"auto", "coder", "researcher"})
_INLINE_COMMAND_PONYTAIL = frozenset({"off", "lite", "full", "ultra"})
_INLINE_COMMAND_RE = re.compile(r"(?<!\S)/[A-Za-z][A-Za-z0-9_-]*")


def split_inline_command(text: str) -> tuple[str, str] | None:
    """Split a trailing, known chat-control command from ordinary user text.

    Only commands that are safe to apply before the accompanying request are
    accepted. Operational commands remain line-oriented so a path, URL, or
    sentence mentioning a command cannot trigger a side effect.
    """
    source = str(text or "").strip()
    result: tuple[str, str] | None = None
    for match in _INLINE_COMMAND_RE.finditer(source):
        prefix = source[:match.start()].strip()
        suffix = source[match.start():].strip()
        if not prefix:
            continue
        try:
            parts = shlex.split(suffix)
        except ValueError:
            continue
        if not parts:
            continue
        command = parts[0].lower()
        args = parts[1:]
        valid = (
            (command in {"/ask", "/plan"} and not args)
            or (command == "/mode" and len(args) == 1 and args[0].lower() in _INLINE_COMMAND_MODES)
            or (command == "/agent" and (not args or (len(args) == 1 and args[0].lower() in _INLINE_COMMAND_PROFILES)))
            or (command in {"/fast", "/system-one", "/system_one"}
                and (not args or (len(args) == 1 and args[0].lower() in _INLINE_COMMAND_BACKENDS)))
            or (command in {"/decision-assist", "/assist"} and (
                not args or (1 <= len(args) <= 2 and args[0].lower() in {*_INLINE_COMMAND_BACKENDS, "revoke"}
                             and (len(args) == 1 or args[1].lower() in {"y", "yes", "consent", "allow"}))
            ))
            or (command == "/ponytail" and (not args or (len(args) == 1 and args[0].lower() in _INLINE_COMMAND_PONYTAIL)))
        )
        if valid:
            result = (prefix, suffix)
    return result


class InteractionError(ValueError):
    """A bounded interaction payload failed validation."""


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise InteractionError(f"{field} must be text")
    value = value.strip()
    if not value or len(value) > limit:
        raise InteractionError(f"{field} must be 1-{limit} characters")
    return value


def _normalise_choice(value: Any, index: int) -> dict[str, str]:
    if isinstance(value, str):
        return {"id": f"choice-{index}", "label": _text(value, "choice label", 80), "description": ""}
    if not isinstance(value, dict):
        raise InteractionError("choices must be strings or objects")
    return {
        "id": _text(value.get("id") or f"choice-{index}", "choice id", 64),
        "label": _text(value.get("label"), "choice label", 80),
        "description": str(value.get("description") or "").strip()[:240],
    }


def validate_question_request(value: Any, *, original_input: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InteractionError("AskUser must contain a JSON object")
    questions = value.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 3:
        raise InteractionError("AskUser requires 1-3 questions")
    normalised = []
    question_ids: set[str] = set()
    for index, question in enumerate(questions, 1):
        if not isinstance(question, dict):
            raise InteractionError("each question must be an object")
        choices = question.get("choices")
        if not isinstance(choices, list) or not 2 <= len(choices) <= 3:
            raise InteractionError("each question requires 2-3 choices")
        prompt = _text(question.get("prompt"), "question prompt", 500)
        if _SENSITIVE_RE.search(prompt):
            raise InteractionError("questions cannot request credentials or secrets")
        if _AUTHORIZATION_RE.search(prompt):
            raise InteractionError("questions cannot replace tool approval")
        normalised_choices = [_normalise_choice(choice, choice_index)
                              for choice_index, choice in enumerate(choices, 1)]
        question_id = _text(question.get("id") or f"question-{index}", "question id", 64)
        if question_id in question_ids or len({choice["id"] for choice in normalised_choices}) != len(normalised_choices):
            raise InteractionError("question and choice ids must be unique")
        question_ids.add(question_id)
        if any(_SENSITIVE_RE.search(choice["label"] + " " + choice["description"])
               for choice in normalised_choices):
            raise InteractionError("questions cannot request credentials or secrets")
        normalised.append({
            "id": question_id,
            "header": _text(question.get("header"), "question header", 40),
            "prompt": prompt,
            "choices": normalised_choices,
        })
    return {
        "request_id": f"question_{uuid.uuid4().hex}",
        "questions": normalised,
        "original_input": str(original_input).strip()[:12_000],
    }


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
                    or not isinstance(action, str) or action not in {"write_file", "edit_file"}
                    or not isinstance(arguments, dict)
                    or not isinstance(arguments.get("path"), str) or not arguments["path"].strip()):
                raise InteractionError("file execution contract must name write_file or edit_file and an exact path")
            required = ({"path", "content"} if action == "write_file" else
                        {"path", "old_text", "new_text", "expected_sha256"})
            if set(arguments) != required or any(not isinstance(item, str) for item in arguments.values()):
                raise InteractionError("file execution arguments must exactly match the selected file operation")
            if action == "edit_file" and (
                    not arguments["old_text"] or not re.fullmatch(r"[0-9a-fA-F]{64}", arguments["expected_sha256"])):
                raise InteractionError("edit_file requires nonempty old_text and a SHA-256 read hash")
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
        "read", "write", "shell", "network", "git", "browser", "destructive", "dynamic",
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


def parse_control_block(text: str, name: str) -> Any | None:
    """Return the JSON object from one named fenced or plain control block."""
    source = str(text or "")
    heading = re.search(
        rf"^[ \t]*{re.escape(name)}(?![\w])[ \t]*:?[ \t]*(?:\r?\n)?",
        source, re.IGNORECASE | re.MULTILINE,
    )
    if not heading:
        return None
    remainder = source[heading.end():].lstrip()
    fenced = re.match(r"```(?:json)?[ \t]*\n([\s\S]*?)\n```", remainder, re.IGNORECASE)
    try:
        if fenced:
            return json.loads(fenced.group(1))
        value, _end = json.JSONDecoder().raw_decode(remainder)
        return value
    except json.JSONDecodeError as exc:
        raise InteractionError(f"{name} contains invalid JSON: {exc.msg}") from exc


def normalize_provider_control(text: str, *, plan_mode: bool = False) -> tuple[str, dict[str, Any]] | None:
    """Map a bounded provider JSON shape to one non-executable control."""
    source = str(text or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", source, re.IGNORECASE)
    if fenced:
        source = fenced.group(1).strip()
    try:
        value = json.loads(source)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None

    executable_keys = {
        "action", "args", "arguments", "command", "commands", "content", "path", "tool", "tool_calls",
    }
    mode = str(value.get("mode") or "").strip().lower()
    plan_shape = (
        plan_mode
        and isinstance(value.get("steps"), list)
        and bool({"title", "summary", "plan_name", "overview", "goal"} & set(value))
    )
    if mode == "plan" or value.get("do_not_execute_until_approved") is True or plan_shape:
        if executable_keys & {str(key).lower() for key in value}:
            return None
        steps = []
        for index, step in enumerate(value.get("steps") or [], 1):
            if not isinstance(step, dict):
                return None
            description = step.get("description") or step.get("details") or step.get("title")
            if not description:
                return None
            steps.append({
                "id": str(step.get("id") or step.get("step_id") or f"step-{step.get('step', index)}"),
                "title": step.get("title") or str(description)[:120],
                "description": description,
                "acceptance": (
                    step.get("acceptance") or step.get("acceptance_criteria")
                    or ["Step completed with observable evidence"]
                ),
                **({"execution": step["execution"]} if "execution" in step else {}),
            })
        return "PlanProposal", {
            "title": value.get("title") or value.get("plan_name"),
            "summary": value.get("summary") or value.get("overview") or value.get("goal"),
            "assumptions": value.get("assumptions") or [],
            **({"bounds": value["bounds"]} if "bounds" in value else {}),
            "steps": steps,
        }

    stack: list[Any] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if executable_keys & {str(key).lower() for key in item}:
                return None
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)

    if mode == "ask" or value.get("ask_user") is True:
        return "AskUser", {"questions": value.get("questions")}
    return None


def route_mode(preference: str, user_input: str, *, pending_plan: bool = False,
               executing_plan: bool = False) -> str:
    if executing_plan:
        return "agent"
    if pending_plan:
        return "plan"
    if preference != "auto":
        return preference
    text = str(user_input or "")
    if _INFORMATIONAL_RE.search(text):
        return "ask"
    return "plan" if _ACTION_RE.search(text) or _READ_ACTION_RE.search(text) else "ask"


def mode_capabilities(capabilities: set[str] | frozenset[str], effective_mode: str) -> frozenset[str]:
    base = frozenset(capabilities)
    return base & READ_ONLY_CAPABILITIES if effective_mode in {"ask", "plan"} else base


def is_plan_acceptance(text: str) -> bool:
    return " ".join(str(text or "").strip().lower().split()).rstrip(".!。！") in PLAN_ACCEPT_PHRASES


class InteractionController:
    """Reconstruct session interaction state from the existing event log."""

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

    def state(self, user_input: str = "") -> dict[str, Any]:
        preference = "auto"
        system_one_backend = "off"
        question = None
        plan = None
        executing = None
        for event in self._history():
            event_type, payload = event["event_type"], event.get("payload", {})
            if event_type == "history.restored":
                restored = payload.get("interaction") if isinstance(payload, dict) else None
                if isinstance(restored, dict):
                    preference = restored.get("preference_mode", preference)
                    system_one_backend = restored.get("system_one_backend",
                                                      restored.get("fast_backend", system_one_backend))
                    question = restored.get("pending_question")
                    plan = restored.get("pending_plan")
                    executing = restored.get("executing_plan")
            elif event_type == "interaction.mode_changed":
                preference = payload.get("mode", preference)
            elif event_type in {"interaction.fast_changed", "interaction.system_one_changed"}:
                system_one_backend = payload.get("backend", "off")
            elif event_type in {"interaction.question_requested", "interaction.question_reopened"}:
                question = payload
            elif event_type == "interaction.question_resolved" and question and payload.get("request_id") == question.get("request_id"):
                question = None
            elif event_type == "interaction.plan_proposed":
                plan = payload
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
        effective = route_mode(
            preference, user_input, pending_plan=plan is not None, executing_plan=executing is not None,
        )
        if question and question.get("resume_mode") in INTERACTION_MODES - {"auto"}:
            effective = question["resume_mode"]
        state = {
            "preference_mode": preference,
            "system_one_backend": (system_one_backend if system_one_backend in SYSTEM_ONE_BACKENDS else "off"),
            "effective_mode": effective,
            "pending_question": question,
            "pending_plan": plan,
            "executing_plan": executing,
        }
        state["fast_backend"] = state["system_one_backend"]
        return state

    def envelope(self, user_input: str = "") -> dict[str, Any]:
        state = self.state(user_input)
        envelope = {key: state[key] for key in (
            "preference_mode", "effective_mode", "system_one_backend", "pending_question", "pending_plan",
        )}
        envelope["fast_backend"] = envelope["system_one_backend"]
        return envelope

    def set_system_one_backend(self, backend: str) -> dict[str, Any]:
        backend = str(backend or "").strip().lower()
        if backend not in SYSTEM_ONE_BACKENDS:
            raise InteractionError("System One backend must be off, jev, or kev")
        self._append("interaction.system_one_changed", {"backend": backend})
        return self.envelope()

    def set_fast_backend(self, backend: str) -> dict[str, Any]:
        """Deprecated compatibility alias for set_system_one_backend."""
        return self.set_system_one_backend(backend)

    def set_mode(self, mode: str) -> dict[str, Any]:
        mode = str(mode or "").strip().lower()
        if mode not in INTERACTION_MODES:
            raise InteractionError("mode must be auto, ask, plan, or agent")
        self._append("interaction.mode_changed", {"mode": mode})
        return self.envelope()

    def request_question(self, value: Any, *, original_input: str = "") -> dict[str, Any]:
        question = validate_question_request(value, original_input=original_input)
        question["resume_mode"] = self.state(original_input)["effective_mode"]
        self._append("interaction.question_requested", question)
        return question

    def resolve_question(self, request_id: str, answers: Any, *, action: str = "answer") -> dict[str, Any]:
        pending = self.state().get("pending_question")
        if not pending or pending.get("request_id") != request_id:
            raise InteractionError("question request is not pending")
        if action not in {"answer", "skip", "cancel"}:
            raise InteractionError("question action must be answer, skip, or cancel")
        if action == "answer" and not isinstance(answers, (dict, list, str)):
            raise InteractionError("question answers must be an object, list, or string")
        payload = {"request_id": request_id, "action": action, "answers": answers}
        self._append("interaction.question_resolved", payload)
        return {"question": pending, **payload}

    def reopen_question(self) -> dict[str, Any]:
        requested = [event["payload"] for event in self._history()
                     if event["event_type"] in {"interaction.question_requested", "interaction.question_reopened"}]
        if not requested:
            raise InteractionError("no question is available to reopen")
        question = requested[-1]
        self._append("interaction.question_reopened", question)
        return question

    def propose_plan(self, value: Any, *, original_input: str = "") -> dict[str, Any]:
        state = self.state()
        previous = state.get("pending_plan") or state.get("executing_plan")
        if previous and state.get("pending_plan") is None:
            latest = [int(event.get("payload", {}).get("version", 0)) for event in self._history()
                      if event.get("event_type") == "interaction.plan_proposed"
                      and event.get("payload", {}).get("plan_id") == previous.get("plan_id")]
            if latest:
                previous = {**previous, "version": max(latest)}
        plan = validate_plan_proposal(value, previous=previous, original_input=original_input)
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
                    version: int | None = None) -> tuple[dict[str, Any], bool]:
        state = self.state()
        plan = state.get("pending_plan")
        executing = state.get("executing_plan")
        if executing and not plan and (not plan_id or plan_id == executing.get("plan_id")):
            if version is not None and version != executing.get("version"):
                raise InteractionError("accepted plan version is stale")
            return executing, False
        if not plan or (plan_id and plan_id != plan.get("plan_id")) or (version is not None and version != plan.get("version")):
            accepted = self.accepted_plan(plan_id, version)
            if accepted:
                return accepted, False
            raise InteractionError("plan is not pending")
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
        reused: set[str] = set()
        for step in plan["steps"]:
            description = f"{step['title']}: {step['description']}"
            existing = next((task for task in previous_tasks
                             if task.get("status") == "succeeded"
                             and (task.get("checkpoint") or {}).get("strategic_plan", {}).get("objective_id") == step["id"]
                             and (task.get("checkpoint") or {}).get("strategic_plan", {}).get("acceptance_ids") == step["acceptance_ids"]
                             and task.get("description") == description
                             and [item.get("criterion") if isinstance(item, dict) else item
                                  for item in task.get("acceptance", [])] == step["acceptance"]
                             and (task.get("checkpoint") or {}).get("action") == (step.get("execution") or {}).get("action")
                             and ((task.get("checkpoint") or {}).get("args") or "") == (
                                 json.dumps((step.get("execution") or {}).get("arguments", {}), ensure_ascii=False,
                                            allow_nan=False, sort_keys=True, separators=(",", ":"))
                                 if step.get("execution") else "")), None)
            if existing:
                reused.add(existing["id"])
                continue
            operations.append({"op": "add", "ref": step["id"], "task": {
                "description": description,
                "acceptance": step["acceptance"],
                **({"execution": step["execution"]} if step.get("execution") else {}),
            }})
            strategic_bindings[step["id"]] = {
                "plan_id": plan["plan_id"], "version": plan["version"],
                "fingerprint": plan["fingerprint"], "objective_id": step["id"],
                "acceptance_ids": step["acceptance_ids"],
                "acceptance_verifications": step["acceptance_verifications"],
                "execution": step.get("execution"),
            }
        for task in previous_tasks:
            if task["id"] not in reused and task.get("status") in {"pending", "failed", "blocked"}:
                operations.insert(0, {"op": "cancel", "task": task["id"]})
        if working_plan and not operations:
            task = previous_tasks[0]
            operations.append({"op": "update", "task": task["id"], "fields": {"priority": task["priority"]}})
        try:
            working = task_manager.edit_plan(
            complexity=working_plan.complexity if working_plan else "complex",
            plan_id=working_plan.id if working_plan else None,
            expected_revision=working_plan.revision if working_plan else 0, operations=operations,
            strategic_bindings=strategic_bindings,
            transaction_event={"event_type": "interaction.plan_accepted", "payload": {
                "plan_id": plan["plan_id"], "version": plan["version"],
                "fingerprint": plan["fingerprint"],
            }, "expected_proposal": {
                "plan_id": plan["plan_id"], "version": plan["version"],
                "fingerprint": plan["fingerprint"],
            }},
            )
        except ValueError:
            accepted = self.accepted_plan(plan["plan_id"], plan["version"])
            if accepted and accepted.get("fingerprint") == plan["fingerprint"]:
                return accepted, False
            raise
        bound_ids = [task["id"] for task in working["tasks"]
                     if (task.get("checkpoint") or {}).get("strategic_plan", {}).get("plan_id") == plan["plan_id"]
                     and (task.get("checkpoint") or {}).get("strategic_plan", {}).get("version") == plan["version"]
                     and (task.get("checkpoint") or {}).get("strategic_plan", {}).get("fingerprint") == plan["fingerprint"]
                     and (task.get("checkpoint") or {}).get("strategic_plan", {}).get("objective_id")
                     in {step["id"] for step in plan["steps"]}]
        return {**plan, "working_plan_id": working["plan_id"],
                "working_plan_revision": working["revision"],
                "working_task_ids": bound_ids}, True

    def complete_plan(self, plan: dict[str, Any], *, status: str = "completed",
                      task_manager: Any | None = None) -> None:
        if status not in {"completed", "failed", "cancelled"}:
            raise InteractionError("Strategic Plan terminal status is invalid")
        prior_completions = [event.get("payload", {}).get("status") for event in self._history()
                             if event["event_type"] == "interaction.plan_completed"
                             and event.get("payload", {}).get("plan_id") == plan.get("plan_id")
                             and event.get("payload", {}).get("version") == plan.get("version")]
        if any(previous in {"completed", "cancelled"} or previous == status
               for previous in prior_completions):
            return
        working = task_manager.working_plan if task_manager is not None else None
        current_tasks = {task.get("id"): task for task in task_manager.tasks} if task_manager is not None else {}
        task_ids = ([task_id for task_id in working.task_ids
                     if (current_tasks.get(task_id, {}).get("checkpoint") or {}).get("strategic_plan", {}).get("version") == plan.get("version")
                     and (current_tasks.get(task_id, {}).get("checkpoint") or {}).get("strategic_plan", {}).get("fingerprint") == plan.get("fingerprint")]
                    if working and working.id == plan.get("working_plan_id")
                    else plan.get("working_task_ids"))
        if status == "completed":
            if task_manager is None or not isinstance(task_ids, list) or not task_ids:
                raise InteractionError("Strategic Plan completion requires its linked Working Plan evidence")
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
        self._append("interaction.plan_completed", {
            "plan_id": plan["plan_id"], "version": plan["version"],
            "fingerprint": plan.get("fingerprint"), "working_plan_id": plan.get("working_plan_id"),
            "working_task_ids": task_ids or [], "status": status,
        })

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


def render_question(question: dict[str, Any]) -> str:
    lines = ["I need a little more detail before continuing:"]
    for index, item in enumerate(question["questions"], 1):
        lines.append(f"\n{index}. {item['header']} — {item['prompt']}")
        for choice_index, choice in enumerate(item["choices"], 1):
            description = f" — {choice['description']}" if choice.get("description") else ""
            lines.append(f"   {choice_index}) {choice['label']}{description}")
        lines.append("   Other / Skip")
    return "\n".join(lines)


def render_plan(plan: dict[str, Any]) -> str:
    lines = [f"# {plan['title']} (v{plan['version']})", "", plan["summary"]]
    if plan["assumptions"]:
        lines.extend(["", "Assumptions:", *[f"- {item}" for item in plan["assumptions"]]])
    lines.extend(["", "Plan:"])
    for index, step in enumerate(plan["steps"], 1):
        lines.append(f"{index}. {step['title']} — {step['description']}")
        lines.extend(f"   - Acceptance [{criterion_id}]: {item}"
                     for criterion_id, item in zip(step["acceptance_ids"], step["acceptance"]))
        if step.get("execution"):
            lines.append(f"   - Approved file operation: {step['execution']['action']} "
                         f"{json.dumps(step['execution']['arguments'], ensure_ascii=False, sort_keys=True)}")
        for verification in step["acceptance_verifications"]:
            if verification:
                detail = f" with the approved arguments"
                if verification.get("expect"):
                    detail += f" and expected {json.dumps(verification['expect'], ensure_ascii=False)}"
                lines.append(f"     Verified by {verification['action']}{detail}.")
            else:
                lines.append("     No verification contract; this criterion cannot pass the completion gate.")
    bounds = plan["bounds"]
    lines.extend([
        "", "Approved bounds:",
        f"- Workspace paths: {', '.join(bounds['workspace_paths']) or '(none; no file paths)' }",
        f"- Capabilities: {', '.join(bounds['capabilities']) or '(none)' }",
        f"- Effects: {', '.join(bounds['effects']) or '(none)' }",
        f"- Risk ceiling: {bounds['risk_ceiling']}",
        f"- Bounds provenance: {plan.get('bounds_source', 'legacy-default')}",
        f"- Strategic fingerprint: {plan['fingerprint']}",
    ])
    lines.extend(["", "Accept with `/plan accept` or `accept plan`; send feedback to revise; cancel with `/plan cancel`."])
    return "\n".join(lines)
