"""Provider-independent non-streaming contracts; no SDK imports or execution."""
from __future__ import annotations

import copy
import json
import math
import uuid
from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any


class ProviderContractError(ValueError):
    """A received response cannot satisfy the contract; do not replay generation."""


class FinishReason(StrEnum):
    FINAL = "final"
    TOOL_REQUEST = "tool_request"
    LENGTH = "length"
    ERROR = "provider_error"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


def _validate_json(value: Any) -> None:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ProviderContractError("Tool argument keys must be strings")
        for item in value.values():
            _validate_json(item)
    elif isinstance(value, list):
        for item in value:
            _validate_json(item)
    elif value is None or isinstance(value, (str, bool, int)):
        return
    elif not isinstance(value, float) or not math.isfinite(value):
        raise ProviderContractError("Tool arguments must contain finite JSON values")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProviderContractError(f"Duplicate tool argument key: {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id.strip():
            raise ProviderContractError("Tool call id must be a nonempty string")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ProviderContractError("Tool call name must be a nonempty string")
        if not isinstance(self.arguments, dict):
            raise ProviderContractError("Tool arguments must be a JSON object")
        try:
            _validate_json(self.arguments)
            object.__setattr__(self, "arguments", copy.deepcopy(self.arguments))
        except RecursionError as exc:
            raise ProviderContractError("Tool arguments exceed supported JSON nesting") from exc


@dataclass(frozen=True)
class ProviderCapabilities:
    native_tools: bool = False
    strict_schemas: bool = False
    parallel_calls: bool = False
    text_streaming: bool = False
    streaming_tool_calls: bool = False
    reasoning_controls: bool = False

    def __post_init__(self):
        if any(type(getattr(self, item.name)) is not bool for item in fields(self)):
            raise ProviderContractError("Provider capability flags must be booleans")

    @classmethod
    def intersection(cls, capabilities):
        capabilities = tuple(capabilities)
        return cls(**{item.name: bool(capabilities) and all(getattr(value, item.name) for value in capabilities)
                      for item in fields(cls)})


@dataclass(frozen=True)
class ModelResponse:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: dict[str, Any] | None = None
    finish_reason: FinishReason = FinishReason.UNKNOWN
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.text, str) or not isinstance(self.finish_reason, FinishReason):
            raise ProviderContractError("Invalid model response text or finish reason")
        if not isinstance(self.tool_calls, (tuple, list)):
            raise ProviderContractError("Model response calls must be an ordered collection")
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        if any(not isinstance(call, ToolCall) for call in self.tool_calls):
            raise ProviderContractError("Model response calls must be ToolCall objects")
        if self.finish_reason == FinishReason.TOOL_REQUEST and not self.tool_calls:
            raise ProviderContractError("Tool request has no calls")
        if len({call.id for call in self.tool_calls}) != len(self.tool_calls):
            raise ProviderContractError("Duplicate tool call ids")
        if self.usage is not None and not isinstance(self.usage, dict):
            raise ProviderContractError("Model response usage must be an object or None")
        if not isinstance(self.metadata, dict):
            raise ProviderContractError("Model response metadata must be an object")
        try:
            _validate_json(self.metadata)
        except RecursionError as exc:
            raise ProviderContractError("Metadata exceeds supported JSON nesting") from exc

    def as_legacy_tuple(self) -> tuple[str, dict | None]:
        """Refuse to hide calls or unsuccessful completion in a text-only API."""
        if (self.tool_calls or self.finish_reason not in {FinishReason.FINAL, FinishReason.UNKNOWN}
                or self.metadata.get("raw_finish_reason") in ("incomplete", "in_progress", "queued", "pause_turn")):
            raise ProviderContractError(f"Structured response ({self.finish_reason}) requires chat_response(); text conversion refused")
        return (self.text if self.metadata.get("legacy") else self.text.strip()), self.usage


def normalize_finish(reason, *, has_calls=False, detail=None) -> FinishReason:
    reason = getattr(reason, "value", reason)
    reason = reason.lower() if isinstance(reason, str) else ""
    if reason == "incomplete":
        reason = detail.lower() if isinstance(detail, str) else reason
    if reason in {"stop", "completed", "end_turn", "stop_sequence"}:
        return FinishReason.TOOL_REQUEST if has_calls else FinishReason.FINAL
    if reason in {"tool_calls", "function_call", "tool_use"}:
        return FinishReason.TOOL_REQUEST
    if reason in {"length", "max_tokens", "max_output_tokens", "max_token", "model_context_window_exceeded"}:
        return FinishReason.LENGTH
    if reason in {"failed", "error", "malformed_function_call", "unexpected_tool_call"}:
        return FinishReason.ERROR
    if reason in {"cancelled", "canceled"}:
        return FinishReason.CANCELLED
    if reason in {"content_filter", "content_filtered", "guardrail_intervened", "safety", "recitation",
                  "refusal", "blocked", "blocklist", "prohibited_content", "spii"}:
        return FinishReason.BLOCKED
    return FinishReason.UNKNOWN


def model_response(*, provider, model, text, usage, raw_finish_reason=None,
                   calls=(), response_id=None, finish_detail=None, blocked=False, actual_model=None) -> ModelResponse:
    """Build canonical calls from adapter-extracted (id, name, arguments) triples."""
    scope = response_id if isinstance(response_id, str) and response_id else "local_" + uuid.uuid4().hex
    parsed, synthesized = [], []
    for index, (call_id, name, arguments) in enumerate(calls):
        if call_id is None:
            call_id = f"call_{scope}_{index}"
            synthesized.append(call_id)
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments, object_pairs_hook=_unique_object)
            except (ValueError, RecursionError) as exc:
                raise ProviderContractError("Invalid serialized tool arguments") from exc
        parsed.append(ToolCall(call_id, name, arguments))
    raw = getattr(raw_finish_reason, "value", raw_finish_reason)
    metadata = {"provider": provider, "model": actual_model if isinstance(actual_model, str) and actual_model else model, "response_id": scope,
                "raw_finish_reason": raw if isinstance(raw, str) else None}
    if isinstance(finish_detail, str):
        metadata["finish_detail"] = finish_detail
    if synthesized:
        metadata["synthesized_call_ids"] = synthesized
    return ModelResponse(text, tuple(parsed), usage,
                         FinishReason.BLOCKED if blocked else normalize_finish(raw, has_calls=bool(parsed), detail=finish_detail), metadata)


def responses_output(response):
    """Extract Responses API function calls without using its flattened text."""
    calls = []
    for item in getattr(response, "output", None) or ():
        if getattr(item, "type", None) == "function_call":
            calls.append((getattr(item, "call_id", None), getattr(item, "name", None),
                          getattr(item, "arguments", None)))
    return calls
