"""
Multi-provider LLM abstraction for OpenKyrozen.
Supports: DeepSeek, OpenAI, Anthropic (Claude), Google (Gemini), Ollama.

Each provider exposes a unified .chat(messages, model) interface that returns
(content: str, usage: dict | None). OpenAI-compat providers also support
.chat_stream() for real-time token streaming.

Features:
- Streaming responses (chat_stream)
- Provider fallback chain
- Rate-limit retry with exponential backoff
- Per-provider cost tracking
"""

from __future__ import annotations

import os
import sys
import time
import random
from dataclasses import dataclass
from typing import Any, Iterator
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
    "ollama":    "",
}

PROVIDER_BASE_URLS: dict[str, str] = {
    "deepseek":  "https://api.deepseek.com/v1",
    "openai":    "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "google":    "",
    "ollama":    "http://localhost:11434/v1",
}

# Fallback chain: if provider X fails, try these in order
PROVIDER_FALLBACKS: dict[str, list[str]] = {
    "deepseek":  ["openai", "anthropic"],
    "openai":    ["deepseek", "anthropic"],
    "anthropic": ["openai", "deepseek"],
    "google":    ["openai", "deepseek"],
    "ollama":    [],  # local, no fallback
}

# Approximate cost per 1M tokens (input, output) in USD
PROVIDER_COSTS: dict[str, tuple[float, float]] = {
    "deepseek":  (0.27, 1.10),
    "openai":    (2.50, 10.00),
    "anthropic": (3.00, 15.00),
    "google":    (0.15, 0.60),
    "ollama":    (0.0, 0.0),
}

# ---------------------------------------------------------------------------
# Global cost tracking
# ---------------------------------------------------------------------------

_cost_tracker: dict[str, dict[str, int]] = {}  # {provider: {prompt_tokens, completion_tokens, cost_cents}}

def _track_cost(provider: str, usage: dict | None) -> None:
    """Accumulate token usage and estimated cost for a provider."""
    if usage is None:
        return
    entry = _cost_tracker.setdefault(provider, {"prompt_tokens": 0, "completion_tokens": 0, "cost_cents": 0})
    pt = usage.get("prompt_tokens", 0) or 0
    ct = usage.get("completion_tokens", 0) or 0
    entry["prompt_tokens"] += pt
    entry["completion_tokens"] += ct
    costs = PROVIDER_COSTS.get(provider, (0, 0))
    entry["cost_cents"] += int((pt * costs[0] + ct * costs[1]) / 10000)

def get_cost_summary() -> str:
    """Return a human-readable cost summary."""
    if not _cost_tracker:
        return "No usage yet"
    parts = []
    for prov, data in _cost_tracker.items():
        cents = data["cost_cents"]
        pt = data["prompt_tokens"]
        ct = data["completion_tokens"]
        if cents >= 100:
            cost_str = f"${cents/100:.2f}"
        else:
            cost_str = f"{cents}c"
        parts.append(f"{prov}: {pt/1000:.0f}K in / {ct/1000:.0f}K out ~{cost_str}")
    return " | ".join(parts)

def reset_cost_tracker() -> None:
    """Reset all cost tracking counters."""
    _cost_tracker.clear()

# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

def _retry_with_backoff(fn, max_retries: int = 3, base_delay: float = 1.0):
    """Call fn() with exponential backoff on rate-limit or server errors."""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            msg = str(e).lower()
            is_rate_limit = "429" in msg or "rate limit" in msg or "too many requests" in msg
            is_server_error = "500" in msg or "502" in msg or "503" in msg or "server error" in msg
            if (is_rate_limit or is_server_error) and attempt < max_retries:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                time.sleep(delay)
                continue
            raise
    raise last_exc  # type: ignore[misc]

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

    def validate(self) -> list[str]:
        """Validate the configuration. Returns a list of warnings/errors."""
        issues: list[str] = []
        if self.provider not in PROVIDER_DEFAULT_MODELS:
            issues.append(f"Unknown provider '{self.provider}'")
        if self.provider != "ollama" and not self.api_key:
            env_var = PROVIDER_ENV_VARS.get(self.provider, "")
            issues.append(f"No API key for {self.provider} (set {env_var} or KYROZEN_API_KEY)")
        if self.model_simple and self.model_simple not in ("", "auto"):
            pass  # model name is user-specified, can't validate here
        return issues

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

    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        """Stream response tokens. Default: fall back to non-streaming chat()."""
        text, _ = self.chat(messages, model)
        yield text

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

        def _call():
            response = self._client.chat.completions.create(model=model, messages=messages)
            return response

        response = _retry_with_backoff(_call)
        text = response.choices[0].message.content or ""
        usage = getattr(response, "usage", None)
        usage_dict = None
        if usage is not None:
            usage_dict = {
                "prompt_tokens": usage.prompt_tokens or 0,
                "completion_tokens": usage.completion_tokens or 0,
            }
        _track_cost(self.config.provider, usage_dict)
        return text.strip(), usage_dict

    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        model = model or self.config.model_simple
        collected: list[str] = []

        def _call():
            return self._client.chat.completions.create(
                model=model, messages=messages, stream=True
            )

        stream = _retry_with_backoff(_call)
        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                collected.append(delta.content)
                yield delta.content

        # Estimate usage from collected text (rough: ~1 token per 4 chars)
        # Real usage tracking happens in non-streaming chat() for accuracy
        full_text = "".join(collected)

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
        _track_cost(self.config.provider, usage_dict)
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
                if user_content:
                    history.append({"role": "user", "parts": [user_content]})
                user_content = content
            elif role == "assistant":
                if user_content:
                    history.append({"role": "user", "parts": [user_content]})
                    user_content = ""
                history.append({"role": "model", "parts": [content]})

        if not user_content:
            user_content = "Continue."

        def _call():
            client = self._genai.GenerativeModel(
                model_name=model,
                system_instruction=system_instruction,
            )
            chat = client.start_chat(history=history if history else None)
            try:
                return chat.send_message(user_content)
            except Exception:
                return client.generate_content(user_content)

        response = _retry_with_backoff(_call)
        text = response.text or ""

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
        _track_cost(self.config.provider, usage_dict)
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
            usage_dict = {
                "prompt_tokens": data.get("prompt_eval_count", 0) or 0,
                "completion_tokens": data.get("eval_count", 0) or 0,
            }
            _track_cost(self.config.provider, usage_dict)
            return text.strip(), usage_dict
        except Exception as e:
            return f"[Ollama Error] {e}", None

# ---------------------------------------------------------------------------
# Fallback-aware provider wrapper
# ---------------------------------------------------------------------------

class FallbackProvider(LLMProvider):
    """Wraps multiple providers and falls back on failure."""

    def __init__(self, primary_config: ProviderConfig) -> None:
        self._primary = get_provider(primary_config)
        self._fallbacks: list[LLMProvider] = []
        fallback_names = PROVIDER_FALLBACKS.get(primary_config.provider, [])
        for fb_name in fallback_names:
            fb_config = ProviderConfig(
                provider=fb_name,
                api_key=os.environ.get(PROVIDER_ENV_VARS.get(fb_name, ""), ""),
            )
            # Only add fallback if it has an API key or is Ollama
            if fb_config.api_key or fb_name == "ollama":
                try:
                    self._fallbacks.append(get_provider(fb_config))
                except Exception:
                    pass

    @property
    def config(self) -> ProviderConfig:
        return self._primary.config

    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        providers = [self._primary] + self._fallbacks
        last_error = None
        for i, prov in enumerate(providers):
            try:
                return prov.chat(messages, model)
            except Exception as e:
                last_error = e
                if i < len(providers) - 1:
                    continue  # try next
        raise last_error or RuntimeError("All providers failed")

    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        providers = [self._primary] + self._fallbacks
        last_error = None
        for i, prov in enumerate(providers):
            try:
                yield from prov.chat_stream(messages, model)
                return
            except Exception as e:
                last_error = e
                if i < len(providers) - 1:
                    continue
        raise last_error or RuntimeError("All providers failed")

    @property
    def name(self) -> str:
        fb_names = [p.name for p in self._fallbacks]
        if fb_names:
            return f"{self._primary.name} (fallback: {', '.join(fb_names)})"
        return self._primary.name

# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_PROVIDER_CLASSES: dict[str, type[LLMProvider]] = {
    "deepseek":   OpenAICompatProvider,
    "openai":     OpenAICompatProvider,
    "ollama":     OpenAICompatProvider,
    "ollama_native": OllamaNativeProvider,
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


def get_fallback_provider(config: ProviderConfig) -> LLMProvider:
    """Create a provider with automatic fallback chain."""
    return FallbackProvider(config)


def detect_provider() -> ProviderConfig:
    """Detect the provider from environment variables or config file.
    Priority: env vars > config file > defaults (deepseek)."""
    import json

    provider_name = os.environ.get("KYROZEN_PROVIDER", "").strip().lower()

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
        if os.environ.get("ANTHROPIC_API_KEY"):
            provider_name = "anthropic"
        elif os.environ.get("GEMINI_API_KEY"):
            provider_name = "google"
        elif os.environ.get("OPENAI_API_KEY"):
            provider_name = "openai"
        elif os.environ.get("DEEPSEEK_API_KEY"):
            provider_name = "deepseek"
        else:
            provider_name = "deepseek"

    api_key = os.environ.get("KYROZEN_API_KEY", "")
    if not api_key:
        env_var = PROVIDER_ENV_VARS.get(provider_name, "")
        if env_var:
            api_key = os.environ.get(env_var, "")
    if not api_key:
        api_key = config_data.get("api_key", "")
    # Auto-decrypt if config was saved encrypted
    if api_key and config_data.get("encrypted"):
        api_key = decrypt_api_key(api_key)

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


# ---------------------------------------------------------------------------
# API key encryption at rest (simple XOR with machine-derived key)
# ---------------------------------------------------------------------------

def _get_encryption_key() -> bytes:
    """Derive a machine-specific encryption key from hostname + platform."""
    import hashlib, platform, socket
    seed = f"{socket.gethostname()}:{platform.node()}:openkyrozen"
    return hashlib.sha256(seed.encode()).digest()

def encrypt_api_key(plaintext: str) -> str:
    """Encrypt an API key using XOR with machine key. Returns base64 string."""
    import base64
    if not plaintext:
        return ""
    key = _get_encryption_key()
    plain_bytes = plaintext.encode()
    encrypted = bytes(p ^ key[i % len(key)] for i, p in enumerate(plain_bytes))
    return base64.b64encode(encrypted).decode()

def decrypt_api_key(ciphertext: str) -> str:
    """Decrypt an API key. Returns original string or empty on failure."""
    import base64
    if not ciphertext:
        return ""
    try:
        key = _get_encryption_key()
        encrypted = base64.b64decode(ciphertext)
        decrypted = bytes(e ^ key[i % len(key)] for i, e in enumerate(encrypted))
        return decrypted.decode()
    except Exception:
        return ciphertext  # return as-is if not encrypted (backward compat)

def save_provider_config_encrypted(config: ProviderConfig) -> None:
    """Save provider settings with encrypted API key."""
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
    existing["api_key"] = encrypt_api_key(config.api_key)
    existing["model_simple"] = config.model_simple
    existing["model_complex"] = config.model_complex
    existing["encrypted"] = True
    try:
        with open(config_path, "w") as f:
            json.dump(existing, f, indent=2)
    except OSError:
        pass
