from __future__ import annotations

import os
import queue
from contextvars import copy_context
import sys
import threading
import time
from typing import Any
from openkyrozen.providers.usage import usage_scope
from openkyrozen.agent.types import ContextOverflowError, ProviderUnavailableError


def _provider_timeout_seconds(self) -> float:
    default = 180.0 if self.execution_context.child_run_id else 90.0
    try:
        return max(1.0, min(float(os.environ.get("KYROZEN_PROVIDER_TIMEOUT_SECONDS", str(default))), 600.0))
    except ValueError:
        return default


def _bounded_provider_call(self, callback: Any) -> Any:
    """Return a provider result by deadline without leaving the CLI waiting on its SDK."""
    outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            outcome.put((True, callback()))
        except Exception as exc:
            outcome.put((False, exc))

    context = copy_context()
    threading.Thread(target=lambda: context.run(run), daemon=True).start()
    try:
        succeeded, value = outcome.get(timeout=self._provider_timeout_seconds())
    except queue.Empty as exc:
        raise TimeoutError(f"Provider timed out after {self._provider_timeout_seconds():g}s") from exc
    if not succeeded:
        raise value
    return value


def _bounded_provider_stream(self, provider: Any, messages: list[dict[str, str]], model: str,
                             on_chunk: Any = None) -> str:
    """Consume one provider stream under the same wall-clock deadline as calls."""
    timeout = self._provider_timeout_seconds()
    deadline = time.monotonic() + timeout
    events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=64)
    cancelled = threading.Event()

    def publish(event: tuple[str, Any]) -> bool:
        try:
            events.put(event, timeout=min(0.1, max(0.0, deadline - time.monotonic())))
            return time.monotonic() < deadline
        except queue.Full:
            return False

    def consume() -> None:
        stream = None
        try:
            stream = iter(provider.chat_stream(messages, model))
            if cancelled.is_set():
                return
            for chunk in stream:
                if cancelled.is_set():
                    return
                if not publish(("chunk", chunk)):
                    return
            publish(("done", None))
        except Exception as exc:
            if not cancelled.is_set():
                publish(("error", exc))
        finally:
            close = getattr(stream, "close", None)
            if close:
                try:
                    close()
                except Exception:
                    pass

    context = copy_context()
    threading.Thread(target=lambda: context.run(consume), daemon=True).start()
    chunks: list[str] = []
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise queue.Empty
            kind, value = events.get(timeout=remaining)
            if kind == "done":
                return "".join(chunks).strip()
            if kind == "error":
                raise value
            chunks.append(str(value))
            if on_chunk:
                on_chunk(value)
            else:
                sys.stdout.write(str(value))
                sys.stdout.flush()
    except queue.Empty as exc:
        cancelled.set()
        raise TimeoutError(f"Provider timed out after {timeout:g}s") from exc


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
                text, usage_dict = self._bounded_provider_call(
                    lambda: provider.chat(messages, model or self.DEEPSEEK_MODEL)
                )
                if (isinstance(text, str) and text.startswith("[Ollama Error]")
                        and self._is_context_overflow_error(RuntimeError(text))):
                    raise RuntimeError(text)
                if usage_dict:
                    self._last_prompt_tokens = usage_dict.get("prompt_tokens", 0)
                    self._last_completion_tokens = usage_dict.get("completion_tokens", 0)
                    if not self.execution_context.child_run_id:
                        self._total_prompt_tokens += self._last_prompt_tokens
                        self._total_completion_tokens += self._last_completion_tokens
                    provider_reported_usage = not bool(usage_dict.get("_estimated"))
                else:
                    self._last_prompt_tokens = 0
                    self._last_completion_tokens = 0
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
