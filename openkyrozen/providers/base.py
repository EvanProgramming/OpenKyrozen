from __future__ import annotations

from typing import Iterator
from abc import ABC, abstractmethod
from inspect import getattr_static
import uuid
from openkyrozen.providers.models import ModelResponse, ProviderCapabilities, ProviderContractError, validate_tool_definitions, validate_tool_history
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.providers.retry import provider_call, provider_stream, provider_request_scope, _retry_with_backoff
from openkyrozen.providers.errors import normalize_provider_error

class LLMProvider(ABC):
    """Unified interface for all LLM backends."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self._continuation_id = uuid.uuid4().hex

    @abstractmethod
    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        """Send messages to the LLM. Returns (content, usage_dict_or_None)."""
        ...

    @provider_call
    def chat_response(self, messages: list[dict], model: str | None = None, *, tools=None) -> ModelResponse:
        """Bridge providers implementing only the existing text contract."""
        if tools is not None or any(not isinstance(message, dict) for message in messages):
            raise ProviderContractError("This legacy provider does not support native tool conversations")
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


def _get_model_response(provider, messages, model=None, *, tools=None) -> ModelResponse:
    """Use an explicitly supplied structured API, or bridge a legacy duck provider."""
    if callable(getattr_static(provider, "chat_response", None)):
        method = getattr_static(provider, "chat_response", None)
        if tools is not None and isinstance(provider, LLMProvider):
            response = provider.chat_response(messages, model, tools=tools)
        elif tools is not None:
            raise ProviderContractError("This provider does not implement the native tool request contract")
        else:
            response = provider.chat_response(messages, model)
        if not isinstance(response, ModelResponse):
            raise ProviderContractError("chat_response must return ModelResponse")
        return response
    if tools is not None or any(not isinstance(message, dict) for message in messages):
        raise ProviderContractError("This legacy provider does not support native tool conversations")
    text, usage = provider.chat(messages, model)
    return ModelResponse(text=text, usage=usage, metadata={"legacy": True})


def get_model_response(provider, messages, model=None, *, tools=None) -> ModelResponse:
    with provider_request_scope():
        if tools is not None:
            tools = validate_tool_definitions(tools)
        method = getattr_static(provider, "chat_response", None)
        if getattr(method, "_provider_retry_managed", False) is True:
            try:
                return _get_model_response(provider, messages, model, tools=tools)
            except Exception as exc:
                raise normalize_provider_error(exc, getattr(provider, "name", None), model)
        return _retry_with_backoff(lambda: _get_model_response(provider, messages, model, tools=tools),
                                   provider=getattr(provider, "name", None), model=model)
