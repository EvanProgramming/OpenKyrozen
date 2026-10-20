from __future__ import annotations

import json
import math
from functools import wraps
from dataclasses import dataclass, field
from typing import Any


def _validate_json(value: Any) -> None:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        for item in value.values():
            _validate_json(item)
    elif isinstance(value, list):
        for item in value:
            _validate_json(item)
    elif value is None or type(value) in {str, int, bool}:
        return
    elif type(value) is float and math.isfinite(value):
        return
    else:
        raise TypeError("Value is not JSON-safe")


def _json_object(value: Any, name: str) -> str:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a JSON object")
    try:
        _validate_json(value)
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} must contain only JSON-safe values") from exc


@dataclass(frozen=True, init=False)
class ToolError:
    """A stable machine-readable tool failure; metadata contains JSON-safe diagnostics."""

    code: str
    message: str
    retriable: bool
    _metadata_json: str = field(repr=False)

    def __init__(self, code: str, message: str, retriable: bool = False,
                 metadata: dict[str, Any] | None = None) -> None:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("ToolError code must be a nonempty string")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("ToolError message must be a nonempty string")
        if type(retriable) is not bool:
            raise TypeError("ToolError retriable must be a boolean")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "retriable", retriable)
        object.__setattr__(self, "_metadata_json", _json_object({} if metadata is None else metadata, "ToolError metadata"))

    @property
    def metadata(self) -> dict[str, Any]:
        return json.loads(self._metadata_json)


@dataclass(frozen=True, init=False)
class ToolResult:
    """A tool's typed outcome with display text kept separate from its status."""

    _data_json: str | None = field(repr=False)
    text: str
    error: ToolError | None
    _metadata_json: str = field(repr=False)

    def __init__(self, data: Any = None, text: str = "", error: ToolError | None = None,
                 metadata: dict[str, Any] | None = None) -> None:
        if not isinstance(text, str):
            raise TypeError("ToolResult text must be a string")
        if error is not None and not isinstance(error, ToolError):
            raise TypeError("ToolResult error must be ToolError or None")
        try:
            _validate_json(data)
            data_json = json.dumps(data, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise TypeError("ToolResult data must be JSON-safe") from exc
        object.__setattr__(self, "_data_json", data_json)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "error", error)
        object.__setattr__(self, "_metadata_json", _json_object({} if metadata is None else metadata, "ToolResult metadata"))

    @property
    def success(self) -> bool:
        return self.error is None

    @property
    def data(self) -> Any:
        return json.loads(self._data_json) if self._data_json is not None else None

    @property
    def metadata(self) -> dict[str, Any]:
        return json.loads(self._metadata_json)


def render_tool_result(result: ToolResult) -> str:
    """Render a typed outcome for legacy text clients without changing its status."""
    if not isinstance(result, ToolResult):
        raise TypeError("Expected ToolResult")
    if result.text:
        return result.text
    if result.error is not None:
        return f"Error: {result.error.message}"
    if result.data is not None:
        return json.dumps(result.data, ensure_ascii=False)
    return ""


def tool_success(text: str, *, data: Any = None, metadata: dict[str, Any] | None = None) -> ToolResult:
    return ToolResult(data=data, text=text, metadata=metadata)


def tool_json_result(data: Any, *, text: str | None = None) -> ToolResult:
    rendered = json.dumps(data, ensure_ascii=False) if text is None else text
    return ToolResult(data=data, text=rendered)


def tool_failure(code: str, text: str, *, message: str | None = None, retriable: bool = False,
                 data: Any = None, metadata: dict[str, Any] | None = None) -> ToolResult:
    display = text if isinstance(text,str) else str(text)
    error_message = message or display.removeprefix("Error: ").strip() or code.replace("_", " ")
    return ToolResult(data=data, text=display,
                      error=ToolError(code,error_message,retriable,metadata))


def require_tool_result(value: Any) -> ToolResult:
    """Convert a bad executor contract into a terminal typed failure."""
    if isinstance(value, ToolResult):
        return value
    return ToolResult(text="Error: tool executor returned an invalid result", error=ToolError(
        "contract_error", "Tool executor must return ToolResult",
        metadata={"return_type": type(value).__name__},
    ))


def legacy_text_api(function):
    """Expose a typed producer through its historical rendered string interface."""
    function.__tool_result_executor__ = function
    @wraps(function)
    def render(*args, **kwargs):
        return render_tool_result(require_tool_result(function(*args, **kwargs)))

    render.__tool_result_executor__ = function
    return render


def invoke_tool_result(function, *args, **kwargs) -> ToolResult:
    """Call a legacy-wrapped producer through its typed implementation."""
    bound = getattr(function, "__func__", function)
    typed = getattr(bound, "__tool_result_executor__", None)
    if typed is None:
        return require_tool_result(function(*args, **kwargs))
    owner = getattr(function, "__self__", None)
    return require_tool_result(typed(owner, *args, **kwargs) if owner is not None else typed(*args, **kwargs))


def command_tool_result(result: "CommandResult") -> ToolResult:
    """Preserve explicit process status and exit details as a typed outcome."""
    if not isinstance(result, CommandResult):
        raise TypeError("Expected CommandResult")
    data = {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_seconds": result.duration_seconds,
        "timed_out": result.timed_out,
        "process_id": result.process_id,
        "sandbox_backend": result.sandbox_backend,
    }
    if result.success:
        return ToolResult(data=data, text=result.output)
    code = {"nonzero_exit": "command_failed", "denied": "permission_denied"}.get(
        result.failure, result.failure or "command_failed"
    )
    return ToolResult(data=data, text=result.output, error=ToolError(
        code, result.output or "Command failed", metadata={"exit_code": result.exit_code},
    ))


@dataclass(frozen=True)
class CommandResult:
    """The durable outcome of one shell command, separate from its display text."""

    output: str
    success: bool
    exit_code: int | None = None
    failure: str | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False
    process_id: str | None = None
    sandbox_backend: str | None = None

    def __str__(self) -> str:
        return self.output
