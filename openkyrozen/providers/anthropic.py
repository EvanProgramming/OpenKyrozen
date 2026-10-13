from __future__ import annotations
import openkyrozen.providers.usage as usage_ledger

import json
import sys
import time
from typing import Any, Iterator
from openkyrozen.providers.base import LLMProvider, text_stream_projection
from openkyrozen.providers.models import (received_response, ModelResponse, model_response, ToolReply,
    ProviderContractError, validate_tool_definitions, validate_tool_history, provider_identity,
    validate_replied_tool_definitions, with_provider_continuation, _snapshot_from_sdk,
    TextDelta, ToolCallDelta, UsageUpdate, ResponseComplete)
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.providers.retry import (_retry_with_backoff, provider_call, provider_stream,
    provider_event_stream, remaining_timeout, close_stream, mark_response_received)


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
        supported = self.config.provider == "anthropic"
        return ProviderCapabilities(native_tools=supported, text_streaming=True, streaming_tool_calls=supported)

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

    @provider_event_stream
    def chat_response_stream(self, messages: list[dict] | list, model: str | None = None, *, tools=None):
        model = model or self.config.model_simple
        tools = validate_tool_definitions(tools)
        validate_tool_history(messages, provider_id=provider_identity(self), model=model, native=tools is not None)
        validate_replied_tool_definitions(messages, tools)
        system_prompts, claude_messages = self._prepare_messages(messages, model)
        kwargs: dict[str, Any] = {"model": model, "max_tokens": 4096, "messages": claude_messages}
        if system_prompts:
            kwargs["system"] = "\n\n".join(system_prompts)
        if tools:
            kwargs["tools"] = [{"name": item["name"], "description": item["description"],
                                "input_schema": item["parameters"]} for item in tools]
        started = time.monotonic()
        content_by_index: dict[int, dict[str, Any]] = {}
        final = None
        stopped = False
        prompt_tokens = 0
        completion_tokens = 0
        stream_context = self._client.messages.stream(**kwargs, timeout=remaining_timeout())
        try:
            with stream_context as stream:
                for event in stream:
                    mark_response_received()
                    event_type = getattr(event, "type", "")
                    if event_type == "message_start":
                        usage = getattr(getattr(event, "message", None), "usage", None)
                        if usage is not None:
                            prompt_tokens = getattr(usage, "input_tokens", prompt_tokens) or prompt_tokens
                            completion_tokens = getattr(usage, "output_tokens", completion_tokens) or completion_tokens
                            yield UsageUpdate({"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens})
                    elif event_type == "content_block_start":
                        block = getattr(event, "content_block", None)
                        index = getattr(event, "index", None)
                        if getattr(block, "type", None) == "tool_use":
                            if type(index) is not int or index < 0:
                                raise ProviderContractError("Anthropic stream tool block has invalid index")
                            content_by_index[index] = {"id": getattr(block, "id", None),
                                "name": getattr(block, "name", None), "arguments": ""}
                            yield ToolCallDelta(index, str(content_by_index[index]["id"] or ""),
                                str(content_by_index[index]["name"] or ""))
                    elif event_type == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        index = getattr(event, "index", None)
                        if getattr(delta, "type", None) == "text_delta":
                            text = str(getattr(delta, "text", "") or "")
                            if text:
                                yield TextDelta(text)
                        elif getattr(delta, "type", None) == "input_json_delta":
                            part = content_by_index.get(index)
                            if part is None:
                                raise ProviderContractError("Anthropic tool JSON arrived before its tool block")
                            fragment = str(getattr(delta, "partial_json", "") or "")
                            part["arguments"] += fragment
                            yield ToolCallDelta(index, arguments_delta=fragment)
                    elif event_type == "message_delta":
                        usage = getattr(event, "usage", None)
                        if usage is not None:
                            input_tokens = getattr(usage, "input_tokens", None)
                            output_tokens = getattr(usage, "output_tokens", None)
                            if input_tokens is not None:
                                prompt_tokens = input_tokens
                            if output_tokens is not None:
                                completion_tokens = output_tokens
                            yield UsageUpdate({"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens})
                    elif event_type == "message_stop":
                        stopped = True
                final = stream.get_final_message()
            if not stopped or final is None:
                raise ProviderContractError("Anthropic stream ended without message_stop")
            usage_obj = getattr(final, "usage", None)
            usage = None if usage_obj is None else {
                "prompt_tokens": getattr(usage_obj, "input_tokens", 0) or 0,
                "completion_tokens": getattr(usage_obj, "output_tokens", 0) or 0}
            usage_ledger._track_cost(self.config.provider, usage, model=getattr(final, "model", None) or model,
                latency_ms=round((time.monotonic() - started) * 1000))
            if usage is not None:
                yield UsageUpdate(usage)
            text = "".join(str(block.text) for block in final.content if getattr(block, "type", None) == "text")
            calls = [(getattr(block, "id", None), getattr(block, "name", None), getattr(block, "input", None))
                     for block in final.content if getattr(block, "type", None) == "tool_use"]
            response = model_response(provider=self.name, model=model, actual_model=getattr(final, "model", None),
                text=text, usage=usage, calls=calls, response_id=getattr(final, "id", None),
                raw_finish_reason=getattr(final, "stop_reason", None))
            response = with_provider_continuation(response, self, model, "anthropic",
                {"content": _snapshot_from_sdk(final.content)})
            yield ResponseComplete(response)
        finally:
            close_stream(stream_context)

    @provider_stream
    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        yield from text_stream_projection(self.chat_response_stream(messages, model))
