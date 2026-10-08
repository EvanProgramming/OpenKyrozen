from __future__ import annotations
import openkyrozen.providers.usage as usage_ledger

import sys
import time
from openkyrozen.providers.base import LLMProvider
from openkyrozen.providers.models import received_response, ModelResponse, model_response
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
        return self.chat_response(messages, model).as_legacy_tuple()

    def chat_response(self, messages: list[dict[str, str]], model: str | None = None) -> ModelResponse:
        model = model or self.config.model_simple
        url = f"{self._base}/api/chat"
        payload = {"model": model, "messages": messages, "stream": False, "think": False}
        started = time.monotonic()
        resp = self._requests.post(url, json=payload, timeout=120)
        try:
            resp.raise_for_status()
        except self._requests.HTTPError as exc:
            try:
                body = resp.json()
            except ValueError:
                body = None
            detail = body.get("error") if isinstance(body, dict) else None
            if isinstance(detail, str):
                raise self._requests.HTTPError(f"Ollama HTTP {resp.status_code}: {detail[:2000]}", response=resp) from exc
            raise
        with received_response():
            data = resp.json()
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
            return model_response(provider=self.name, model=model, text=text, usage=usage_dict,
                                  calls=calls, raw_finish_reason="error" if data.get("error") else data.get("done_reason"))
