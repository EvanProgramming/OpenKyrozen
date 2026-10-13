from __future__ import annotations
import openkyrozen.providers.usage as usage_ledger

import json
import sys
import time
from openkyrozen.providers.base import LLMProvider
from openkyrozen.providers.models import (received_response, ModelResponse, model_response, ToolReply,
    ProviderContractError, validate_tool_definitions, validate_tool_history, provider_identity,
    validate_replied_tool_definitions, with_provider_continuation, _snapshot_from_sdk)
from openkyrozen.providers.retry import _retry_with_backoff, provider_call, provider_stream, remaining_timeout, close_stream
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
        return ProviderCapabilities(native_tools=self.config.provider == "ollama")

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
