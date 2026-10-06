from __future__ import annotations

import html
import json
import re
from typing import Any
from openkyrozen.tasks.engine import canonical_status
from openkyrozen.agent.modes import InteractionError, normalize_provider_control, parse_control_block, render_plan, render_question, validate_plan_proposal, validate_question_request
from openkyrozen.security.tool_policy import tool_capability
from openkyrozen.agent.types import DeepSeekDSMLFilter


def _is_valid_action(self, name: str | None) -> bool:
    if not name:
        return False
    return name in self.AVAILABLE_TOOLS or name in self.TOOL_ALIASES


def _detect_unknown_action(self, text: str) -> str | None:
    """Return the first action name in the text that is not a valid tool."""
    for obj in self._extract_json_objects(text):
        action = obj.get("action")
        if action and not self._is_valid_action(action):
            return str(action)
    return None


def _has_unsupported_action_protocol(self, text: str) -> bool:
    """Detect the provider wrapper that is not an executable tool call."""
    return bool(self._UNSUPPORTED_ACTION_PROTOCOL_RE.search(str(text or "")))


def parse_json_from_response(self, text: str) -> dict | None:
    text = (text or "").strip()
    for pattern in (
        r"Action:\s*```(?:json)?\s*([\s\S]*?)\s*```",
        r"Action:\s*(\{[\s\S]*?\})\s*(?:```|$)",
        r"```(?:json)?\s*([\s\S]*?)\s*```",
    ):
        for match in re.finditer(pattern, text):
            raw = match.group(1).strip()
            raw = raw.rstrip("`").strip()
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and "action" in data and self._is_valid_action(data.get("action")):
                    return data
            except json.JSONDecodeError:
                continue

    try:
        start = text.find("{")
        if start != -1:
            end = text.rfind("}")
            if end != -1 and end > start:
                raw = text[start:end + 1]
                data = json.loads(raw)
                if isinstance(data, dict) and "action" in data and self._is_valid_action(data.get("action")):
                    return data
    except (json.JSONDecodeError, ValueError):
        pass

    return None


def _extract_json_objects(self, text: str) -> list[dict]:
    objects: list[dict] = []
    i = 0
    decoder = json.JSONDecoder()
    while True:
        start = text.find("{", i)
        if start == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            i = start + 1
            continue
        if isinstance(obj, dict):
            objects.append(obj)
        i = end
    return objects


def _collect_unwrapped_tool_calls(self, text: str) -> tuple[list[dict], bool]:
    """Parse safe, line/fence-bounded provider aliases such as ``run_cmd:``."""
    calls: list[dict] = []
    malformed = False
    value = str(text or "")
    for match in self._ACTION_MARKER_RE.finditer(value):
        if not self._action_marker_is_protocol(value, match, final=True):
            continue
        line_end = value.find("\n", match.end())
        line_end = len(value) if line_end < 0 else line_end
        prefix = value[value.rfind("\n", 0, match.start()) + 1:match.start()].rstrip()
        if prefix and not prefix.endswith((".", "!", "?", ")", "]", "`")):
            continue
        cursor = match.end()
        while cursor < len(value) and value[cursor] in " \t":
            cursor += 1
        fence_cursor = cursor
        if value[fence_cursor:fence_cursor + 2] == "\r\n":
            fence_cursor += 2
        elif value[fence_cursor:fence_cursor + 1] == "\n":
            fence_cursor += 1
        if fence_cursor != cursor:
            while fence_cursor < len(value) and value[fence_cursor] in " \t":
                fence_cursor += 1
        if value[fence_cursor:fence_cursor + 3] == "```":
            fence_line = value.find("\n", fence_cursor + 3)
            close = value.find("```", fence_line + 1) if fence_line >= 0 else -1
            if fence_line < 0 or close < 0:
                malformed = True
                continue
            args = value[fence_line + 1:close].strip()
            if not args:
                malformed = True
                continue
            trailing_end = value.find("\n", close + 3)
            trailing_end = len(value) if trailing_end < 0 else trailing_end
            if value[close + 3:trailing_end].strip():
                malformed = True
                continue
        else:
            args = value[cursor:line_end].strip().strip("`\"'")
            if not args:
                malformed = True
                continue
            next_marker = self._ACTION_MARKER_RE.search(value, cursor)
            if next_marker is not None and next_marker.start() < line_end:
                malformed = True
                continue
        raw_action = match.group("name").lower()
        calls.append({"action": self.TOOL_ALIASES.get(raw_action, raw_action), "args": args})
    return calls, malformed


def _collect_tool_calls(self, text: str) -> list[dict]:
    """Extract tool-call dicts from LLM response. Deduplicated by (action, args)."""
    if self._has_unsupported_action_protocol(text):
        return []
    calls: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(data: dict) -> None:
        key = (str(data.get("action", "")), str(data.get("args", "")))
        if key not in seen:
            seen.add(key)
            calls.append(data)

    # 1. standard triple‑backtick block: Action:\n```json\n{...}\n```
    pattern = r"Action:\s*```(?:json)?\s*([\s\S]*?)\s*```"
    for match in re.finditer(pattern, text, re.DOTALL):
        raw = match.group(1).strip().rstrip("`").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "action" in data and self._is_valid_action(data.get("action")):
            _add(data)

    # 2. plain "Action: { … }" without backticks
    plain_pattern = r"Action:\s*(?:\n)?\s*(\{[\s\S]*?\})"
    for match in re.finditer(plain_pattern, text, re.DOTALL):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "action" in data and self._is_valid_action(data.get("action")):
            _add(data)

    # 3. extract any JSON dict from anywhere (only if it matches tool format exactly)
    for obj in self._extract_json_objects(text):
        if (isinstance(obj, dict) and "action" in obj and "args" in obj
                and self._is_valid_action(obj.get("action"))
                and len(obj) <= 3):  # action, args + optionally one more key
            _add(obj)

    marker = r"(?:｜｜DSML｜｜|｜DSML｜|\|\|DSML\|\||\|DSML\|)"
    invoke_pattern = re.compile(
        rf"<{marker}\s*invoke\s+name=[\"'](?P<name>[^\"']+)[\"'][^>]*>"
        rf"(?P<body>[\s\S]*?)</{marker}\s*invoke\s*>",
        re.IGNORECASE,
    )
    parameter_pattern = re.compile(
        rf"<{marker}\s*parameter\s+name=[\"'](?P<name>[^\"']+)[\"'][^>]*>"
        rf"(?P<value>[\s\S]*?)</{marker}\s*parameter\s*>",
        re.IGNORECASE,
    )
    for invoke in invoke_pattern.finditer(text):
        parameters = list(parameter_pattern.finditer(invoke.group("body")))
        raw_action = invoke.group("name").strip().lower()
        if (len(parameters) == 1 and parameters[0].group("name").strip().lower() == "args"
                and self._is_valid_action(raw_action)):
            args = html.unescape(parameters[0].group("value").strip())
            if args:
                _add({"action": self.TOOL_ALIASES.get(raw_action, raw_action), "args": args})

    xml_action_pattern = re.compile(
        r"<\s*action\s*>\s*(?P<name>[A-Za-z_]\w*)[ \t]*\r?\n"
        r"(?P<args>[\s\S]*?)</\s*action\s*>",
        re.IGNORECASE,
    )
    xml_actions = list(xml_action_pattern.finditer(text))
    # Provider XML is accepted only as one complete, unambiguous action block.
    if len(xml_actions) == 1 and not re.search(
            r"<\s*action\s*>[\s\S]*<\s*action\s*>", text, re.IGNORECASE):
        xml_action = xml_actions[0]
        raw_action = xml_action.group("name").strip().lower()
        args = html.unescape(xml_action.group("args").strip())
        if args and self._is_valid_action(raw_action):
            _add({"action": self.TOOL_ALIASES.get(raw_action, raw_action), "args": args})

    for data in self._collect_unwrapped_tool_calls(text)[0]:
        if self._is_valid_action(data.get("action")):
            _add(data)

    return calls


def _marker_line_prefix(self, value: str, start: int) -> str:
    return value[value.rfind("\n", 0, start) + 1:start].rstrip()


def _action_marker_is_protocol(self, value: str, match: re.Match[str], *, final: bool) -> bool:
    """Accept only bounded alias lines, not prose that mentions a tool name."""
    prefix = self._marker_line_prefix(value, match.start())
    if prefix and not prefix.endswith(self._ACTION_SENTENCE_ENDS):
        return False
    cursor = match.end()
    while cursor < len(value) and value[cursor] in " \t":
        cursor += 1
    if value[cursor:cursor + 2] == "\r\n":
        cursor += 2
    elif value[cursor:cursor + 1] == "\n":
        cursor += 1
    while cursor < len(value) and value[cursor] in " \t":
        cursor += 1
    if value[cursor:cursor + 3] == "```":
        return True
    line_end = value.find("\n", cursor)
    line_end = len(value) if line_end < 0 else line_end
    args = value[cursor:line_end].strip().strip("`\"'")
    if not args:
        return final
    return not self._ACTION_PROSE_START_RE.match(args)


def _control_marker_is_protocol(self, value: str, match: re.Match[str]) -> bool:
    prefix = self._marker_line_prefix(value, match.start())
    if not prefix or prefix.endswith(self._ACTION_SENTENCE_ENDS):
        return True
    return bool(re.search(
        r"(?i)(?:Action|Plan|TaskList|TaskDone|DefineTool)\s*:", prefix[-400:],
    ))


def _remove_protocol_headings(self, value: str) -> str:
    """Remove control headings only when their context is protocol-like."""
    matches = list(DeepSeekDSMLFilter._CONTROL_RE.finditer(value))
    for match in reversed(matches):
        if self._control_marker_is_protocol(value, match):
            value = value[:match.start()] + value[match.end():]
    return value


def _clean_final_response(self, text: str) -> str:
    """Return only user-facing prose from one model response.

    Plan, TaskList, TaskDone, Thought, and Action are control protocol.  They
    are useful in the next model prompt, but must never become the final
    answer merely because the turn stopped after a tool call.
    """
    cleaned = DeepSeekDSMLFilter().feed(str(text or ""), final=True).strip()
    if not cleaned:
        return ""
    # Some providers omit the ``Action:`` heading and return only the JSON
    # payload. It is still protocol, never a user-facing answer.
    json_source = cleaned
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", json_source, re.IGNORECASE)
    if fenced:
        json_source = fenced.group(1).strip()
    try:
        payload = json.loads(json_source)
    except json.JSONDecodeError:
        payload = None
    if (
        isinstance(payload, dict)
        and self._is_valid_action(str(payload.get("action", "")))
        and "args" in payload
    ) or (
        isinstance(payload, list)
        and payload
        and all(
            isinstance(item, dict)
            and self._is_valid_action(str(item.get("action", "")))
            and "args" in item
            for item in payload
        )
    ):
        return ""
    decoder = json.JSONDecoder()
    spans: list[tuple[int, int]] = []
    for index, char in enumerate(cleaned):
        if char not in "[{" or (index and cleaned[index - 1] in "[{,"):
            continue
        try:
            candidate, end = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        items = candidate if isinstance(candidate, list) else [candidate]
        if items and all(
            isinstance(item, dict)
            and self._is_valid_action(str(item.get("action", "")))
            and "args" in item
            for item in items
        ):
            spans.append((index, index + end))
    for start, end in reversed(spans):
        cleaned = cleaned[:start] + cleaned[end:]
    cleaned = cleaned.strip()
    if not cleaned:
        return ""
    cleaned = self._UNSUPPORTED_ACTION_PROTOCOL_RE.sub("", cleaned).strip()
    if not cleaned:
        return ""
    # Remove fenced protocol blocks first.  The model's JSON may contain
    # braces and newlines, so a line-based JSON parser would be less reliable.
    cleaned = re.sub(r"DefineTool:\s*```(?:python)?\s*[\s\S]*?```", "", cleaned,
                     flags=re.IGNORECASE)
    cleaned = re.sub(r"Action:\s*```(?:json)?\s*[\s\S]*?```", "", cleaned,
                     flags=re.IGNORECASE)
    cleaned = re.sub(r"TaskList:\s*```(?:json)?\s*[\s\S]*?```", "", cleaned,
                     flags=re.IGNORECASE)
    cleaned = re.sub(r"(?<![\w])AskUser[ \t]*:?[ \t]*\n```(?:json)?\s*[\s\S]*?```", "", cleaned,
                     flags=re.IGNORECASE)
    cleaned = re.sub(r"(?<![\w])PlanProposal[ \t]*:?[ \t]*\n```(?:json)?\s*[\s\S]*?```", "", cleaned,
                     flags=re.IGNORECASE)
    # Accept the legacy plain JSON forms too.
    cleaned = re.sub(r"Action:\s*(?:\{[\s\S]*?\}|\[[\s\S]*?\])", "", cleaned,
                     flags=re.IGNORECASE)
    cleaned = re.sub(r"TaskList:\s*(?:\[[\s\S]*?\])", "", cleaned,
                     flags=re.IGNORECASE)
    # Plans are normally numbered.  Remove only the numbered lines so a
    # natural-language sentence after a plan-only response is preserved.
    cleaned = re.sub(
        r"^[ \t]*Plan:[ \t]*\n(?:[ \t]*(?:\d+[.)]|[-*])[ \t]+.*(?:\n|$))+",
        "", cleaned, flags=re.IGNORECASE | re.MULTILINE,
    )
    # For non-numbered legacy plans, remove the plan through the next control
    # heading.  This branch is deliberately narrower than a greedy Plan:*.*
    # removal so it cannot swallow a final paragraph.
    cleaned = re.sub(
        r"^[ \t]*Plan:[ \t]*\n[\s\S]*?(?=^[ \t]*(?:Action|TaskList|TaskDone):)",
        "", cleaned, flags=re.IGNORECASE | re.MULTILINE,
    )
    cleaned = re.sub(r"^[ \t]*TaskDone:[ \t]*\d+[ \t]*$", "", cleaned,
                     flags=re.IGNORECASE | re.MULTILINE)
    # Thought is internal narration.  Remove its heading line while keeping
    # any following natural-language answer.
    cleaned = re.sub(r"^[ \t]*Thought:[ \t]*.*(?:\n|$)", "", cleaned,
                     flags=re.IGNORECASE | re.MULTILINE)
    cleaned = self._remove_protocol_headings(cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _stream_buffer_has_tool_prefix(self, text: str) -> bool:
    """Hold a split bare JSON tool payload until it can be removed safely."""
    tail = str(text or "")[max(str(text or "").rfind("{"), str(text or "").rfind("[")):]
    compact = re.sub(r"\s+", "", tail).lower()
    return any(marker.startswith(compact) or compact.startswith(marker)
               for marker in ('{"action', '[{"action'))


def _clean_stream_buffer(self, text: str) -> str:
    return self._clean_final_response(text) if self._collect_tool_calls(text) else text


def _remove_task_blocks(self, text: str) -> str:
    """Backward-compatible name for protocol-block removal."""
    return self._clean_final_response(text)


def _legacy_plan_value(self, text: str, *, allow_unheaded: bool = False) -> dict[str, Any] | None:
    """Convert a bounded numbered Markdown plan into a proposal value."""
    source = str(text or "")
    match = re.search(
        r"^[ \t]*(?:Plan|PlanProposal):[ \t]*\n(?P<body>[\s\S]*?)"
        r"(?=^[ \t]*(?:Action|TaskList|TaskDone|DefineTool):|\Z)",
        source, re.IGNORECASE | re.MULTILINE,
    )
    if match:
        body = match.group("body")
    elif allow_unheaded:
        body = source
    else:
        return None
    lines = body.splitlines()
    numbered = [
        item.group(1).strip() for line in lines
        if (item := re.match(r"^[ \t]*\d+[.)][ \t]+(.+?)\s*$", line))
    ]
    descriptions = numbered or [
        item.group(1).strip() for line in lines
        if (item := re.match(r"^[ \t]*[-*][ \t]+(.+?)\s*$", line))
    ]
    steps = []
    for description in descriptions:
        steps.append({
            "id": f"step-{len(steps) + 1}",
            "title": description.strip("*_ ")[:120],
            "description": description,
            "acceptance": ["Step completed with observable evidence"],
        })
    if not 1 <= len(steps) <= 10 or (allow_unheaded and len(steps) < 2):
        return None
    return {
        "title": steps[0]["title"],
        "summary": "Review and approve these steps before execution.",
        "assumptions": [],
        "steps": steps,
    }


def _parse_model_response(self, text: str) -> dict[str, Any]:
    """Parse one model response exactly once for the turn state machine."""
    raw = str(text or "").strip()
    _unwrapped_calls, malformed_unwrapped = self._collect_unwrapped_tool_calls(raw)
    tool_calls = [] if malformed_unwrapped else self._collect_tool_calls(raw)
    question = None
    plan_proposal = None
    control_error = None
    try:
        question_value = parse_control_block(raw, "AskUser")
        plan_value = parse_control_block(raw, "PlanProposal")
        if question_value is None and plan_value is None:
            normalized = normalize_provider_control(
                raw, plan_mode=self._active_interaction_mode.get() == "plan",
            )
            if normalized is not None:
                name, value = normalized
                question_value = value if name == "AskUser" else None
                plan_value = value if name == "PlanProposal" else None
        if (question_value is None and plan_value is None and tool_calls
                and self._active_interaction_mode.get() == "plan"):
            plan_value = self._legacy_plan_value(raw)
            if plan_value is not None:
                # A usable plan wins over provider attempts to execute it early.
                tool_calls = []
        if question_value is not None:
            question = validate_question_request(question_value)
        if plan_value is not None:
            plan_proposal = validate_plan_proposal(plan_value)
    except InteractionError as exc:
        control_error = str(exc)
    markdown_proposal = bool(re.search(r"^[ \t]*PlanProposal:", raw, re.IGNORECASE | re.MULTILINE))
    if (plan_proposal is None and self._active_interaction_mode.get() == "plan"
            and (tool_calls or markdown_proposal)):
        legacy_plan = self._legacy_plan_value(raw)
        if legacy_plan is not None:
            plan_proposal = validate_plan_proposal(legacy_plan)
            tool_calls = []
            control_error = None
    has_control = question is not None or plan_proposal is not None
    combined_control = has_control and (
        bool(tool_calls)
        or bool(re.search(r"^[ \t]*(?:TaskList|TaskDone|DefineTool):", raw, re.IGNORECASE | re.MULTILINE))
        or (question is not None and plan_proposal is not None)
    )
    disallowed_plan_action = (
        self._active_interaction_mode.get() == "plan"
        and any(tool_capability(self._operation_action(call.get("action", ""))) not in {"read", "network"}
                for call in tool_calls)
    )
    if disallowed_plan_action:
        tool_calls = []
    return {
        "raw": raw,
        "clean": self._clean_final_response(raw),
        "has_plan": bool(re.search(r"^[ \t]*Plan:", raw, re.IGNORECASE | re.MULTILINE)),
        "has_tasklist": bool(re.search(r"^[ \t]*TaskList:", raw, re.IGNORECASE | re.MULTILINE)),
        "tool_calls": tool_calls,
        "question": question,
        "plan_proposal": plan_proposal,
        "unknown_action": self._detect_unknown_action(raw),
        "unsupported_action_protocol": self._has_unsupported_action_protocol(raw),
        "protocol_error": control_error or (
            "AskUser and PlanProposal must be the only control block in a response. "
            "No tool or task action was executed."
            if combined_control else
            "Plan mode rejected an executable action; no action was executed. Retry with one PlanProposal block."
            if disallowed_plan_action else
            "The model returned an incomplete or ambiguous unwrapped tool call. "
            "No tool was executed. Retry with one complete structured tool call."
            if malformed_unwrapped else None
        ),
    }


def _observe_model_response(self, text: str) -> dict[str, Any]:
    """Parse a response once and merge its durable task signals once."""
    define_tool_present = bool(re.search(r"^[ \t]*DefineTool\s*:", str(text or ""),
                                         re.IGNORECASE | re.MULTILINE))
    parsed = self._parse_model_response(text)
    has_control = parsed["question"] is not None or parsed["plan_proposal"] is not None
    define_tool_registered = (
        self._attempt_define_tool(text)
        if define_tool_present and not has_control and not parsed["protocol_error"] else False
    )
    parsed["define_tool_present"] = define_tool_present
    parsed["define_tool_registered"] = define_tool_registered
    if (parsed["raw"] and not has_control and not parsed["protocol_error"]
            and self._active_interaction_mode.get() == "agent"):
        if not self._interaction_controller.state().get("executing_plan"):
            self.tasks.from_llm_block(parsed["raw"])
        self.tasks.mark_done_from_text(parsed["raw"])
    return parsed


def _persist_interaction_control(self, parsed: dict[str, Any], user_input: str) -> str | None:
    if ((parsed.get("question") is not None or parsed.get("plan_proposal") is not None)
            and not self._interaction_controls_enabled.get()):
        return "Interaction controls are unavailable on this non-interactive surface; no action was executed."
    if parsed.get("question") is not None:
        question = self._interaction_controller.request_question(
            parsed["question"], original_input=user_input,
        )
        self._emit_stream_event({"event": "interaction", "interaction": self.interaction_envelope()})
        return render_question(question)
    if parsed.get("plan_proposal") is not None:
        plan = self._interaction_controller.propose_plan(
            parsed["plan_proposal"], original_input=user_input,
        )
        self._emit_stream_event({"event": "interaction", "interaction": self.interaction_envelope()})
        return render_plan(plan)
    return None


def _interaction_gate(self, parsed: dict[str, Any], user_input: str) -> str | None:
    """Stop a response before any executable path when it carries a control."""
    return parsed.get("protocol_error") or self._persist_interaction_control(parsed, user_input)


def _deterministic_tool_summary(self, tool_records: list[dict[str, Any]]) -> str:
    """Build a truthful user-facing summary when the model emitted only protocol."""
    lines: list[str] = []
    for record in tool_records:
        status = "Completed" if record.get("success") else "Could not complete"
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


def _split_reply(self, text: str) -> tuple[str, str]:
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
