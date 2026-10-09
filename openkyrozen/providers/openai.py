from __future__ import annotations
import openkyrozen.providers.usage as usage_ledger

import sys
import time
from typing import Any, Iterator
from openkyrozen.providers.base import LLMProvider
from openkyrozen.providers.models import received_response, ModelResponse, model_response, responses_output, ProviderContractError
from openkyrozen.providers.config import ProviderConfig, _provider_env_key
from openkyrozen.providers.usage import _openai_usage_dict
from openkyrozen.providers.retry import _retry_with_backoff, provider_call, provider_stream, remaining_timeout, close_stream, mark_response_received

class OpenAICompatProvider(LLMProvider):
    """Handles any OpenAI-compatible /v1/chat/completions endpoint."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        try:
            from openai import OpenAI
        except ImportError:
            sys.exit(
                "The 'openai' package is required for this provider. "
                "Install it with: pip install openai"
            )
        kwargs: dict[str, Any] = {
            "api_key": config.api_key or ("" if config.provider == "custom" else _provider_env_key(config.provider)) or "sk-placeholder",
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self._client = OpenAI(max_retries=0, timeout=90.0, **kwargs)

    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        return self.chat_response(messages, model).as_legacy_tuple()

    @provider_call
    def chat_response(self, messages: list[dict[str, str]], model: str | None = None) -> ModelResponse:
        model = model or self.config.model_simple
        started = time.monotonic()

        def _call():
            response = self._client.chat.completions.create(model=model, messages=messages, timeout=remaining_timeout())
            return response

        response = _retry_with_backoff(_call)
        with received_response():
            usage = getattr(response, "usage", None)
            usage_dict = None
            if usage is not None:
                usage_dict = _openai_usage_dict(usage)
            usage_ledger._track_cost(self.config.provider, usage_dict, model=getattr(response, "model", None) or model,
                        latency_ms=round((time.monotonic() - started) * 1000))
            message = response.choices[0].message
            text = message.content or ""
            calls = []
            for call in getattr(message, "tool_calls", None) or ():
                function = getattr(call, "function", None)
                if function is None:
                    raise ProviderContractError("Unsupported native tool call type")
                calls.append((getattr(call, "id", None), getattr(function, "name", None),
                              getattr(function, "arguments", None)))
            legacy_call = getattr(message, "function_call", None)
            if legacy_call is not None and not calls:
                calls.append((None, getattr(legacy_call, "name", None), getattr(legacy_call, "arguments", None)))
            return model_response(provider=self.name, model=model, actual_model=getattr(response, "model", None), text=text, usage=usage_dict,
                                  calls=calls, response_id=getattr(response, "id", None),
                                  raw_finish_reason=getattr(response.choices[0], "finish_reason", None),
                                  blocked=bool(getattr(message, "refusal", None)))

    @provider_stream
    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        model = model or self.config.model_simple
        collected: list[str] = []
        billed_model = model
        started = time.monotonic()
        final_usage: dict[str, int | None] | None = None
        completed = False

        def _call():
            kwargs: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
            # Only APIs documented to accept OpenAI's stream_options receive it.
            # Fireworks and several compatible providers include usage in their
            # final chunk without this extension; the rest remain compatible.
            if self.config.provider in {"deepseek", "openai", "ollama", "glm", "kimi"}:
                kwargs["stream_options"] = {"include_usage": True}
            return self._client.chat.completions.create(**kwargs, timeout=remaining_timeout())

        stream = _retry_with_backoff(_call)
        mark_response_received()
        try:
            for chunk in stream:
                billed_model = getattr(chunk, "model", None) or billed_model
                usage = _openai_usage_dict(getattr(chunk, "usage", None))
                if usage is not None:
                    final_usage = usage
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    collected.append(delta.content)
                    yield delta.content
            completed = True
        finally:
            close_stream(stream)
            if completed:
                if final_usage is None:
                    final_usage = {
                        "prompt_tokens": sum(len(str(message.get("content", ""))) for message in messages) // 4,
                        "completion_tokens": len("".join(collected)) // 4,
                        "_estimated": 1,
                    }
                usage_ledger._track_cost(
                    self.config.provider, final_usage, model=billed_model,
                    latency_ms=round((time.monotonic() - started) * 1000),
                )


class OpenAIResponsesProvider(LLMProvider):
    """OpenAI's current Responses API, including event-based streaming."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        try:
            from openai import OpenAI
        except ImportError:
            sys.exit("The 'openai' package is required for OpenAI. Install it with: pip install openai")
        kwargs: dict[str, Any] = {
            "api_key": config.api_key or _provider_env_key(config.provider) or "sk-placeholder",
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self._client = OpenAI(max_retries=0, timeout=90.0, **kwargs)

    @staticmethod
    def _usage(response: Any) -> dict[str, int | None] | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        details = getattr(usage, "output_tokens_details", None)
        return {
            "prompt_tokens": getattr(usage, "input_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
            "reasoning_tokens": getattr(details, "reasoning_tokens", None),
        }

    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        return self.chat_response(messages, model).as_legacy_tuple()

    @provider_call
    def chat_response(self, messages: list[dict[str, str]], model: str | None = None) -> ModelResponse:
        model = model or self.config.model_simple
        started = time.monotonic()

        response = _retry_with_backoff(
            lambda: self._client.responses.create(model=model, input=messages, timeout=remaining_timeout()),
        )
        with received_response():
            usage = self._usage(response)
            usage_ledger._track_cost(self.config.provider, usage, model=getattr(response, "model", None) or model,
                        latency_ms=round((time.monotonic() - started) * 1000))
            return model_response(provider=self.name, model=model, actual_model=getattr(response, "model", None),
                                  text=str(getattr(response, "output_text", "") or ""), usage=usage,
                                  calls=responses_output(response), response_id=getattr(response, "id", None),
                                  raw_finish_reason=getattr(response, "status", None),
                                  finish_detail=getattr(getattr(response, "incomplete_details", None), "reason", None),
                                  blocked=any(getattr(part, "type", None) == "refusal"
                                              for item in getattr(response, "output", None) or ()
                                              for part in getattr(item, "content", None) or ()))

    @provider_stream
    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        model = model or self.config.model_simple
        started = time.monotonic()
        collected: list[str] = []
        final_usage: dict[str, int | None] | None = None
        completed = False

        stream = _retry_with_backoff(
            lambda: self._client.responses.create(model=model, input=messages, stream=True, timeout=remaining_timeout()),
        )
        mark_response_received()
        try:
            for event in stream:
                event_type = str(getattr(event, "type", ""))
                usage = self._usage(getattr(event, "response", None) or event)
                if usage is not None:
                    final_usage = usage
                if event_type in {"response.output_text.delta", "response.text.delta"}:
                    delta = str(getattr(event, "delta", "") or "")
                else:
                    delta = str(getattr(event, "text", "") or "") if event_type.endswith("text.delta") else ""
                if delta:
                    collected.append(delta)
                    yield delta
            completed = True
        finally:
            close_stream(stream)
            if completed:
                if final_usage is None:
                    final_usage = {
                        "prompt_tokens": sum(len(str(item.get("content", ""))) for item in messages) // 4,
                        "completion_tokens": len("".join(collected)) // 4,
                        "_estimated": 1,
                    }
                usage_ledger._track_cost(self.config.provider, final_usage, model=model,
                            latency_ms=round((time.monotonic() - started) * 1000))
