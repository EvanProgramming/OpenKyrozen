from __future__ import annotations
import openkyrozen.providers.usage as usage_ledger

import json
import sys
import time
from typing import Any, Iterator
from openkyrozen.providers.base import LLMProvider
from openkyrozen.providers.models import (received_response, ModelResponse, model_response, ToolReply,
    ProviderContractError, validate_tool_definitions, validate_tool_history, provider_identity,
    validate_replied_tool_definitions, with_provider_continuation, _snapshot_from_sdk)
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.providers.retry import _retry_with_backoff, provider_call, provider_stream, remaining_timeout, close_stream, mark_response_received


class AnthropicProvider(LLMProvider):
    """Handles Anthropic Claude models via the Messages API."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        try:
            import anthropic
        except ImportError:
            sys.exit(
                "The 'anthropic' package is required for Claude. "
                "Install it with: pip install anthropic"
            )
        kwargs: dict[str, Any] = {"api_key": config.api_key}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self._client = anthropic.Anthropic(max_retries=0, timeout=90.0, **kwargs)

    def _prepare_messages(self, messages, model):
        system_prompts: list[str] = []
        claude_messages: list[dict] = []
        for msg in messages:
            if isinstance(msg, dict):
                role = msg["role"]
                content = msg["content"]
                if role == "system":
                    system_prompts.append(content)
                else:
                    claude_messages.append({"role": role, "content": content})
            elif isinstance(msg, ModelResponse):
                if msg.continuation.protocol != "anthropic":
                    raise ProviderContractError("Continuation protocol does not match Anthropic Messages")
                claude_messages.append({"role": "assistant", "content": msg.continuation.payload["content"]})
            else:
                result = {"type": "tool_result", "tool_use_id": msg.call_id,
                          "content": json.dumps({"is_error": msg.is_error, "result": msg.output}, ensure_ascii=False)}
                if msg.is_error:
                    result["is_error"] = True
                if claude_messages and claude_messages[-1]["role"] == "user" and isinstance(claude_messages[-1]["content"], list) and all(
                        block.get("type") == "tool_result" for block in claude_messages[-1]["content"]):
                    claude_messages[-1]["content"].append(result)
                else:
                    claude_messages.append({"role": "user", "content": [result]})
        return system_prompts, claude_messages

    def get_capabilities(self, model: str | None = None):
        from openkyrozen.providers.models import ProviderCapabilities
        return ProviderCapabilities(native_tools=self.config.provider == "anthropic", text_streaming=True)

    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        return self.chat_response(messages, model).as_legacy_tuple()

    @provider_call
    def chat_response(self, messages: list[dict[str, str]] | list, model: str | None = None, *, tools=None) -> ModelResponse:
        model = model or self.config.model_simple
        tools = validate_tool_definitions(tools)
        validate_tool_history(messages, provider_id=provider_identity(self), model=model, native=tools is not None)
        validate_replied_tool_definitions(messages, tools)
        system_prompts, claude_messages = self._prepare_messages(messages, model)
        started = time.monotonic()

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": 4096,
            "messages": claude_messages,
        }
        if system_prompts:
            kwargs["system"] = "\n\n".join(system_prompts)
        if tools:
            kwargs["tools"] = [{"name": item["name"], "description": item["description"],
                                "input_schema": item["parameters"]} for item in tools]

        def _call():
            return self._client.messages.create(**kwargs, timeout=remaining_timeout())

        response = _retry_with_backoff(_call)
        with received_response():
            usage = getattr(response, "usage", None)
            usage_dict = None
            if usage is not None:
                usage_dict = {
                    "prompt_tokens": getattr(usage, "input_tokens", 0) or 0,
                    "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
                }
            usage_ledger._track_cost(self.config.provider, usage_dict, model=getattr(response, "model", None) or model,
                        latency_ms=round((time.monotonic() - started) * 1000))
            text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text += block.text
            calls = [(getattr(block, "id", None), getattr(block, "name", None), getattr(block, "input", None))
                     for block in response.content if getattr(block, "type", None) == "tool_use"]
            result = model_response(provider=self.name, model=model, actual_model=getattr(response, "model", None), text=text, usage=usage_dict,
                                    calls=calls, response_id=getattr(response, "id", None),
                                    raw_finish_reason=getattr(response, "stop_reason", None))
            return with_provider_continuation(result, self, model, "anthropic",
                                              {"content": _snapshot_from_sdk(response.content)})

    @provider_stream
    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        model = model or self.config.model_simple
        system_prompts, claude_messages = self._prepare_messages(messages, model)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": 4096,
            "messages": claude_messages,
        }
        if system_prompts:
            kwargs["system"] = "\n\n".join(system_prompts)
        started = time.monotonic()
        collected: list[str] = []
        final_usage: dict[str, int | None] | None = None
        completed = False

        def _call():
            return self._client.messages.stream(**kwargs, timeout=remaining_timeout())

        stream_context = _retry_with_backoff(_call)
        try:
            with stream_context as stream:
                mark_response_received()
                for delta in stream.text_stream:
                    collected.append(delta)
                    yield delta
                final = stream.get_final_message()
                usage = getattr(final, "usage", None)
                if usage is not None:
                    final_usage = {
                        "prompt_tokens": getattr(usage, "input_tokens", 0) or 0,
                        "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
                    }
            completed = True
        finally:
            if completed:
                usage_ledger._track_cost(self.config.provider, final_usage, model=model,
                            latency_ms=round((time.monotonic() - started) * 1000))
