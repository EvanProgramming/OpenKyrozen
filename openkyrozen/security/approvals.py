"""Immutable approval facts bound to one concrete tool invocation."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field


def _json_copy(value: object) -> tuple[dict, str]:
    if not isinstance(value, dict):
        raise ValueError("Approval arguments must be a JSON object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                             separators=(",", ":"))
        copied = json.loads(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("Approval arguments must contain only finite JSON values") from exc
    if not isinstance(copied, dict):
        raise ValueError("Approval arguments must be a JSON object")
    return copied, encoded


@dataclass(frozen=True, init=False)
class ApprovalRequest:
    request_id: str
    call_id: str
    tool_name: str
    workspace_id: str
    session_id: str
    tool_version: str
    tool_source: str
    capability: str
    risk: str
    side_effects: str
    fingerprint: str
    _arguments_json: str = field(repr=False)

    def __init__(self, *, request_id: str | None = None, call_id: str, tool_name: str,
                 arguments: dict, workspace_id: str, session_id: str, tool_version: str,
                 tool_source: str, capability: str, risk: str, side_effects: str):
        values = {
            "call_id": call_id, "tool_name": tool_name, "workspace_id": workspace_id,
            "session_id": session_id, "tool_version": tool_version, "tool_source": tool_source,
            "capability": capability, "risk": risk, "side_effects": side_effects,
        }
        if any(not isinstance(value, str) or not value.strip() for value in values.values()):
            raise ValueError("Approval identity and policy fields must be nonempty strings")
        _, arguments_json = _json_copy(arguments)
        fingerprint = hashlib.sha256(json.dumps(
            {**values, "arguments": json.loads(arguments_json)}, ensure_ascii=False,
            allow_nan=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        object.__setattr__(self, "request_id", request_id or uuid.uuid4().hex)
        for key, value in values.items():
            object.__setattr__(self, key, value)
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "_arguments_json", arguments_json)

    @property
    def arguments(self) -> dict:
        """Return a fresh copy so approval UI code cannot mutate the request."""
        return json.loads(self._arguments_json)
