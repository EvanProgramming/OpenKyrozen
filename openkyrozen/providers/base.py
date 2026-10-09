from __future__ import annotations

from typing import Iterator
from abc import ABC, abstractmethod
from inspect import getattr_static
from openkyrozen.providers.models import ModelResponse, ProviderCapabilities, ProviderContractError
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.providers.retry import provider_call, provider_stream, provider_request_scope, _retry_with_backoff
from openkyrozen.providers.errors import normalize_provider_error

class LLMProvider(ABC):
    """Unified interface for all LLM backends."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @abstractmethod
    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        """Send messages to the LLM. Returns (content, usage_dict_or_None)."""
        ...

    @provider_call
    def chat_response(self, messages: list[dict], model: str | None = None) -> ModelResponse:
        """Bridge providers implementing only the existing text contract."""
        text, usage = self.chat(messages, model)
        return ModelResponse(text=text, usage=usage,
                             metadata={"provider": self.name, "model": model or self.config.model_simple, "legacy": True})

    def get_capabilities(self, model: str | None = None) -> ProviderCapabilities:
        """Only advertise features usable through the shipped adapter API."""
        return ProviderCapabilities(text_streaming=type(self).chat_stream is not LLMProvider.chat_stream)

    @provider_stream
    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        """Stream response tokens. Default: fall back to non-streaming chat()."""
        text, _ = self.chat(messages, model)
        yield text

    @property
    def name(self) -> str:
        return self.config.provider


def _get_model_response(provider, messages, model=None) -> ModelResponse:
    """Use an explicitly supplied structured API, or bridge a legacy duck provider."""
    if callable(getattr_static(provider, "chat_response", None)):
        response = provider.chat_response(messages, model)
        if not isinstance(response, ModelResponse):
            raise ProviderContractError("chat_response must return ModelResponse")
        return response
    text, usage = provider.chat(messages, model)
    return ModelResponse(text=text, usage=usage, metadata={"legacy": True})


def get_model_response(provider, messages, model=None) -> ModelResponse:
    with provider_request_scope():
        method = getattr_static(provider, "chat_response", None)
        if getattr(method, "_provider_retry_managed", False) is True:
            try:
                return _get_model_response(provider, messages, model)
            except Exception as exc:
                raise normalize_provider_error(exc, getattr(provider, "name", None), model)
        return _retry_with_backoff(lambda: _get_model_response(provider, messages, model),
                                   provider=getattr(provider, "name", None), model=model)
