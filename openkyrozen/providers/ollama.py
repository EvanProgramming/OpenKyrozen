from __future__ import annotations
import openkyrozen.providers.usage as usage_ledger

import sys
import time
from openkyrozen.providers.base import LLMProvider
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

    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        model = model or self.config.model_simple
        url = f"{self._base}/api/chat"
        payload = {"model": model, "messages": messages, "stream": False, "think": False}
        started = time.monotonic()
        try:
            resp = self._requests.post(url, json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            text = data.get("message", {}).get("content", "")
            usage_dict = {
                "prompt_tokens": data.get("prompt_eval_count", 0) or 0,
                "completion_tokens": data.get("eval_count", 0) or 0,
            }
            usage_ledger._track_cost("ollama", usage_dict, model=model,
                        latency_ms=round((time.monotonic() - started) * 1000))
            return text.strip(), usage_dict
        except Exception as e:
            return f"[Ollama Error] {e}", None
