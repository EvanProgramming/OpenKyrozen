"""User-facing response formatting shared by native and legacy execution."""
from __future__ import annotations

import re
from typing import Any
from openkyrozen.tasks.engine import canonical_status


class _NativeResponseText(str):
    """Immutable per-reply provenance that survives the runtime turn scope."""
    __slots__ = ()


def is_native_response(runtime, text) -> bool:
    state = getattr(runtime, "_last_response_is_native", None)
    return isinstance(text, _NativeResponseText) or bool(state is not None and state.get())


def native_response_metadata(text: str) -> dict[str, Any]:
    """Expose native text literally through the established compatibility result shape."""
    raw = str(text or "").strip()
    return {
        "raw": raw, "clean": raw, "has_plan": False, "has_tasklist": False,
        "tool_calls": [], "question": None, "plan_proposal": None,
        "unknown_action": None, "unsupported_action_protocol": False,
        "protocol_error": None,
    }


def _clean_final_response(self, text: str) -> str:
    """Native output is literal; only the legacy boundary loads textual cleaning."""
    if is_native_response(self, text):
        return _NativeResponseText(str(text or "").strip())
    from openkyrozen.agent.protocol import _clean_final_response as legacy_clean
    return legacy_clean(self, text)


def _deterministic_tool_summary(self, tool_records: list[dict[str, Any]]) -> str:
    """Build a truthful user-facing summary when the model emitted only protocol."""
    lines: list[str] = []
    for record in tool_records:
        verified_failure = (record.get("verification") or {}).get("status") == "failed"
        status = "Completed" if record.get("success") and not verified_failure else "Could not complete"
        result = self._clean_final_response(str(record.get("result") or "")).replace("\n", " ")
        if len(result) > 300:
            result = result[:297] + "..."
        line = f"- {status}"
        if result:
            line += f": {result}"
        if record.get("unmatched_reason"):
            line += f" ({record['unmatched_reason']})"
        lines.append(line)
    task_lines: list[str] = []
    for task in self.tasks.tasks:
        status = canonical_status(task.get("status", "pending"))
        task_lines.append(f"- {status}: {task.get('description', '')}")
    if task_lines:
        lines.append("Durable task status (source of truth):")
        lines.extend(task_lines)
        attention = [task for task in self.tasks.tasks if canonical_status(task.get("status", "pending")) != "succeeded"]
        if attention:
            lines.append("Recovery required for non-succeeded tasks:")
            lines.extend(
                f"- {canonical_status(task.get('status', 'pending'))}: {task.get('description', '')}"
                for task in attention
            )
    if not lines:
        return "I could not produce a user-facing response."
    return "Here is what I completed:\n\n" + "\n".join(lines)


def _safe_fstring(self, s: str) -> str:
    """Escape curly braces so they do not interfere with f-string parsing."""
    return s.replace('{', '{{').replace('}', '}}')


def _remove_task_blocks(self, text: str) -> str:
    if is_native_response(self, text):
        return _NativeResponseText(str(text or "").strip())
    from openkyrozen.agent.protocol import _remove_task_blocks as legacy_remove
    return legacy_remove(self, text)


def _split_reply(self, text: str) -> tuple[str, str]:
    if is_native_response(self, text):
        return "", str(text or "").strip()
    text = text.strip()
    thought_match = re.search(r"^Thought:\s*(.*?)(?=\n(?:Action:|(?:\n|$)))", text, re.DOTALL | re.MULTILINE)
    if thought_match:
        thinking = thought_match.group(1).strip()
        answer = re.sub(r"^Thought:\s*.*?(?=\n(?:Action:|(?:\n|$))|\n?$)", "", text, count=1, flags=re.DOTALL | re.MULTILINE).strip()
        answer = re.sub(r"Action:\s*```(?:json)?[\s\S]*?```", "", answer).strip()
        answer = re.sub(r"\{[\s\S]*?\}", "", answer).strip()
        answer = answer.lstrip('"').lstrip("'").strip()
        if not answer:
            answer = text
        return thinking, answer
    else:
        return "", text
