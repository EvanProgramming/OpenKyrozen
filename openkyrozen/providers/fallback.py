from __future__ import annotations

import os
from typing import Iterator
from openkyrozen.providers.base import LLMProvider
from openkyrozen.providers.registry import PROVIDER_DEFAULT_MODELS, PROVIDER_ENV_VARS, PROVIDER_FALLBACKS
from openkyrozen.providers.config import ProviderConfig, provider_is_configured
from openkyrozen.providers.factory import get_provider

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

    @staticmethod
    def _raise_all_failed(attempts: list[tuple[str, Exception]]) -> None:
        """Raise a useful error without discarding the primary failure."""
        if not attempts:
            raise RuntimeError("All providers failed")
        if len(attempts) == 1:
            raise attempts[0][1]
        details = "; ".join(
            f"{provider}: {type(error).__name__}: {error}"
            for provider, error in attempts
        )
        error = RuntimeError(f"All providers failed ({details})")
        raise error from attempts[0][1]

    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        providers = [self._primary] + self._fallbacks
        attempts: list[tuple[str, Exception]] = []
        for prov in providers:
            try:
                return prov.chat(messages, self._model_for(prov, model))
            except Exception as e:
                attempts.append((prov.name, e))
        self._raise_all_failed(attempts)

    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        providers = [self._primary] + self._fallbacks
        attempts: list[tuple[str, Exception]] = []
        for prov in providers:
            try:
                yield from prov.chat_stream(messages, self._model_for(prov, model))
                return
            except Exception as e:
                attempts.append((prov.name, e))
        self._raise_all_failed(attempts)

    @property
    def name(self) -> str:
        fb_names = [p.name for p in self._fallbacks]
        if fb_names:
            return f"{self._primary.name} (fallback: {', '.join(fb_names)})"
        return self._primary.name
