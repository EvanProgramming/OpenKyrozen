from __future__ import annotations
import openkyrozen.providers.usage as usage_ledger

import json
import os
import sys
import time
from typing import Any, Iterator
from openkyrozen.providers.base import LLMProvider, text_stream_projection
from openkyrozen.providers.models import (received_response, ModelResponse, model_response, responses_output,
    ToolReply, ProviderContractError, validate_tool_definitions, validate_tool_history, provider_identity,
    validate_replied_tool_definitions, with_provider_continuation, _snapshot_from_sdk,
    TextDelta, ToolCallDelta, UsageUpdate, ResponseComplete)
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.providers.retry import (_retry_with_backoff, provider_call, provider_stream,
    provider_event_stream, remaining_timeout, close_stream, mark_response_received)


class PerplexityProvider(LLMProvider):
    """Perplexity Agent API Responses adapter."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        try:
            from perplexity import Perplexity
        except ImportError:
            sys.exit("The 'perplexityai' package is required for Perplexity. Install it with: pip install perplexityai")
        self._client = Perplexity(max_retries=0, timeout=90.0, api_key=config.api_key or os.environ.get("PERPLEXITY_API_KEY", ""))

    def get_capabilities(self, model: str | None = None):
        from openkyrozen.providers.models import ProviderCapabilities
        supported = self.config.provider == "perplexity"
        return ProviderCapabilities(native_tools=supported, text_streaming=True, streaming_tool_calls=supported)

    @staticmethod
    def _prompt(messages: list[dict[str, str]]) -> tuple[str, str | None]:
        instructions = "\n\n".join(
            str(item.get("content", "")) for item in messages if item.get("role") == "system"
        ) or None
        prompt = "\n\n".join(
            f"{item.get('role', 'user')}: {item.get('content', '')}"
            for item in messages if item.get("role") != "system"
        )
        return prompt or "Continue.", instructions

    @staticmethod
    def _usage(response: Any) -> dict[str, int | None] | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        return {
            "prompt_tokens": getattr(usage, "input_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
        }

    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        return self.chat_response(messages, model).as_legacy_tuple()

    @provider_call
    def chat_response(self, messages: list[dict[str, str]] | list, model: str | None = None, *, tools=None) -> ModelResponse:
        model = model or self.config.model_simple
        tools = validate_tool_definitions(tools)
        validate_tool_history(messages, provider_id=provider_identity(self), model=model, native=tools is not None)
        validate_replied_tool_definitions(messages, tools)
        input_items = []
        instructions = []
        for item in messages:
            if isinstance(item, dict):
                if item.get("role") == "system":
                    instructions.append(str(item.get("content", "")))
                else:
                    role = "assistant" if item.get("role") == "assistant" else "user"
                    input_items.append({"type": "message", "role": role,
                        "content": [{"type": "input_text", "text": str(item.get("content", ""))}]})
            elif isinstance(item, ModelResponse):
                if item.continuation.protocol != "perplexity-responses":
                    raise ProviderContractError("Continuation protocol does not match Perplexity Responses")
                input_items.extend(item.continuation.payload["output"])
            else:
                input_items.append({"type": "function_call_output", "call_id": item.call_id,
                    "output": json.dumps({"is_error": item.is_error, "result": item.output}, ensure_ascii=False)})
        started = time.monotonic()
        kwargs: dict[str, Any] = {"model": model, "input": input_items or "Continue."}
        if instructions:
            kwargs["instructions"] = "\n\n".join(instructions)
        if tools:
            kwargs["tools"] = [{"type": "function", "name": item["name"], "description": item["description"],
                                "parameters": item["parameters"], "strict": False} for item in tools]
        response = _retry_with_backoff(lambda: self._client.responses.create(**kwargs, timeout=remaining_timeout()))
        with received_response():
            usage = self._usage(response)
            usage_ledger._track_cost(self.config.provider, usage, model=model,
                        latency_ms=round((time.monotonic() - started) * 1000))
            result = model_response(provider=self.name, model=model, actual_model=getattr(response, "model", None),
                                  text=str(getattr(response, "output_text", "") or ""), usage=usage,
                                  calls=responses_output(response), response_id=getattr(response, "id", None),
                                  raw_finish_reason=getattr(response, "status", None),
                                  finish_detail=getattr(getattr(response, "incomplete_details", None), "reason", None),
                                  blocked=any(getattr(part, "type", None) == "refusal"
                                              for item in getattr(response, "output", None) or ()
                                              for part in getattr(item, "content", None) or ()))
            return with_provider_continuation(result, self, model, "perplexity-responses",
                                              {"output": _snapshot_from_sdk(getattr(response, "output", None) or [])})

    @provider_event_stream
    def chat_response_stream(self, messages: list | list[dict], model: str | None = None, *, tools=None):
        model = model or self.config.model_simple
        tools = validate_tool_definitions(tools)
        validate_tool_history(messages, provider_id=provider_identity(self), model=model, native=tools is not None)
        validate_replied_tool_definitions(messages, tools)
        input_items, instructions = [], []
        for item in messages:
            if isinstance(item, dict):
                if item.get("role") == "system":
                    instructions.append(str(item.get("content", "")))
                else:
                    input_items.append({"type": "message",
                        "role": "assistant" if item.get("role") == "assistant" else "user",
                        "content": [{"type": "input_text", "text": str(item.get("content", ""))}]})
            elif isinstance(item, ModelResponse):
                if item.continuation.protocol != "perplexity-responses":
                    raise ProviderContractError("Continuation protocol does not match Perplexity Responses")
                input_items.extend(item.continuation.payload["output"])
            else:
                input_items.append({"type": "function_call_output", "call_id": item.call_id,
                    "output": json.dumps({"is_error": item.is_error, "result": item.output}, ensure_ascii=False)})
        kwargs: dict[str, Any] = {"model": model, "input": input_items or "Continue.", "stream": True,
                                  "timeout": remaining_timeout()}
        if instructions:
            kwargs["instructions"] = "\n\n".join(instructions)
        if tools:
            kwargs["tools"] = [{"type": "function", "name": item["name"],
                "description": item["description"], "parameters": item["parameters"], "strict": False}
                for item in tools]
        started = time.monotonic()
        stream = self._client.responses.create(**kwargs)
        text_parts, calls = [], {}
        final = None
        usage = None
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
                            raise ProviderContractError("Perplexity stream function call has invalid index")
                        calls[index] = {"id": getattr(item, "call_id", None),
                            "name": getattr(item, "name", None), "arguments": ""}
                        yield ToolCallDelta(index, str(calls[index]["id"] or ""), str(calls[index]["name"] or ""))
                elif event_type == "response.function_call_arguments.delta":
                    index = getattr(event, "output_index", None)
                    call = calls.get(index)
                    if call is None:
                        raise ProviderContractError("Perplexity tool arguments arrived before function call item")
                    delta = str(getattr(event, "delta", "") or "")
                    call["arguments"] += delta
                    yield ToolCallDelta(index, arguments_delta=delta)
                elif event_type in {"response.completed", "response.incomplete"}:
                    mark_response_received()
                    final = response
                    terminal = True
                    break
                elif event_type == "response.failed":
                    from openkyrozen.providers.errors import ProviderError, ProviderErrorKind
                    raise ProviderError(ProviderErrorKind.SERVER, provider=self.name, model=model, terminal=True)
                mark_response_received()
            if not terminal or final is None:
                raise ProviderContractError("Perplexity stream ended without a terminal response event")
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
                usage = {"prompt_tokens": sum(len(str(item.get("content", ""))) for item in messages
                    if isinstance(item, dict)) // 4, "completion_tokens": len(text) // 4, "_estimated": 1}
                result = ModelResponse(result.text, result.tool_calls, usage, result.finish_reason, result.metadata)
                yield UsageUpdate(usage)
            result = with_provider_continuation(result, self, model, "perplexity-responses",
                {"output": _snapshot_from_sdk(output)})
            usage_ledger._track_cost(self.config.provider, usage, model=model,
                latency_ms=round((time.monotonic() - started) * 1000))
            yield ResponseComplete(result)
        finally:
            close_stream(stream)

    @provider_stream
    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        yield from text_stream_projection(self.chat_response_stream(messages, model))
