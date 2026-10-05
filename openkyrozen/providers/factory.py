from __future__ import annotations

import os
import sys
from typing import Any
from openkyrozen.providers.anthropic import AnthropicProvider
from openkyrozen.providers.azure import AzureOpenAIProvider
from openkyrozen.providers.bedrock import BedrockProvider
from openkyrozen.providers.google import GoogleProvider, VertexProvider
from openkyrozen.providers.base import LLMProvider
from openkyrozen.providers.ollama import OllamaNativeProvider
from openkyrozen.providers.openai import OpenAICompatProvider, OpenAIResponsesProvider
from openkyrozen.providers.registry import PROVIDER_BASE_URLS, PROVIDER_DEFAULT_MODELS, PROVIDER_ENV_VARS
from openkyrozen.providers.perplexity import PerplexityProvider
from openkyrozen.providers.config import ProviderConfig, _ambient_provider_available
from openkyrozen.security.credentials import decrypt_api_key, save_provider_config_encrypted

_PROVIDER_CLASSES: dict[str, type[LLMProvider]] = {
    "deepseek": OpenAICompatProvider,
    "openai": OpenAIResponsesProvider,
    "ollama": OpenAICompatProvider,
    "ollama_native": OllamaNativeProvider,
    "anthropic": AnthropicProvider,
    "google": GoogleProvider,
    "glm": OpenAICompatProvider,
    "kimi": OpenAICompatProvider,
    "openrouter": OpenAICompatProvider,
    "groq": OpenAICompatProvider,
    "mistral": OpenAICompatProvider,
    "xai": OpenAICompatProvider,
    "together": OpenAICompatProvider,
    "fireworks": OpenAICompatProvider,
    "cohere": OpenAICompatProvider,
    "azure_openai": AzureOpenAIProvider,
    "perplexity": PerplexityProvider,
    "bedrock": BedrockProvider,
    "vertex": VertexProvider,
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
    from openkyrozen.providers.fallback import FallbackProvider
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
        for candidate in PROVIDER_DEFAULT_MODELS:
            env_var = PROVIDER_ENV_VARS.get(candidate, "")
            if (env_var and os.environ.get(env_var)) or _ambient_provider_available(candidate):
                provider_name = candidate
                break
        provider_name = provider_name or "deepseek"

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
    if provider_name == "azure_openai" and not base_url:
        base_url = os.environ.get("AZURE_OPENAI_ENDPOINT", "")

    model_simple = (
        os.environ.get("KYROZEN_MODEL_SIMPLE", "")
        or config_data.get("model_simple", "")
    )
    model_complex = (
        os.environ.get("KYROZEN_MODEL_COMPLEX", "")
        or config_data.get("model_complex", "")
    )
    model_main = os.environ.get("KYROZEN_MODEL_MAIN", "") or config_data.get("model_main", "")
    context_window_raw = os.environ.get("KYROZEN_CONTEXT_WINDOW_TOKENS", "") or config_data.get("context_window_tokens")
    try:
        context_window_tokens = int(context_window_raw) if context_window_raw not in (None, "") else None
    except (TypeError, ValueError):
        context_window_tokens = None
    if isinstance(context_window_tokens, int) and (context_window_tokens < 1 or context_window_tokens > 10_000_000):
        context_window_tokens = None

    # Auto-decrypt if config was saved encrypted
    if api_key and config_data.get("encrypted"):
        api_key = decrypt_api_key(api_key)
    # Auto-upgrade: if key exists in config but is not encrypted, re-save with encryption
    elif api_key and config_data and not config_data.get("encrypted"):
        try:
            save_provider_config_encrypted(ProviderConfig(
                provider=provider_name,
                api_key=api_key,
                base_url=base_url,
                model_simple=model_simple,
                model_complex=model_complex,
                model_main=model_main,
                context_window_tokens=context_window_tokens,
            ))
        except Exception:
            pass  # non-critical — will encrypt on next explicit save

    return ProviderConfig(
        provider=provider_name,
        api_key=api_key,
        base_url=base_url,
        model_simple=model_simple,
        model_complex=model_complex,
        model_main=model_main,
        context_window_tokens=context_window_tokens,
    )


def save_provider_config(config: ProviderConfig) -> None:
    """Save provider settings using the encrypted configuration path."""
    save_provider_config_encrypted(config)
