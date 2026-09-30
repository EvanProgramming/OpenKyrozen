from __future__ import annotations
import openkyrozen.providers.usage as usage_ledger

import os
import sys
import time
from typing import Any, Iterator
from openkyrozen.providers.base import LLMProvider
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.providers.retry import _retry_with_backoff


class PerplexityProvider(LLMProvider):
    """Perplexity Agent API Responses adapter."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        try:
            from perplexity import Perplexity
        except ImportError:
            sys.exit("The 'perplexityai' package is required for Perplexity. Install it with: pip install perplexityai")
        self._client = Perplexity(api_key=config.api_key or os.environ.get("PERPLEXITY_API_KEY", ""))

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
        model = model or self.config.model_simple
        prompt, instructions = self._prompt(messages)
        started = time.monotonic()
        kwargs: dict[str, Any] = {"model": model, "input": prompt}
        if instructions:
            kwargs["instructions"] = instructions
        response = _retry_with_backoff(lambda: self._client.responses.create(**kwargs))
        usage = self._usage(response)
        usage_ledger._track_cost(self.config.provider, usage, model=model,
                    latency_ms=round((time.monotonic() - started) * 1000))
        return str(getattr(response, "output_text", "") or "").strip(), usage

    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        model = model or self.config.model_simple
        prompt, instructions = self._prompt(messages)
        started = time.monotonic()
        kwargs: dict[str, Any] = {"model": model, "input": prompt, "stream": True}
        if instructions:
            kwargs["instructions"] = instructions
        stream = _retry_with_backoff(lambda: self._client.responses.create(**kwargs))
        final_usage: dict[str, int | None] | None = None
        completed = False
        try:
            for event in stream:
                response = getattr(event, "response", None) or event
                usage = self._usage(response)
                if usage is not None:
                    final_usage = usage
                event_type = str(getattr(event, "type", ""))
                delta = str(getattr(event, "delta", "") or "") if "delta" in event_type else ""
                if not delta and event_type.endswith("text.delta"):
                    delta = str(getattr(event, "text", "") or "")
                if delta:
                    yield delta
            completed = True
        finally:
            if completed:
                usage_ledger._track_cost(self.config.provider, final_usage, model=model,
                            latency_ms=round((time.monotonic() - started) * 1000))
