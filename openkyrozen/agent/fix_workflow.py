from __future__ import annotations

import re
import uuid
import datetime
from typing import Any
from openkyrozen.persistence.models import stable_hash


def _fix_feedback_signal(self, text: str) -> str | None:
    """Return explicit user feedback without treating a new bug report as feedback."""
    lowered = str(text or "").lower()
    positive = any(marker in lowered for marker in self._FIX_SUCCESS_FEEDBACK)
    negative = any(marker in lowered for marker in self._FIX_FAILURE_FEEDBACK)
    if positive == negative:
        return None
    return "success" if positive else "failure"


def _fix_safe_text(self, value: Any, limit: int = 500) -> str:
    """Bound and redact fix-workflow diagnostics before persisting them."""
    text = str(value or "")
    text = re.sub(
        r"(?i)((?:api[_-]?key|password|secret|token)\s*[:=])\s*\S+",
        r"\1<redacted>", text,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "<redacted>", text)
    return text.replace("\n", " ")[:limit]


def _fix_scope_kwargs(self) -> dict[str, Any]:
    return {
        "user_id": self.memory_bank.user_id,
        "workspace_id": self.memory_bank.workspace_id,
        "session_id": self.memory_bank.session_id,
    }


def _latest_fix_workflow(self) -> dict[str, Any] | None:
    """Recover the latest fix workflow from scoped SQLite events."""
    events = self.memory_bank.store.list_events(
        "bug_fix.stage", limit=10000, **self._fix_scope_kwargs(),
    )
    for event in events:
        payload = event.get("payload", {})
        if isinstance(payload, dict) and payload.get("workflow_id"):
            return dict(payload)
    return None


def _persist_fix_stage(self, state: dict[str, Any], stage: str, *, reason: str = "",
                       evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Persist one bounded state snapshot; only the declared stage order is legal."""
    current = str(state.get("stage", "reported"))
    if stage not in (*self._FIX_WORKFLOW_STAGES, "blocked"):
        raise ValueError(f"Unknown fix workflow stage: {stage}")
    if stage != "blocked":
        if current == "blocked" or current == "explained":
            return state
        if self._FIX_WORKFLOW_STAGES.index(stage) < self._FIX_WORKFLOW_STAGES.index(current):
            return state
        if stage in self._FIX_EVIDENCE_STAGES and not evidence:
            return state
    merged_evidence = list(state.get("evidence", []))
    for item in evidence or []:
        if item not in merged_evidence:
            merged_evidence.append(item)
    next_state = {
        **state,
        "stage": stage,
        "evidence": merged_evidence[-24:],
        "updated_at": datetime.datetime.now(self.UTC).isoformat(),
    }
    if reason:
        next_state["reason"] = self._fix_safe_text(reason, 300)
    self.memory_bank.store.append_event(
        "bug_fix.stage", next_state, task_id=next_state.get("task_id"), **self._fix_scope_kwargs(),
    )
    return next_state


def _start_fix_workflow(self, user_input: str) -> dict[str, Any]:
    workflow_id = f"fix_{uuid.uuid4().hex}"
    state = {
        "workflow_id": workflow_id,
        "task_id": f"task_fix_{uuid.uuid4().hex}",
        "attempt_id": f"attempt_{uuid.uuid4().hex}",
        "stage": "reported",
        "error_signature": stable_hash(self._fix_safe_text(user_input, 500))[:16],
        "report": self._fix_safe_text(user_input, 500),
        "step_count": 0,
        "evidence": [],
        "created_at": datetime.datetime.now(self.UTC).isoformat(),
    }
    return self._persist_fix_stage(state, "reported", reason="bug report received")


def _prepare_fix_workflow(self, user_input: str) -> dict[str, Any] | None:
    """Start a new bug attempt or recover the active scoped attempt."""
    latest = self._latest_fix_workflow()
    signal = self._fix_feedback_signal(user_input)
    if latest and str(latest.get("stage")) not in self._FIX_TERMINAL_STAGES:
        return latest
    if signal and latest:
        return latest
    if self._is_bug_report(user_input):
        return self._start_fix_workflow(user_input)
    return latest if latest and signal else None


def _fix_evidence_record(self, state: dict[str, Any], stage: str, record: dict[str, Any], *, observed: bool) -> dict[str, Any]:
    receipt = {
        "workflow_id": state["workflow_id"],
        "attempt_id": state["attempt_id"],
        "task_id": state["task_id"],
        "stage": stage,
        "receipt_id": str(record.get("receipt_id") or f"fix_receipt_{uuid.uuid4().hex}"),
        "action": self._fix_safe_text(record.get("action"), 80),
        "args": self._fix_safe_text(record.get("args"), 500),
        "result": self._fix_safe_text(record.get("result"), 1000),
        "tool_success": bool(record.get("success")),
        "observed": bool(observed),
    }
    self.memory_bank.store.append_event(
        "bug_fix.receipt", receipt, task_id=state["task_id"], **self._fix_scope_kwargs(),
    )
    return receipt


def _fix_is_mutation(self, record: dict[str, Any]) -> bool:
    action = str(record.get("action", "")).strip()
    if not record.get("success"):
        return False
    if action in self._FIX_MUTATION_ACTIONS:
        return True
    if action in {"run_cmd", "execute_terminal_command"}:
        command = str(record.get("args", "")).lower()
        return not bool(re.search(r"pytest|unittest|make\s+(?:test|check|lint)|cargo\s+test|go\s+test", command)) and bool(
            re.search(
                r"(?:sed|perl|apply_patch|mv\s|cp\s|touch\s|mkdir\s|"
                r"python(?:3)?\s+-c\s+.*(?:open|write|replace|unlink|rename))",
                command,
            )
        )
    return False


def _fix_reproduction_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mutation_seen = False
    result = []
    for record in records:
        if self._fix_is_mutation(record):
            mutation_seen = True
            continue
        if not mutation_seen and str(record.get("action", "")) in self._FIX_REPRODUCTION_ACTIONS:
            if str(record.get("result", "")).strip():
                result.append(record)
    return result


def _fix_verification_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mutation_seen = False
    result = []
    for record in records:
        if self._fix_is_mutation(record):
            mutation_seen = True
            continue
        if mutation_seen and str(record.get("action", "")) in self._FIX_VERIFICATION_ACTIONS:
            result.append(record)
    return result


def _fix_has_marker(self, text: str, markers: tuple[str, ...]) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in markers)


def _fix_success_claim(self, text: str) -> bool:
    lowered = str(text or "").lower()
    if any(marker in lowered for marker in ("cannot claim", "can't claim", "not verified", "not fixed", "unable to verify")):
        return False
    return self._fix_has_marker(lowered, ("fixed", "resolved", "verified", "works successfully", "修复完成", "已修复"))


def _fix_blocked_reply(self, state: dict[str, Any]) -> str:
    reason = state.get("reason") or "the required observable reproduction and verification evidence is incomplete"
    return (
        "I cannot claim this bug is fixed. The bounded bug-fix workflow is "
        f"blocked at `{state.get('stage', 'reported')}`: {self._fix_safe_text(reason, 300)}. "
        "No successful fix-and-verify result was recorded, so the issue needs another diagnosed attempt."
    )


def _advance_fix_workflow(self, state: dict[str, Any] | None, user_input: str, proposed_reply: str,
                          tool_records: list[dict[str, Any]], model_text: str = "") -> tuple[dict[str, Any] | None, str]:
    """Advance only on observable receipts and prevent unsupported fix claims."""
    if state is None:
        return None, proposed_reply
    state = dict(state)
    state["step_count"] = int(state.get("step_count", 0)) + 1
    if state["step_count"] > self._FIX_MAX_STEPS and state.get("stage") not in self._FIX_TERMINAL_STAGES:
        state = self._persist_fix_stage(state, "blocked", reason="workflow step budget exhausted")
        return state, self._fix_blocked_reply(state)

    records = [item for item in tool_records if isinstance(item, dict)]
    reproduction = self._fix_reproduction_records(records)
    verification = self._fix_verification_records(records)
    mutations = [item for item in records if self._fix_is_mutation(item)]
    if state.get("stage") == "reported" and reproduction:
        evidence = [self._fix_evidence_record(state, "reproduced", item, observed=True) for item in reproduction]
        state = self._persist_fix_stage(state, "reproduced", reason="tool receipt captured the reported behavior", evidence=evidence)
    analysis_text = f"{model_text}\n{proposed_reply}"
    if state.get("stage") == "reproduced" and (
        self._fix_has_marker(analysis_text, ("diagnos", "root cause", "原因", "根因")) or reproduction
    ):
        state = self._persist_fix_stage(state, "diagnosed", reason="reproduction was available for diagnosis")
    if state.get("stage") == "diagnosed" and self._fix_has_marker(
        analysis_text, ("hypothes", "i believe", "assume", "假设", "推测")
    ):
        state = self._persist_fix_stage(state, "hypothesized", reason="the model stated a repair hypothesis")
    if state.get("stage") == "hypothesized" and mutations:
        evidence = [self._fix_evidence_record(state, "fixed", item, observed=True) for item in mutations]
        state = self._persist_fix_stage(state, "fixed", reason="a successful mutation receipt was recorded", evidence=evidence)
    if state.get("stage") == "fixed":
        if verification and any(item.get("success") for item in verification):
            evidence = [self._fix_evidence_record(state, "verified", item, observed=True)
                        for item in verification if item.get("success")]
            state = self._persist_fix_stage(state, "verified", reason="the verification command succeeded", evidence=evidence)
        elif verification:
            state = self._persist_fix_stage(state, "blocked", reason="verification was attempted but failed")
    if state.get("stage") == "verified" and str(proposed_reply).strip():
        state = self._persist_fix_stage(state, "explained", reason="verified result was returned to the user")
    if state.get("stage") not in {"verified", "explained"} and self._fix_success_claim(proposed_reply):
        state = self._persist_fix_stage(state, "blocked", reason="the response claimed success without verified evidence")
    if state.get("stage") == "blocked":
        return state, self._fix_blocked_reply(state)
    if state.get("stage") != "explained" and self._fix_success_claim(proposed_reply):
        return state, self._fix_blocked_reply(state)
    if state.get("stage") in {"fixed", "hypothesized", "diagnosed", "reproduced", "reported"}:
        return state, (
            proposed_reply if proposed_reply.strip() else
            "The bug-fix workflow is still in progress; observable reproduction, repair, and verification are required."
        )
    return state, proposed_reply


def _track_fix_outcome(self, user_input: str, agent_reply: str) -> None:
    """Persist every fix turn and attach explicit feedback to its attempt."""
    state = self._latest_fix_workflow()
    signal = self._fix_feedback_signal(user_input)
    turn_payload = {
        "workflow_id": state.get("workflow_id") if state else None,
        "task_id": state.get("task_id") if state else None,
        "attempt_id": state.get("attempt_id") if state else None,
        "feedback": signal,
        "user_input": self._fix_safe_text(user_input, 200),
        "agent_reply": self._fix_safe_text(agent_reply, 300),
    }
    track_turn = bool(state and (state.get("stage") not in self._FIX_TERMINAL_STAGES or signal))
    if track_turn:
        self.memory_bank.store.append_event(
            "bug_fix.turn", turn_payload, task_id=state.get("task_id"), **self._fix_scope_kwargs(),
        )
    if signal is None:
        return

    recent = self.memory_bank.get_recent(5)
    fix_context = ""
    for item in recent:
        if "FAILURE:" in item or "bug" in item.lower() or "fix" in item.lower() or "error" in item.lower():
            fix_context = item[:500]
            break
    outcome = {
        "timestamp": datetime.datetime.now(self.UTC).isoformat(),
        "success": signal == "success",
        "feedback": signal,
        "user_feedback": self._fix_safe_text(user_input, 200),
        "fix_context": self._fix_safe_text(fix_context, 300),
        "workflow_id": state.get("workflow_id") if state else None,
        "task_id": state.get("task_id") if state else None,
        "attempt_id": state.get("attempt_id") if state else None,
    }
    self._fix_outcomes.append(outcome)
    if len(self._fix_outcomes) > 20:
        self._fix_outcomes.pop(0)
    if state:
        self.memory_bank.store.append_event(
            "bug_fix.feedback", outcome, task_id=state.get("task_id"), **self._fix_scope_kwargs(),
        )
        if signal == "failure" and state.get("stage") != "blocked":
            self._persist_fix_stage(state, "blocked", reason="the user reported that the fix still fails")

    verdict = "SUCCESS" if signal == "success" else "FAILURE"
    self.memory_bank.add_log(
        f"FIX_OUTCOME: {verdict} | User said: {self._fix_safe_text(user_input, 100)}\n"
        f"Fix context: {self._fix_safe_text(fix_context, 300)}"
    )
    if signal == "failure":
        self._store_failure(
            user_input, "bug-fix-attempt",
            f"User indicated fix did not work: {self._fix_safe_text(user_input, 200)}",
            f"Agent replied: {self._fix_safe_text(agent_reply, 200)}",
        )


def _get_fix_success_rate(self) -> float:
    """Return the success rate of recent bug fixes."""
    if not self._fix_outcomes:
        return 1.0  # no data, assume success
    successes = sum(1 for o in self._fix_outcomes if o["success"])
    return successes / len(self._fix_outcomes)
