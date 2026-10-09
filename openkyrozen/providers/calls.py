from __future__ import annotations

import os
import math
import queue
from contextvars import copy_context
import sys
import threading
import time
from typing import Any
from openkyrozen.providers.usage import usage_scope
from openkyrozen.providers.base import get_model_response
from openkyrozen.providers.models import ModelResponse
from openkyrozen.providers.retry import provider_request_scope, close_stream
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
    """Keep foreground waiting bounded even when a legacy transport ignores timeout."""
    with provider_request_scope(self._provider_timeout_seconds(), _child_cancellation(self)) as request:
        outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
        def run():
            try:
                value = callback()
                request.check()
                outcome.put_nowait((True, value))
            except Exception as exc:
                if not request.cancelled.is_set():
                    outcome.put_nowait((False, exc))
        context = copy_context()
        request.check()
        threading.Thread(target=lambda: context.run(run), daemon=True).start()
        while True:
            request.check()
            try:
                succeeded, value = outcome.get(timeout=max(0.001, min(0.05, request.deadline - time.monotonic())))
            except queue.Empty:
                continue
            request.check()
            if not succeeded:
                raise value
            return value


def _bounded_provider_stream(self, provider: Any, messages: list[dict[str, str]], model: str,
                             on_chunk: Any = None) -> str:
    with provider_request_scope(self._provider_timeout_seconds(), _child_cancellation(self)) as request:
        events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=64)
        def publish(event):
            while not request.cancelled.is_set():
                request.check()
                try:
                    events.put(event, timeout=max(0.001, min(0.05, request.deadline - time.monotonic())))
                    return
                except queue.Full:
                    continue
        def consume():
            stream = None
            try:
                stream = iter(provider.chat_stream(messages, model))
                for chunk in stream:
                    request.check()
                    publish(("chunk", chunk))
                publish(("done", None))
            except Exception as exc:
                if not request.cancelled.is_set():
                    try:
                        publish(("error", exc))
                    except Exception:
                        pass
            finally:
                close_stream(stream)
        context = copy_context()
        request.check()
        threading.Thread(target=lambda: context.run(consume), daemon=True).start()
        chunks = []
        while True:
            request.check()
            try:
                kind, value = events.get(timeout=max(0.001, min(0.05, request.deadline - time.monotonic())))
            except queue.Empty:
                continue
            request.check()
            if kind == "done":
                return "".join(chunks).strip()
            if kind == "error":
                from openkyrozen.providers.errors import normalize_provider_error
                raise normalize_provider_error(value, getattr(provider, "name", None), model)
            chunks.append(str(value))
            if on_chunk:
                on_chunk(value)
            else:
                sys.stdout.write(str(value))
                sys.stdout.flush()


def _get_model_response(self, messages: list[dict], model: str | None = None) -> ModelResponse:
    """Receive the full response under existing deadline, usage and context scopes."""
    provider = self.execution_context.provider or self.llm_provider
    if provider is None:
        raise ProviderUnavailableError(self.PROVIDER_UNAVAILABLE_MESSAGE)
    self._last_prompt_tokens = self._last_completion_tokens = 0
    with usage_scope(store=self.memory_bank.store, user_id=self.memory_bank.user_id,
                     workspace_id=self.memory_bank.workspace_id, session_id=self.memory_bank.session_id,
                     run_id=self._active_usage_run_id.get(), surface=self._EXECUTION_SURFACE):
        response = self._bounded_provider_call(
            lambda: get_model_response(provider, messages, model or self.DEEPSEEK_MODEL)
        )
    usage = response.usage or {}
    self._last_prompt_tokens = usage.get("prompt_tokens", 0) or 0
    self._last_completion_tokens = usage.get("completion_tokens", 0) or 0
    if not self.execution_context.child_run_id:
        self._total_prompt_tokens += self._last_prompt_tokens
        self._total_completion_tokens += self._last_completion_tokens
    if response.text.startswith("[Ollama Error]") and self._is_context_overflow_error(RuntimeError(response.text)):
        raise RuntimeError(response.text)
    state = self._active_context_state.get()
    if state is not None and not self._in_context_compaction.get():
        if usage and not usage.get("_estimated"):
            state.note_provider_usage(messages, int(self._last_prompt_tokens))
            self._cache_reported_context_tokens(messages, int(self._last_prompt_tokens))
        else:
            state.update(messages)
        self._store_context_status(state)
    return response


def _get_llm_response(self, messages: list[dict[str, str]], model: str | None = None, stream: bool = False,
                      on_chunk: Any = None, on_stream_end: Any = None) -> str:
    provider = self.execution_context.provider or self.llm_provider
    if provider is None:
        raise ProviderUnavailableError(self.PROVIDER_UNAVAILABLE_MESSAGE)
    provider_reported_usage = False
    self._last_prompt_tokens = 0
    self._last_completion_tokens = 0
    try:
        with usage_scope(
                store=self.memory_bank.store, user_id=self.memory_bank.user_id,
                workspace_id=self.memory_bank.workspace_id, session_id=self.memory_bank.session_id,
                run_id=self._active_usage_run_id.get(), surface=self._EXECUTION_SURFACE):
            if stream and hasattr(provider, 'chat_stream'):
                before = self.memory_bank.store.usage_totals(
                    user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
                    session_id=self.memory_bank.session_id, run_id=self._active_usage_run_id.get(),
                )
                text = self._bounded_provider_stream(
                    provider, messages, model or self.DEEPSEEK_MODEL, on_chunk,
                )
                if text.startswith("[Ollama Error]") and self._is_context_overflow_error(RuntimeError(text)):
                    raise RuntimeError(text)
                after = self.memory_bank.store.usage_totals(
                    user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
                    session_id=self.memory_bank.session_id, run_id=self._active_usage_run_id.get(),
                )
                self._last_prompt_tokens = max(0, int(after["prompt_tokens"]) - int(before["prompt_tokens"]))
                self._last_completion_tokens = max(0, int(after["completion_tokens"]) - int(before["completion_tokens"]))
                if not self.execution_context.child_run_id:
                    self._total_prompt_tokens += self._last_prompt_tokens
                    self._total_completion_tokens += self._last_completion_tokens
                if on_stream_end:
                    on_stream_end()
            else:
                return self._get_model_response(messages, model).as_legacy_tuple()[0]
    except TimeoutError as exc:
        return f"[LLM Error] {exc}"
    except Exception as exc:
        if (self._active_context_state.get() is not None and not self._in_context_compaction.get()
                and self._is_context_overflow_error(exc)):
            raise ContextOverflowError(str(exc)) from exc
        return f"[LLM Error] {exc}"
    state = self._active_context_state.get()
    if state is not None and not self._in_context_compaction.get():
        if provider_reported_usage:
            state.note_provider_usage(messages, int(self._last_prompt_tokens or 0))
            self._cache_reported_context_tokens(messages, int(self._last_prompt_tokens or 0))
        else:
            state.update(messages)
        self._store_context_status(state)
    return text
