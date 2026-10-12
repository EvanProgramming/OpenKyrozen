from __future__ import annotations
import openkyrozen.providers.usage as usage_ledger

import json
import os
import sys
import time
from typing import Any, Iterator
from openkyrozen.providers.base import LLMProvider
from openkyrozen.providers.models import (received_response, ModelResponse, model_response, ToolReply,
    ProviderContractError, validate_tool_definitions, validate_tool_history, provider_identity,
    validate_replied_tool_definitions, with_provider_continuation, _snapshot_from_sdk)
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.providers.retry import _retry_with_backoff, provider_call, provider_stream, remaining_timeout, close_stream, mark_response_received


class GoogleProvider(LLMProvider):
    """Handles Google Gemini through the current Google Gen AI SDK."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        try:
            from google import genai
        except ImportError:
            sys.exit(
                "The 'google-genai' package is required for Gemini. "
                "Install it with: pip install google-genai"
            )
        self._supports_retry_options = "retry_options" in genai.types.HttpOptions.model_fields
        self._client = genai.Client(api_key=config.api_key or os.environ.get("GEMINI_API_KEY", ""))

    def get_capabilities(self, model: str | None = None):
        from openkyrozen.providers.models import ProviderCapabilities
        return ProviderCapabilities(native_tools=self.config.provider in {"google", "vertex"},
                                   text_streaming=True)

    def _http_options(self):
        options = {"timeout": max(1, int(remaining_timeout() * 1000))}
        # Gen AI 1.0 has no SDK retries and rejects the later retry_options field.
        if getattr(self, "_supports_retry_options", False):
            options["retry_options"] = {"attempts": 1}
        return options

    @staticmethod
    def _contents(messages: list, model: str, provider) -> tuple[list[dict[str, Any]], str | None]:
        contents: list[dict[str, Any]] = []
        system: list[str] = []
        for message in messages:
            if isinstance(message, ModelResponse):
                if message.continuation.protocol != "google-genai":
                    raise ProviderContractError("Continuation protocol does not match Gemini/Vertex")
                contents.append({"role": "model", "parts": message.continuation.payload["parts"]})
                continue
            if isinstance(message, ToolReply):
                part = {"function_response": {"id": message.call_id, "name": message.name,
                        "response": {"is_error": message.is_error, "result": message.output}}}
                if contents and contents[-1]["role"] == "user" and all(
                        "function_response" in old for old in contents[-1]["parts"]):
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
                continue
            role = message.get("role", "user")
            text = str(message.get("content", ""))
            if role == "system":
                system.append(text)
                continue
            contents.append({
                "role": "model" if role == "assistant" else "user",
                "parts": [{"text": text}],
            })
        return contents or [{"role": "user", "parts": [{"text": "Continue."}]}], (
            "\n\n".join(system) if system else None
        )

    @staticmethod
    def _usage(response: Any) -> dict[str, int | None] | None:
        meta = getattr(response, "usage_metadata", None)
        if meta is None:
            return None
        return {
            "prompt_tokens": getattr(meta, "prompt_token_count", 0) or 0,
            "completion_tokens": getattr(meta, "candidates_token_count", 0) or 0,
        }

    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        return self.chat_response(messages, model).as_legacy_tuple()

    @provider_call
    def chat_response(self, messages: list[dict[str, str]] | list, model: str | None = None, *, tools=None) -> ModelResponse:
        model = model or self.config.model_simple
        tools = validate_tool_definitions(tools)
        if tools:
            from google.genai import types
            supported = {"type", "properties", "required", "items", "enum", "description", "title", "default",
                         "examples", "minLength", "maxLength", "minItems", "maxItems", "minProperties",
                         "maxProperties", "minimum", "maximum", "pattern", "additionalProperties", "anyOf",
                         "format", "nullable"}

            def check_schema(schema):
                if set(schema) - supported:
                    raise ProviderContractError("Gemini/Vertex cannot represent every field in this tool schema")
                for nested in schema.get("properties", {}).values():
                    check_schema(nested)
                if "items" in schema:
                    check_schema(schema["items"])
                for nested in schema.get("anyOf", ()):
                    check_schema(nested)

            for tool in tools:
                try:
                    check_schema(tool["parameters"])
                    types.Schema.model_validate(tool["parameters"])
                except ProviderContractError:
                    raise
                except Exception as exc:
                    raise ProviderContractError("Gemini/Vertex rejected the provider tool schema") from exc
        validate_tool_history(messages, provider_id=provider_identity(self), model=model, native=tools is not None)
        validate_replied_tool_definitions(messages, tools)
        started = time.monotonic()

        contents, system_instruction = self._contents(messages, model, self)
        request_config: dict[str, Any] = {"http_options": self._http_options()}
        if system_instruction:
            request_config["system_instruction"] = system_instruction
        if tools:
            request_config["tools"] = [{"function_declarations": [
                {"name": item["name"], "description": item["description"], "parameters": item["parameters"]}
                for item in tools]}]

        def _call():
            return self._client.models.generate_content(
                model=model, contents=contents, config=request_config or None,
            )

        response = _retry_with_backoff(_call)
        with received_response():
            usage_dict = self._usage(response)
            usage_ledger._track_cost(self.config.provider, usage_dict, model=model,
                        latency_ms=round((time.monotonic() - started) * 1000))
            candidates = getattr(response, "candidates", None) or ()
            candidate = candidates[0] if candidates else None
            parts = getattr(getattr(candidate, "content", None), "parts", None)
            text = ("".join(part.text for part in parts if getattr(part, "text", None)
                            and not getattr(part, "thought", False)) if parts is not None
                    else str(getattr(response, "text", "") or ""))
            calls = []
            for part in parts or ():
                call = getattr(part, "function_call", None)
                if call is not None:
                    arguments = getattr(call, "args", None)
                    calls.append((getattr(call, "id", None), getattr(call, "name", None),
                                  {} if arguments is None else arguments))
            block_reason = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
            result = model_response(provider=self.name, model=model, actual_model=getattr(response, "model_version", None), text=text, usage=usage_dict,
                                    calls=calls, response_id=getattr(response, "response_id", None),
                                    raw_finish_reason=getattr(candidate, "finish_reason", None) or block_reason,
                                    blocked=block_reason not in {None, "BLOCK_REASON_UNSPECIFIED", "BLOCKED_REASON_UNSPECIFIED"})
            return with_provider_continuation(result, self, model, "google-genai",
                                              {"parts": _snapshot_from_sdk(parts or [])})

    @provider_stream
    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        model = model or self.config.model_simple
        contents, system_instruction = self._contents(messages, model, self)
        request_config: dict[str, Any] = {"http_options": self._http_options()}
        if system_instruction:
            request_config["system_instruction"] = system_instruction
        started = time.monotonic()
        final_usage: dict[str, int | None] | None = None
        completed = False
        stream = _retry_with_backoff(lambda: self._client.models.generate_content_stream(
            model=model, contents=contents, config=request_config or None,
        ))
        try:
            for chunk in stream:
                mark_response_received()
                usage = self._usage(chunk)
                if usage is not None:
                    final_usage = usage
                delta = str(getattr(chunk, "text", "") or "")
                if delta:
                    yield delta
            completed = True
        finally:
            close_stream(stream)
            if completed:
                usage_ledger._track_cost(self.config.provider, final_usage, model=model,
                            latency_ms=round((time.monotonic() - started) * 1000))


class VertexProvider(GoogleProvider):
    """Google Gen AI SDK configured for Vertex AI and ADC."""

    def __init__(self, config: ProviderConfig) -> None:
        LLMProvider.__init__(self, config)
        try:
            from google import genai
        except ImportError:
            sys.exit(
                "The 'google-genai' package is required for Vertex AI. "
                "Install it with: pip install google-genai"
            )
        self._supports_retry_options = "retry_options" in genai.types.HttpOptions.model_fields
        self._client = genai.Client(
            vertexai=True,
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        )
