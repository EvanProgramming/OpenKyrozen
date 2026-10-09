from __future__ import annotations
import openkyrozen.providers.usage as usage_ledger

import os
import sys
import time
from typing import Any, Iterator
from openkyrozen.providers.base import LLMProvider
from openkyrozen.providers.models import received_response, ModelResponse, model_response
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.providers.retry import _retry_with_backoff


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
        self._client = genai.Client(api_key=config.api_key or os.environ.get("GEMINI_API_KEY", ""))

    @staticmethod
    def _contents(messages: list[dict[str, str]]) -> tuple[list[dict[str, Any]], str | None]:
        import json
        contents: list[dict[str, Any]] = []
        system: list[str] = []
        
        for message in messages:
            role = message.get("role", "user")
            content_payload = message.get("content", "")
            
            if role == "system":
                system.append(str(content_payload))
                continue
                
            if role == "assistant" and message.get("calls"):
                parts = []
                text_content = str(content_payload)
                if text_content:
                    parts.append({"text": text_content})
                
                for call_id, name, args in message.get("calls", []):
                    parts.append({
                        "function_call": {
                            "name": name,
                            "args": args if isinstance(args, dict) else {}
                        }
                    })
                contents.append({"role": "model", "parts": parts})
                continue
                
            if role in ["tool", "function"]:
                if isinstance(content_payload, (dict, list)):
                    text_content = json.dumps(content_payload)
                else:
                    text_content = str(content_payload)
                
                tool_name = message.get("name")
                if tool_name:
                    contents.append({
                        "role": "user",
                        "parts": [{
                            "function_response": {
                                "name": tool_name,
                                "response": {"output": text_content}
                            }
                        }],
                    })
                else:
                    contents.append({
                        "role": "user",
                        "parts": [{"text": text_content}],
                    })
                continue

            text = str(content_payload)
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

    def chat_response(self, messages: list[dict[str, str]], model: str | None = None) -> ModelResponse:
        model = model or self.config.model_simple
        started = time.monotonic()

        contents, system_instruction = self._contents(messages)
        request_config: dict[str, Any] = {}
        if system_instruction:
            request_config["system_instruction"] = system_instruction

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
            return model_response(provider=self.name, model=model, actual_model=getattr(response, "model_version", None), text=text, usage=usage_dict,
                                  calls=calls, response_id=getattr(response, "response_id", None),
                                  raw_finish_reason=getattr(candidate, "finish_reason", None) or block_reason,
                                  blocked=block_reason not in {None, "BLOCK_REASON_UNSPECIFIED", "BLOCKED_REASON_UNSPECIFIED"})

    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        model = model or self.config.model_simple
        contents, system_instruction = self._contents(messages)
        request_config: dict[str, Any] = {}
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
                usage = self._usage(chunk)
                if usage is not None:
                    final_usage = usage
                delta = str(getattr(chunk, "text", "") or "")
                if delta:
                    yield delta
            completed = True
        finally:
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
        self._client = genai.Client(
            vertexai=True,
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        )
