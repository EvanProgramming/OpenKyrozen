from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any


from openkyrozen.tools.catalog import builtin_aliases, builtin_names
from openkyrozen.tools.models import ToolError, ToolResult
TOOL_ALIASES = builtin_aliases()
_ACTION_MARKER_NAMES = tuple(sorted(set(builtin_names()) | set(TOOL_ALIASES), key=len, reverse=True))
_ACTION_MARKER_RE = re.compile(
    r"(?i)(?<![\w])(?P<name>(?:" + "|".join(map(re.escape, _ACTION_MARKER_NAMES)) + r"))\s*:"
)

_ACTION_SENTENCE_ENDS = (".", "!", "?", "…", "。", "！", "？", ")", "]", "}", "`", '"', "'")

_ACTION_PROSE_START_RE = re.compile(
    r"(?i)^(?:a|an|and|are|accepts|can|does|for|from|is|means|must|not|or|returns|the|that|this|to|used|use|will|which|with)\b"
)


def _is_valid_action(name: str | None) -> bool:
    if not name:
        return False
    return name in _ACTION_MARKER_NAMES or name in TOOL_ALIASES

def _marker_line_prefix(value: str, start: int) -> str:
    return value[value.rfind("\n", 0, start) + 1:start].rstrip()

def _action_marker_is_protocol(value: str, match: re.Match[str], *, final: bool) -> bool:
    """Accept only bounded alias lines, not prose that mentions a tool name."""
    prefix = _marker_line_prefix(value, match.start())
    if prefix and not prefix.endswith(_ACTION_SENTENCE_ENDS):
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
    return not _ACTION_PROSE_START_RE.match(args)

def _control_marker_is_protocol(value: str, match: re.Match[str]) -> bool:
    prefix = _marker_line_prefix(value, match.start())
    if not prefix or prefix.endswith(_ACTION_SENTENCE_ENDS):
        return True
    return bool(re.search(
        r"(?i)(?:Action|Plan|TaskList|TaskDone|DefineTool)\s*:", prefix[-400:],
    ))

class ProviderUnavailableError(RuntimeError):
    """Raised when a chat request reaches the provider boundary unconfigured."""

    code = "provider_unavailable"


class ContextOverflowError(RuntimeError):
    """A provider rejected a model request because its input exceeds context."""


class ContextTooLargeError(RuntimeError):
    """Fixed instructions plus protected current work cannot fit the model."""


def _json_snapshot(value: Any, label: str) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise TypeError(f"{label} must contain only JSON-safe values") from exc


def _bounded_redacted(value: Any, *, budget: int = 4000, depth: int = 0) -> Any:
    remaining = [budget]

    def clean(item: Any, level: int) -> Any:
        if remaining[0] <= 0:
            return "<truncated>"
        if isinstance(item, dict):
            result = {}
            for key, nested in list(item.items())[:40]:
                key = str(key)[:100]
                remaining[0] -= len(key)
                if re.search(r"(?i)(api.?key|credential|password|secret|token|authorization|request.?body|^body$)", key):
                    result[key] = "<redacted>"
                    remaining[0] -= 10
                else:
                    result[key] = "<truncated>" if level >= 5 else clean(nested, level + 1)
            return result
        if isinstance(item, (list, tuple)):
            result = []
            for nested in item[:40]:
                result.append(clean(nested, level + 1))
                if remaining[0] <= 0:
                    break
            return result
        if isinstance(item, str):
            size = min(1000, remaining[0])
            result = item[:size]
            remaining[0] -= len(result)
            return result + ("<truncated>" if len(item) > size else "")
        if item is None or type(item) in {bool, int} or type(item) is float and math.isfinite(item):
            remaining[0] -= 16
            return item
        return f"<{type(item).__name__}>"

    return clean(value, depth)


def _receipt_args_projection(value: str, arguments: dict[str, Any] | None) -> str:
    if arguments is not None:
        return json.dumps(_bounded_redacted(arguments, budget=1000), ensure_ascii=False, sort_keys=True)
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        return json.dumps(_bounded_redacted(parsed, budget=1000), ensure_ascii=False, sort_keys=True)
    value = re.sub(
        r'(?i)((?:[\w.-]*(?:api[-_]?key|credential|password|secret|token|authorization)[\w.-]*|bearer)\s*[:=]\s*["\']?)[^,\s;"\'&]+',
        r'\1<redacted>', value,
    )
    return value[:1000]


@dataclass(frozen=True, init=False)
class ExecutionReceipt:
    """Authoritative, bounded result of one tool attempt."""

    receipt_id: str
    operation_id: str
    action: str
    args: str
    authorized: bool
    started_at: str
    completed_at: str
    success: bool
    result: str
    call_id: str | None
    call_id_source: str
    _arguments_json: str | None = field(repr=False)
    _tool_result: ToolResult | None = field(repr=False)
    user_id: str | None
    workspace_id: str | None
    session_id: str | None
    plan_id: str | None
    task_id: str | None
    tool_version: str | None
    tool_source: str | None
    side_effects: str
    _verification_json: str = field(repr=False)
    failure: str | None = None
    exit_code: int | None = None
    verified_effect: str | None = None
    acceptance: str | None = None
    args_sha256: str | None = None
    _tool_error_json: str | None = field(repr=False)
    _result_data_json: str = field(repr=False)

    def __init__(self, receipt_id: str, operation_id: str, action: str, args: str,
                 authorized: bool, started_at: str, completed_at: str, success: bool,
                 result: str, failure: str | None = None, exit_code: int | None = None,
                 verified_effect: str | None = None, acceptance: str | None = None,
                 args_sha256: str | None = None, tool_error: dict[str, Any] | None = None,
                 result_data: Any = None, *, call_id: str | None = None,
                 call_id_source: str = "legacy", arguments: dict[str, Any] | None = None,
                 tool_result: ToolResult | None = None, user_id: str | None = None,
                 workspace_id: str | None = None, session_id: str | None = None,
                 plan_id: str | None = None, task_id: str | None = None,
                 tool_version: str | None = None, tool_source: str | None = None,
                 side_effects: str = "unknown", verification: dict[str, Any] | None = None):
        if call_id is not None and (not isinstance(call_id, str) or not call_id.strip()):
            raise ValueError("Receipt call_id must be nonempty when present")
        if call_id_source not in {"native", "legacy", "synthetic"}:
            raise ValueError("Receipt call_id_source must be native, legacy, or synthetic")
        if arguments is not None and not isinstance(arguments, dict):
            raise TypeError("Receipt arguments must be a JSON object")
        if tool_result is not None and not isinstance(tool_result, ToolResult):
            raise TypeError("Receipt tool_result must be ToolResult or None")
        if verification is None:
            verification = {"status": "unavailable", "method": "none", "observed": None}
        if not isinstance(verification, dict):
            raise TypeError("Receipt verification must be a JSON object")
        verification_status = verification.get("status")
        if verification_status not in {"verified", "failed", "unavailable", "not_applicable"}:
            raise ValueError("Receipt verification status is invalid")
        if tool_result is None:
            if tool_error is not None:
                error = ToolError(
                    str(tool_error.get("code", "execution_failed")),
                    str(tool_error.get("message", result or "Tool execution failed")),
                    bool(tool_error.get("retriable", False)),
                    tool_error.get("metadata") if isinstance(tool_error.get("metadata"), dict) else {},
                )
                tool_result = ToolResult(data=result_data, text=result, error=error)
        object.__setattr__(self, "receipt_id", receipt_id)
        object.__setattr__(self, "operation_id", operation_id)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "args", args)
        object.__setattr__(self, "authorized", bool(authorized))
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(self, "result", result)
        object.__setattr__(self, "success", tool_result.success if tool_result is not None else bool(success))
        object.__setattr__(self, "call_id", call_id)
        object.__setattr__(self, "call_id_source", call_id_source)
        object.__setattr__(self, "_arguments_json", _json_snapshot(arguments, "Receipt arguments") if arguments is not None else None)
        object.__setattr__(self, "_tool_result", tool_result)
        for name, value in (("user_id", user_id), ("workspace_id", workspace_id), ("session_id", session_id),
                            ("plan_id", plan_id), ("task_id", task_id), ("tool_version", tool_version),
                            ("tool_source", tool_source), ("side_effects", side_effects),
                            ("failure", tool_result.error.code if tool_result is not None and tool_result.error else failure),
                            ("exit_code", exit_code), ("verified_effect", verified_effect),
                            ("acceptance", acceptance), ("args_sha256", args_sha256)):
            object.__setattr__(self, name, value)
        object.__setattr__(self, "_verification_json", _json_snapshot(verification, "Receipt verification"))
        error_projection = tool_error
        if error_projection is None and tool_result is not None and tool_result.error is not None:
            error_projection = {"code": tool_result.error.code, "message": tool_result.error.message,
                                "retriable": tool_result.error.retriable,
                                "metadata": tool_result.error.metadata}
        object.__setattr__(self, "_tool_error_json", _json_snapshot(error_projection, "Receipt tool_error")
                           if error_projection is not None else None)
        object.__setattr__(self, "_result_data_json", _json_snapshot(tool_result.data if tool_result is not None else result_data, "Receipt result_data"))

    @property
    def arguments(self) -> dict[str, Any] | None:
        return json.loads(self._arguments_json) if self._arguments_json is not None else None

    @property
    def tool_result(self) -> ToolResult | None:
        return self._tool_result

    @property
    def verification(self) -> dict[str, Any]:
        return json.loads(self._verification_json)

    @property
    def tool_error(self) -> dict[str, Any] | None:
        return json.loads(self._tool_error_json) if self._tool_error_json is not None else None

    @property
    def result_data(self) -> Any:
        return json.loads(self._result_data_json)

    def as_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "operation_id": self.operation_id,
            "action": self.action,
            "args": _receipt_args_projection(self.args, self.arguments),
            "arguments": _bounded_redacted(self.arguments) if self.arguments is not None else None,
            "args_sha256": self.args_sha256,
            "authorized": self.authorized,
            "call_id": self.call_id,
            "call_id_source": self.call_id_source,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
            "plan_id": self.plan_id,
            "task_id": self.task_id,
            "tool_version": self.tool_version,
            "tool_source": self.tool_source,
            "side_effects": self.side_effects,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "success": self.success,
            "result": self.result,
            "failure": self.failure,
            "exit_code": self.exit_code,
            "verified_effect": self.verified_effect,
            "verification": _bounded_redacted(self.verification),
            "acceptance": self.acceptance,
            "tool_error": _bounded_redacted(self.tool_error) if self.tool_error is not None else None,
            "result_data": _bounded_redacted(self.result_data),
            "tool_result": _bounded_redacted({
                "data": self.tool_result.data,
                "text": self.tool_result.text,
                "error": ({"code": self.tool_result.error.code,
                           "message": self.tool_result.error.message,
                           "retriable": self.tool_result.error.retriable,
                           "metadata": self.tool_result.error.metadata}
                          if self.tool_result.error else None),
                "metadata": self.tool_result.metadata,
            }) if self.tool_result is not None else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ExecutionReceipt":
        """Read current or historical persisted receipt projections."""
        if not isinstance(value, dict):
            raise TypeError("Receipt payload must be an object")
        projection = value.get("tool_result")
        typed_result = None
        if isinstance(projection, dict):
            raw_error = projection.get("error")
            error = ToolError(
                raw_error["code"], raw_error["message"], raw_error.get("retriable", False),
                raw_error.get("metadata", {}),
            ) if isinstance(raw_error, dict) else None
            typed_result = ToolResult(
                data=projection.get("data"), text=projection.get("text", ""),
                error=error, metadata=projection.get("metadata", {}),
            )
        return cls(
            receipt_id=value["receipt_id"], operation_id=value["operation_id"],
            action=value["action"], args=value.get("args", ""),
            authorized=value.get("authorized", False), started_at=value.get("started_at", ""),
            completed_at=value.get("completed_at", ""), success=value.get("success", False),
            result=value.get("result", ""), failure=value.get("failure"),
            exit_code=value.get("exit_code"), verified_effect=value.get("verified_effect"),
            acceptance=value.get("acceptance"), args_sha256=value.get("args_sha256"),
            tool_error=value.get("tool_error"), result_data=value.get("result_data"),
            call_id=value.get("call_id"), call_id_source=value.get("call_id_source", "legacy"),
            arguments=value.get("arguments") if isinstance(value.get("arguments"), dict) else None,
            tool_result=typed_result, user_id=value.get("user_id"),
            workspace_id=value.get("workspace_id"), session_id=value.get("session_id"),
            plan_id=value.get("plan_id"), task_id=value.get("task_id"),
            tool_version=value.get("tool_version"), tool_source=value.get("tool_source"),
            side_effects=value.get("side_effects", "unknown"),
            verification=value.get("verification"),
        )


class DeepSeekDSMLFilter:
    """Remove provider control syntax without buffering ordinary prose."""

    _OPEN_RE = re.compile(
        r"<(?P<marker>｜DSML｜|\|DSML\|)"
        r"(?P<kind>tool_calls|function_calls|toolcalls|invoke|parameter)\b[^>]*>",
        re.IGNORECASE,
    )
    _STARTS = ("<｜DSML｜", "<|DSML|", "｜｜DSML｜｜", "||DSML||")
    _PLAIN_MARKER = re.compile(r"｜｜DSML｜｜|\|\|DSML\|\|")
    _CONTROL_RE = re.compile(
        r"(?i)(?<![\w])(?P<kind>Action|Thought|Plan|TaskList|TaskDone|DefineTool)\s*:"
    )
    _GENERIC_OPEN_RE = re.compile(
        r"<\s*(?P<kind>action|invoke|parameter|calls|tool_calls|function_calls|tool_use|notes|thought|reasoning|ssai_action)\b[^>]*>",
        re.IGNORECASE,
    )
    _GENERIC_CLOSE_RE = re.compile(
        r"</\s*(?P<kind>action|invoke|parameter|calls|tool_calls|function_calls|tool_use|notes|thought|reasoning|ssai_action)\s*>",
        re.IGNORECASE,
    )
    _GENERIC_KINDS = (
        "action", "invoke", "parameter", "calls", "tool_calls", "function_calls",
        "tool_use", "notes", "thought", "reasoning", "ssai_action",
    )
    _ACTION_MARKER_RE = re.compile(r"(?i)(?<![\w])(?P<name>" + "|".join(map(re.escape, _ACTION_MARKER_NAMES)) + r")\s*:")
    _CONTROL_PREFIXES = tuple(
        item[:length].lower()
        for item in (
            "action:", "thought:", "plan:", "tasklist:", "taskdone:", "definetool:",
            "<invoke", "<parameter", "<calls", "<tool_calls", "<function_calls",
            "< invoke", "< parameter", "< calls", "< tool_calls", "< function_calls",
            "</invoke", "</parameter", "</calls", "</tool_calls", "</function_calls",
            "</ invoke", "</ parameter", "</ calls", "</ tool_calls", "</ function_calls",
            *_ACTION_MARKER_NAMES,
        )
        for length in range(1, len(item) + 1)
    )

    def __init__(self, is_valid_action=None, action_names=None) -> None:
        self._is_valid_action = is_valid_action or _is_valid_action
        if action_names is not None:
            self._ACTION_MARKER_RE = re.compile(r"(?i)(?<![\w])(?P<name>"+'|'.join(map(re.escape,action_names))+r")\s*:")
            self._CONTROL_PREFIXES = self._CONTROL_PREFIXES + tuple(name[:length].lower() for name in action_names for length in range(1,len(name)+1))
        self._buffer = ""
        self._control_buffer = ""

    @classmethod
    def _partial_suffix_length(cls, value: str) -> int:
        prefixes = {
            start[:length]
            for start in cls._STARTS
            for length in range(1, len(start) + 1)
        }
        for length in range(min(len(value), max(map(len, cls._STARTS))), 0, -1):
            if value[-length:] in prefixes:
                return length
        return 0

    @classmethod
    def _close_for(cls, match: re.Match[str]) -> str:
        return f"</{match.group('marker')}{match.group('kind')}>"

    def _control_partial_suffix_length(self, value: str) -> int:
        start = value.rfind("<")
        if start >= 0:
            suffix = value[start:]
            if ">" not in suffix:
                remainder = suffix[1:]
                if remainder.startswith("/"):
                    remainder = remainder[1:]
                remainder = remainder.lstrip()
                if not remainder:
                    return len(suffix)
                name_match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", remainder)
                if name_match:
                    name = name_match.group(0).lower()
                    if any(kind.startswith(name) for kind in self._GENERIC_KINDS):
                        return len(suffix)
        lowered = value.lower()
        for length in range(min(len(value), max(map(len, self._CONTROL_PREFIXES))), 0, -1):
            if lowered[-length:] in self._CONTROL_PREFIXES:
                start = len(value) - length
                if start and value[start - 1].isalnum():
                    continue
                return length
        return 0

    @classmethod
    def _find_control_marker(cls, value: str, *, final: bool) -> re.Match[str] | None:
        for match in cls._CONTROL_RE.finditer(value):
            if _control_marker_is_protocol(value, match):
                return match
        return None

    def _find_action_marker(self, value: str, *, final: bool) -> re.Match[str] | None:
        for match in self._ACTION_MARKER_RE.finditer(value):
            if _action_marker_is_protocol(value, match, final=final):
                return match
        return None

    @staticmethod
    def _balanced_end(value: str, start: int, opening: str, closing: str) -> int | None:
        depth = 0
        quote = ""
        escaped = False
        for index in range(start, len(value)):
            char = value[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
                continue
            if char in "'\"":
                quote = char
            elif char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
                if depth == 0:
                    return index + 1
        return None

    @classmethod
    def _action_end(cls, value: str, start: int, *, final: bool) -> int | None:
        cursor = start
        while cursor < len(value) and value[cursor].isspace():
            cursor += 1
        if cursor >= len(value):
            return cursor if final else None
        if value[cursor] in "({[":
            pair = {"(": ")", "{": "}", "[": "]"}[value[cursor]]
            return cls._balanced_end(value, cursor, value[cursor], pair)
        if value[cursor] in "'\"":
            quote = value[cursor]
            escaped = False
            for index in range(cursor + 1, len(value)):
                char = value[index]
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    return index + 1
            return None
        end = cursor
        while end < len(value) and not value[end].isspace():
            end += 1
        return end

    @classmethod
    def _generic_block_end(cls, value: str, match: re.Match[str]) -> int | None:
        kind = match.group("kind").lower()
        close_kinds = (
            "parameter" if kind == "parameter" else
            "ssai_action" if kind == "ssai_action" else
            "action|invoke|calls|tool_calls|function_calls|tool_use|notes|thought|reasoning"
        )
        close = re.compile(rf"</\s*(?:{close_kinds})\s*>", re.IGNORECASE).search(value, match.end())
        return close.end() if close else None

    @staticmethod
    def _action_marker_end(value: str, match: re.Match[str], *, final: bool) -> int | None:
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
            close = value.find("```", cursor + 3)
            if close < 0:
                return len(value) if final else None
            return close + 3
        newline = value.find("\n", cursor)
        if newline >= 0:
            return newline + 1
        return len(value) if final else None

    def _filter_control(self, chunk: str, *, final: bool) -> str:
        self._control_buffer += chunk
        output: list[str] = []
        while self._control_buffer:
            control = self._find_control_marker(self._control_buffer, final=final)
            generic_open = self._GENERIC_OPEN_RE.search(self._control_buffer)
            generic_close = self._GENERIC_CLOSE_RE.search(self._control_buffer)
            action_marker = self._find_action_marker(self._control_buffer, final=final)
            starts = [item for item in (control, generic_open, generic_close, action_marker) if item is not None]
            if not starts:
                if final:
                    output.append(self._control_buffer)
                    self._control_buffer = ""
                else:
                    keep = self._control_partial_suffix_length(self._control_buffer)
                    safe_end = len(self._control_buffer) - keep
                    if safe_end:
                        output.append(self._control_buffer[:safe_end])
                        self._control_buffer = self._control_buffer[safe_end:]
                break

            start = min(item.start() for item in starts)
            if start:
                output.append(self._control_buffer[:start])
                self._control_buffer = self._control_buffer[start:]
                continue

            if generic_close is not None and generic_close.start() == 0 and (
                    control is None or generic_close.start() <= control.start()):
                self._control_buffer = self._control_buffer[generic_close.end():]
                continue

            if generic_open is not None and generic_open.start() == 0 and (
                    control is None or generic_open.start() <= control.start()):
                end = self._generic_block_end(self._control_buffer, generic_open)
                if end is None:
                    self._control_buffer = "" if final else self._control_buffer
                    break
                self._control_buffer = self._control_buffer[end:]
                continue

            if action_marker is not None and action_marker.start() == 0 and (
                    control is None or action_marker.start() <= control.start()):
                end = self._action_marker_end(self._control_buffer, action_marker, final=final)
                if end is None:
                    break
                self._control_buffer = self._control_buffer[end:]
                continue

            control = self._CONTROL_RE.match(self._control_buffer)
            if control is None:  # pragma: no cover - defensive loop guard
                output.append(self._control_buffer[0])
                self._control_buffer = self._control_buffer[1:]
                continue
            kind = control.group("kind").lower()
            cursor = control.end()
            if kind == "action":
                remainder = self._control_buffer[cursor:].lstrip()
                leading = len(self._control_buffer[cursor:]) - len(remainder)
                json_end = None
                if remainder.startswith(("{", "[")):
                    json_end = self._balanced_end(remainder, 0, remainder[0], {"{": "}", "[": "]"}[remainder[0]])
                    if json_end is None:
                        self._control_buffer = "" if final else self._control_buffer
                        break
                    try:
                        parsed = json.loads(remainder[:json_end])
                    except (TypeError, ValueError):
                        parsed = None
                    valid = (
                        isinstance(parsed, dict) and self._is_valid_action(str(parsed.get("action", "")))
                    ) or (
                        isinstance(parsed, list) and any(
                            isinstance(item, dict) and self._is_valid_action(str(item.get("action", "")))
                            for item in parsed
                        )
                    )
                    if not valid:
                        output.append(self._control_buffer[:control.end()])
                        self._control_buffer = self._control_buffer[control.end():]
                        continue
                    self._control_buffer = self._control_buffer[cursor + leading + json_end:]
                    continue
                if remainder.startswith("```"):
                    closing = remainder.find("```", 3)
                    if closing < 0:
                        self._control_buffer = "" if final else self._control_buffer
                        break
                    self._control_buffer = self._control_buffer[cursor + leading + closing + 3:]
                    continue
                name_match = re.match(r"[A-Za-z_]\w*", remainder)
                name = name_match.group(0) if name_match else ""
                if not self._is_valid_action(name):
                    output.append(self._control_buffer[:control.end()])
                    self._control_buffer = self._control_buffer[control.end():]
                    continue
                name_end = cursor + leading + len(name)
                end = self._action_end(self._control_buffer, name_end, final=final)
                if end is None:
                    self._control_buffer = "" if final else self._control_buffer
                    break
                self._control_buffer = self._control_buffer[end:]
                continue

            remainder = self._control_buffer[cursor:]
            leading = len(remainder) - len(remainder.lstrip())
            remainder = remainder.lstrip()
            if kind in {"action", "tasklist", "definetool"} and remainder.startswith("```"):
                closing = remainder.find("```", 3)
                if closing < 0:
                    self._control_buffer = "" if final else self._control_buffer
                    break
                self._control_buffer = self._control_buffer[cursor + leading + closing + 3:]
                continue
            # Preserve complete headings at the final boundary so the legacy
            # cleaner can remove their full line/block; live streams only need
            # the marker itself removed.
            if final:
                output.append(self._control_buffer[:cursor])
            self._control_buffer = self._control_buffer[cursor:]
        return "".join(output)

    def feed(self, chunk: str = "", *, final: bool = False) -> str:
        self._buffer += str(chunk or "")
        output: list[str] = []
        while self._buffer:
            match = self._OPEN_RE.search(self._buffer)
            plain_marker = self._PLAIN_MARKER.search(self._buffer)
            starts = [item for item in (match, plain_marker) if item is not None]
            if not starts:
                if final:
                    output.append(self._buffer)
                    self._buffer = ""
                else:
                    keep = self._partial_suffix_length(self._buffer)
                    safe_end = len(self._buffer) - keep
                    if safe_end:
                        output.append(self._buffer[:safe_end])
                        self._buffer = self._buffer[safe_end:]
                break

            start = min(item.start() for item in starts)
            if start:
                output.append(self._buffer[:start])
                self._buffer = self._buffer[start:]
                continue

            if plain_marker is not None and plain_marker.start() == 0 and (
                    match is None or plain_marker.start() <= match.start()):
                marker = plain_marker.group(0)
                end = self._buffer.find(marker, len(marker))
                if end < 0:
                    self._buffer = "" if final else self._buffer
                    break
                self._buffer = self._buffer[end + len(marker):]
                continue

            # A complete DSML opening tag is present.  Drop it and everything
            # through its matching close; an incomplete block is held until
            # the next provider delta (or discarded at the final boundary).
            match = self._OPEN_RE.match(self._buffer)
            if match is None:
                if final:
                    self._buffer = ""
                break
            close = self._close_for(match)
            end = self._buffer.find(close, match.end())
            if end < 0:
                self._buffer = "" if final else self._buffer
                break
            self._buffer = self._buffer[end + len(close):]
        return self._filter_control("".join(output), final=final)
