"""
Multi-provider LLM abstraction for OpenKyrozen.
Supports: DeepSeek, OpenAI, Anthropic (Claude), Google (Gemini), Ollama.

Each provider exposes a unified .chat(messages, model) interface that returns
(content: str, usage: dict | None).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any
from abc import ABC, abstractmethod

# ---------------------------------------------------------------------------
# Provider metadata
# ---------------------------------------------------------------------------

PROVIDER_DEFAULT_MODELS: dict[str, tuple[str, str]] = {
    "deepseek":  ("deepseek-chat",     "deepseek-reasoner"),
    "openai":    ("gpt-4o",             "gpt-4o"),
    "anthropic": ("claude-sonnet-4-20250514", "claude-sonnet-4-20250514"),
    "google":    ("gemini-2.5-flash",   "gemini-2.5-pro"),
    "ollama":    ("llama3.2",           "llama3.2"),
}

PROVIDER_ENV_VARS: dict[str, str] = {
    "deepseek":  "DEEPSEEK_API_KEY",
    "openai":    "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google":    "GEMINI_API_KEY",
    "ollama":    "",   # Ollama needs no key locally
}

PROVIDER_BASE_URLS: dict[str, str] = {
    "deepseek":  "https://api.deepseek.com/v1",
    "openai":    "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "google":    "",   # uses SDK default
    "ollama":    "http://localhost:11434/v1",
}

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class ProviderConfig:
    provider: str = "deepseek"
    api_key: str = ""
    base_url: str = ""
    model_simple: str = ""
    model_complex: str = ""

    def __post_init__(self) -> None:
        if not self.model_simple:
            self.model_simple = PROVIDER_DEFAULT_MODELS.get(self.provider, ("", ""))[0]
        if not self.model_complex:
            self.model_complex = PROVIDER_DEFAULT_MODELS.get(self.provider, ("", ""))[1]
        if not self.base_url:
            self.base_url = PROVIDER_BASE_URLS.get(self.provider, "")

# ---------------------------------------------------------------------------
# Abstract provider
# ---------------------------------------------------------------------------

class LLMProvider(ABC):
    """Unified interface for all LLM backends."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @abstractmethod
    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        """Send messages to the LLM. Returns (content, usage_dict_or_None)."""
        ...

    @property
    def name(self) -> str:
        return self.config.provider

# ---------------------------------------------------------------------------
# OpenAI-compatible (DeepSeek, OpenAI, Ollama, any /v1 endpoint)
# ---------------------------------------------------------------------------

class OpenAICompatProvider(LLMProvider):
    """Handles any OpenAI-compatible /v1/chat/completions endpoint."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        try:
            from openai import OpenAI
        except ImportError:
            sys.exit(
                "The 'openai' package is required for this provider. "
                "Install it with: pip install openai"
            )
        kwargs: dict[str, Any] = {"api_key": config.api_key or "sk-placeholder"}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self._client = OpenAI(**kwargs)

    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        model = model or self.config.model_simple
        response = self._client.chat.completions.create(model=model, messages=messages)
        text = response.choices[0].message.content or ""
        usage = getattr(response, "usage", None)
        usage_dict = None
        if usage is not None:
            usage_dict = {
                "prompt_tokens": usage.prompt_tokens or 0,
                "completion_tokens": usage.completion_tokens or 0,
            }
        return text.strip(), usage_dict

# ---------------------------------------------------------------------------
# Anthropic (Claude)
# ---------------------------------------------------------------------------

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

    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        model = model or self.config.model_simple

        # Claude's API requires messages in a different format:
        # - system prompt must be passed separately
        # - roles are "user" / "assistant" only (no "system" in messages)
        system_prompts: list[str] = []
        claude_messages: list[dict] = []
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            if role == "system":
                system_prompts.append(content)
            else:
                claude_messages.append({"role": role, "content": content})

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": 4096,
            "messages": claude_messages,
        }
        if system_prompts:
            kwargs["system"] = "\n\n".join(system_prompts)

        response = self._client.messages.create(**kwargs)
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
        return text.strip(), usage_dict

# ---------------------------------------------------------------------------
# Google (Gemini)
# ---------------------------------------------------------------------------

class GoogleProvider(LLMProvider):
    """Handles Google Gemini models via the generativeai SDK."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        try:
            import google.generativeai as genai
        except ImportError:
            sys.exit(
                "The 'google-generativeai' package is required for Gemini. "
                "Install it with: pip install google-generativeai"
            )
        genai.configure(api_key=config.api_key or os.environ.get("GEMINI_API_KEY", ""))
        self._genai = genai

    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        model = model or self.config.model_simple

        # Gemini uses a different conversation format:
        # - system prompt goes into system_instruction
        # - messages become a flat history list
        system_instruction: str | None = None
        history: list[dict] = []
        user_content: str = ""

        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            if role == "system":
                if system_instruction is None:
                    system_instruction = content
                else:
                    system_instruction += "\n\n" + content
            elif role == "user":
                # If there's a pending assistant reply before this, push as history
                pass
                user_content = content
            elif role == "assistant":
                if user_content:
                    history.append({"role": "user", "parts": [user_content]})
                    user_content = ""
                history.append({"role": "model", "parts": [content]})

        # If the last message is a user message, it becomes the current prompt
        if user_content:
            pass  # use as the prompt below
        elif history:
            user_content = "Continue."  # fallback

        client = self._genai.GenerativeModel(
            model_name=model,
            system_instruction=system_instruction,
        )

        # Build conversation
        chat = client.start_chat(history=history if history else None)
        try:
            response = chat.send_message(user_content or "Hello")
            text = response.text or ""
        except Exception:
            # Fallback: try as a single prompt
            response = client.generate_content(user_content or "Hello")
            text = response.text or ""

        # Gemini doesn't provide token counts in the same way
        usage_dict = None
        try:
            meta = getattr(response, "usage_metadata", None)
            if meta is not None:
                usage_dict = {
                    "prompt_tokens": getattr(meta, "prompt_token_count", 0) or 0,
                    "completion_tokens": getattr(meta, "candidates_token_count", 0) or 0,
                }
        except Exception:
            pass

        return text.strip(), usage_dict

# ---------------------------------------------------------------------------
# Ollama native (optional, OpenAI-compat is recommended)
# ---------------------------------------------------------------------------

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
        payload = {"model": model, "messages": messages, "stream": False}
        try:
            resp = self._requests.post(url, json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            text = data.get("message", {}).get("content", "")
            # Ollama reports eval_count / prompt_eval_count
            usage_dict = {
                "prompt_tokens": data.get("prompt_eval_count", 0) or 0,
                "completion_tokens": data.get("eval_count", 0) or 0,
            }
            return text.strip(), usage_dict
        except Exception as e:
            return f"[Ollama Error] {e}", None

# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_PROVIDER_CLASSES: dict[str, type[LLMProvider]] = {
    "deepseek":   OpenAICompatProvider,
    "openai":     OpenAICompatProvider,
    "ollama":     OpenAICompatProvider,    # OpenAI-compatible endpoint
    "ollama_native": OllamaNativeProvider, # native API (use provider="ollama_native")
    "anthropic":  AnthropicProvider,
    "google":     GoogleProvider,
}


def get_provider(config: ProviderConfig) -> LLMProvider:
    """Create and return the provider instance for the given config."""
    cls = _PROVIDER_CLASSES.get(config.provider)
    if cls is None:
        supported = ", ".join(sorted(_PROVIDER_CLASSES))
        sys.exit(
            f"Unknown provider '{config.provider}'. "
            f"Supported providers: {supported}\n"
            f"Set KYROZEN_PROVIDER or add 'provider' to ~/.kyrozen_config.json"
        )
    return cls(config)


def detect_provider() -> ProviderConfig:
    """Detect the provider from environment variables or config file.
    Priority: env vars > config file > defaults (deepseek)."""
    import json

    provider_name = os.environ.get("KYROZEN_PROVIDER", "").strip().lower()

    # Read config file for stored preferences
    config_path = os.path.expanduser("~/.kyrozen_config.json")
    config_data: dict[str, Any] = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                config_data = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    if not provider_name:
        provider_name = config_data.get("provider", "").strip().lower()
    if not provider_name:
        # Auto-detect from common env vars
        if os.environ.get("ANTHROPIC_API_KEY"):
            provider_name = "anthropic"
        elif os.environ.get("GEMINI_API_KEY"):
            provider_name = "google"
        elif os.environ.get("OPENAI_API_KEY"):
            provider_name = "openai"
        elif os.environ.get("DEEPSEEK_API_KEY"):
            provider_name = "deepseek"
        else:
            provider_name = "deepseek"  # default

    # Get API key: explicit env > config > provider-specific env
    api_key = os.environ.get("KYROZEN_API_KEY", "")
    if not api_key:
        env_var = PROVIDER_ENV_VARS.get(provider_name, "")
        if env_var:
            api_key = os.environ.get(env_var, "")
    if not api_key:
        api_key = config_data.get("api_key", "")

    base_url = os.environ.get("KYROZEN_BASE_URL", "")
    if not base_url:
        base_url = PROVIDER_BASE_URLS.get(provider_name, "")

    model_simple = (
        os.environ.get("KYROZEN_MODEL_SIMPLE", "")
        or config_data.get("model_simple", "")
    )
    model_complex = (
        os.environ.get("KYROZEN_MODEL_COMPLEX", "")
        or config_data.get("model_complex", "")
    )

    return ProviderConfig(
        provider=provider_name,
        api_key=api_key,
        base_url=base_url,
        model_simple=model_simple,
        model_complex=model_complex,
    )


def save_provider_config(config: ProviderConfig) -> None:
    """Save provider settings to ~/.kyrozen_config.json."""
    import json
    config_path = os.path.expanduser("~/.kyrozen_config.json")
    existing: dict[str, Any] = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    existing["provider"] = config.provider
    existing["api_key"] = config.api_key
    existing["model_simple"] = config.model_simple
    existing["model_complex"] = config.model_complex
    try:
        with open(config_path, "w") as f:
            json.dump(existing, f, indent=2)
    except OSError:
        pass
