from __future__ import annotations
import openkyrozen.providers.usage as usage_ledger

import json
import sys
import time
from typing import Any, Iterator
from openkyrozen.providers.base import LLMProvider
from openkyrozen.providers.models import (received_response, ModelResponse, model_response, responses_output,
    ProviderContractError, ModelResponse as ProviderModelResponse, ToolReply, validate_tool_history,
    validate_tool_definitions, validate_replied_tool_definitions, provider_identity,
    with_provider_continuation, _snapshot_from_sdk, TextDelta, ToolCallDelta, UsageUpdate, ResponseComplete)
from openkyrozen.providers.config import ProviderConfig, _provider_env_key
from openkyrozen.providers.usage import _openai_usage_dict
from openkyrozen.providers.retry import (_retry_with_backoff, provider_call, provider_stream,
    provider_event_stream, remaining_timeout, close_stream, mark_response_received)

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

    def get_capabilities(self, model: str | None = None):
        from openkyrozen.providers.models import ProviderCapabilities
        supported = self.config.provider in {
            "openai", "azure_openai", "deepseek", "ollama", "glm", "kimi", "openrouter",
            "groq", "mistral", "xai", "together", "fireworks", "cohere"}
        return ProviderCapabilities(native_tools=supported, text_streaming=True,
                                    streaming_tool_calls=supported)

    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        return self.chat_response(messages, model).as_legacy_tuple()

    @provider_call
    def chat_response(self, messages: list[dict[str, str]] | list, model: str | None = None, *, tools=None) -> ModelResponse:
        model = model or self.config.model_simple
        tools = validate_tool_definitions(tools)
        validate_tool_history(messages, provider_id=provider_identity(self), model=model, native=tools is not None)
        validate_replied_tool_definitions(messages, tools)
        request_messages = []
        for item in messages:
            if isinstance(item, dict):
                request_messages.append(item)
            elif isinstance(item, ProviderModelResponse):
                if item.continuation.protocol != "openai-chat":
                    raise ProviderContractError("Continuation protocol does not match OpenAI Chat Completions")
                request_messages.append(item.continuation.payload["assistant"])
            elif isinstance(item, ToolReply):
                result = {"is_error": item.is_error, "result": item.output}
                request_messages.append({"role": "tool", "tool_call_id": item.call_id,
                                         "content": json.dumps(result, ensure_ascii=False)})
        started = time.monotonic()

        def _call():
            kwargs = {"model": model, "messages": request_messages, "timeout": remaining_timeout()}
            if tools:
                kwargs["tools"] = [{"type": "function", "function": {"name": tool["name"],
                    "description": tool["description"], "parameters": tool["parameters"]}}
                    for tool in tools]
                if tools:
                    kwargs["tool_choice"] = "auto"
            response = self._client.chat.completions.create(**kwargs)
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
            result = model_response(provider=self.name, model=model, actual_model=getattr(response, "model", None), text=text, usage=usage_dict,
                                  calls=calls, response_id=getattr(response, "id", None),
                                  raw_finish_reason=getattr(response.choices[0], "finish_reason", None),
                                  blocked=bool(getattr(message, "refusal", None)))
            assistant = _snapshot_from_sdk(message)
            if "role" not in assistant:
                assistant["role"] = "assistant"
            return with_provider_continuation(result, self, model, "openai-chat", {"assistant": assistant})

    @provider_event_stream
    def chat_response_stream(self, messages: list[dict] | list, model: str | None = None, *, tools=None):
        model = model or self.config.model_simple
        tools = validate_tool_definitions(tools)
        validate_tool_history(messages, provider_id=provider_identity(self), model=model, native=tools is not None)
        validate_replied_tool_definitions(messages, tools)
        request_messages = []
        for item in messages:
            if isinstance(item, dict):
                request_messages.append(item)
            elif isinstance(item, ProviderModelResponse):
                if item.continuation.protocol != "openai-chat":
                    raise ProviderContractError("Continuation protocol does not match OpenAI Chat Completions")
                request_messages.append(item.continuation.payload["assistant"])
            elif isinstance(item, ToolReply):
                request_messages.append({"role": "tool", "tool_call_id": item.call_id,
                    "content": json.dumps({"is_error": item.is_error, "result": item.output}, ensure_ascii=False)})
        kwargs: dict[str, Any] = {"model": model, "messages": request_messages, "stream": True,
                                  "timeout": remaining_timeout()}
        if tools:
            kwargs["tools"] = [{"type": "function", "function": {"name": tool["name"],
                "description": tool["description"], "parameters": tool["parameters"]}} for tool in tools]
        if tools:
            kwargs["tool_choice"] = "auto"
        if self.config.provider in {"deepseek", "openai", "ollama", "glm", "kimi"}:
            kwargs["stream_options"] = {"include_usage": True}
        started = time.monotonic()
        stream = self._client.chat.completions.create(**kwargs)
        text_parts: list[str] = []
        call_parts: dict[int, dict[str, str]] = {}
        usage = None
        finish = None
        response_id = None
        actual_model = model
        blocked = False
        try:
            for chunk in stream:
                mark_response_received()
                response_id = getattr(chunk, "id", None) or response_id
                actual_model = getattr(chunk, "model", None) or actual_model
                update = _openai_usage_dict(getattr(chunk, "usage", None))
                if update is not None:
                    usage = update
                    yield UsageUpdate(usage)
                for choice in getattr(chunk, "choices", None) or ():
                    delta = getattr(choice, "delta", None)
                    if delta is not None and getattr(delta, "content", None):
                        text = str(delta.content)
                        text_parts.append(text)
                        yield TextDelta(text)
                    blocked = blocked or bool(getattr(delta, "refusal", None))
                    for call in getattr(delta, "tool_calls", None) or ():
                        index = getattr(call, "index", None)
                        if type(index) is not int or index < 0:
                            raise ProviderContractError("Streamed tool call has invalid index")
                        part = call_parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        call_id = str(getattr(call, "id", None) or "")
                        function = getattr(call, "function", None)
                        name = str(getattr(function, "name", None) or "")
                        arguments = str(getattr(function, "arguments", None) or "")
                        part["id"] += call_id
                        part["name"] += name
                        part["arguments"] += arguments
                        yield ToolCallDelta(index, call_id, name, arguments)
                    finish = getattr(choice, "finish_reason", None) or finish
            if finish is None:
                raise ProviderContractError("OpenAI-compatible stream ended without a finish reason")
            if sorted(call_parts) != list(range(len(call_parts))):
                raise ProviderContractError("Streamed tool call indexes are not contiguous")
            calls = [(part["id"] or None, part["name"], part["arguments"])
                     for _, part in sorted(call_parts.items())]
            text = "".join(text_parts)
            response = model_response(provider=self.name, model=model, actual_model=actual_model, text=text,
                usage=usage, calls=calls, response_id=response_id, raw_finish_reason=finish, blocked=blocked)
            if usage is None:
                usage = {"prompt_tokens": sum(len(str(message.get("content", ""))) for message in request_messages) // 4,
                         "completion_tokens": len(text) // 4, "_estimated": 1}
                response = ModelResponse(response.text, response.tool_calls, usage, response.finish_reason, response.metadata)
                yield UsageUpdate(usage)
            assistant = {"role": "assistant", "content": text or None}
            if response.tool_calls:
                assistant["tool_calls"] = [{"id": call.id, "type": "function", "function": {
                    "name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)}}
                    for call in response.tool_calls]
            response = with_provider_continuation(response, self, model, "openai-chat", {"assistant": assistant})
            usage_ledger._track_cost(self.config.provider, usage, model=actual_model,
                latency_ms=round((time.monotonic() - started) * 1000))
            yield ResponseComplete(response)
        finally:
            close_stream(stream)

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

    def get_capabilities(self, model: str | None = None):
        from openkyrozen.providers.models import ProviderCapabilities
        supported = self.config.provider in {"openai", "perplexity"}
        return ProviderCapabilities(native_tools=supported, text_streaming=True, streaming_tool_calls=supported)

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
    def chat_response(self, messages: list[dict[str, str]] | list, model: str | None = None, *, tools=None) -> ModelResponse:
        model = model or self.config.model_simple
        tools = validate_tool_definitions(tools)
        validate_tool_history(messages, provider_id=provider_identity(self), model=model, native=tools is not None)
        validate_replied_tool_definitions(messages, tools)
        request_input = []
        for item in messages:
            if isinstance(item, dict):
                request_input.append(item)
            elif isinstance(item, ProviderModelResponse):
                if item.continuation.protocol != "openai-responses":
                    raise ProviderContractError("Continuation protocol does not match OpenAI Responses")
                request_input.extend(item.continuation.payload["output"])
            elif isinstance(item, ToolReply):
                result = {"is_error": item.is_error, "result": item.output}
                request_input.append({"type": "function_call_output", "call_id": item.call_id,
                                     "output": json.dumps(result, ensure_ascii=False)})
        started = time.monotonic()

        kwargs = {"model": model, "input": request_input, "timeout": remaining_timeout()}
        if tools:
            kwargs["tools"] = [{"type": "function", "name": tool["name"], "description": tool["description"],
                                "parameters": tool["parameters"], "strict": False} for tool in tools]
        response = _retry_with_backoff(lambda: self._client.responses.create(**kwargs))
        with received_response():
            usage = self._usage(response)
            usage_ledger._track_cost(self.config.provider, usage, model=getattr(response, "model", None) or model,
                        latency_ms=round((time.monotonic() - started) * 1000))
            result = model_response(provider=self.name, model=model, actual_model=getattr(response, "model", None),
                                  text=str(getattr(response, "output_text", "") or ""), usage=usage,
                                  calls=responses_output(response), response_id=getattr(response, "id", None),
                                  raw_finish_reason=getattr(response, "status", None),
                                  finish_detail=getattr(getattr(response, "incomplete_details", None), "reason", None),
                                  blocked=any(getattr(part, "type", None) == "refusal"
                                              for item in getattr(response, "output", None) or ()
                                              for part in getattr(item, "content", None) or ()))
            return with_provider_continuation(result, self, model, "openai-responses",
                                              {"output": _snapshot_from_sdk(getattr(response, "output", None) or [])})

    @provider_event_stream
    def chat_response_stream(self, messages: list[dict] | list, model: str | None = None, *, tools=None):
        model = model or self.config.model_simple
        tools = validate_tool_definitions(tools)
        validate_tool_history(messages, provider_id=provider_identity(self), model=model, native=tools is not None)
        validate_replied_tool_definitions(messages, tools)
        request_input = []
        for item in messages:
            if isinstance(item, dict):
                request_input.append(item)
            elif isinstance(item, ProviderModelResponse):
                if item.continuation.protocol != "openai-responses":
                    raise ProviderContractError("Continuation protocol does not match OpenAI Responses")
                request_input.extend(item.continuation.payload["output"])
            else:
                request_input.append({"type": "function_call_output", "call_id": item.call_id,
                    "output": json.dumps({"is_error": item.is_error, "result": item.output}, ensure_ascii=False)})
        kwargs: dict[str, Any] = {"model": model, "input": request_input, "stream": True,
                                  "timeout": remaining_timeout()}
        if tools:
            kwargs["tools"] = [{"type": "function", "name": item["name"],
                "description": item["description"], "parameters": item["parameters"], "strict": False}
                for item in tools]
        started = time.monotonic()
        stream = self._client.responses.create(**kwargs)
        text_parts = []
        calls = {}
        usage = None
        final = None
        terminal = False
        try:
            for event in stream:
                event_type = str(getattr(event, "type", ""))
                response = getattr(event, "response", None) or event
                update = self._usage(response)
                if update is not None:
                    usage = update
                    yield UsageUpdate(update)
                if event_type in {"response.output_text.delta", "response.text.delta"}:
                    delta = str(getattr(event, "delta", "") or "")
                    text_parts.append(delta)
                    yield TextDelta(delta)
                elif event_type == "response.output_item.added":
                    item = getattr(event, "item", None)
                    if getattr(item, "type", None) == "function_call":
                        index = getattr(event, "output_index", None)
                        if type(index) is not int or index < 0:
                            raise ProviderContractError("Responses stream function call has invalid index")
                        calls[index] = {"id": getattr(item, "call_id", None),
                            "name": getattr(item, "name", None), "arguments": ""}
                        yield ToolCallDelta(index, str(calls[index]["id"] or ""), str(calls[index]["name"] or ""))
                elif event_type == "response.function_call_arguments.delta":
                    index = getattr(event, "output_index", None)
                    call = calls.get(index)
                    if call is None:
                        raise ProviderContractError("Responses tool arguments arrived before function call item")
                    delta = str(getattr(event, "delta", "") or "")
                    call["arguments"] += delta
                    yield ToolCallDelta(index, arguments_delta=delta)
                elif event_type in {"response.completed", "response.incomplete"}:
                    terminal = True
                    final = response
                    break
                elif event_type == "response.failed":
                    from openkyrozen.providers.errors import ProviderError, ProviderErrorKind
                    raise ProviderError(ProviderErrorKind.SERVER, provider=self.name, model=model, terminal=True)
                mark_response_received()
            if not terminal or final is None:
                raise ProviderContractError("OpenAI Responses stream ended without a terminal response event")
            usage = self._usage(final) or usage
            if usage is not None:
                yield UsageUpdate(usage)
            text = str(getattr(final, "output_text", None) or "".join(text_parts))
            output = getattr(final, "output", None) or []
            result = model_response(provider=self.name, model=model, actual_model=getattr(final, "model", None),
                text=text, usage=usage, calls=responses_output(final), response_id=getattr(final, "id", None),
                raw_finish_reason=getattr(final, "status", None),
                finish_detail=getattr(getattr(final, "incomplete_details", None), "reason", None),
                blocked=any(getattr(part, "type", None) == "refusal" for item in output
                    for part in getattr(item, "content", None) or ()))
            if usage is None:
                usage = {"prompt_tokens": sum(len(str(item.get("content", ""))) for item in messages if isinstance(item, dict)) // 4,
                         "completion_tokens": len(text) // 4, "_estimated": 1}
                result = ModelResponse(result.text, result.tool_calls, usage, result.finish_reason, result.metadata)
                yield UsageUpdate(usage)
            result = with_provider_continuation(result, self, model, "openai-responses",
                {"output": _snapshot_from_sdk(output)})
            usage_ledger._track_cost(self.config.provider, usage, model=getattr(final, "model", None) or model,
                latency_ms=round((time.monotonic() - started) * 1000))
            yield ResponseComplete(result)
        finally:
            close_stream(stream)

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
