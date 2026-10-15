from __future__ import annotations

import os
import math
import sys
import uuid
from typing import Any
from openkyrozen.providers.usage import usage_scope
from openkyrozen.providers.base import get_model_response, get_model_events
from openkyrozen.providers.models import (
    ModelResponse, ToolReply, ResponseComplete, ResponseError, TextDelta, ToolCallDelta, UsageUpdate,
)
from openkyrozen.providers.context import context_accounting_messages
from openkyrozen.providers.retry import provider_request_scope, bounded_call, bounded_stream
from openkyrozen.agent.types import ContextOverflowError, ProviderUnavailableError


def _provider_timeout_seconds(self) -> float:
    default = 180.0 if self.execution_context.child_run_id else 90.0
    try:
        value = float(os.environ.get("KYROZEN_PROVIDER_TIMEOUT_SECONDS", str(default)))
        return max(1.0, min(value, 600.0)) if math.isfinite(value) else default
    except ValueError:
        return default


def _child_cancellation(self):
    context = self.execution_context
    if context.child_run_id and context.coordinator:
        return context.coordinator.cancelled.get(context.child_run_id)
    return None


def _bounded_provider_call(self, callback: Any) -> Any:
    with provider_request_scope(self._provider_timeout_seconds(), _child_cancellation(self)) as request:
        return bounded_call(callback, request)


def _bounded_provider_events(self, provider, messages, model, tools, on_event=None):
    from openkyrozen.providers.retry import bounded_stream
    with provider_request_scope(self._provider_timeout_seconds(), _child_cancellation(self)) as request:
        response = None
        events = bounded_stream(lambda: get_model_events(provider, messages, model, tools=tools), request)
        try:
            for event in events:
                if on_event is not None:
                    on_event(event)
                if isinstance(event, ResponseError):
                    raise event.error
                if isinstance(event, ResponseComplete):
                    response = event.response
            if response is None:
                raise RuntimeError("Provider stream ended without a complete response")
            return response
        except Exception as exc:
            from openkyrozen.providers.errors import normalize_provider_error
            if isinstance(exc, (ProviderUnavailableError, ContextOverflowError)):
                raise
            raise normalize_provider_error(exc, getattr(provider, "name", None), model)


def _bounded_provider_stream(self, provider: Any, messages: list[dict[str, str]], model: str,
                             on_chunk: Any = None) -> str:
    with provider_request_scope(self._provider_timeout_seconds(), _child_cancellation(self)) as request:
        chunks = []
        try:
            for chunk in bounded_stream(lambda: provider.chat_stream(messages, model), request):
                chunks.append(str(chunk))
                if on_chunk:
                    on_chunk(chunk)
                else:
                    sys.stdout.write(str(chunk))
                    sys.stdout.flush()
        except Exception as exc:
            from openkyrozen.providers.errors import normalize_provider_error
            raise normalize_provider_error(exc, getattr(provider, "name", None), model)
        return "".join(chunks).strip()


def _get_model_response(self, messages: list[dict], model: str | None = None, *, tool_names=None,
                        stream: bool = False, on_event=None) -> ModelResponse:
    """Receive the full response under existing deadline, usage and context scopes."""
    provider = self.execution_context.provider or self.llm_provider
    tools = None
    if tool_names is not None:
        if isinstance(tool_names, str) or not isinstance(tool_names, (list, tuple, set, frozenset)):
            raise TypeError("tool_names must be a collection of canonical tool names")
        registry = self.tool_registry
        allowed = self._effective_execution_capabilities()
        names = set()
        for requested in tool_names:
            canonical = registry.resolve_legacy_name(requested)
            if canonical in registry.specs:
                names.add(canonical)
        tools = [item for item in registry.provider_schemas(names=names)
                 if registry.get_spec(item["name"]).capability in allowed]
    self._last_prompt_tokens = self._last_completion_tokens = 0
    self._last_response_is_native.set(False)
    self._last_model_response.set(None)
    self._turn_failed.set(False)
    selected_model = model or self.DEEPSEEK_MODEL
    if self._active_context_state.get() is not None and not self._in_context_compaction.get():
        self._record_context_history_metrics(messages)
    request_id = f"model_{uuid.uuid4().hex}"
    request_token = self._active_model_request_id.set(request_id)
    self._last_model_request_id.set(request_id)
    self._publish_runtime_event("model.requested", {"model": selected_model}, model_request_id=request_id)

    def usage_snapshot(fallback=None):
        try:
            totals = self.memory_bank.store.usage_totals(
                user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
                session_id=self.memory_bank.session_id, run_id=self._active_usage_run_id.get(),
            )
            return {key: int(totals.get(key, 0) or 0) for key in (
                "attempts", "authoritative_attempts", "estimated_attempts", "prompt_tokens",
                "cache_hit_tokens", "cache_miss_tokens", "completion_tokens", "reasoning_tokens", "cost_picos",
            )}
        except Exception:
            usage = fallback if isinstance(fallback, dict) else {}
            return {key: int(usage.get(key, 0) or 0) for key in ("prompt_tokens", "completion_tokens")}

    def observe(event):
        if isinstance(event, TextDelta):
            self._publish_runtime_event("model.text_delta", {"text": event.text}, model_request_id=request_id)
        elif isinstance(event, ToolCallDelta):
            self._publish_runtime_event("model.tool_call_delta", {
                "index": event.index, "id_delta": event.id_delta,
                "name_delta": event.name_delta,
                "arguments_delta": "<redacted>" if event.arguments_delta else "",
                "arguments_length": len(event.arguments_delta),
            }, model_request_id=request_id)
        elif isinstance(event, UsageUpdate):
            self._publish_runtime_event("usage.updated", usage_snapshot(event.usage), model_request_id=request_id)
        elif isinstance(event, ResponseComplete):
            response = event.response
            self._publish_runtime_event("usage.updated", usage_snapshot(response.usage), model_request_id=request_id)
            self._publish_runtime_event("model.completed", {
                "finish_reason": response.finish_reason.value,
                "provider": response.metadata.get("provider"),
                "model": response.metadata.get("model", selected_model),
            }, model_request_id=request_id)
        if callable(on_event):
            try:
                on_event(event)
            except Exception:
                pass

    with usage_scope(store=self.memory_bank.store, user_id=self.memory_bank.user_id,
                     workspace_id=self.memory_bank.workspace_id, session_id=self.memory_bank.session_id,
                     run_id=self._active_usage_run_id.get(), surface=self._EXECUTION_SURFACE):
        try:
            if provider is None:
                raise ProviderUnavailableError(self.PROVIDER_UNAVAILABLE_MESSAGE)
            if stream:
                response = _bounded_provider_events(self, provider, messages, selected_model, tools, observe)
            else:
                response = self._bounded_provider_call(
                    lambda: get_model_response(provider, messages, selected_model, tools=tools)
                )
                self._publish_runtime_event("usage.updated", usage_snapshot(response.usage), model_request_id=request_id)
                self._publish_runtime_event("model.completed", {
                    "finish_reason": response.finish_reason.value,
                    "provider": response.metadata.get("provider"),
                    "model": response.metadata.get("model", selected_model),
                }, model_request_id=request_id)
            self._last_response_is_native.set(bool(tools) and not bool(response.metadata.get("legacy")))
            self._last_model_response.set(response)
            if self._record_foreground_model.get() and self._conversation_turn_id.get():
                from openkyrozen.agent.conversation import model_response_to_dict
                self._record_conversation_entry("model.response", {
                    "response": model_response_to_dict(response),
                }, request_id=request_id)
        except Exception as exc:
            self._turn_failed.set(True)
            self._publish_runtime_event("model.failed", {
                "error_type": type(exc).__name__,
                "error_kind": getattr(getattr(exc, "kind", None), "value", None),
            }, model_request_id=request_id)
            raise
        finally:
            self._active_model_request_id.reset(request_token)
    usage = response.usage or {}
    self._last_prompt_tokens = usage.get("prompt_tokens", 0) or 0
    self._last_completion_tokens = usage.get("completion_tokens", 0) or 0
    if not self.execution_context.child_run_id:
        self._total_prompt_tokens += self._last_prompt_tokens
        self._total_completion_tokens += self._last_completion_tokens
    if response.text.startswith("[Ollama Error]") and self._is_context_overflow_error(RuntimeError(response.text)):
        from openkyrozen.providers.errors import normalize_provider_error
        raise normalize_provider_error(RuntimeError(response.text), getattr(provider, "name", None), model or self.DEEPSEEK_MODEL)
    state = self._active_context_state.get()
    if state is not None and not self._in_context_compaction.get():
        context_messages = context_accounting_messages(messages)
        if usage and not usage.get("_estimated"):
            state.note_provider_usage(context_messages, int(self._last_prompt_tokens))
            self._cache_reported_context_tokens(context_messages, int(self._last_prompt_tokens))
        else:
            state.update(context_messages)
        self._store_context_status(state)
    return response


def _get_llm_response(self, messages: list[dict[str, str]], model: str | None = None, stream: bool = False,
                      on_chunk: Any = None, on_stream_end: Any = None) -> str:
    from openkyrozen.providers.base import LLMProvider
    provider = self.execution_context.provider or self.llm_provider
    if provider is None:
        self._last_response_is_native.set(False)
        self._turn_failed.set(True)
        request_id = f"model_{uuid.uuid4().hex}"
        request_token = self._active_model_request_id.set(request_id)
        self._last_model_request_id.set(request_id)
        try:
            self._publish_runtime_event("model.requested", {"model": model or self.DEEPSEEK_MODEL},
                                        model_request_id=request_id)
            self._publish_runtime_event("model.failed", {"error_type": "ProviderUnavailableError"},
                                        model_request_id=request_id)
        finally:
            self._active_model_request_id.reset(request_token)
        raise ProviderUnavailableError(self.PROVIDER_UNAVAILABLE_MESSAGE)
    publisher = self._runtime_event_publisher.get()
    self._last_prompt_tokens = self._last_completion_tokens = 0
    stream_contract = getattr(type(provider), "chat_response_stream", None)
    if stream and callable(stream_contract) and stream_contract is not LLMProvider.chat_response_stream:
        def forward(event):
            if (isinstance(event, TextDelta) and callable(on_chunk)
                    and (publisher is None or not publisher.observes_events)):
                on_chunk(event.text)
            if (isinstance(event, ResponseComplete) and callable(on_stream_end)
                    and (publisher is None or not publisher.observes_events)):
                on_stream_end()

        try:
            result = self._get_model_response(messages, model, stream=True, on_event=forward).as_legacy_tuple()[0]
            self._turn_failed.set(False)
            return result
        except TimeoutError as exc:
            self._turn_failed.set(True)
            return f"[LLM Error] {exc}"
        except Exception as exc:
            self._turn_failed.set(True)
            if (self._active_context_state.get() is not None and not self._in_context_compaction.get()
                    and self._is_context_overflow_error(exc)):
                raise ContextOverflowError(str(exc)) from exc
            return f"[LLM Error] {exc}"
    if stream and hasattr(provider, "chat_stream"):
        self._last_response_is_native.set(False)
        self._turn_failed.set(False)
        request_id = f"model_{uuid.uuid4().hex}"
        request_token = self._active_model_request_id.set(request_id)
        self._last_model_request_id.set(request_id)
        self._publish_runtime_event("model.requested", {"model": model or self.DEEPSEEK_MODEL},
                                    model_request_id=request_id)
        before = self.memory_bank.store.usage_totals(
            user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
            session_id=self.memory_bank.session_id, run_id=self._active_usage_run_id.get(),
        )
        try:
            with usage_scope(
                    store=self.memory_bank.store, user_id=self.memory_bank.user_id,
                    workspace_id=self.memory_bank.workspace_id, session_id=self.memory_bank.session_id,
                    run_id=self._active_usage_run_id.get(), surface=self._EXECUTION_SURFACE):
                text = self._bounded_provider_stream(provider, messages, model or self.DEEPSEEK_MODEL, on_chunk)
            if text.startswith(("[Ollama Error]", "[LLM Error]")) and self._is_context_overflow_error(RuntimeError(text)):
                raise ContextOverflowError(text)
            after = self.memory_bank.store.usage_totals(
                user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
                session_id=self.memory_bank.session_id, run_id=self._active_usage_run_id.get(),
            )
            self._last_prompt_tokens = max(0, int(after["prompt_tokens"]) - int(before["prompt_tokens"]))
            self._last_completion_tokens = max(0, int(after["completion_tokens"]) - int(before["completion_tokens"]))
            if not self.execution_context.child_run_id:
                self._total_prompt_tokens += self._last_prompt_tokens
                self._total_completion_tokens += self._last_completion_tokens
            snapshot = {key: int(after.get(key, 0) or 0) for key in (
                "attempts", "authoritative_attempts", "estimated_attempts", "prompt_tokens",
                "cache_hit_tokens", "cache_miss_tokens", "completion_tokens", "reasoning_tokens", "cost_picos",
            )}
            self._publish_runtime_event("usage.updated", snapshot, model_request_id=request_id)
            self._publish_runtime_event("model.completed", {
                "finish_reason": "unknown", "provider": getattr(provider, "name", None),
                "model": model or self.DEEPSEEK_MODEL, "legacy_stream": True,
            }, model_request_id=request_id)
            self._turn_failed.set(False)
            if callable(on_stream_end) and (publisher is None or not publisher.observes_events):
                on_stream_end()
        except Exception as exc:
            self._turn_failed.set(True)
            self._publish_runtime_event("model.failed", {
                "error_type": type(exc).__name__,
                "error_kind": getattr(getattr(exc, "kind", None), "value", None),
            }, model_request_id=request_id)
            if (self._active_context_state.get() is not None and not self._in_context_compaction.get()
                    and self._is_context_overflow_error(exc)):
                raise ContextOverflowError(str(exc)) from exc
            return f"[LLM Error] {exc}"
        finally:
            self._active_model_request_id.reset(request_token)
        state = self._active_context_state.get()
        if state is not None and not self._in_context_compaction.get():
            state.update(messages)
            self._store_context_status(state)
        return text
    try:
        result = self._get_model_response(messages, model).as_legacy_tuple()[0]
        self._turn_failed.set(False)
        return result
    except TimeoutError as exc:
        return f"[LLM Error] {exc}"
    except Exception as exc:
        self._turn_failed.set(True)
        if (self._active_context_state.get() is not None and not self._in_context_compaction.get()
                and self._is_context_overflow_error(exc)):
            raise ContextOverflowError(str(exc)) from exc
        return f"[LLM Error] {exc}"
