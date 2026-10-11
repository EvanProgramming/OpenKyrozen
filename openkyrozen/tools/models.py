from __future__ import annotations

import json
import math
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


@dataclass(frozen=True)
class CommandResult:
    """The durable outcome of one shell command, separate from its display text."""

    output: str
    success: bool
    exit_code: int | None = None
    failure: str | None = None

    def __str__(self) -> str:
        return self.output
