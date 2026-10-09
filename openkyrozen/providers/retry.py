"""One deadline and attempt budget for adapter calls and fallback chains."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from functools import wraps
import math
import queue
import random
import threading
import time

from openkyrozen.providers.errors import ProviderError, ProviderErrorKind, normalize_provider_error


@dataclass
class ProviderRequest:
    deadline: float
    cancelled: threading.Event = field(default_factory=threading.Event)
    external_cancelled: threading.Event | None = None
    attempts: int = 0
    received_response: bool = False
    provider: str | None = None
    model: str | None = None

    def check(self):
        if self.cancelled.is_set() or (self.external_cancelled is not None and self.external_cancelled.is_set()):
            raise ProviderError(ProviderErrorKind.CANCELLED, provider=self.provider, model=self.model,
                                attempts=self.attempts, terminal=True)
        if time.monotonic() >= self.deadline:
            raise ProviderError(ProviderErrorKind.TIMEOUT, provider=self.provider, model=self.model,
                                attempts=self.attempts, terminal=True)

    def wait(self, seconds):
        end = min(self.deadline, time.monotonic() + seconds)
        while time.monotonic() < end:
            self.check()
            self.cancelled.wait(min(0.05, end - time.monotonic()))
        self.check()


_request: ContextVar[ProviderRequest | None] = ContextVar("provider_request", default=None)
_retry_limit: ContextVar[int] = ContextVar("provider_retry_limit", default=3)
_in_worker: ContextVar[bool] = ContextVar("provider_in_worker", default=False)
_in_attempt: ContextVar[bool] = ContextVar("provider_in_attempt", default=False)


@contextmanager
def provider_request_scope(timeout=90.0, cancelled=None):
    existing = _request.get()
    if existing is not None:
        existing.check()
        yield existing
        return
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Provider timeout must be finite and positive")
    state = ProviderRequest(time.monotonic() + timeout, external_cancelled=cancelled)
    token = _request.set(state)
    try:
        yield state
    finally:
        state.cancelled.set()
        _request.reset(token)


@contextmanager
def single_provider_attempt():
    token = _retry_limit.set(0)
    try:
        yield
    finally:
        _retry_limit.reset(token)


def manages_provider_retry(fn):
    fn._provider_retry_managed = True
    return fn


def mark_response_received():
    state = _request.get()
    if state is not None:
        state.received_response = True


def terminal_after_response(error, state):
    if state.received_response:
        error.retryable = False
        error.terminal = True
    return error


def budget_exhausted_error(state):
    return ProviderError(ProviderErrorKind.UNKNOWN, provider=state.provider, model=state.model,
                         provider_code="attempt_limit_exceeded", attempts=state.attempts, terminal=True)


def wait_for_retry(state, seconds, error):
    state.check()
    if seconds >= state.deadline - time.monotonic():
        error.retryable = False
        error.terminal = True
        raise error
    state.wait(seconds)


def _start_worker(callback, state):
    context = copy_context()
    def run():
        request_token = _request.set(state)
        worker_token = _in_worker.set(True)
        try:
            callback()
        finally:
            _in_worker.reset(worker_token)
            _request.reset(request_token)
    state.check()
    threading.Thread(target=lambda: context.run(run), daemon=True).start()


def _next_event(events, state):
    while True:
        state.check()
        try:
            event = events.get(timeout=max(0.001, min(0.05, state.deadline - time.monotonic())))
        except queue.Empty:
            continue
        state.check()
        return event


def bounded_call(callback, state):
    """Reuse the active worker, or bound a standalone caller's entire lifecycle."""
    if _in_worker.get():
        state.check()
        return callback()
    outcome = queue.Queue(maxsize=1)
    def run():
        try:
            value = callback()
            state.check()
            outcome.put_nowait((True, value))
        except BaseException as exc:
            if not state.cancelled.is_set():
                outcome.put_nowait((False, exc))
    _start_worker(run, state)
    succeeded, value = _next_event(outcome, state)
    if not succeeded:
        raise value
    return value


def bounded_stream(factory, state):
    """Keep stream contexts in their worker, never across a caller's yield."""
    events = queue.Queue(maxsize=64)
    def publish(event):
        while True:
            state.check()
            try:
                events.put(event, timeout=max(0.001, min(0.05, state.deadline - time.monotonic())))
                return
            except queue.Full:
                continue
    def consume():
        iterator = None
        try:
            iterator = iter(factory())
            for chunk in iterator:
                publish(("chunk", chunk))
            publish(("done", None))
        except BaseException as exc:
            try:
                publish(("error", exc))
            except Exception:
                pass
        finally:
            close_stream(iterator)
    _start_worker(consume, state)
    finished = False
    try:
        while True:
            kind, value = _next_event(events, state)
            if kind != "chunk":
                finished = True
                if kind == "error":
                    raise value
                return
            yield value
    finally:
        if not finished:
            state.cancelled.set()


def remaining_timeout():
    state = _request.get()
    if state is None:
        return 90.0
    state.check()
    return max(0.001, state.deadline - time.monotonic())


def _retry_with_backoff(fn, max_retries=3, base_delay=1.0, *, provider=None, model=None):
    with provider_request_scope() as state:
        state.provider, state.model = provider or state.provider, model or state.model
        if not _in_worker.get():
            return bounded_call(lambda: _retry_with_backoff(
                fn, max_retries, base_delay, provider=provider, model=model), state)
        if _in_attempt.get():
            state.check()
            return fn()
        max_retries = min(max_retries, _retry_limit.get())
        for attempt in range(max_retries + 1):
            state.check()
            if state.attempts >= 4:
                raise budget_exhausted_error(state)
            state.attempts += 1
            token = _in_attempt.set(True)
            try:
                result = fn()
                state.check()
                return result
            except Exception as exc:
                error = terminal_after_response(normalize_provider_error(exc, state.provider, state.model), state)
                error.attempts = state.attempts
                state.check()
                if not error.retryable or attempt >= max_retries or state.attempts >= 4:
                    raise error
            finally:
                _in_attempt.reset(token)
            wait_for_retry(state, backoff_delay(attempt, error.retry_after or 0.0, base_delay), error)


def backoff_delay(attempt, retry_after=0.0, base_delay=1.0):
    return max(base_delay * 2 ** attempt + random.uniform(0, base_delay), retry_after)


def provider_call(fn):
    @wraps(fn)
    def call(self, messages, model=None):
        if getattr(fn, "_provider_retry_managed", False) is True:
            with provider_request_scope() as state:
                state.provider, state.model = self.name, model or self.config.model_simple
                return bounded_call(lambda: fn(self, messages, model), state)
        return _retry_with_backoff(lambda: fn(self, messages, model), provider=self.name,
                                   model=model or self.config.model_simple)
    return manages_provider_retry(call)


def provider_stream(fn):
    @wraps(fn)
    def stream(self, messages, model=None):
        if not _in_worker.get():
            existing = _request.get()
            state = existing or ProviderRequest(time.monotonic() + 90.0)
            try:
                yield from bounded_stream(lambda: stream(self, messages, model), state)
            finally:
                if existing is None:
                    state.cancelled.set()
            return
        with provider_request_scope() as state:
            if getattr(fn, "_provider_retry_managed", False) is True:
                yield from fn(self, messages, model)
                return
            iterator = None
            def start():
                nonlocal iterator
                if iterator is not None:
                    close_stream(iterator)
                iterator = iter(fn(self, messages, model))
                return next(iterator, None)
            try:
                first = _retry_with_backoff(start, provider=self.name, model=model or self.config.model_simple)
                if first is not None:
                    yield first
                for chunk in iterator:
                    state.check()
                    yield chunk
                state.check()
            except Exception as exc:
                error = terminal_after_response(normalize_provider_error(exc, self.name, model or self.config.model_simple), state)
                error.attempts = state.attempts
                raise error
            finally:
                close_stream(iterator)
    return manages_provider_retry(stream)


def close_stream(stream):
    close = getattr(stream, "close", None)
    if close:
        try:
            close()
        except Exception:
            pass
