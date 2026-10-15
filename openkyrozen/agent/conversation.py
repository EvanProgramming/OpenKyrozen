"""Private, scoped conversation journal backed by the existing events table."""
from __future__ import annotations

import copy
import json
import math
import uuid
from dataclasses import dataclass, field
from typing import Any

_MISSING = object()


class ConversationHistoryError(ValueError):
    """A journal write, read, or provider projection cannot be applied safely."""


def _validate_json(value: Any) -> None:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ConversationHistoryError("Conversation object keys must be strings")
        for item in value.values():
            _validate_json(item)
    elif isinstance(value, list):
        for item in value:
            _validate_json(item)
    elif value is None or isinstance(value, (str, bool, int)):
        return
    elif not isinstance(value, float) or not math.isfinite(value):
        raise ConversationHistoryError("Conversation payload must contain finite JSON values")


def _encode(value: Any) -> str:
    _validate_json(value)
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ConversationHistoryError("Conversation payload must be JSON serializable") from exc


@dataclass(frozen=True, init=False)
class ConversationEntry:
    id: str
    kind: str
    parent_id: str | None
    version: int
    user_id: str
    workspace_id: str
    session_id: str
    turn_id: str | None
    request_id: str | None
    call_id: str | None
    task_id: str | None
    created_at: str
    _payload: str = field(repr=False)

    def __init__(self, *, id: str, kind: str, payload: Any, user_id: str, workspace_id: str,
                 session_id: str, parent_id: str | None = None, version: int = 1,
                 turn_id: str | None = None, request_id: str | None = None,
                 call_id: str | None = None, task_id: str | None = None,
                 created_at: str | None = None):
        if not all(isinstance(value, str) and value.strip() for value in (id, kind, user_id, workspace_id, session_id)):
            raise ConversationHistoryError("Conversation entry identity and scope fields must be nonempty")
        if type(version) is not int or version != 1:
            raise ConversationHistoryError("Unsupported conversation entry version")
        from openkyrozen.persistence.models import utc_now
        values = {"id": id, "kind": kind, "parent_id": parent_id, "version": version,
                  "user_id": user_id, "workspace_id": workspace_id, "session_id": session_id,
                  "turn_id": turn_id, "request_id": request_id, "call_id": call_id,
                  "task_id": task_id, "created_at": created_at or utc_now(),
                  "_payload": _encode(payload)}
        for key, value in values.items():
            object.__setattr__(self, key, value)

    @property
    def payload(self) -> Any:
        return json.loads(self._payload)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "parent_id": self.parent_id,
                "version": self.version, "user_id": self.user_id,
                "workspace_id": self.workspace_id, "session_id": self.session_id,
                "turn_id": self.turn_id, "request_id": self.request_id,
                "call_id": self.call_id, "task_id": self.task_id,
                "created_at": self.created_at, "payload": self.payload}


class ConversationHistory:
    def __init__(self, store, *, user_id: str = "local", workspace_id: str = "default", session_id: str):
        self.store = store
        self.user_id, self.workspace_id, self.session_id = user_id, workspace_id, session_id

    @property
    def head(self) -> str | None:
        return self.store.conversation_head(user_id=self.user_id, workspace_id=self.workspace_id,
                                            session_id=self.session_id)

    def entry(self, kind: str, payload: Any, *, entry_id: str | None = None, **correlation) -> ConversationEntry:
        parent_id = correlation.pop("parent_id", self.head)
        return ConversationEntry(id=entry_id or f"conv_{uuid.uuid4().hex}", kind=kind,
                                 payload=copy.deepcopy(payload), user_id=self.user_id,
                                 workspace_id=self.workspace_id, session_id=self.session_id,
                                 parent_id=parent_id, **correlation)

    def append(self, entry: ConversationEntry, *, expected_head: str | None | object = _MISSING) -> str:
        if (entry.user_id, entry.workspace_id, entry.session_id) != (self.user_id, self.workspace_id, self.session_id):
            raise ConversationHistoryError("Conversation entry belongs to a different scope")
        if expected_head is _MISSING:
            return self.store.append_conversation_entry(entry.as_dict())
        return self.store.append_conversation_entry(entry.as_dict(), expected_head=expected_head)

    def entries(self) -> tuple[ConversationEntry, ...]:
        values = self.store.list_conversation_entries(
            user_id=self.user_id, workspace_id=self.workspace_id, session_id=self.session_id,
        )
        return tuple(ConversationEntry(**value) for value in values)

    def active_entries(self) -> tuple[ConversationEntry, ...]:
        entries = self.entries()
        targets = {item.payload.get("target_journal_head") for item in entries
                   if item.kind == "branch.selection" and item.payload.get("target_journal_head")}
        active_at = {}
        active = None
        for item in entries:
            if item.kind == "branch.selection":
                target = item.payload.get("target_journal_head")
                if target is not None and target not in active_at:
                    raise ConversationHistoryError("Branch selection references an unknown journal entry")
                active = active_at.get(target)
            else:
                if item.payload.get("provenance") == "legacy_history_node" and item.payload.get("legacy_index") == 0:
                    active = None
                active = (item, active)
            if item.id in targets:
                active_at[item.id] = active
        result = []
        while active:
            item, active = active
            result.append(item)
        result.reverse()
        return tuple(result)

    def display_messages(self, *, exclude_turn_id: str | None = None) -> list[dict[str, Any]]:
        messages = []
        for entry in self.active_entries():
            if exclude_turn_id is not None and entry.turn_id == exclude_turn_id:
                continue
            payload = entry.payload
            if entry.kind == "user.input":
                messages.append({"role": "user", "content": payload["text"]})
            elif entry.kind == "legacy.assistant":
                messages.append({"role": "assistant", "content": payload["text"]})
            elif entry.kind == "turn.outcome":
                messages.append({"role": "assistant", "content": payload.get("text", "")})
        return messages

    def provider_messages(self, *, provider_id: str, model: str, native: bool = True) -> list[Any]:
        from openkyrozen.providers.models import ToolReply, validate_tool_history
        messages = []
        pending_calls = []
        for entry in self.active_entries():
            payload = entry.payload
            if entry.kind == "user.input":
                messages.append({"role": "user", "content": payload["text"]})
            elif entry.kind == "model.response":
                response = model_response_from_dict(payload["response"])
                if response.continuation is None and not response.tool_calls:
                    messages.append({"role": "assistant", "content": response.text})
                else:
                    messages.append(response)
                    pending_calls.extend(response.tool_calls)
            elif entry.kind == "tool.result":
                call_id = payload.get("call_id")
                if native and pending_calls:
                    messages.append(ToolReply(
                        call_id, payload["name"], payload["output"], payload.get("is_error", False),
                    ))
                    if pending_calls[0].id == call_id:
                        pending_calls.pop(0)
                else:
                    output = payload.get("output", {})
                    messages.append({"role": "user", "content": (
                        f"Tool result ({payload.get('name', 'tool')}): {output.get('text', '')}"
                    )})
        validate_tool_history(messages, provider_id=provider_id, model=model, native=native)
        return messages

    def import_legacy(self, messages: list[dict[str, Any]]) -> None:
        """Import old display-only snapshots once; never infer controls from their text."""
        if self.head is not None:
            return
        for index, message in enumerate(messages):
            if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
                continue
            content = message.get("content", "")
            if not isinstance(content, str):
                continue
            kind = "user.input" if message["role"] == "user" else "legacy.assistant"
            entry = self.entry(kind, {"text": content, "provenance": "legacy_snapshot", "index": index})
            self.append(entry, expected_head=entry.parent_id)

    def reconcile_legacy(self, messages: list[dict[str, Any]]) -> None:
        """Import an explicitly supplied replacement list as a new branch."""
        normalized = [item for item in messages if isinstance(item, dict)
                      and item.get("role") in {"user", "assistant"}
                      and isinstance(item.get("content", ""), str)]
        if normalized == self.display_messages():
            return
        parent = self.head
        branch = self.entry("branch.selection", {
            "target_journal_head": None, "reason": "legacy_message_list_replaced",
        }, parent_id=parent)
        self.append(branch, expected_head=parent)
        for index, message in enumerate(normalized):
            kind = "user.input" if message["role"] == "user" else "legacy.assistant"
            entry = self.entry(kind, {
                "text": message.get("content", ""), "provenance": "legacy_replacement", "index": index,
            })
            self.append(entry, expected_head=entry.parent_id)


def model_response_to_dict(response) -> dict[str, Any]:
    from openkyrozen.providers.models import ModelResponse
    if not isinstance(response, ModelResponse):
        raise TypeError("Expected ModelResponse")
    continuation = response.continuation
    return {
        "text": response.text,
        "tool_calls": [{"id": call.id, "name": call.name, "arguments": call.arguments}
                       for call in response.tool_calls],
        "usage": copy.deepcopy(response.usage),
        "finish_reason": response.finish_reason.value,
        "metadata": copy.deepcopy(response.metadata),
        "continuation": ({"provider_id": continuation.provider_id,
                          "request_model": continuation.request_model,
                          "protocol": continuation.protocol,
                          "payload": continuation.payload} if continuation else None),
    }


def model_response_from_dict(value):
    from openkyrozen.providers.models import (
        FinishReason, ModelResponse, ProviderContinuation, ToolCall,
    )
    continuation = value.get("continuation")
    return ModelResponse(
        text=value["text"],
        tool_calls=tuple(ToolCall(call["id"], call["name"], call["arguments"])
                         for call in value.get("tool_calls", ())),
        usage=value.get("usage"), finish_reason=FinishReason(value["finish_reason"]),
        metadata=value.get("metadata", {}),
        continuation=(ProviderContinuation(**continuation) if continuation else None),
    )


def tool_result_to_dict(result) -> dict[str, Any]:
    from openkyrozen.tools.models import ToolResult
    if not isinstance(result, ToolResult):
        raise TypeError("Expected ToolResult")
    error = result.error
    return {"data": result.data, "text": result.text,
            "error": ({"code": error.code, "message": error.message,
                       "retriable": error.retriable, "metadata": error.metadata} if error else None),
            "metadata": result.metadata}


def tool_result_from_dict(value):
    from openkyrozen.tools.models import ToolError, ToolResult
    error = value.get("error")
    return ToolResult(data=value.get("data"), text=value.get("text", ""),
                      error=ToolError(**error) if error else None,
                      metadata=value.get("metadata", {}))
