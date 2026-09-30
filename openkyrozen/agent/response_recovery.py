from __future__ import annotations

import json
import re
from typing import Any
from openkyrozen.agent.modes import InteractionError, validate_plan_proposal, validate_question_request
from openkyrozen.security.tool_policy import tool_capability

def _observe_turn_response(self, turn, text: str, context: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
    """Parse once, with one side-effect-free repair for malformed interaction JSON."""
    fast_mode = self.fast_mode
    parsed = self._observe_model_response(text)
    if (not turn.auto_clarified and turn.fast_backend != "off" and not self._interaction_controller.state().get("executing_plan")
            and parsed.get("question") is not None
            and not parsed.get("protocol_error") and self._interaction_controls_enabled.get()):
        clarification_details: dict[str, Any] = {}
        clarification_failed = False
        try:
            request = validate_question_request(parsed["question"], original_input=turn.user_input)
            answers = fast_mode.implied_answers(turn.fast_backend, turn.user_input, request, self._user_preferences,
                                                 clarification_details)
        except Exception as exc:
            answers = None
            clarification_failed = True
            self._record_fast_decision(turn.fast_backend, {"stage": "clarification", "choices": {},
                                                  "fallback_reason": type(exc).__name__})
        if answers:
            turn.auto_clarified = True
            if not self._fast_used_backend.get():
                self._fast_used_backend.set(turn.fast_backend)
                self._emit_stream_event({"event": "fast_decision", "backend": turn.fast_backend})
            self._record_fast_decision(turn.fast_backend, {"stage": "clarification",
                                                  "choices": {"resolved_questions": len(answers)},
                                                  "fallback_reason": "", **clarification_details})
            context.extend([{"role": "assistant", "content": str(text)}, {
                "role": "user", "content": "The user's original request or saved preference already specifies: "
                + json.dumps(answers, ensure_ascii=False) + ". Continue without another clarification."
            }])
            continuation = self._call_llm_with_spinner(context).strip()
            turn.turn_prompt_total += self._last_prompt_tokens
            turn.turn_completion_total += self._last_completion_tokens
            return self._observe_turn_response(turn, continuation, context)
        if not clarification_failed:
            self._record_fast_decision(turn.fast_backend, {"stage": "clarification", "choices": {},
                                                  "fallback_reason": "user_decision_or_low_confidence",
                                                  **clarification_details})
    if self._active_interaction_mode.get() == "plan":
        return text, parsed
    has_control_marker = re.search(
        r"^[ \t]*(?:AskUser|PlanProposal)(?![\w])[ \t]*:?", str(text),
        re.IGNORECASE | re.MULTILINE,
    )
    if not parsed.get("protocol_error") or (
            not has_control_marker and self._active_interaction_mode.get() != "plan"):
        return text, parsed
    question_control = bool(re.search(r"^[ \t]*AskUser(?![\w])", str(text), re.IGNORECASE | re.MULTILINE))
    example = (
        'AskUser:\n```json\n{"questions":[{"id":"scope","header":"Scope",'
        '"prompt":"Which scope?","choices":[{"id":"a","label":"Option A","description":"Impact"},'
        '{"id":"b","label":"Option B","description":"Impact"}]}]}\n```'
        if question_control else
        'PlanProposal:\n```json\n{"title":"Plan title","summary":"Outcome",'
        '"assumptions":[],"steps":[{"id":"step-1","title":"Step title",'
        '"description":"Work to perform","acceptance":["Observable result"]}]}\n```'
    )
    repaired = self._call_llm_with_spinner(context + [{
        "role": "assistant",
        "content": str(text),
    }, {
        "role": "user",
        "content": (
            f"System: The interaction control was invalid ({parsed['protocol_error']}). "
            "Re-emit the same content as exactly one fenced JSON control in the shape below. "
            "Replace the example values, but preserve every key and JSON syntax. Output no Markdown outside "
            f"this block, prose, Action, TaskList, TaskDone, or DefineTool.\n{example}"
        ),
    }]).strip()
    turn.turn_prompt_total += self._last_prompt_tokens
    turn.turn_completion_total += self._last_completion_tokens
    candidate = self._parse_model_response(repaired)
    if not candidate.get("protocol_error") and not (
            candidate.get("question") is not None or candidate.get("plan_proposal") is not None):
        control_name = "AskUser" if question_control else "PlanProposal"
        candidate = self._parse_model_response(f"{control_name}:\n{repaired}")
    if candidate.get("protocol_error") or not (
            candidate.get("question") is not None or candidate.get("plan_proposal") is not None):
        candidate["tool_calls"] = []
        candidate["protocol_error"] = "The model could not produce a valid interaction control; no action was executed."
    candidate["define_tool_present"] = False
    candidate["define_tool_registered"] = False
    return repaired, candidate


def _recover_plan_response(self, turn, text: str, parsed: dict[str, Any],
                          context: list[dict[str, str]], *,
                          inspection_complete: bool = False) -> tuple[str, dict[str, Any], bool]:
    """Bound Plan-mode recovery to read-only inspection or one valid control."""

    def valid(candidate: dict[str, Any]) -> bool:
        if (candidate.get("protocol_error") or candidate.get("unknown_action")
                or candidate.get("unsupported_action_protocol")
                or candidate.get("define_tool_present")):
            return False
        if candidate.get("question") is not None:
            return inspection_complete
        if candidate.get("plan_proposal") is not None:
            return inspection_complete
        calls = candidate.get("tool_calls") or []
        return bool(calls) and all(
            tool_capability(self._operation_action(call.get("action", ""))) in {"read", "network"}
            for call in calls
        )

    def normalize_structured_prose(candidate_text: str,
                                   candidate: dict[str, Any]) -> dict[str, Any]:
        if (not inspection_complete or candidate.get("protocol_error")
                or candidate.get("tool_calls") or candidate.get("question") is not None
                or candidate.get("plan_proposal") is not None):
            return candidate
        legacy_plan = self._legacy_plan_value(candidate_text, allow_unheaded=True)
        if legacy_plan is None:
            return candidate
        try:
            candidate["plan_proposal"] = validate_plan_proposal(legacy_plan)
        except InteractionError:
            return candidate
        candidate["protocol_error"] = None
        return candidate

    parsed = normalize_structured_prose(text, parsed)

    if turn.interaction_mode != "plan" or valid(parsed):
        return text, parsed, False
    for _attempt in range(3):
        context = context + [
            {"role": "assistant", "content": str(text)},
            {"role": "user", "content": (
                "System: Plan mode is read-only and no changes were made. "
                + (
                    "This is the inspection phase: output exactly one listed read-only Action now. "
                    "Do not output AskUser or PlanProposal yet."
                    if not inspection_complete else
                    "Inspection is complete: output exactly one valid PlanProposal now, or AskUser only "
                    "if a material ambiguity remains."
                )
                + " Output no task, execution, or tool-definition controls."
            )},
        ]
        text = self._call_llm_with_spinner(context).strip()
        turn.turn_prompt_total += self._last_prompt_tokens
        turn.turn_completion_total += self._last_completion_tokens
        parsed = self._observe_model_response(text)
        if (not parsed.get("protocol_error") and not parsed.get("tool_calls")
                and parsed.get("question") is None and parsed.get("plan_proposal") is None):
            control_name = (
                "AskUser" if re.search(r'"questions"\s*:', text) else
                "PlanProposal" if re.search(r'"(?:title|plan_name)"\s*:', text)
                and re.search(r'"steps"\s*:', text) else ""
            )
            if control_name:
                candidate = self._observe_model_response(f"{control_name}:\n{text}")
                if not candidate.get("protocol_error"):
                    parsed = candidate
        if valid(parsed):
            return text, parsed, False
        parsed = normalize_structured_prose(text, parsed)
        if valid(parsed):
            return text, parsed, False
    parsed["tool_calls"] = []
    return text, parsed, True
