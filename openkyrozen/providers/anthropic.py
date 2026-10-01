from __future__ import annotations
import openkyrozen.providers.usage as usage_ledger

import sys
import time
from typing import Any, Iterator
from openkyrozen.providers.base import LLMProvider
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.providers.retry import _retry_with_backoff


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
        self._client = anthropic.Anthropic(**kwargs)

    def _prepare_messages(self, messages):
        system_prompts: list[str] = []
        claude_messages: list[dict] = []
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            if role == "system":
                system_prompts.append(content)
            else:
                claude_messages.append({"role": role, "content": content})
        return system_prompts, claude_messages

    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        model = model or self.config.model_simple
        system_prompts, claude_messages = self._prepare_messages(messages)
        started = time.monotonic()

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": 4096,
            "messages": claude_messages,
        }
        if system_prompts:
            kwargs["system"] = "\n\n".join(system_prompts)

        def _call():
            return self._client.messages.create(**kwargs)

        response = _retry_with_backoff(_call)
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text
        usage = getattr(response, "usage", None)
        usage_dict = None
        if usage is not None:
            usage_dict = {
                "prompt_tokens": getattr(usage, "input_tokens", 0) or 0,
                "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
            }
        usage_ledger._track_cost(self.config.provider, usage_dict, model=getattr(response, "model", None) or model,
                    latency_ms=round((time.monotonic() - started) * 1000))
        return text.strip(), usage_dict

    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        model = model or self.config.model_simple
        system_prompts, claude_messages = self._prepare_messages(messages)
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
            return self._client.messages.stream(**kwargs)

        stream_context = _retry_with_backoff(_call)
        try:
            with stream_context as stream:
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
