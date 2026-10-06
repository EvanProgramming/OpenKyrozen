from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


TOOL_ALIASES = {'bash': 'run_cmd', 'shell': 'run_cmd', 'sh': 'run_cmd', 'browse_summary': 'read_webpage', 'run_terminal_command': 'execute_terminal_command', 'run_terminal': 'execute_terminal_command', 'terminal': 'execute_terminal_command', 'run_command': 'run_cmd', 'cmd': 'run_cmd', 'exec': 'run_cmd', 'execute': 'run_cmd', 'list_tree': 'list_tree', 'tree': 'list_tree', 'check_memory': 'check_stored_data', 'run_shell_command': 'run_cmd', 'run_shell': 'run_cmd', 'shell_command': 'run_cmd', 'execute_shell': 'run_cmd', 'shell_cmd': 'run_cmd', 'bash_cmd': 'run_cmd', 'command': 'run_cmd', 'run': 'run_cmd', 'run_shell': 'run_cmd', 'write': 'write_file', 'status': 'git_status', 'diff': 'git_diff', 'log': 'git_log', 'branch': 'git_branch', 'add': 'git_add', 'commit': 'git_commit', 'push': 'git_push', 'pull': 'git_pull', 'checkout': 'git_checkout', 'stash': 'git_stash', 'clone': 'git_clone', 'reset': 'git_reset', 'show': 'git_show', 'remote': 'git_remote'}
_ACTION_MARKER_NAMES = ('execute_terminal_command', 'run_terminal_command', 'analyze_remote_repo', 'run_shell_command', 'check_stored_data', 'browser_snapshot', 'browse_summary', 'browser_close', 'graph_refresh', 'graph_explain', 'shell_command', 'github_status', 'browser_click', 'search_memory', 'execute_shell', 'read_webpage', 'graph_status', 'browser_open', 'run_terminal', 'git_checkout', 'browser_type', 'check_memory', 'run_command', 'graph_query', 'github_read', 'graph_path', 'git_status', 'search_web', 'find_files', 'github_cli', 'git_remote', 'git_branch', 'git_commit', 'write_file', 'git_reset', 'read_file', 'list_tree', 'git_stash', 'shell_cmd', 'run_shell', 'git_clone', 'list_dir', 'git_pull', 'git_diff', 'terminal', 'checkout', 'bash_cmd', 'git_push', 'git_show', 'git_log', 'execute', 'run_cmd', 'git_add', 'command', 'status', 'commit', 'remote', 'branch', 'write', 'shell', 'reset', 'stash', 'clone', 'show', 'pull', 'push', 'diff', 'bash', 'exec', 'tree', 'cmd', 'add', 'run', 'log', 'sh')
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


@dataclass(frozen=True)
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
    failure: str | None = None
    exit_code: int | None = None
    verified_effect: str | None = None
    acceptance: str | None = None
    args_sha256: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "operation_id": self.operation_id,
            "action": self.action,
            "args": self.args,
            "args_sha256": self.args_sha256,
            "authorized": self.authorized,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "success": self.success,
            "result": self.result,
            "failure": self.failure,
            "exit_code": self.exit_code,
            "verified_effect": self.verified_effect,
            "acceptance": self.acceptance,
        }


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

    def __init__(self, is_valid_action=None) -> None:
        self._is_valid_action = is_valid_action or _is_valid_action
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

    @classmethod
    def _control_partial_suffix_length(cls, value: str) -> int:
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
                    if any(kind.startswith(name) for kind in cls._GENERIC_KINDS):
                        return len(suffix)
        lowered = value.lower()
        for length in range(min(len(value), max(map(len, cls._CONTROL_PREFIXES))), 0, -1):
            if lowered[-length:] in cls._CONTROL_PREFIXES:
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

    @classmethod
    def _find_action_marker(cls, value: str, *, final: bool) -> re.Match[str] | None:
        for match in cls._ACTION_MARKER_RE.finditer(value):
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
