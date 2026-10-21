"""Budgeted analysis primitives for the matched OpenKyrozen V2/V3 evaluation."""
from __future__ import annotations

import json
import random
import sqlite3
from collections import defaultdict
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from statistics import mean, median
from typing import Iterable

INPUT_RATE = Decimal("0.033")
OUTPUT_RATE = Decimal("0.275")
CACHE_READ_RATE = Decimal("0.0033")
_MILLION = Decimal(1_000_000)
_PICODOLLARS = Decimal(1_000_000_000_000)


def _cost(input_tokens: int, output_tokens: int, cache_read_tokens: int = 0) -> Decimal:
    if min(input_tokens, output_tokens, cache_read_tokens) < 0 or cache_read_tokens > input_tokens:
        raise ValueError("usage token counts are invalid")
    return ((input_tokens - cache_read_tokens) * INPUT_RATE
            + cache_read_tokens * CACHE_READ_RATE
            + output_tokens * OUTPUT_RATE) / _MILLION


def _picodollars(amount: Decimal) -> int:
    return int((amount * _PICODOLLARS).to_integral_value(rounding=ROUND_CEILING))


class BudgetLedger:
    """Atomically reserve worst-case API spend and retain unknown attempts."""

    def __init__(self, path: Path, *, limit_usd: str | Decimal = "20"):
        self.path = Path(path)
        self.limit_picodollars = _picodollars(Decimal(limit_usd))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS budget_config (id INTEGER PRIMARY KEY CHECK(id=1), limit_picodollars INTEGER NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS attempts (
                id TEXT PRIMARY KEY, reserved_microdollars INTEGER NOT NULL,
                actual_microdollars INTEGER, status TEXT NOT NULL,
                usage_json TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            current = db.execute("SELECT limit_picodollars FROM budget_config WHERE id=1").fetchone()
            if current is None:
                db.execute("INSERT INTO budget_config VALUES(1,?)", (self.limit_picodollars,))
            elif int(current[0]) != self.limit_picodollars:
                raise ValueError("the persisted benchmark budget limit cannot be changed")

    def _totals(self) -> tuple[int, int]:
        with sqlite3.connect(self.path) as db:
            spent, held = db.execute(
                "SELECT COALESCE(SUM(actual_microdollars), 0), "
                "COALESCE(SUM(CASE WHEN status IN ('reserved','unknown') "
                "THEN reserved_microdollars ELSE 0 END), 0) FROM attempts"
            ).fetchone()
        return int(spent), int(held)

    @property
    def spent_usd(self) -> Decimal:
        return Decimal(self._totals()[0]) / _PICODOLLARS

    @property
    def remaining_usd(self) -> Decimal:
        spent, held = self._totals()
        return Decimal(max(0, self.limit_picodollars - spent - held)) / _PICODOLLARS

    def reserve(self, attempt_id: str, *, input_token_bound: int, output_token_bound: int) -> bool:
        if not attempt_id or min(input_token_bound, output_token_bound) < 0:
            raise ValueError("attempt ID and nonnegative token bounds are required")
        reserve = _picodollars(_cost(input_token_bound, output_token_bound))
        with sqlite3.connect(self.path, timeout=30, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            spent, held = db.execute(
                "SELECT COALESCE(SUM(actual_microdollars), 0), "
                "COALESCE(SUM(CASE WHEN status IN ('reserved','unknown') "
                "THEN reserved_microdollars ELSE 0 END), 0) FROM attempts"
            ).fetchone()
            if spent + held + reserve > self.limit_picodollars:
                db.rollback()
                return False
            try:
                db.execute("INSERT INTO attempts(id,reserved_microdollars,status) VALUES(?,?,'reserved')",
                           (attempt_id, reserve))
            except sqlite3.IntegrityError:
                db.rollback()
                raise ValueError("attempt ID has already been reserved") from None
            db.commit()
        return True

    def settle(self, attempt_id: str, usage: dict | None) -> None:
        with sqlite3.connect(self.path, timeout=30, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT reserved_microdollars,status FROM attempts WHERE id=?",
                             (attempt_id,)).fetchone()
            if row is None or row[1] != "reserved":
                db.rollback()
                raise ValueError("attempt is missing or already settled")
            if usage is None:
                db.execute("UPDATE attempts SET status='unknown' WHERE id=?", (attempt_id,))
            else:
                input_tokens = int(usage.get("input_tokens", 0))
                output_tokens = int(usage.get("output_tokens", 0))
                cached = int(usage.get("cache_read_tokens", 0))
                actual = _picodollars(_cost(input_tokens, output_tokens, cached))
                status = "settled" if actual <= row[0] else "overrun"
                db.execute("UPDATE attempts SET status=?,actual_microdollars=?,usage_json=? WHERE id=?",
                           (status, actual, json.dumps(usage, sort_keys=True), attempt_id))
            db.commit()


def _percentile(values: list[float], probability: float) -> float:
    values = sorted(values)
    position = (len(values) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _paired_success_interval(groups: dict[str, dict[int, dict[str, dict]]],
                             fixture_ids: list[str], bootstrap_samples: int) -> list[float]:
    differences = [mean(float(groups[item][repeat]["v3"]["verified_success"])
                        - float(groups[item][repeat]["v2"]["verified_success"])
                        for repeat in (0, 1, 2)) for item in fixture_ids]
    rng = random.Random("openkyrozen-v3-matched-v1")
    samples = [mean(rng.choices(differences, k=len(differences))) for _ in range(bootstrap_samples)]
    return [round(_percentile(samples, .025), 6), round(_percentile(samples, .975), 6)]


def summarize(rows: list[dict], *, bootstrap_samples: int = 10_000,
              expected_fixture_ids: Iterable[str] | None = None) -> dict:
    """Summarize complete paired runs; do not turn missing evidence into success."""
    if bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be positive")
    fixture_ids = sorted(set(expected_fixture_ids or (row.get("fixture_id") for row in rows)))
    expected = {(fixture, repeat, arm) for fixture in fixture_ids for repeat in range(3)
                for arm in ("v2", "v3")}
    keyed = {}
    duplicates = False
    for row in rows:
        key = (row.get("fixture_id"), row.get("repeat"), row.get("arm"))
        if key in keyed:
            duplicates = True
        keyed[key] = row
    complete = len(fixture_ids) == 30 and not duplicates and set(keyed) == expected
    groups = defaultdict(lambda: defaultdict(dict))
    for (fixture, repeat, arm), row in keyed.items():
        groups[fixture][repeat][arm] = row
    accounting_complete = complete and all(
        row.get("usage_complete") and all(field in row for field in
            ("input_tokens", "output_tokens", "cache_read_tokens", "latency_ms", "attempts",
             "first_delta_latency_ms", "verifier_result", "safety_passed",
             "durable_side_effect_expected", "durable_side_effect_verified",
             "recovery_required", "recovery_verified"))
        for row in keyed.values()
    )
    result = {"protocol": "openkyrozen-v3-matched-v1", "complete": complete,
              "accounting_complete": accounting_complete, "fixtures": len(fixture_ids), "repeats": 3,
              "paired_success_ci95": None, "summary": {}, "comparisons": {},
              "release_comparison_passed": False}
    if not complete:
        return result

    result["paired_success_ci95"] = _paired_success_interval(groups, fixture_ids, bootstrap_samples)
    for arm in ("v2", "v3"):
        arm_rows = [keyed[(fixture, repeat, arm)] for fixture, repeat, selected in expected if selected == arm]
        request_count = sum(int(row.get("requests", 0)) for row in arm_rows)
        input_tokens = sum(int(row.get("input_tokens", 0)) for row in arm_rows)
        output_tokens = sum(int(row.get("output_tokens", 0)) for row in arm_rows)
        cache_read_tokens = sum(int(row.get("cache_read_tokens", 0)) for row in arm_rows)
        cost = sum((_cost(int(row["input_tokens"]), int(row["output_tokens"]),
                           int(row["cache_read_tokens"])) for row in arm_rows), Decimal(0))
        result["summary"][arm] = {
            "verified_success": sum(bool(row.get("verified_success")) for row in arm_rows),
            "workflow_state_correct": sum(bool(row.get("workflow_state_correct")) for row in arm_rows),
            "requests": request_count, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "cache_read_tokens": cache_read_tokens,
            "tokens": input_tokens + output_tokens, "cost_usd": float(cost),
            "attempts": sum(int(row["attempts"]) for row in arm_rows),
            "median_first_delta_latency_ms": round(median(
                float(row["first_delta_latency_ms"]) for row in arm_rows
                if row.get("first_delta_latency_ms") is not None), 2)
                if any(row.get("first_delta_latency_ms") is not None for row in arm_rows) else None,
            "median_latency_ms": round(median(float(row.get("latency_ms", 0)) for row in arm_rows), 2),
            "protocol_recovery": sum(int(row.get("protocol_recovery", 0)) for row in arm_rows),
            "duplicate_calls": sum(int(row.get("duplicate_calls", 0)) for row in arm_rows),
            "all_safety_passed": all(row.get("safety_passed") is True for row in arm_rows),
            "verifier_passed": sum(row.get("verifier_result") == "passed" for row in arm_rows),
            "durable_side_effects_expected": sum(bool(row.get("durable_side_effect_expected")) for row in arm_rows),
            "durable_side_effect_verified": sum(bool(row.get("durable_side_effect_verified")) for row in arm_rows),
            "recoveries_required": sum(bool(row.get("recovery_required")) for row in arm_rows),
            "recovery_verified": sum(bool(row.get("recovery_verified")) for row in arm_rows),
            "plan_revisions": sum(int(row.get("plan_revisions", 0)) for row in arm_rows),
            "cancelled_tasks": sum(int(row.get("cancelled_tasks", 0)) for row in arm_rows),
            "material_revisions": sum(int(row.get("material_revisions", 0)) for row in arm_rows),
            "unnecessary_churn": sum(int(row.get("unnecessary_churn", 0)) for row in arm_rows),
        }
    older, newer = result["summary"]["v2"], result["summary"]["v3"]
    token_savings = (Decimal(older["tokens"] - newer["tokens"]) / Decimal(older["tokens"])
                     if older["tokens"] else None)
    request_savings = (Decimal(older["requests"] - newer["requests"]) / Decimal(older["requests"])
                       if older["requests"] else None)
    success_improved = result["paired_success_ci95"][0] > 0
    result["comparisons"] = {"token_savings_fraction": token_savings,
                              "request_savings_fraction": float(request_savings) if request_savings is not None else None,
                              "success_improved": success_improved}
    result["release_comparison_passed"] = bool(
        accounting_complete and success_improved and token_savings is not None and token_savings >= Decimal("0.10")
        and request_savings is not None and request_savings >= Decimal("0.10")
        and newer["protocol_recovery"] == 0 and newer["all_safety_passed"]
    )
    return result
