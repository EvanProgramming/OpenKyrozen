from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterator
from openkyrozen.persistence.store import EventStore
from openkyrozen.providers.registry import PROVIDER_COSTS, _DEEPSEEK_V4_ALIASES, _DEEPSEEK_V4_EFFECTIVE_AT, _DEEPSEEK_V4_MODELS, _PICOS_PER_DOLLAR, _TOKENS_PER_MILLION

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
    instant = occurred_at.astimezone(timezone.utc)
    effective_at = _DEEPSEEK_V4_EFFECTIVE_AT
    version = "deepseek-v4-pricing-2026-08-16"
    # Flash price transition: https://api-docs.deepseek.com/news/news260910/
    # Pro remains on its August rate card per the current pricing/changelog.
    flash_effective_at = datetime(2026, 9, 10, 4, tzinfo=timezone.utc)
    if instant >= flash_effective_at and canonical_model in {
        "deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp",
    }:
        canonical_model = "deepseek-flash"
        rates = ("0.006", "0.30", "1.20")
        effective_at = flash_effective_at
        version = "deepseek-flash-pricing-2026-09-10"
    else:
        rates = _DEEPSEEK_V4_MODELS.get(canonical_model)
    if rates is None:
        return None, False
    # State Council 2026 calendar, in China Standard Time:
    # https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm
    holidays_2026 = ((1, 1, 3), (2, 15, 23), (4, 4, 6), (5, 1, 5),
                     (6, 19, 21), (9, 25, 27), (10, 1, 7))
    local_date = instant.astimezone(timezone(timedelta(hours=8))).date()
    holiday = local_date.year == 2026 and any(
        local_date.month == month and start <= local_date.day <= end
        for month, start, end in holidays_2026
    )
    peak_hours = instant.weekday() < 5 and (1 <= instant.hour < 4 or 6 <= instant.hour < 10)
    peak = peak_hours and not holiday
    multiplier = 1 if peak else 0.5
    hit, miss, output = (
        int(Decimal(rate) * Decimal(str(multiplier)) * _PICOS_PER_DOLLAR)
        for rate in rates
    )
    current_schedule = instant >= effective_at and (not peak_hours or local_date.year == 2026)
    return {
        "version": version,
        "source": "https://api-docs.deepseek.com/quick_start/pricing/",
        "currency": "USD",
        "model": canonical_model,
        "priced_at": instant.isoformat(),
        "effective_at": effective_at.isoformat(),
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
