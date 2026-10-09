from __future__ import annotations

import os
from typing import Iterator
from inspect import getattr_static
from openkyrozen.providers.base import LLMProvider, get_model_response
from openkyrozen.providers.models import ModelResponse, ProviderCapabilities
from openkyrozen.providers.registry import PROVIDER_DEFAULT_MODELS, PROVIDER_ENV_VARS, PROVIDER_FALLBACKS
from openkyrozen.providers.config import ProviderConfig, provider_is_configured
from openkyrozen.providers.factory import get_provider
from openkyrozen.providers.errors import ProviderError, ProviderErrorKind, normalize_provider_error
from openkyrozen.providers.retry import provider_request_scope, _retry_with_backoff, close_stream, backoff_delay, single_provider_attempt, manages_provider_retry, terminal_after_response, provider_call, provider_stream, budget_exhausted_error, wait_for_retry

class FallbackProvider(LLMProvider):
    """Wraps multiple providers and falls back on failure."""

    def __init__(self, primary_config: ProviderConfig) -> None:
        self._primary = get_provider(primary_config)
        self._fallbacks: list[LLMProvider] = []
        fallback_names = PROVIDER_FALLBACKS.get(primary_config.provider, [])
        for fb_name in fallback_names:
            fb_config = ProviderConfig(
                provider=fb_name,
                api_key=os.environ.get(PROVIDER_ENV_VARS.get(fb_name, ""), ""),
                model_simple=PROVIDER_DEFAULT_MODELS.get(fb_name, ("", ""))[0],
                model_complex=PROVIDER_DEFAULT_MODELS.get(fb_name, ("", ""))[1],
            )
            # Only add fallback if it has an API key or is Ollama
            if provider_is_configured(fb_config):
                try:
                    self._fallbacks.append(get_provider(fb_config))
                except Exception:
                    pass

    @property
    def config(self) -> ProviderConfig:
        return self._primary.config

    def _model_for(self, provider: LLMProvider, requested_model: str | None) -> str:
        """Choose a model that has the same simple/complex meaning for *provider*.

        The model names in a provider configuration are provider-specific.  A
        model selected from the primary provider must therefore not be sent to
        a fallback merely because it is a non-empty string.  Names configured
        for the primary (including its built-in defaults) are treated as
        semantic simple/complex slots and mapped to the fallback's slots.
        An unrecognised explicit name is forwarded as-is so users can use a
        model name shared by several compatible endpoints, with the fallback
        provider responsible for accepting or rejecting it.
        """
        primary_config = self._primary.config
        target_config = provider.config
        requested = (requested_model or "").strip()

        if not requested or requested.lower() == "auto":
            return target_config.model_simple

        primary_simple = {
            primary_config.model_simple,
            PROVIDER_DEFAULT_MODELS.get(primary_config.provider, ("", ""))[0],
        }
        primary_complex = {
            primary_config.model_complex,
            PROVIDER_DEFAULT_MODELS.get(primary_config.provider, ("", ""))[1],
        }
        if requested in primary_complex and requested not in primary_simple:
            return target_config.model_complex
        if requested in primary_simple:
            return target_config.model_simple
        return requested

    def get_capabilities(self, model: str | None = None) -> ProviderCapabilities:
        return ProviderCapabilities.intersection(
            provider.get_capabilities(self._model_for(provider, model))
            for provider in [self._primary] + self._fallbacks
        )

    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        return self.chat_response(messages, model).as_legacy_tuple()

    @provider_call
    @manages_provider_retry
    def chat_response(self, messages: list[dict[str, str]], model: str | None = None) -> ModelResponse:
        with provider_request_scope() as state:
            return self._fallback_response(state, messages, model)

    def _fallback_response(self, state, messages, model):
        candidates = [self._primary] + self._fallbacks
        last_error = None
        round_number = 0
        retry_after = 0.0
        while candidates and state.attempts < 4:
            if round_number:
                wait_for_retry(state, backoff_delay(round_number - 1, retry_after), last_error)
            transient = []
            retry_after = 0.0
            for provider in candidates:
                if state.attempts >= 4:
                    break
                mapped = self._model_for(provider, model)
                try:
                    with single_provider_attempt():
                        return get_model_response(provider, messages, mapped)
                except ProviderError as error:
                    last_error = error
                    if error.retryable:
                        transient.append(provider)
                        retry_after = max(retry_after, error.retry_after or 0.0)
                    elif error.terminal or error.kind != ProviderErrorKind.AUTHENTICATION:
                        raise
            candidates = transient
            round_number += 1
        raise last_error if last_error is not None else budget_exhausted_error(state)

    @provider_stream
    @manages_provider_retry
    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        with provider_request_scope() as state:
            candidates = [self._primary] + self._fallbacks
            last_error = None
            round_number = 0
            retry_after = 0.0
            while candidates and state.attempts < 4:
                if round_number:
                    wait_for_retry(state, backoff_delay(round_number - 1, retry_after), last_error)
                transient = []
                retry_after = 0.0
                for provider in candidates:
                    if state.attempts >= 4:
                        break
                    iterator = None
                    emitted = False
                    mapped = self._model_for(provider, model)
                    def start():
                        nonlocal iterator
                        iterator = iter(provider.chat_stream(messages, mapped))
                        return next(iterator, None)
                    try:
                        with single_provider_attempt():
                            method = getattr_static(provider, "chat_stream", None)
                            if getattr(method, "_provider_retry_managed", False) is True:
                                first = start()
                            else:
                                first = _retry_with_backoff(start, max_retries=0, provider=provider.name, model=mapped)
                        if first is not None:
                            emitted = True
                            yield first
                        for chunk in iterator:
                            state.check()
                            emitted = True
                            yield chunk
                        state.check()
                        return
                    except Exception as exc:
                        error = terminal_after_response(normalize_provider_error(exc, provider.name, mapped), state)
                        error.attempts = state.attempts
                        if emitted:
                            raise error
                        last_error = error
                        if error.retryable:
                            transient.append(provider)
                            retry_after = max(retry_after, error.retry_after or 0.0)
                        elif error.terminal or error.kind != ProviderErrorKind.AUTHENTICATION:
                            raise error
                    finally:
                        close_stream(iterator)
                candidates = transient
                round_number += 1
            raise last_error if last_error is not None else budget_exhausted_error(state)

    @property
    def name(self) -> str:
        fb_names = [p.name for p in self._fallbacks]
        if fb_names:
            return f"{self._primary.name} (fallback: {', '.join(fb_names)})"
        return self._primary.name
