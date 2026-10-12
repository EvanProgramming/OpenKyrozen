from __future__ import annotations
import openkyrozen.providers.usage as usage_ledger

import json
import sys
import time
from openkyrozen.providers.base import LLMProvider
from openkyrozen.providers.models import (received_response, ModelResponse, model_response, ToolReply,
    ProviderContractError, validate_tool_definitions, validate_tool_history, provider_identity,
    validate_replied_tool_definitions, with_provider_continuation, _snapshot_from_sdk,
    TextDelta, ToolCallDelta, UsageUpdate, ResponseComplete)
from openkyrozen.providers.retry import (_retry_with_backoff, provider_call, provider_stream,
    provider_event_stream, remaining_timeout, close_stream, mark_response_received)
from openkyrozen.providers.config import ProviderConfig


class OllamaNativeProvider(LLMProvider):
    """Handles Ollama via its native API (alternative to OpenAI-compat)."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        try:
            import requests
        except ImportError:
            sys.exit("The 'requests' package is required for Ollama native.")
        self._requests = requests
        self._base = config.base_url.replace("/v1", "") or "http://localhost:11434"

    def get_capabilities(self, model: str | None = None):
        from openkyrozen.providers.models import ProviderCapabilities
        supported = self.config.provider == "ollama"
        return ProviderCapabilities(native_tools=supported, text_streaming=True, streaming_tool_calls=supported)

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
            elif isinstance(item, ModelResponse):
                if item.continuation.protocol != "ollama-chat":
                    raise ProviderContractError("Continuation protocol does not match Ollama")
                request_messages.append(item.continuation.payload["assistant"])
            elif isinstance(item, ToolReply):
                request_messages.append({"role": "tool", "tool_call_id": item.call_id,
                    "tool_name": item.name,
                    "content": json.dumps({"is_error": item.is_error, "result": item.output}, ensure_ascii=False)})
        url = f"{self._base}/api/chat"
        payload = {"model": model, "messages": request_messages, "stream": False, "think": False}
        if tools:
            payload["tools"] = [{"type": "function", "function": {"name": item["name"],
                "description": item["description"], "parameters": item["parameters"]}} for item in tools]
        started = time.monotonic()
        def transport():
            resp = self._requests.post(url, json=payload, timeout=remaining_timeout())
            try:
                resp.raise_for_status()
            except Exception:
                close_stream(resp)
                raise
            return resp
        resp = _retry_with_backoff(transport)
        with received_response():
            try:
                data = resp.json()
            finally:
                close_stream(resp)
            usage_dict = {
                "prompt_tokens": data.get("prompt_eval_count", 0) or 0,
                "completion_tokens": data.get("eval_count", 0) or 0,
            }
            usage_ledger._track_cost("ollama", usage_dict, model=model,
                        latency_ms=round((time.monotonic() - started) * 1000))
            text = data.get("message", {}).get("content", "")
            calls = [(call.get("id"), call.get("function", {}).get("name"),
                      call.get("function", {}).get("arguments", {}))
                     for call in data.get("message", {}).get("tool_calls", [])]
            result = model_response(provider=self.name, model=model, text=text, usage=usage_dict,
                                    calls=calls, raw_finish_reason="error" if data.get("error") else data.get("done_reason"))
            return with_provider_continuation(result, self, model, "ollama-chat",
                                              {"assistant": _snapshot_from_sdk(data.get("message", {}))})

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
            elif isinstance(item, ModelResponse):
                if item.continuation.protocol != "ollama-chat":
                    raise ProviderContractError("Continuation protocol does not match Ollama")
                request_messages.append(item.continuation.payload["assistant"])
            else:
                request_messages.append({"role": "tool", "tool_call_id": item.call_id,
                    "tool_name": item.name, "content": json.dumps({"is_error": item.is_error,
                    "result": item.output}, ensure_ascii=False)})
        payload = {"model": model, "messages": request_messages, "stream": True, "think": False}
        if tools:
            payload["tools"] = [{"type": "function", "function": {"name": item["name"],
                "description": item["description"], "parameters": item["parameters"]}} for item in tools]
        started = time.monotonic()
        response = self._requests.post(f"{self._base}/api/chat", json=payload,
            timeout=remaining_timeout(), stream=True)
        try:
            response.raise_for_status()
            text_parts, calls = [], {}
            usage = None
            response_id = finish = None
            terminal = False
            for line in response.iter_lines():
                if not line:
                    continue
                data = json.loads(line.decode("utf-8") if isinstance(line, bytes) else line)
                if data.get("error"):
                    raise RuntimeError("Ollama stream returned an error")
                mark_response_received()
                message = data.get("message") or {}
                response_id = data.get("created_at") or response_id
                text = message.get("content") or ""
                if text:
                    text_parts.append(text)
                    yield TextDelta(text)
                for call in message.get("tool_calls") or ():
                    index = len(calls)
                    function = call.get("function") or {}
                    arguments = function.get("arguments") or {}
                    if not isinstance(arguments, dict):
                        raise ProviderContractError("Ollama stream tool arguments must be an object")
                    calls[index] = (call.get("id"), function.get("name"), arguments)
                    yield ToolCallDelta(index, str(call.get("id") or ""), str(function.get("name") or ""),
                        json.dumps(arguments, ensure_ascii=False, separators=(",", ":")))
                if data.get("done") is True:
                    terminal = True
                    finish = data.get("done_reason") or "stop"
                    usage = {"prompt_tokens": data.get("prompt_eval_count", 0) or 0,
                             "completion_tokens": data.get("eval_count", 0) or 0}
                    yield UsageUpdate(usage)
            if not terminal:
                raise ProviderContractError("Ollama NDJSON stream ended without done=true")
            text = "".join(text_parts)
            result = model_response(provider=self.name, model=model, text=text, usage=usage,
                calls=list(calls.values()), response_id=response_id, raw_finish_reason=finish)
            assistant = {"role": "assistant", "content": text}
            if result.tool_calls:
                assistant["tool_calls"] = [{"id": call.id, "function": {"name": call.name,
                    "arguments": call.arguments}} for call in result.tool_calls]
            result = with_provider_continuation(result, self, model, "ollama-chat", {"assistant": assistant})
            usage_ledger._track_cost("ollama", usage, model=model,
                latency_ms=round((time.monotonic() - started) * 1000))
            yield ResponseComplete(result)
        finally:
            close_stream(response)
