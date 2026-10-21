"""Canonical immutable events emitted by the agent runtime."""
from __future__ import annotations

import json
import math
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


def _validate_json(value: Any) -> None:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Runtime event payload keys must be strings")
        for item in value.values():
            _validate_json(item)
    elif isinstance(value, list):
        for item in value:
            _validate_json(item)
    elif value is None or isinstance(value, (str, bool, int)):
        return
    elif not isinstance(value, float) or not math.isfinite(value):
        raise ValueError("Runtime event payload must contain finite JSON values")


def runtime_event_as_legacy(event: "RuntimeEvent") -> dict[str, Any] | None:
    """Project canonical events onto the existing interface callback shape."""
    payload = event.payload
    if event.kind == "model.text_delta":
        return {"event": "content", "chunk": payload.get("text", "")}
    if event.kind == "model.completed":
        return {"event": "model_complete"}
    if event.kind == "tool.result":
        return {"event": "tool_receipt", "tool_receipt": payload}
    if event.kind == "task.snapshot":
        return {"event": "tasks", "tasks": payload.get("tasks", []),
                "working_plan": payload.get("working_plan")}
    if event.kind.startswith("working_plan."):
        return {"event": "working_plan", "session_id": event.session_id, "working_plan": payload}
    if event.kind.startswith("task.") and event.task_id:
        name = event.kind.partition(".")[2]
        status = {"created": "pending", "claimed": "running", "recovered": "pending",
                  "resumed": "pending"}.get(name, name)
        if status not in {"pending", "running", "succeeded", "failed", "blocked", "cancelled"}:
            status = None
        return {"event": "task_update", "task_id": event.task_id,
                "session_id": event.session_id, "status": status, "description": payload.get("description"),
                "reason": payload.get("reason", "")}
    if event.kind == "interaction.updated":
        return {"event": "interaction", "interaction": payload.get("interaction", payload)}
    if event.kind == "security.permission_check":
        return {"event": "permission_check", "state": payload.get("state", ""),
                "category": payload.get("category", "")}
    if event.kind == "subagent.updated":
        return {"event": "subagent", "agent": payload.get("agent", {}),
                "session_id": payload.get("session_id"),
                "source_scope_id": payload.get("source_scope_id")}
    if event.kind == "routing.decision":
        return {"event": "fast_decision", "backend": payload.get("backend", "")}
    return None


@dataclass(frozen=True, init=False)
class RuntimeEvent:
    schema_version: int
    event_id: str
    kind: str
    timestamp: str
    sequence: int
    user_id: str
    workspace_id: str
    session_id: str
    turn_id: str | None
    model_request_id: str | None
    call_id: str | None
    task_id: str | None
    _payload_json: str = field(repr=False)

    def __init__(self, *, kind: str, user_id: str, workspace_id: str, session_id: str,
                 sequence: int, payload: dict[str, Any], turn_id: str | None = None,
                 model_request_id: str | None = None, call_id: str | None = None,
                 task_id: str | None = None, event_id: str | None = None,
                 timestamp: str | None = None, schema_version: int = 1):
        if not isinstance(kind, str) or not kind.strip() or len(kind) > 96:
            raise ValueError("Runtime event kind must be a nonempty string of at most 96 characters")
        if type(sequence) is not int or sequence < 1:
            raise ValueError("Runtime event sequence must be a positive integer")
        if type(schema_version) is not int or schema_version != 1:
            raise ValueError("Unsupported runtime event schema version")
        for label, value in (("user_id", user_id), ("workspace_id", workspace_id), ("session_id", session_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Runtime event {label} must be nonempty")
        if not isinstance(payload, dict):
            raise ValueError("Runtime event payload must be an object")
        event_id = event_id or f"evt_{uuid.uuid4().hex}"
        timestamp = timestamp or datetime.now(timezone.utc).isoformat()
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("Runtime event event_id must be nonempty")
        if not isinstance(timestamp, str) or not timestamp.strip():
            raise ValueError("Runtime event timestamp must be nonempty")
        try:
            datetime.fromisoformat(timestamp)
        except ValueError as exc:
            raise ValueError("Runtime event timestamp must be ISO-8601") from exc
        for label, value in (("turn_id", turn_id), ("model_request_id", model_request_id),
                             ("call_id", call_id), ("task_id", task_id)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"Runtime event {label} must be nonempty when supplied")
        _validate_json(payload)
        try:
            payload_json = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("Runtime event payload must be finite JSON") from exc
        if len(payload_json.encode("utf-8")) > 128_000:
            raise ValueError("Runtime event payload exceeds 128 KB")
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "kind", kind.strip())
        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "user_id", user_id)
        object.__setattr__(self, "workspace_id", workspace_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "model_request_id", model_request_id)
        object.__setattr__(self, "call_id", call_id)
        object.__setattr__(self, "task_id", task_id)
        object.__setattr__(self, "_payload_json", payload_json)

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self._payload_json)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "event_id": self.event_id,
            "kind": self.kind, "timestamp": self.timestamp, "sequence": self.sequence,
            "user_id": self.user_id, "workspace_id": self.workspace_id,
            "session_id": self.session_id, "turn_id": self.turn_id,
            "model_request_id": self.model_request_id, "call_id": self.call_id,
            "task_id": self.task_id, "payload": self.payload,
        }


class RuntimeEventPublisher:
    """Assign per-turn order and isolate event observers from execution."""

    def __init__(self, *, user_id: str, workspace_id: str, session_id: str,
                 on_runtime_event: Callable[[RuntimeEvent], None] | None = None,
                 on_legacy_event: Callable[[dict[str, Any]], None] | None = None,
                 turn_id: str | None = None):
        self.user_id, self.workspace_id, self.session_id = user_id, workspace_id, session_id
        self.turn_id = turn_id or f"turn_{uuid.uuid4().hex}"
        self._runtime_observer = on_runtime_event
        self._legacy_observer = on_legacy_event
        self._sequence = 0
        self._lock = threading.RLock()
        self.last_model_failed = False

    @property
    def observes_events(self) -> bool:
        return callable(self._runtime_observer) or callable(self._legacy_observer)

    def fork(self, *, session_id: str) -> "RuntimeEventPublisher":
        return RuntimeEventPublisher(
            user_id=self.user_id, workspace_id=self.workspace_id, session_id=session_id,
            on_runtime_event=self._runtime_observer, on_legacy_event=self._legacy_observer,
        )

    def publish(self, kind: str, payload: dict[str, Any], *, model_request_id: str | None = None,
                call_id: str | None = None, task_id: str | None = None,
                legacy_event: dict[str, Any] | None = None) -> RuntimeEvent:
        with self._lock:
            sequence = self._sequence + 1
            event = RuntimeEvent(
                kind=kind, user_id=self.user_id, workspace_id=self.workspace_id,
                session_id=self.session_id, turn_id=self.turn_id, model_request_id=model_request_id,
                call_id=call_id, task_id=task_id, sequence=sequence, payload=payload,
            )
            self._sequence = sequence
            self._notify(self._runtime_observer, event)
            if kind == "model.failed":
                self.last_model_failed = True
            elif kind == "model.completed":
                self.last_model_failed = False
            if legacy_event is None:
                legacy_event = runtime_event_as_legacy(event)
            if legacy_event is not None:
                self._notify(self._legacy_observer, legacy_event)
        return event

    def publish_legacy(self, legacy_event: dict[str, Any], *, model_request_id: str | None = None) -> RuntimeEvent:
        if not isinstance(legacy_event, dict) or not isinstance(legacy_event.get("event"), str):
            raise ValueError("Legacy runtime event requires an event name")
        name = legacy_event["event"]
        fields = {key: value for key, value in legacy_event.items() if key != "event"}
        kind_map = {
            "content": "model.text_delta", "model_complete": "model.completed",
            "tool_receipt": "tool.result", "tasks": "task.snapshot",
            "interaction": "interaction.updated", "permission_check": "security.permission_check",
            "subagent": "subagent.updated", "fast_decision": "routing.decision",
        }
        payload = fields
        if name == "content":
            payload = {"text": str(fields.get("chunk", "")), "legacy_protocol": True}
        elif name == "model_complete":
            payload = {"legacy_protocol": True}
        elif name == "tool_receipt":
            from .types import _bounded_redacted
            payload = _bounded_redacted(dict(fields.get("tool_receipt") or {}), budget=12_000)
            legacy_event = {"event": "tool_receipt", "tool_receipt": payload}
        elif name == "tasks":
            legacy_event = {"event": "tasks", "tasks": fields.get("tasks", []),
                            "working_plan": fields.get("working_plan")}
        call_id = payload.get("call_id") if isinstance(payload.get("call_id"), str) else None
        task_id = payload.get("task_id") if isinstance(payload.get("task_id"), str) else None
        event = self.publish(kind_map.get(name, f"legacy.{name}"), payload,
                             model_request_id=model_request_id, call_id=call_id, task_id=task_id,
                             legacy_event=json.loads(json.dumps(legacy_event, ensure_ascii=False)))
        return event

    @staticmethod
    def _notify(observer, value) -> None:
        if callable(observer):
            try:
                observer(value)
            except Exception:
                pass
