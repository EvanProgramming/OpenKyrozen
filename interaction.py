"""Durable interaction modes, questions, and plan proposals."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from event_store import EventStore


INTERACTION_MODES = frozenset({"auto", "ask", "plan", "agent"})
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
        acceptance = step.get("acceptance")
        if isinstance(acceptance, str):
            acceptance = [acceptance]
        if not isinstance(acceptance, list) or not 1 <= len(acceptance) <= 5:
            raise InteractionError("each plan step requires 1-5 acceptance criteria")
        normalised.append({
            "id": step_id,
            "title": _text(step.get("title") or f"Step {index}", "step title", 120),
            "description": _text(step.get("description"), "step description", 800),
            "acceptance": [_text(item, "acceptance criterion", 300) for item in acceptance],
        })
    assumptions = value.get("assumptions", [])
    if assumptions is None:
        assumptions = []
    if not isinstance(assumptions, list) or len(assumptions) > 10:
        raise InteractionError("assumptions must contain at most 10 items")
    return {
        "plan_id": _text(
            (previous or {}).get("plan_id") or f"plan_{uuid.uuid4().hex}",
            "plan_id", 100,
        ),
        "version": version,
        "title": _text(value.get("title"), "plan title", 160),
        "summary": _text(value.get("summary"), "plan summary", 1200),
        "assumptions": [_text(item, "assumption", 300) for item in assumptions],
        "steps": normalised,
        "original_input": str(original_input).strip()[:12_000],
    }


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

    def __init__(self, store: EventStore, *, user_id: str = "local", workspace_id: str = "default",
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
        events = self.store.list_events(
            limit=10_000, workspace_id=self.workspace_id, session_id=self.session_id,
            user_id=self.user_id,
        )
        return [event for event in reversed(events) if str(event.get("event_type", "")).startswith("interaction.")]

    def state(self, user_input: str = "") -> dict[str, Any]:
        preference = "auto"
        question = None
        plan = None
        executing = None
        for event in self._history():
            event_type, payload = event["event_type"], event.get("payload", {})
            if event_type == "interaction.mode_changed":
                preference = payload.get("mode", preference)
            elif event_type in {"interaction.question_requested", "interaction.question_reopened"}:
                question = payload
            elif event_type == "interaction.question_resolved" and question and payload.get("request_id") == question.get("request_id"):
                question = None
            elif event_type == "interaction.plan_proposed":
                plan = payload
                executing = None
            elif event_type == "interaction.plan_accepted":
                if plan and payload.get("plan_id") == plan.get("plan_id") and payload.get("version") == plan.get("version"):
                    executing = dict(plan)
                    plan = None
            elif event_type == "interaction.plan_cancelled":
                if plan and payload.get("plan_id") == plan.get("plan_id"):
                    plan = None
            elif event_type == "interaction.plan_completed":
                if executing and payload.get("plan_id") == executing.get("plan_id"):
                    executing = None
        effective = route_mode(
            preference, user_input, pending_plan=plan is not None, executing_plan=executing is not None,
        )
        if question and question.get("resume_mode") in INTERACTION_MODES - {"auto"}:
            effective = question["resume_mode"]
        return {
            "preference_mode": preference,
            "effective_mode": effective,
            "pending_question": question,
            "pending_plan": plan,
            "executing_plan": executing,
        }

    def envelope(self, user_input: str = "") -> dict[str, Any]:
        state = self.state(user_input)
        return {key: state[key] for key in (
            "preference_mode", "effective_mode", "pending_question", "pending_plan",
        )}

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
        previous = self.state().get("pending_plan")
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
        accepted: set[tuple[str, int]] = set()
        for event in self._history():
            payload = event.get("payload", {})
            key = (str(payload.get("plan_id") or ""), int(payload.get("version") or 0))
            if event["event_type"] == "interaction.plan_proposed":
                proposals[key] = payload
            elif event["event_type"] == "interaction.plan_accepted":
                accepted.add(key)
        matches = [proposal for key, proposal in proposals.items() if key in accepted
                   and (not plan_id or key[0] == plan_id) and (version is None or key[1] == version)]
        return matches[-1] if matches else None

    def accept_plan(self, task_manager: Any, *, plan_id: str | None = None,
                    version: int | None = None) -> tuple[dict[str, Any], bool]:
        state = self.state()
        plan = state.get("pending_plan")
        executing = state.get("executing_plan")
        if executing and (not plan_id or plan_id == executing.get("plan_id")):
            return executing, False
        if not plan or (plan_id and plan_id != plan.get("plan_id")) or (version is not None and version != plan.get("version")):
            accepted = self.accepted_plan(plan_id, version)
            if accepted:
                return accepted, False
            raise InteractionError("plan is not pending")
        descriptions = [f"{step['title']}: {step['description']}" for step in plan["steps"]]
        acceptance = [
            [{"criterion": criterion} for criterion in step["acceptance"]]
            for step in plan["steps"]
        ]
        prefix = f"{plan['plan_id']}-v{plan['version']}"
        task_manager.add_ordered_plan(descriptions, task_id_prefix=prefix, acceptance=acceptance)
        self._append("interaction.plan_accepted", {"plan_id": plan["plan_id"], "version": plan["version"]})
        return plan, True

    def complete_plan(self, plan: dict[str, Any], *, status: str = "completed") -> None:
        self._append("interaction.plan_completed", {
            "plan_id": plan["plan_id"], "version": plan["version"], "status": status,
        })


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
        lines.extend(f"   - Acceptance: {item}" for item in step["acceptance"])
    lines.extend(["", "Accept with `/plan accept` or `accept plan`; send feedback to revise; cancel with `/plan cancel`."])
    return "\n".join(lines)
