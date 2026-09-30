from __future__ import annotations

from typing import Iterator
from abc import ABC, abstractmethod
from openkyrozen.providers.config import ProviderConfig

class LLMProvider(ABC):
    """Unified interface for all LLM backends."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @abstractmethod
    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        """Send messages to the LLM. Returns (content, usage_dict_or_None)."""
        ...

    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        """Stream response tokens. Default: fall back to non-streaming chat()."""
        text, _ = self.chat(messages, model)
        yield text

    @property
    def name(self) -> str:
        return self.config.provider
