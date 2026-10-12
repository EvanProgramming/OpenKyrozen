"""Provider-independent non-streaming contracts; no SDK imports or execution."""
from __future__ import annotations

import copy
import json
import math
import uuid
from dataclasses import dataclass, field, fields
from contextlib import contextmanager
from enum import StrEnum
from typing import Any


class ProviderContractError(ValueError):
    """A received response cannot satisfy the contract; do not replay generation."""


@contextmanager
def received_response():
    """A successful SDK return ends failover eligibility, including parse/billing errors."""
    from openkyrozen.providers.retry import mark_response_received
    mark_response_received()
    try:
        yield
    except ProviderContractError:
        raise
    except Exception as exc:
        raise ProviderContractError("Received provider response could not be processed") from exc


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


def _json_snapshot(value, label):
    try:
        _validate_json(value)
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProviderContractError(f"{label} must be a finite JSON value") from exc


def _snapshot_from_sdk(value):
    """Convert the installed SDK's typed response items into the public JSON contract."""
    if isinstance(value, dict):
        return {str(key): _snapshot_from_sdk(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_snapshot_from_sdk(item) for item in value]
    if value is None or isinstance(value, (str, int, bool, float)):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _snapshot_from_sdk(dump(mode="json", exclude_none=True))
        except TypeError:
            return _snapshot_from_sdk(dump(exclude_none=True))
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return _snapshot_from_sdk({key: item for key, item in attributes.items() if not key.startswith("_")})
    raise ProviderContractError("Provider continuation contains an unsupported SDK value")


@dataclass(frozen=True, init=False)
class ToolReply:
    """One correlated tool result; output is a defensive machine-readable JSON snapshot."""

    call_id: str
    name: str
    is_error: bool
    _output_json: str = field(repr=False)

    def __init__(self, call_id: str, name: str, output: dict[str, Any], is_error: bool = False):
        if not isinstance(call_id, str) or not call_id.strip():
            raise ProviderContractError("Tool reply call_id must be nonempty")
        if not isinstance(name, str) or not name.strip():
            raise ProviderContractError("Tool reply name must be nonempty")
        if type(is_error) is not bool:
            raise ProviderContractError("Tool reply is_error must be a boolean")
        if not isinstance(output, dict):
            raise ProviderContractError("Tool reply output must be a JSON object")
        object.__setattr__(self, "call_id", call_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "is_error", is_error)
        object.__setattr__(self, "_output_json", _json_snapshot(output, "Tool reply output"))

    @property
    def output(self) -> dict[str, Any]:
        return json.loads(self._output_json)


@dataclass(frozen=True, init=False)
class ProviderContinuation:
    """Opaque, provider-bound JSON state required to replay a native response."""

    provider_id: str
    request_model: str
    protocol: str
    _payload_json: str = field(repr=False)

    def __init__(self, provider_id: str, request_model: str, protocol: str, payload: dict[str, Any]):
        for label, value in (("provider_id", provider_id), ("request_model", request_model), ("protocol", protocol)):
            if not isinstance(value, str) or not value.strip():
                raise ProviderContractError(f"Continuation {label} must be nonempty")
        if not isinstance(payload, dict):
            raise ProviderContractError("Continuation payload must be a JSON object")
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "request_model", request_model)
        object.__setattr__(self, "protocol", protocol)
        object.__setattr__(self, "_payload_json", _json_snapshot(payload, "Continuation payload"))

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self._payload_json)


def provider_continuation(provider, request_model: str, protocol: str, payload: dict[str, Any]):
    return ProviderContinuation(provider_identity(provider), request_model, protocol, _snapshot_from_sdk(payload))


def provider_identity(provider) -> str:
    identity = getattr(provider, "_continuation_id", None)
    return identity if isinstance(identity, str) else f"{getattr(provider, 'name', type(provider).__name__)}:{id(provider):x}"


def with_provider_continuation(response, provider, request_model, protocol, payload):
    from dataclasses import replace
    return replace(response, continuation=provider_continuation(provider, request_model, protocol, payload))


def validate_tool_definitions(tools):
    if tools is None:
        return None
    if not isinstance(tools, (list, tuple)):
        raise ProviderContractError("Provider tools must be a list of registry descriptors")
    normalized, names = [], set()
    for item in tools:
        if not isinstance(item, dict) or set(item) != {"name", "description", "parameters"}:
            raise ProviderContractError("Provider tool must contain name, description and parameters")
        name, description, schema = item["name"], item["description"], item["parameters"]
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ProviderContractError("Provider tool names must be nonempty and unique")
        if not isinstance(description, str) or not isinstance(schema, dict):
            raise ProviderContractError("Provider tool description and parameters are invalid")
        _json_snapshot(schema, "Provider tool schema")
        if schema.get("type") != "object":
            raise ProviderContractError("Provider tool parameters must be an object schema")
        names.add(name)
        normalized.append({"name": name, "description": description, "parameters": copy.deepcopy(schema)})
    return normalized


def validate_replied_tool_definitions(messages, tools):
    names = {item["name"] for item in tools or ()}
    for message in messages:
        if isinstance(message, ToolReply) and message.name not in names:
            raise ProviderContractError("A tool reply requires its matching provider tool declaration")


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
    continuation: ProviderContinuation | None = field(default=None, repr=False, compare=False)

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
        if self.continuation is not None and not isinstance(self.continuation, ProviderContinuation):
            raise ProviderContractError("Model response continuation must be ProviderContinuation or None")
        try:
            _validate_json(self.metadata)
        except RecursionError as exc:
            raise ProviderContractError("Metadata exceeds supported JSON nesting") from exc

    def as_legacy_tuple(self) -> tuple[str, dict | None]:
        """Refuse to hide calls or unsuccessful completion in a text-only API."""
        raw = self.metadata.get("raw_finish_reason")
        unfinished = isinstance(raw, str) and raw.lower() in ("incomplete", "in_progress", "queued", "pause_turn", "continuation")
        if (self.tool_calls or self.finish_reason not in {FinishReason.FINAL, FinishReason.UNKNOWN}
                or unfinished):
            raise ProviderContractError(f"Structured response ({self.finish_reason}) requires chat_response(); text conversion refused")
        return (self.text if self.metadata.get("legacy") else self.text.strip()), self.usage


@dataclass(frozen=True)
class TextDelta:
    text: str

    def __post_init__(self):
        if not isinstance(self.text, str):
            raise ProviderContractError("Text delta must be a string")


@dataclass(frozen=True)
class ToolCallDelta:
    index: int
    id_delta: str = ""
    name_delta: str = ""
    arguments_delta: str = ""

    def __post_init__(self):
        if type(self.index) is not int or self.index < 0:
            raise ProviderContractError("Tool-call delta index must be a nonnegative integer")
        if any(not isinstance(value, str) for value in (self.id_delta, self.name_delta, self.arguments_delta)):
            raise ProviderContractError("Tool-call delta fields must be strings")


@dataclass(frozen=True, init=False)
class UsageUpdate:
    _usage_json: str = field(repr=False)

    def __init__(self, usage: dict[str, Any]):
        if not isinstance(usage, dict):
            raise ProviderContractError("Usage update must be an object")
        object.__setattr__(self, "_usage_json", _json_snapshot(usage, "Usage update"))

    @property
    def usage(self) -> dict[str, Any]:
        return json.loads(self._usage_json)


@dataclass(frozen=True)
class ResponseComplete:
    response: ModelResponse

    def __post_init__(self):
        if not isinstance(self.response, ModelResponse):
            raise ProviderContractError("Completion event requires ModelResponse")


@dataclass(frozen=True)
class ResponseError:
    error: Exception

    def __post_init__(self):
        from openkyrozen.providers.errors import ProviderError
        if not isinstance(self.error, (ProviderError, ProviderContractError)):
            raise ProviderContractError("Stream error event requires a typed provider error")


def validate_tool_history(messages, *, provider_id: str, model: str, native: bool = False):
    """Reject malformed native call/reply groups before an adapter sends a request."""
    pending: list[ToolCall] = []
    seen_ids: set[str] = set()
    saw_typed = False
    for message in messages:
        if isinstance(message, ModelResponse):
            saw_typed = True
            if pending:
                raise ProviderContractError("Tool call replies must immediately follow their assistant response")
            continuation = message.continuation
            if continuation is None:
                raise ProviderContractError("Native assistant history is missing provider continuation state")
            if continuation.provider_id != provider_id or continuation.request_model != model:
                raise ProviderContractError("Provider continuation belongs to a different provider or model")
            if message.tool_calls:
                for call in message.tool_calls:
                    if call.id in seen_ids:
                        raise ProviderContractError("Duplicate tool call id in conversation history")
                    seen_ids.add(call.id)
                pending = list(message.tool_calls)
        elif isinstance(message, ToolReply):
            saw_typed = True
            if not pending:
                raise ProviderContractError("Orphaned tool reply in conversation history")
            expected = pending.pop(0)
            if (message.call_id, message.name) != (expected.id, expected.name):
                raise ProviderContractError("Tool reply does not match the pending provider call")
        elif isinstance(message, dict):
            if pending:
                raise ProviderContractError("Tool call replies must form a contiguous group")
            if (native or saw_typed) and message.get("role") == "tool":
                raise ProviderContractError("Native tool results must use correlated ToolReply values")
        else:
            raise ProviderContractError("Conversation entries must be messages, ModelResponse, or ToolReply")
    if pending:
        raise ProviderContractError("Conversation history is missing one or more tool replies")
    return saw_typed


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
    if reason in {"failed", "error", "malformed_function_call", "unexpected_tool_call",
                  "too_many_tool_calls", "language", "no_image"}:
        return FinishReason.ERROR
    if reason in {"cancelled", "canceled"}:
        return FinishReason.CANCELLED
    if reason in {"content_filter", "content_filtered", "guardrail_intervened", "safety", "recitation",
                  "refusal", "blocked", "blocklist", "prohibited_content", "spii", "model_armor",
                  "image_safety", "image_prohibited_content", "image_recitation"}:
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
