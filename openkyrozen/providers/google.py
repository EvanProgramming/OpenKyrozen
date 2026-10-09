
from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator, Mapping
from typing import Any

import openkyrozen.providers.usage as usage_ledger
from openkyrozen.providers.base import LLMProvider
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.providers.models import (
    ModelResponse,
    model_response,
    received_response,
)
from openkyrozen.providers.retry import _retry_with_backoff


class GoogleProvider(LLMProvider):
    """Handles Google Gemini through the Google Gen AI SDK."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)

        try:
            from google import genai
        except ImportError:
            raise ImportError(
                "The 'google-genai' package is required for Gemini. "
                "Install it with: pip install google-genai"
            ) from None

        api_key = getattr(config, "api_key", None) or os.getenv(
            "GEMINI_API_KEY"
        ) or os.getenv("GOOGLE_API_KEY")

        if not api_key:
            raise ValueError(
                "Gemini API key is missing. Set GEMINI_API_KEY or "
                "configure the provider API key."
            )

        self._client = genai.Client(api_key=api_key)

    @staticmethod
    def _json_value(value: Any) -> Any:
        """Convert values into JSON-compatible Python values."""
        if isinstance(value, Mapping):
            return dict(value)

        if isinstance(value, (list, tuple)):
            return list(value)

        if isinstance(value, str):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return value

        if value is None:
            return {}

        if isinstance(value, (bool, int, float)):
            return value

        return str(value)

    @classmethod
    def _function_args(cls, value: Any) -> dict[str, Any]:
        """Gemini function-call args must be an object."""
        value = cls._json_value(value)

        if isinstance(value, dict):
            return value

        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {}

            if isinstance(parsed, dict):
                return parsed

        return {}

    @classmethod
    def _contents(
        cls,
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], str | None]:
        """
        Convert conversation messages into Gemini contents.

        Tool responses are only emitted as function_response parts when
        a matching function call exists in the preceding assistant turn.
        """
        contents: list[dict[str, Any]] = []
        system_messages: list[str] = []

        pending_calls: dict[str, str] = {}

        for message in messages:
            role = str(message.get("role", "user")).lower()
            content = message.get("content", "")
            name = message.get("name") or message.get("tool_name")
            call_id = (
                message.get("tool_call_id")
                or message.get("call_id")
                or message.get("id")
            )

            if role == "system":
                if content:
                    system_messages.append(str(content))
                continue

            if role in ("tool", "function"):
                payload = cls._json_value(content)

                # Match the receipt to the original assistant call.
                function_name = pending_calls.pop(str(call_id), None) if call_id else None

                # Some internal message formats identify the tool by name.
                if function_name is None and name:
                    function_name = str(name)

                if function_name is not None:
                    if not isinstance(payload, dict):
                        payload = {"result": payload}

                    contents.append(
                        {
                            "role": "user",
                            "parts": [
                                {
                                    "function_response": {
                                        "name": function_name,
                                        "response": payload,
                                    }
                                }
                            ],
                        }
                    )
                else:
                    # Never send an unmatched function_response.
                    # Preserve the receipt as ordinary text instead.
                    contents.append(
                        {
                            "role": "user",
                            "parts": [
                                {
                                    "text": (
                                        f"Tool result ({name or 'unknown tool'}): "
                                        f"{json.dumps(payload, ensure_ascii=False)}"
                                    )
                                }
                            ],
                        }
                    )
                continue

            if role == "assistant":
                parts: list[dict[str, Any]] = []

                if isinstance(content, str) and content.strip():
                    parts.append({"text": content})
                elif content is not None and not isinstance(content, str):
                    normalized = cls._json_value(content)
                    if normalized:
                        parts.append(
                            {
                                "text": json.dumps(
                                    normalized,
                                    ensure_ascii=False,
                                )
                            }
                        )

                calls = message.get("calls") or message.get("tool_calls") or []

                for call in calls:
                    if isinstance(call, Mapping):
                        call_id = call.get("id") or call.get("call_id")
                        function = call.get("function", call)
                        function_name = function.get("name")
                        arguments = function.get(
                            "arguments",
                            function.get("args", {}),
                        )
                    elif isinstance(call, (list, tuple)) and len(call) >= 3:
                        call_id, function_name, arguments = call[:3]
                    else:
                        continue

                    if not function_name:
                        continue

                    function_name = str(function_name)

                    parts.append(
                        {
                            "function_call": {
                                "name": function_name,
                                "args": cls._function_args(arguments),
                            }
                        }
                    )

                    if call_id is not None:
                        pending_calls[str(call_id)] = function_name

                if parts:
                    contents.append({"role": "model", "parts": parts})
                continue

            # Default: user message.
            if isinstance(content, str):
                text = content
            else:
                text = json.dumps(
                    cls._json_value(content),
                    ensure_ascii=False,
                )

            contents.append(
                {
                    "role": "user",
                    "parts": [{"text": text or " "}],
                }
            )

        if not contents:
            contents.append(
                {
                    "role": "user",
                    "parts": [{"text": "Continue."}],
                }
            )

        system_instruction = (
            "\n\n".join(system_messages) if system_messages else None
        )

        return contents, system_instruction

    @staticmethod
    def _response_text(response: Any) -> str:
        """Extract visible text from a Gemini response."""
        text = getattr(response, "text", None)
        if isinstance(text, str):
            return text

        output: list[str] = []

        for candidate in getattr(response, "candidates", None) or []:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or []:
                part_text = getattr(part, "text", None)
                if isinstance(part_text, str) and not getattr(
                    part, "thought", False
                ):
                    output.append(part_text)

        return "".join(output)

    @staticmethod
    def _response_calls(response: Any) -> list[tuple[str, str, dict[str, Any]]]:
        """Extract function calls while preserving IDs when available."""
        calls: list[tuple[str, str, dict[str, Any]]] = []

        for candidate in getattr(response, "candidates", None) or []:
            content = getattr(candidate, "content", None)

            for part in getattr(content, "parts", None) or []:
                function_call = getattr(part, "function_call", None)
                if not function_call:
                    continue

                name = getattr(function_call, "name", None)
                if not name:
                    continue

                args = getattr(function_call, "args", None) or {}
                if not isinstance(args, dict):
                    args = GoogleProvider._function_args(args)

                call_id = getattr(part, "id", None) or ""
                calls.append((str(call_id), str(name), args))

        return calls

    def chat_response(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> ModelResponse:
        """Send a conversation to Gemini and return the provider response."""
        from google.genai import types

        contents, system_instruction = self._contents(messages)

        model = (
            kwargs.pop("model", None)
            or getattr(self._config, "model", None)
            or "gemini-2.5-flash"
        )

        config_kwargs: dict[str, Any] = {}
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction

        generation_config = types.GenerateContentConfig(**config_kwargs)

        start = time.monotonic()

        response = self._client.models.generate_content(
            model=model,
            contents=contents,
            config=generation_config,
        )

        elapsed = time.monotonic() - start
        text = self._response_text(response)
        calls = self._response_calls(response)

        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            usage_data = {
                "prompt_tokens": getattr(usage, "prompt_token_count", 0) or 0,
                "completion_tokens": getattr(
                    usage, "candidates_token_count", 0
                ) or 0,
                "total_tokens": getattr(usage, "total_token_count", 0) or 0,
            }
        else:
            usage_data = {}

        result = model_response(
            text=text,
            tool_calls=calls,
            usage=usage_data,
            elapsed=elapsed,
        )

        return result

    def stream_response(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Iterator[str]:
        """Stream generated text from Gemini."""
        from google.genai import types

        contents, system_instruction = self._contents(messages)

        model = (
            kwargs.pop("model", None)
            or getattr(self._config, "model", None)
            or "gemini-2.5-flash"
        )

        config_kwargs: dict[str, Any] = {}
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction

        config = types.GenerateContentConfig(**config_kwargs)

        stream = self._client.models.generate_content_stream(
            model=model,
            contents=contents,
            config=config,
        )

        for chunk in stream:
            text = self._response_text(chunk)
            if text:
                yield text
