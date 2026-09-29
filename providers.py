"""
Multi-provider LLM abstraction for OpenKyrozen.

The registry deliberately keeps the agent-facing interface small while
adapting provider-specific transports at this boundary.  OpenAI-compatible
providers share one implementation; Responses, Anthropic Messages, Google
Gen AI, Perplexity Agent, and Bedrock Converse have focused adapters.

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
import uuid
from functools import lru_cache
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterator
from abc import ABC, abstractmethod

from event_store import EventStore

# ---------------------------------------------------------------------------
# Provider metadata
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderSpec:
    """The single source of truth for selectable provider metadata."""

    canonical_name: str
    display_name: str
    api_style: str
    base_url: str
    api_key_env: str
    model_simple: str
    model_complex: str
    context_window_tokens: int | None
    auto_selection: bool
    fallbacks: tuple[str, ...] = ()


PROVIDER_REGISTRY: dict[str, ProviderSpec] = {
    # Defaults are taken from the providers' current official model catalogs.
    "deepseek": ProviderSpec("deepseek", "DeepSeek", "openai_compat", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "deepseek-flash", "deepseek-v4-pro", 1_048_576, True, ("openai", "anthropic")),
    "openai": ProviderSpec("openai", "OpenAI", "responses", "https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-6-luna", "gpt-6-astra", 1_048_576, True, ("deepseek", "anthropic")),
    "anthropic": ProviderSpec("anthropic", "Anthropic", "messages", "https://api.anthropic.com", "ANTHROPIC_API_KEY", "claude-haiku-4-5", "claude-fable-5-1", 1_000_000, True, ("openai", "deepseek")),
    "google": ProviderSpec("google", "Google Gemini", "google_genai", "", "GEMINI_API_KEY", "gemini-3.5-flash-lite", "gemini-3.1-pro-preview", 1_048_576, True, ("openai", "deepseek")),
    "ollama": ProviderSpec("ollama", "Ollama", "openai_compat", "http://localhost:11434/v1", "", "llama3.2", "llama3.2", None, True),
    "glm": ProviderSpec("glm", "Z.AI / GLM", "openai_compat", "https://api.z.ai/api/paas/v4/", "ZAI_API_KEY", "glm-5.3-flash", "glm-5.3", 1_048_576, True, ("openai", "deepseek")),
    "kimi": ProviderSpec("kimi", "Moonshot / Kimi", "openai_compat", "https://api.moonshot.cn/v1", "MOONSHOT_API_KEY", "kimi-k2.6", "kimi-k3", 1_048_576, True, ("openai", "deepseek")),
    "openrouter": ProviderSpec("openrouter", "OpenRouter", "openai_compat", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "~openai/gpt-sol-latest", "~openai/gpt-sol-latest", 1_048_576, False, ("openai", "deepseek")),
    "groq": ProviderSpec("groq", "Groq", "openai_compat", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai/gpt-oss-120b", "openai/gpt-oss-120b", 131_072, False, ("openai", "deepseek")),
    "mistral": ProviderSpec("mistral", "Mistral", "openai_compat", "https://api.mistral.ai/v1", "MISTRAL_API_KEY", "mistral-small-2603", "mistral-small-2603", 256_000, False, ("openai", "deepseek")),
    "xai": ProviderSpec("xai", "xAI", "openai_compat", "https://api.x.ai/v1", "XAI_API_KEY", "grok-4.7", "grok-4.7", 500_000, False, ("openai", "deepseek")),
    "together": ProviderSpec("together", "Together AI", "openai_compat", "https://api.together.ai/v1", "TOGETHER_API_KEY", "MiniMaxAI/MiniMax-M3", "MiniMaxAI/MiniMax-M3", 196_608, False, ("openai", "deepseek")),
    "fireworks": ProviderSpec("fireworks", "Fireworks AI", "openai_compat", "https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY", "accounts/fireworks/models/deepseek-v3p1", "accounts/fireworks/models/deepseek-v3p1", 1_048_576, False, ("openai", "deepseek")),
    "cohere": ProviderSpec("cohere", "Cohere", "openai_compat", "https://api.cohere.ai/compatibility/v1", "COHERE_API_KEY", "command-a-plus-05-2026", "command-a-plus-05-2026", 256_000, False, ("openai", "deepseek")),
    # Azure deployments, Bedrock model access, and Vertex locations are
    # account/region scoped; leave their model slots explicit by design.
    "azure_openai": ProviderSpec("azure_openai", "Azure OpenAI", "azure_openai", "", "AZURE_OPENAI_API_KEY", "", "", None, False, ("openai", "deepseek")),
    "perplexity": ProviderSpec("perplexity", "Perplexity", "responses", "https://api.perplexity.ai", "PERPLEXITY_API_KEY", "perplexity/sonar", "perplexity/sonar", 1_000_000, False, ("openai", "deepseek")),
    "bedrock": ProviderSpec("bedrock", "Amazon Bedrock", "bedrock_converse", "", "", "", "", None, False, ("openai", "deepseek")),
    "vertex": ProviderSpec("vertex", "Google Vertex AI", "vertex_genai", "", "", "", "", None, False, ("openai", "deepseek")),
}

PROVIDER_DEFAULT_MODELS = {
    name: (spec.model_simple, spec.model_complex) for name, spec in PROVIDER_REGISTRY.items()
}
PROVIDER_DISPLAY_NAMES = {name: spec.display_name for name, spec in PROVIDER_REGISTRY.items()}
PROVIDER_AUTO_SELECTION = frozenset(
    name for name, spec in PROVIDER_REGISTRY.items() if spec.auto_selection
)
PROVIDER_ENV_VARS = {name: spec.api_key_env for name, spec in PROVIDER_REGISTRY.items()}
PROVIDER_BASE_URLS = {name: spec.base_url for name, spec in PROVIDER_REGISTRY.items()}
PROVIDER_FALLBACKS = {
    name: list(spec.fallbacks) for name, spec in PROVIDER_REGISTRY.items()
}
PROVIDER_CONTEXT_WINDOWS = {
    name: spec.context_window_tokens for name, spec in PROVIDER_REGISTRY.items()
    if spec.context_window_tokens is not None
}

# Approximate cost per 1M tokens (input, output) in USD
PROVIDER_COSTS: dict[str, tuple[float, float]] = {
    "deepseek":  (0.27, 1.10),
    "openai":    (2.50, 10.00),
    "anthropic": (3.00, 15.00),
    "google":    (0.15, 0.60),
    "ollama":    (0.0, 0.0),
}

AMBIENT_CREDENTIAL_PROVIDERS = frozenset({"ollama", "bedrock", "vertex"})

# ---------------------------------------------------------------------------
# Durable usage ledger
# ---------------------------------------------------------------------------

_PICOS_PER_DOLLAR = 10**12
_TOKENS_PER_MILLION = 1_000_000
_DEEPSEEK_V4_EFFECTIVE_AT = datetime(2026, 8, 16, 16, tzinfo=timezone.utc)
_DEEPSEEK_V4_MODELS = {
    "deepseek-v4-flash": ("0.014", "0.44", "1.32"),
    "deepseek-v4-flash-vision-exp": ("0.014", "0.44", "1.32"),
    "deepseek-v4-pro": ("0.044", "1.32", "3.96"),
}
_DEEPSEEK_V4_ALIASES = {
    "deepseek-chat": "deepseek-v4-flash",
    "deepseek-reasoner": "deepseek-v4-flash",
}


@dataclass(frozen=True)
class UsageScope:
    store: EventStore
    user_id: str = "local"
    workspace_id: str = "default"
    session_id: str | None = None
    run_id: str | None = None
    surface: str = "cli"


_usage_scope: ContextVar[UsageScope | None] = ContextVar("usage_scope", default=None)


@contextmanager
def usage_scope(*, store: EventStore, user_id: str = "local", workspace_id: str = "default",
                session_id: str | None = None, run_id: str | None = None,
                surface: str | None = None) -> Iterator[None]:
    """Attribute all provider calls in this execution context to one durable scope."""
    token = _usage_scope.set(UsageScope(
        store=store, user_id=user_id, workspace_id=workspace_id, session_id=session_id,
        run_id=run_id, surface=surface or os.environ.get("KYROZEN_EXECUTION_SURFACE", "cli"),
    ))
    try:
        yield
    finally:
        _usage_scope.reset(token)


def _current_usage_scope() -> UsageScope:
    return _usage_scope.get() or UsageScope(
        EventStore(), surface=os.environ.get("KYROZEN_EXECUTION_SURFACE", "cli"),
    )


def _legacy_pricing_snapshot(provider: str) -> tuple[dict[str, Any], int, int]:
    input_usd, output_usd = PROVIDER_COSTS.get(provider, (0.0, 0.0))
    input_picos = int(Decimal(str(input_usd)) * _PICOS_PER_DOLLAR)
    output_picos = int(Decimal(str(output_usd)) * _PICOS_PER_DOLLAR)
    if provider not in PROVIDER_COSTS:
        return {
            "version": "provider-pricing-unknown-v1",
            "currency": "USD",
            "pricing_status": "unknown",
            "provider": provider,
        }, input_picos, output_picos
    return {
        "version": "legacy-provider-costs-v1",
        "currency": "USD",
        "input_picos_per_million": input_picos,
        "output_picos_per_million": output_picos,
    }, input_picos, output_picos


def _deepseek_v4_pricing_snapshot(model: str, occurred_at: datetime) -> tuple[dict[str, Any] | None, bool]:
    """Freeze the official DeepSeek V4 rate card selected at a UTC timestamp."""
    canonical_model = _DEEPSEEK_V4_ALIASES.get(model.strip().lower(), model.strip().lower())
    rates = _DEEPSEEK_V4_MODELS.get(canonical_model)
    if rates is None:
        return None, False
    instant = occurred_at.astimezone(timezone.utc)
    peak = instant.weekday() < 5 and (1 <= instant.hour < 4 or 6 <= instant.hour < 10)
    multiplier = 1 if peak else 0.5
    hit, miss, output = (
        int(Decimal(rate) * Decimal(str(multiplier)) * _PICOS_PER_DOLLAR)
        for rate in rates
    )
    current_schedule = instant >= _DEEPSEEK_V4_EFFECTIVE_AT
    return {
        "version": "deepseek-v4-pricing-2026-08-16",
        "source": "https://api-docs.deepseek.com/quick_start/pricing/",
        "currency": "USD",
        "model": canonical_model,
        "priced_at": instant.isoformat(),
        "effective_at": _DEEPSEEK_V4_EFFECTIVE_AT.isoformat(),
        "billing_window": "peak" if peak else "off_peak",
        "cache_hit_picos_per_million": hit,
        "cache_miss_picos_per_million": miss,
        "output_picos_per_million": output,
        "pricing_status": "authoritative" if current_schedule else "estimated_current_schedule",
    }, current_schedule


def _usage_integer(usage: dict | None, key: str) -> int | None:
    if usage is None or usage.get(key) is None:
        return None
    try:
        return max(0, int(usage[key]))
    except (TypeError, ValueError):
        return None


def _openai_usage_dict(usage: Any) -> dict[str, int | None] | None:
    if usage is None:
        return None
    completion_details = getattr(usage, "completion_tokens_details", None)
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "prompt_cache_hit_tokens": getattr(usage, "prompt_cache_hit_tokens", None),
        "prompt_cache_miss_tokens": getattr(usage, "prompt_cache_miss_tokens", None),
        "reasoning_tokens": getattr(completion_details, "reasoning_tokens", None),
    }


def _track_cost(provider: str, usage: dict | None, *, model: str = "unknown",
                latency_ms: int | None = None, completion_state: str = "completed",
                occurred_at: datetime | None = None) -> str:
    """Write one immutable provider attempt; summaries always read this ledger."""
    scope = _current_usage_scope()
    prompt_tokens = _usage_integer(usage, "prompt_tokens")
    completion_tokens = _usage_integer(usage, "completion_tokens")
    cache_hit_tokens = _usage_integer(usage, "cache_hit_tokens")
    cache_miss_tokens = _usage_integer(usage, "cache_miss_tokens")
    if cache_hit_tokens is None:
        cache_hit_tokens = _usage_integer(usage, "prompt_cache_hit_tokens")
    if cache_miss_tokens is None:
        cache_miss_tokens = _usage_integer(usage, "prompt_cache_miss_tokens")
    reasoning_tokens = _usage_integer(usage, "reasoning_tokens")
    pricing_snapshot = None
    current_schedule = True
    if provider == "deepseek":
        pricing_snapshot, current_schedule = _deepseek_v4_pricing_snapshot(
            model, occurred_at or datetime.now(timezone.utc),
        )
    if pricing_snapshot is None:
        pricing_snapshot, input_rate, output_rate = _legacy_pricing_snapshot(provider)
    else:
        input_rate = int(pricing_snapshot["cache_miss_picos_per_million"])
        output_rate = int(pricing_snapshot["output_picos_per_million"])
    pricing_known = pricing_snapshot.get("pricing_status") != "unknown"
    cost_picos = None
    if pricing_known and (prompt_tokens is not None or completion_tokens is not None):
        prompt = prompt_tokens or 0
        if "cache_hit_picos_per_million" in pricing_snapshot:
            hit = min(prompt, cache_hit_tokens or 0)
            input_cost = (hit * int(pricing_snapshot["cache_hit_picos_per_million"])
                          + (prompt - hit) * input_rate)
        else:
            input_cost = prompt * input_rate
        cost_picos = (input_cost + (completion_tokens or 0) * output_rate) // _TOKENS_PER_MILLION
    if cache_hit_tokens is None and cache_miss_tokens is None:
        cache_status = "unknown"
    elif (cache_hit_tokens or 0) and (cache_miss_tokens or 0):
        cache_status = "mixed"
    elif cache_hit_tokens:
        cache_status = "hit"
    else:
        cache_status = "miss"
    usage_status = "unknown" if usage is None or not pricing_known else (
        "estimated" if usage.get("_estimated") else
        "authoritative" if current_schedule and (provider != "deepseek" or cache_status != "unknown")
        else "estimated"
    )
    attempt_id = f"usage_{uuid.uuid4().hex}"
    scope.store.record_usage_attempt(
        attempt_id=attempt_id, provider=provider, model=model, surface=scope.surface,
        user_id=scope.user_id, workspace_id=scope.workspace_id, session_id=scope.session_id,
        run_id=scope.run_id, cache_status=cache_status, prompt_tokens=prompt_tokens,
        cache_hit_tokens=cache_hit_tokens, cache_miss_tokens=cache_miss_tokens,
        completion_tokens=completion_tokens, reasoning_tokens=reasoning_tokens,
        latency_ms=latency_ms, completion_state=completion_state,
        usage_status=usage_status,
        cost_picos=cost_picos, pricing_snapshot=pricing_snapshot,
    )
    return attempt_id


def _format_cost_picos(cost_picos: int) -> str:
    if cost_picos <= 0:
        return "$0"
    cents = Decimal(cost_picos) / Decimal(_PICOS_PER_DOLLAR // 100)
    if cents < 1:
        return f"{cents.normalize():f}c"
    dollars = Decimal(cost_picos) / _PICOS_PER_DOLLAR
    return f"${dollars:.2f}"


def _format_cost_summary(totals: dict[str, Any]) -> str:
    if not totals["attempts"]:
        return "No usage yet"
    return " | ".join(
        f"{item['provider']}: {item['prompt_tokens'] / 1000:.0f}K in / "
        f"{item['completion_tokens'] / 1000:.0f}K out ~{_format_cost_picos(item['cost_picos'])}"
        for item in totals["providers"]
    )


def _scope_totals(store: EventStore, *, scope: str, user_id: str, workspace_id: str,
                  session_id: str | None = None) -> dict[str, Any]:
    reset = None if scope == "installation" else store.latest_usage_reset(
        scope=scope, user_id=user_id, workspace_id=workspace_id, session_id=session_id,
    )
    filters: dict[str, Any] = {"user_id": user_id}
    if scope in {"workspace", "session"}:
        filters["workspace_id"] = workspace_id
    if scope == "session":
        filters["session_id"] = session_id
    totals = store.usage_totals(since=reset["created_at"] if reset else None, **filters)
    totals["window_started_at"] = reset["created_at"] if reset else None
    return totals


def get_cost_report(*, store: EventStore | None = None, user_id: str = "local",
                    workspace_id: str = "default", session_id: str | None = None,
                    scope: str = "installation") -> dict[str, Any]:
    """Return durable installation, workspace, and optional session usage windows."""
    if scope not in {"installation", "workspace", "session"}:
        raise ValueError("scope must be installation, workspace, or session")
    if scope == "session" and not session_id:
        raise ValueError("session scope requires a session_id")
    store = store or EventStore()
    totals = {
        "installation": _scope_totals(store, scope="installation", user_id=user_id,
                                       workspace_id=workspace_id),
        "workspace": _scope_totals(store, scope="workspace", user_id=user_id,
                                    workspace_id=workspace_id),
        "session": (_scope_totals(store, scope="session", user_id=user_id,
                                   workspace_id=workspace_id, session_id=session_id)
                    if session_id else None),
    }
    return {"summary": _format_cost_summary(totals[scope]), "selected_scope": scope, "totals": totals}


def get_cost_summary(**kwargs: Any) -> str:
    """Return the legacy display string, reconstructed from durable attempts."""
    return get_cost_report(**kwargs)["summary"]


def reset_cost_tracker(*, store: EventStore | None = None, user_id: str = "local",
                       workspace_id: str = "default", session_id: str | None = None,
                       scope: str = "workspace") -> dict[str, str]:
    """Create an audited reporting-window reset without deleting usage attempts."""
    return (store or EventStore()).create_usage_reset(
        scope=scope, user_id=user_id, workspace_id=workspace_id, session_id=session_id,
    )

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
    context_window_tokens: int | None = None

    def __post_init__(self) -> None:
        if not self.model_simple:
            self.model_simple = os.environ.get(
                "KYROZEN_MODEL_SIMPLE", "",
            ) or PROVIDER_DEFAULT_MODELS.get(self.provider, ("", ""))[0]
        if not self.model_complex:
            self.model_complex = os.environ.get(
                "KYROZEN_MODEL_COMPLEX", "",
            ) or PROVIDER_DEFAULT_MODELS.get(self.provider, ("", ""))[1]
        if not self.base_url:
            self.base_url = os.environ.get(
                "KYROZEN_BASE_URL", "",
            ) or PROVIDER_BASE_URLS.get(self.provider, "")
        if self.provider == "azure_openai" and not self.base_url:
            self.base_url = os.environ.get("AZURE_OPENAI_ENDPOINT", "")

    def validate(self) -> list[str]:
        """Validate the configuration. Returns a list of warnings/errors."""
        issues: list[str] = []
        if self.provider not in PROVIDER_DEFAULT_MODELS:
            issues.append(f"Unknown provider '{self.provider}'")
        if self.provider in {"azure_openai", "bedrock", "vertex"} and not self.model_simple:
            issues.append(f"No model/deployment configured for {self.provider}")
        if self.provider in AMBIENT_CREDENTIAL_PROVIDERS and not _ambient_provider_available(self.provider):
            issues.append(f"No ambient credentials available for {self.provider}")
        if self.provider not in AMBIENT_CREDENTIAL_PROVIDERS and not self.api_key and not _provider_env_key(self.provider):
            env_var = PROVIDER_ENV_VARS.get(self.provider, "")
            if self.provider == "azure_openai" and self.base_url and _azure_identity_available(self.base_url):
                pass
            else:
                issues.append(f"No API key for {self.provider} (set {env_var} or KYROZEN_API_KEY)")
        if self.model_simple and self.model_simple not in ("", "auto"):
            pass  # model name is user-specified, can't validate here
        return issues


def _provider_env_key(provider: str) -> str:
    env_var = PROVIDER_ENV_VARS.get(provider, "")
    return os.environ.get("KYROZEN_API_KEY", "") or (os.environ.get(env_var, "") if env_var else "")


def _azure_identity_available(endpoint: str | None = None) -> bool:
    """Return whether Azure Entra credentials can be resolved without prompting."""
    if not (endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT")):
        return False
    try:
        from azure.identity import DefaultAzureCredential
        DefaultAzureCredential(exclude_interactive_browser_credential=True)
        return True
    except Exception:
        return False


def _ambient_provider_available(provider: str) -> bool:
    if provider == "ollama":
        return True
    if provider == "bedrock":
        if not (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")):
            return False
        try:
            import boto3
            return boto3.Session().get_credentials() is not None
        except ImportError:
            return bool(os.environ.get("AWS_PROFILE") or os.environ.get("AWS_ACCESS_KEY_ID"))
        except Exception:
            return False
    if provider == "vertex":
        if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
            return False
        if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.environ.get("GOOGLE_GENAI_USE_VERTEXAI") == "True":
            return True
        try:
            import google.auth
            google.auth.default()
            return True
        except Exception:
            return False
    if provider == "azure_openai":
        return _azure_identity_available()
    return False


def provider_is_configured(config: ProviderConfig) -> bool:
    """Whether a provider has a key or can use its documented ambient auth."""
    ambient = _azure_identity_available(config.base_url) if config.provider == "azure_openai" else _ambient_provider_available(config.provider)
    return bool(config.api_key or _provider_env_key(config.provider)) or ambient


def model_for_complexity(config: ProviderConfig, complex_task: bool) -> str:
    """Resolve a provider-specific simple/complex slot without cross-provider names."""
    if config.provider in PROVIDER_AUTO_SELECTION:
        return (config.model_complex if complex_task else config.model_simple) or "auto"
    return config.model_complex or config.model_simple or "auto"


@lru_cache(maxsize=8)
def discover_ollama_models(base_url: str) -> tuple[tuple[str, str, int], ...]:
    """Return locally installed models as (name, modified_at, parameter_bytes)."""
    try:
        import requests
        endpoint = (base_url or "http://localhost:11434/v1").rstrip("/")
        endpoint = endpoint[:-3] if endpoint.endswith("/v1") else endpoint
        response = requests.get(f"{endpoint}/api/tags", timeout=2)
        response.raise_for_status()
        models = response.json().get("models", [])
        result: list[tuple[str, str, int]] = []
        for item in models:
            name = str(item.get("name") or item.get("model") or "").strip()
            if not name:
                continue
            result.append((name, str(item.get("modified_at") or ""), int(item.get("size") or 0)))
        return tuple(sorted(result, key=lambda item: (item[1], item[2], item[0]), reverse=True))
    except Exception:
        return ()


def resolve_ollama_models(config: ProviderConfig) -> tuple[str, str]:
    """Use explicit Ollama models, then the newest installed model inventory."""
    simple = os.environ.get("OLLAMA_MODEL_SIMPLE", "").strip()
    complex_model = os.environ.get("OLLAMA_MODEL_COMPLEX", "").strip()
    installed = discover_ollama_models(config.base_url)
    names = {item[0] for item in installed}
    configured_simple = config.model_simple not in {"", PROVIDER_DEFAULT_MODELS["ollama"][0]}
    configured_complex = config.model_complex not in {"", PROVIDER_DEFAULT_MODELS["ollama"][1]}
    if not simple and configured_simple:
        simple = config.model_simple
    if not complex_model and configured_complex:
        complex_model = config.model_complex
    if installed:
        simple = simple or (config.model_simple if config.model_simple in names else installed[0][0])
        complex_model = complex_model or (config.model_complex if config.model_complex in names else max(installed, key=lambda item: item[2])[0])
    return simple or config.model_simple, complex_model or config.model_complex

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
        kwargs: dict[str, Any] = {
            "api_key": config.api_key or _provider_env_key(config.provider) or "sk-placeholder",
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self._client = OpenAI(**kwargs)

    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        model = model or self.config.model_simple
        started = time.monotonic()

        def _call():
            response = self._client.chat.completions.create(model=model, messages=messages)
            return response

        response = _retry_with_backoff(_call)
        text = response.choices[0].message.content or ""
        usage = getattr(response, "usage", None)
        usage_dict = None
        if usage is not None:
            usage_dict = _openai_usage_dict(usage)
        _track_cost(self.config.provider, usage_dict, model=model,
                    latency_ms=round((time.monotonic() - started) * 1000))
        return text.strip(), usage_dict

    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        model = model or self.config.model_simple
        collected: list[str] = []
        started = time.monotonic()
        final_usage: dict[str, int | None] | None = None
        completed = False

        def _call():
            kwargs: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
            # Only APIs documented to accept OpenAI's stream_options receive it.
            # Fireworks and several compatible providers include usage in their
            # final chunk without this extension; the rest remain compatible.
            if self.config.provider in {"deepseek", "openai", "ollama", "glm", "kimi"}:
                kwargs["stream_options"] = {"include_usage": True}
            return self._client.chat.completions.create(**kwargs)

        stream = _retry_with_backoff(_call)
        try:
            for chunk in stream:
                usage = _openai_usage_dict(getattr(chunk, "usage", None))
                if usage is not None:
                    final_usage = usage
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    collected.append(delta.content)
                    yield delta.content
            completed = True
        finally:
            if completed:
                if final_usage is None:
                    final_usage = {
                        "prompt_tokens": sum(len(str(message.get("content", ""))) for message in messages) // 4,
                        "completion_tokens": len("".join(collected)) // 4,
                        "_estimated": 1,
                    }
                _track_cost(
                    self.config.provider, final_usage, model=model,
                    latency_ms=round((time.monotonic() - started) * 1000),
                )


class OpenAIResponsesProvider(LLMProvider):
    """OpenAI's current Responses API, including event-based streaming."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        try:
            from openai import OpenAI
        except ImportError:
            sys.exit("The 'openai' package is required for OpenAI. Install it with: pip install openai")
        kwargs: dict[str, Any] = {
            "api_key": config.api_key or _provider_env_key(config.provider) or "sk-placeholder",
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self._client = OpenAI(**kwargs)

    @staticmethod
    def _usage(response: Any) -> dict[str, int | None] | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        details = getattr(usage, "output_tokens_details", None)
        return {
            "prompt_tokens": getattr(usage, "input_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
            "reasoning_tokens": getattr(details, "reasoning_tokens", None),
        }

    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        model = model or self.config.model_simple
        started = time.monotonic()

        response = _retry_with_backoff(
            lambda: self._client.responses.create(model=model, input=messages),
        )
        usage = self._usage(response)
        _track_cost(self.config.provider, usage, model=model,
                    latency_ms=round((time.monotonic() - started) * 1000))
        return str(getattr(response, "output_text", "") or "").strip(), usage

    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        model = model or self.config.model_simple
        started = time.monotonic()
        collected: list[str] = []
        final_usage: dict[str, int | None] | None = None
        completed = False

        stream = _retry_with_backoff(
            lambda: self._client.responses.create(model=model, input=messages, stream=True),
        )
        try:
            for event in stream:
                event_type = str(getattr(event, "type", ""))
                usage = self._usage(getattr(event, "response", None) or event)
                if usage is not None:
                    final_usage = usage
                if event_type in {"response.output_text.delta", "response.text.delta"}:
                    delta = str(getattr(event, "delta", "") or "")
                else:
                    delta = str(getattr(event, "text", "") or "") if event_type.endswith("text.delta") else ""
                if delta:
                    collected.append(delta)
                    yield delta
            completed = True
        finally:
            if completed:
                if final_usage is None:
                    final_usage = {
                        "prompt_tokens": sum(len(str(item.get("content", ""))) for item in messages) // 4,
                        "completion_tokens": len("".join(collected)) // 4,
                        "_estimated": 1,
                    }
                _track_cost(self.config.provider, final_usage, model=model,
                            latency_ms=round((time.monotonic() - started) * 1000))

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
        _track_cost(self.config.provider, usage_dict, model=model,
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
                _track_cost(self.config.provider, final_usage, model=model,
                            latency_ms=round((time.monotonic() - started) * 1000))

# ---------------------------------------------------------------------------
# Google (Gemini)
# ---------------------------------------------------------------------------

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
        contents: list[dict[str, Any]] = []
        system: list[str] = []
        for message in messages:
            role = message.get("role", "user")
            text = str(message.get("content", ""))
            if role == "system":
                system.append(text)
                continue
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
        text = str(getattr(response, "text", "") or "")
        usage_dict = self._usage(response)
        _track_cost(self.config.provider, usage_dict, model=model,
                    latency_ms=round((time.monotonic() - started) * 1000))
        return text.strip(), usage_dict

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
                _track_cost(self.config.provider, final_usage, model=model,
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


class AzureOpenAIProvider(OpenAICompatProvider):
    """Azure OpenAI v1 Chat Completions; model is the deployment name."""

    def __init__(self, config: ProviderConfig) -> None:
        LLMProvider.__init__(self, config)
        try:
            from openai import OpenAI
        except ImportError:
            sys.exit("The 'openai' package is required for Azure OpenAI. Install it with: pip install openai")
        endpoint = (config.base_url or os.environ.get("AZURE_OPENAI_ENDPOINT", "")).rstrip("/")
        base_url = endpoint if endpoint.endswith("/openai/v1") else f"{endpoint}/openai/v1/"
        kwargs: dict[str, Any] = {
            "api_key": config.api_key or os.environ.get("AZURE_OPENAI_API_KEY", "") or "azure-placeholder",
            "base_url": base_url,
        }
        if not config.api_key and not os.environ.get("AZURE_OPENAI_API_KEY"):
            try:
                from azure.identity import DefaultAzureCredential, get_bearer_token_provider
                kwargs["api_key"] = get_bearer_token_provider(
                    DefaultAzureCredential(exclude_interactive_browser_credential=True),
                    "https://cognitiveservices.azure.com/.default",
                )
            except ImportError:
                pass
        self._client = OpenAI(**kwargs)


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
        _track_cost(self.config.provider, usage, model=model,
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
                _track_cost(self.config.provider, final_usage, model=model,
                            latency_ms=round((time.monotonic() - started) * 1000))


class BedrockProvider(LLMProvider):
    """Amazon Bedrock Converse adapter using the default boto3 credential chain."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        try:
            import boto3
        except ImportError:
            sys.exit("The 'boto3' package is required for Bedrock. Install it with: pip install boto3")
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
        )

    @staticmethod
    def _request(messages: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        system: list[dict[str, str]] = []
        conversation: list[dict[str, Any]] = []
        for item in messages:
            if item.get("role") == "system":
                system.append({"text": str(item.get("content", ""))})
            else:
                conversation.append({
                    "role": "assistant" if item.get("role") == "assistant" else "user",
                    "content": [{"text": str(item.get("content", ""))}],
                })
        return conversation, system

    @staticmethod
    def _usage(data: dict[str, Any] | None) -> dict[str, int | None] | None:
        usage = (data or {}).get("usage")
        if not usage:
            return None
        return {
            "prompt_tokens": usage.get("inputTokens", 0) or 0,
            "completion_tokens": usage.get("outputTokens", 0) or 0,
        }

    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        model = model or self.config.model_simple
        conversation, system = self._request(messages)
        started = time.monotonic()
        kwargs: dict[str, Any] = {
            "modelId": model,
            "messages": conversation,
            "inferenceConfig": {"maxTokens": 4096},
        }
        if system:
            kwargs["system"] = system
        response = _retry_with_backoff(lambda: self._client.converse(**kwargs))
        content = response.get("output", {}).get("message", {}).get("content", [])
        text = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        usage = self._usage(response)
        _track_cost(self.config.provider, usage, model=model,
                    latency_ms=round((time.monotonic() - started) * 1000))
        return text.strip(), usage

    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        model = model or self.config.model_simple
        conversation, system = self._request(messages)
        started = time.monotonic()
        kwargs: dict[str, Any] = {
            "modelId": model,
            "messages": conversation,
            "inferenceConfig": {"maxTokens": 4096},
        }
        if system:
            kwargs["system"] = system
        response = _retry_with_backoff(lambda: self._client.converse_stream(**kwargs))
        final_usage: dict[str, int | None] | None = None
        completed = False
        try:
            for event in response.get("stream", []):
                if "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"].get("delta", {}).get("text", "")
                    if delta:
                        yield str(delta)
                if "metadata" in event:
                    final_usage = self._usage(event["metadata"])
            completed = True
        finally:
            if completed:
                _track_cost(self.config.provider, final_usage, model=model,
                            latency_ms=round((time.monotonic() - started) * 1000))

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
            _track_cost("ollama", usage_dict, model=model,
                        latency_ms=round((time.monotonic() - started) * 1000))
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
                model_simple=PROVIDER_DEFAULT_MODELS.get(fb_name, ("", ""))[0],
                model_complex=PROVIDER_DEFAULT_MODELS.get(fb_name, ("", ""))[1],
            )
            # Only add fallback if it has an API key or is Ollama
            if provider_is_configured(fb_config):
                try:
                    self._fallbacks.append(get_provider(fb_config))
                except Exception:
                    pass

    @property
    def config(self) -> ProviderConfig:
        return self._primary.config

    def _model_for(self, provider: LLMProvider, requested_model: str | None) -> str:
        """Choose a model that has the same simple/complex meaning for *provider*.

        The model names in a provider configuration are provider-specific.  A
        model selected from the primary provider must therefore not be sent to
        a fallback merely because it is a non-empty string.  Names configured
        for the primary (including its built-in defaults) are treated as
        semantic simple/complex slots and mapped to the fallback's slots.
        An unrecognised explicit name is forwarded as-is so users can use a
        model name shared by several compatible endpoints, with the fallback
        provider responsible for accepting or rejecting it.
        """
        primary_config = self._primary.config
        target_config = provider.config
        requested = (requested_model or "").strip()

        if not requested or requested.lower() == "auto":
            return target_config.model_simple

        primary_simple = {
            primary_config.model_simple,
            PROVIDER_DEFAULT_MODELS.get(primary_config.provider, ("", ""))[0],
        }
        primary_complex = {
            primary_config.model_complex,
            PROVIDER_DEFAULT_MODELS.get(primary_config.provider, ("", ""))[1],
        }
        if requested in primary_complex and requested not in primary_simple:
            return target_config.model_complex
        if requested in primary_simple:
            return target_config.model_simple
        return requested

    @staticmethod
    def _raise_all_failed(attempts: list[tuple[str, Exception]]) -> None:
        """Raise a useful error without discarding the primary failure."""
        if not attempts:
            raise RuntimeError("All providers failed")
        if len(attempts) == 1:
            raise attempts[0][1]
        details = "; ".join(
            f"{provider}: {type(error).__name__}: {error}"
            for provider, error in attempts
        )
        error = RuntimeError(f"All providers failed ({details})")
        raise error from attempts[0][1]

    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        providers = [self._primary] + self._fallbacks
        attempts: list[tuple[str, Exception]] = []
        for prov in providers:
            try:
                return prov.chat(messages, self._model_for(prov, model))
            except Exception as e:
                attempts.append((prov.name, e))
        self._raise_all_failed(attempts)

    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        providers = [self._primary] + self._fallbacks
        attempts: list[tuple[str, Exception]] = []
        for prov in providers:
            try:
                yield from prov.chat_stream(messages, self._model_for(prov, model))
                return
            except Exception as e:
                attempts.append((prov.name, e))
        self._raise_all_failed(attempts)

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
        context_window_tokens=context_window_tokens,
    )


def save_provider_config(config: ProviderConfig) -> None:
    """Save provider settings using the encrypted configuration path."""
    save_provider_config_encrypted(config)


# ---------------------------------------------------------------------------
# API key encryption at rest
# ---------------------------------------------------------------------------

def _get_encryption_key() -> bytes:
    """Derive a machine-specific encryption key from hostname + platform."""
    import hashlib, platform, socket
    seed = f"{socket.gethostname()}:{platform.node()}:openkyrozen"
    return hashlib.sha256(seed.encode()).digest()


def _get_fernet(*, create: bool = True):
    """Return the per-install Fernet cipher, optionally without creating it."""
    from cryptography.fernet import Fernet

    secret_path = os.path.expanduser("~/.kyrozen_secret")
    try:
        with open(secret_path, "rb") as f:
            key = f.read().strip()
    except FileNotFoundError:
        if not create:
            return None
        key = Fernet.generate_key()
        try:
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            with open(secret_path, "rb") as f:
                key = f.read().strip()
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(key)
    os.chmod(secret_path, 0o600)
    return Fernet(key)

def encrypt_api_key(plaintext: str) -> str:
    """Encrypt an API key with a random per-install Fernet key."""
    if not plaintext:
        return ""
    return "v2:" + _get_fernet().encrypt(plaintext.encode()).decode()

def decrypt_api_key(ciphertext: str) -> str:
    """Decrypt a Fernet key, with backward-compatible support for legacy XOR."""
    import base64
    if not ciphertext:
        return ""
    if ciphertext.startswith("v2:"):
        try:
            fernet = _get_fernet(create=False)
            return fernet.decrypt(ciphertext[3:].encode()).decode() if fernet else ""
        except Exception:
            return ""
    try:
        # Legacy configs used reversible XOR with a machine-derived key.
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
    if config.context_window_tokens is None:
        existing.pop("context_window_tokens", None)
    else:
        existing["context_window_tokens"] = config.context_window_tokens
    existing["encrypted"] = True
    existing["encryption"] = "fernet"
    try:
        with open(config_path, "w") as f:
            json.dump(existing, f, indent=2)
        os.chmod(config_path, 0o600)
    except OSError:
        pass
