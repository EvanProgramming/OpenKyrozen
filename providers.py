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
import uuid
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

PROVIDER_DEFAULT_MODELS: dict[str, tuple[str, str]] = {
    "deepseek":  ("deepseek-v4-flash", "deepseek-v4-pro"),
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
    cost_picos = None
    if prompt_tokens is not None or completion_tokens is not None:
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
    usage_status = "unknown" if usage is None else (
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
        started = time.monotonic()

        def _call():
            response = self._client.chat.completions.create(model=model, messages=messages)
            return response

        response = _retry_with_backoff(_call)
        text = response.choices[0].message.content or ""
        usage = getattr(response, "usage", None)
        usage_dict = None
        if usage is not None:
            completion_details = getattr(usage, "completion_tokens_details", None)
            usage_dict = {
                "prompt_tokens": usage.prompt_tokens or 0,
                "completion_tokens": usage.completion_tokens or 0,
                "prompt_cache_hit_tokens": getattr(usage, "prompt_cache_hit_tokens", None),
                "prompt_cache_miss_tokens": getattr(usage, "prompt_cache_miss_tokens", None),
                "reasoning_tokens": getattr(completion_details, "reasoning_tokens", None),
            }
        _track_cost(self.config.provider, usage_dict, model=model,
                    latency_ms=round((time.monotonic() - started) * 1000))
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
        started = time.monotonic()

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
        _track_cost(self.config.provider, usage_dict, model=model,
                    latency_ms=round((time.monotonic() - started) * 1000))
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
            _track_cost(self.config.provider, usage_dict, model=model,
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
            ))
        except Exception:
            pass  # non-critical — will encrypt on next explicit save

    return ProviderConfig(
        provider=provider_name,
        api_key=api_key,
        base_url=base_url,
        model_simple=model_simple,
        model_complex=model_complex,
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


def _get_fernet():
    """Return a Fernet cipher backed by a random per-install secret."""
    from cryptography.fernet import Fernet

    secret_path = os.path.expanduser("~/.kyrozen_secret")
    try:
        with open(secret_path, "rb") as f:
            key = f.read().strip()
    except FileNotFoundError:
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
            return _get_fernet().decrypt(ciphertext[3:].encode()).decode()
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
    existing["encrypted"] = True
    existing["encryption"] = "fernet"
    try:
        with open(config_path, "w") as f:
            json.dump(existing, f, indent=2)
        os.chmod(config_path, 0o600)
    except OSError:
        pass
