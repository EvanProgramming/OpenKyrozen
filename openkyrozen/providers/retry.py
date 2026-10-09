"""One deadline and attempt budget for adapter calls and fallback chains."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import math
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


def remaining_timeout():
    state = _request.get()
    if state is None:
        return 90.0
    state.check()
    return max(0.001, state.deadline - time.monotonic())


def _retry_with_backoff(fn, max_retries=3, base_delay=1.0, *, provider=None, model=None):
    with provider_request_scope() as state:
        state.provider, state.model = provider or state.provider, model or state.model
        if _in_attempt.get():
            state.check()
            return fn()
        max_retries = min(max_retries, _retry_limit.get())
        for attempt in range(max_retries + 1):
            state.check()
            if state.attempts >= 4:
                raise ProviderError(ProviderErrorKind.UNKNOWN, provider=state.provider, model=state.model,
                                    attempts=state.attempts, terminal=True)
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
            state.wait(backoff_delay(attempt, error, base_delay))


def backoff_delay(attempt, error, base_delay=1.0):
    return max(base_delay * 2 ** attempt + random.uniform(0, base_delay), error.retry_after or 0)


def provider_call(fn):
    @wraps(fn)
    def call(self, messages, model=None):
        return _retry_with_backoff(lambda: fn(self, messages, model), provider=self.name,
                                   model=model or self.config.model_simple)
    return manages_provider_retry(call)


def provider_stream(fn):
    @wraps(fn)
    def stream(self, messages, model=None):
        with provider_request_scope() as state:
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
                raise terminal_after_response(normalize_provider_error(exc, self.name, model or self.config.model_simple), state)
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
