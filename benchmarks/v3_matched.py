"""Budgeted analysis primitives for the matched OpenKyrozen V2/V3 evaluation."""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from collections import defaultdict
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from statistics import mean, median
from benchmarks.v3_fixtures import FIXTURES

USD_CNY = Decimal("6.6759")  # Wise USD/CNY rate, 2026-10-22.
INPUT_RATE = Decimal("0.033") * USD_CNY
OUTPUT_RATE = Decimal("0.275") * USD_CNY
CACHE_WRITE_RATE = INPUT_RATE  # No separate cache-write rate is quoted by Panstar.
CACHE_READ_RATE = Decimal("0.0033") * USD_CNY
MAX_LIMIT_USD = Decimal("20")
MAX_LIMIT_RMB = MAX_LIMIT_USD * USD_CNY
_MILLION = Decimal(1_000_000)
_PICORMB = Decimal(1_000_000_000_000)
PRICING = {"currency": "CNY", "source_currency": "USD", "usd_cny": str(USD_CNY),
           "fx_source": "Wise USD/CNY historical rate 2026-10-22",
           "input_usd_per_million": "0.033", "output_usd_per_million": "0.275",
           "cache_write_usd_per_million": "same_as_input", "cache_read_usd_per_million": "0.0033",
           "input_per_million": str(INPUT_RATE),
           "output_per_million": str(OUTPUT_RATE), "cache_write_per_million": str(CACHE_WRITE_RATE),
           "cache_read_per_million": str(CACHE_READ_RATE)}
PRICING_SHA256 = hashlib.sha256(json.dumps(PRICING, sort_keys=True).encode()).hexdigest()


def _cost(input_tokens: int, output_tokens: int, cache_read_tokens: int = 0,
          cache_write_tokens: int = 0) -> Decimal:
    if (min(input_tokens, output_tokens, cache_read_tokens, cache_write_tokens) < 0
            or cache_read_tokens + cache_write_tokens > input_tokens):
        raise ValueError("usage token counts are invalid")
    return ((input_tokens - cache_read_tokens - cache_write_tokens) * INPUT_RATE
            + cache_write_tokens * CACHE_WRITE_RATE
            + cache_read_tokens * CACHE_READ_RATE
            + output_tokens * OUTPUT_RATE) / _MILLION


def _picormb(amount: Decimal) -> int:
    return int((amount * _PICORMB).to_integral_value(rounding=ROUND_CEILING))


class BudgetLedger:
    """Atomically reserve worst-case API spend and retain unknown attempts."""

    def __init__(self, path: Path, *, limit_rmb: str | Decimal = MAX_LIMIT_RMB,
                 prior_hold_rmb: str | Decimal = "0", allow_limit_increase: bool = False):
        self.path = Path(path)
        limit = Decimal(limit_rmb)
        prior_hold = Decimal(prior_hold_rmb)
        if not Decimal(0) < limit <= MAX_LIMIT_RMB or not Decimal(0) <= prior_hold < limit:
            raise ValueError("the cumulative benchmark cap must be at most USD 20 at the recorded exchange rate")
        self.limit_picormb = _picormb(limit)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "budget_config" in tables:
                columns = {row[1] for row in db.execute("PRAGMA table_info(budget_config)")}
                if "limit_picodollars" in columns or "currency_code" not in columns or "pricing_sha256" not in columns:
                    raise ValueError("legacy USD budget ledger cannot be reused under the RMB cap")
            if "attempts" in tables:
                columns = {row[1] for row in db.execute("PRAGMA table_info(attempts)")}
                if "reserved_microdollars" in columns:
                    raise ValueError("legacy USD budget ledger cannot be reused under the RMB cap")
            db.execute("CREATE TABLE IF NOT EXISTS budget_config (id INTEGER PRIMARY KEY CHECK(id=1), currency_code TEXT NOT NULL, limit_picormb INTEGER NOT NULL, pricing_sha256 TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS attempts (
                id TEXT PRIMARY KEY, reserved_picormb INTEGER NOT NULL,
                actual_picormb INTEGER, status TEXT NOT NULL,
                usage_json TEXT, metadata_json TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(attempts)")}
            if "metadata_json" not in columns:
                db.execute("ALTER TABLE attempts ADD COLUMN metadata_json TEXT")
            current = db.execute("SELECT currency_code,limit_picormb,pricing_sha256 FROM budget_config WHERE id=1").fetchone()
            if current is None:
                db.execute("INSERT INTO budget_config VALUES(1,'CNY',?,?)", (self.limit_picormb, PRICING_SHA256))
                if prior_hold:
                    db.execute("INSERT INTO attempts(id,reserved_picormb,status,metadata_json) VALUES(?,?,'unknown',?)",
                               ("prior-campaign-reservations", _picormb(prior_hold),
                                json.dumps({"archived_reservation": True,
                                            "prior_cny_reservations": str(prior_hold)}, sort_keys=True)))
            elif current[0] != "CNY" or current[2] != PRICING_SHA256:
                raise ValueError("the persisted RMB benchmark cap and pricing cannot be changed")
            elif int(current[1]) != self.limit_picormb:
                if not allow_limit_increase or int(current[1]) > self.limit_picormb:
                    raise ValueError("the persisted RMB benchmark cap and pricing cannot be changed")
                db.execute("UPDATE budget_config SET limit_picormb=? WHERE id=1", (self.limit_picormb,))
            if prior_hold and db.execute(
                    "SELECT reserved_picormb FROM attempts WHERE id='prior-campaign-reservations'"
            ).fetchone() != (_picormb(prior_hold),):
                raise ValueError("the persisted ledger is missing the prior campaign reservation")

    def _totals(self) -> tuple[int, int, bool]:
        with sqlite3.connect(self.path) as db:
            spent, held, overruns = db.execute(
                "SELECT COALESCE(SUM(actual_picormb), 0), "
                "COALESCE(SUM(CASE WHEN status IN ('reserved','unknown') "
                "THEN reserved_picormb ELSE 0 END), 0), "
                "SUM(CASE WHEN status='overrun' THEN 1 ELSE 0 END) FROM attempts"
            ).fetchone()
        return int(spent), int(held), bool(overruns)

    @property
    def spent_rmb(self) -> Decimal:
        return Decimal(self._totals()[0]) / _PICORMB

    @property
    def budget_ok(self) -> bool:
        return not self._totals()[2]

    @property
    def remaining_rmb(self) -> Decimal:
        spent, held, overrun = self._totals()
        if overrun:
            return Decimal(0)
        return Decimal(max(0, self.limit_picormb - spent - held)) / _PICORMB

    def reserve(self, attempt_id: str, *, input_token_bound: int, output_token_bound: int,
                metadata: dict | None = None) -> bool:
        if not attempt_id or min(input_token_bound, output_token_bound) < 0:
            raise ValueError("attempt ID and nonnegative token bounds are required")
        reserve = _picormb(_cost(input_token_bound, output_token_bound))
        with sqlite3.connect(self.path, timeout=30, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            spent, held, overruns = db.execute(
                "SELECT COALESCE(SUM(actual_picormb), 0), "
                "COALESCE(SUM(CASE WHEN status IN ('reserved','unknown') "
                "THEN reserved_picormb ELSE 0 END), 0), "
                "SUM(CASE WHEN status='overrun' THEN 1 ELSE 0 END) FROM attempts"
            ).fetchone()
            if overruns or spent + held + reserve > self.limit_picormb:
                db.rollback()
                return False
            try:
                db.execute("INSERT INTO attempts(id,reserved_picormb,status,metadata_json) "
                           "VALUES(?,?,'reserved',?)",
                           (attempt_id, reserve, json.dumps(metadata, sort_keys=True) if metadata is not None else None))
            except sqlite3.IntegrityError:
                db.rollback()
                raise ValueError("attempt ID has already been reserved") from None
            db.commit()
        return True

    def campaign_calls(self) -> list[dict]:
        with sqlite3.connect(self.path) as db:
            attempts = db.execute(
                "SELECT id,reserved_picormb,actual_picormb,status,usage_json,metadata_json "
                "FROM attempts ORDER BY created_at,id"
            ).fetchall()
        calls = []
        for attempt_id, reserved, actual, status, usage_json, metadata_json in attempts:
            if not metadata_json:
                raise ValueError("budget ledger contains an attempt without its campaign journal")
            if status == "reserved":
                self.settle(attempt_id, None)
                status, actual = "unknown", None
            call = json.loads(metadata_json)
            if call.get("archived_reservation"):
                if status not in ("unknown", "settled"):
                    raise ValueError("archived attempts must remain settled or held as unknown")
                continue
            if reserved != _picormb(Decimal(call.get("reserved_rmb", "-1"))):
                raise ValueError("campaign journal reservation does not match budget ledger")
            call.update(attempt_id=attempt_id, status=status,
                        usage=json.loads(usage_json) if usage_json else None,
                        actual_rmb=(str(Decimal(actual) / _PICORMB) if actual is not None else None))
            calls.append(call)
        return calls

    def settle(self, attempt_id: str, usage: dict | None) -> None:
        with sqlite3.connect(self.path, timeout=30, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT reserved_picormb,status FROM attempts WHERE id=?",
                             (attempt_id,)).fetchone()
            if row is None:
                db.rollback()
                raise ValueError("attempt is missing or already settled")
            if row[1] != "reserved":
                if usage is None and row[1] in {"unknown", "settled", "overrun"}:
                    db.rollback()
                    return
                db.rollback()
                raise ValueError("attempt is missing or already settled")
            if usage is None:
                db.execute("UPDATE attempts SET status='unknown' WHERE id=?", (attempt_id,))
            else:
                input_tokens = int(usage.get("input_tokens", 0))
                output_tokens = int(usage.get("output_tokens", 0))
                cached = int(usage.get("cache_read_tokens", 0))
                cache_write = int(usage.get("cache_write_tokens", 0))
                actual = _picormb(_cost(input_tokens, output_tokens, cached, cache_write))
                status = "settled" if actual <= row[0] else "overrun"
                db.execute("UPDATE attempts SET status=?,actual_picormb=?,usage_json=? WHERE id=?",
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


def summarize(rows: list[dict], *, bootstrap_samples: int = 10_000) -> dict:
    """Summarize complete paired runs; do not turn missing evidence into success."""
    if bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be positive")
    fixture_ids = sorted(fixture["fixture_id"] for fixture in FIXTURES)
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
        row.get("usage_complete") and row.get("worker_result_complete") is True
        and isinstance(row.get("worker_exit_code"), int) and all(field in row for field in
            ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "latency_ms", "attempts",
             "first_delta_latency_ms", "verifier_result", "safety_passed",
             "durable_side_effect_expected", "durable_side_effect_verified",
             "recovery_required", "recovery_verified", "verified_success",
             "workflow_state_correct", "requests", "protocol_recovery", "duplicate_calls",
             "plan_revisions", "cancelled_tasks", "material_revisions", "unnecessary_churn",
             "budget_ok", "plan_scenario", "plan_scenario_passed", "worker_result_complete",
             "worker_exit_code"))
        for row in keyed.values()
    )
    result = {"protocol": "openkyrozen-v3-matched-v1", "complete": complete,
              "accounting_complete": accounting_complete, "fixtures": len(fixture_ids), "repeats": 3,
              "paired_success_ci95": None, "summary": {}, "comparisons": {},
              "comparison_thresholds_met": False, "release_comparison_passed": False,
              "release_evidence_status": "blocked: live fixture runner evidence is not attached"}
    if not complete or not accounting_complete:
        return result

    result["paired_success_ci95"] = _paired_success_interval(groups, fixture_ids, bootstrap_samples)
    for arm in ("v2", "v3"):
        arm_rows = [keyed[(fixture, repeat, arm)] for fixture, repeat, selected in expected if selected == arm]
        request_count = sum(int(row.get("requests", 0)) for row in arm_rows)
        input_tokens = sum(int(row.get("input_tokens", 0)) for row in arm_rows)
        output_tokens = sum(int(row.get("output_tokens", 0)) for row in arm_rows)
        cache_read_tokens = sum(int(row.get("cache_read_tokens", 0)) for row in arm_rows)
        cache_write_tokens = sum(int(row.get("cache_write_tokens", 0)) for row in arm_rows)
        cost = sum((_cost(int(row["input_tokens"]), int(row["output_tokens"]),
                           int(row["cache_read_tokens"]), int(row["cache_write_tokens"]))
                    for row in arm_rows), Decimal(0))
        result["summary"][arm] = {
            "verified_success": sum(bool(row.get("verified_success")) for row in arm_rows),
            "workflow_state_correct": sum(bool(row.get("workflow_state_correct")) for row in arm_rows),
            "requests": request_count, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "tokens": input_tokens + output_tokens, "cost_rmb": float(cost),
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
            "plan_scenarios": sum(bool(row.get("plan_scenario")) for row in arm_rows),
            "plan_scenarios_passed": sum(row.get("plan_scenario_passed") is True for row in arm_rows
                                           if row.get("plan_scenario")),
        }
    older, newer = result["summary"]["v2"], result["summary"]["v3"]
    token_savings = (Decimal(older["tokens"] - newer["tokens"]) / Decimal(older["tokens"])
                     if older["tokens"] else None)
    request_savings = (Decimal(older["requests"] - newer["requests"]) / Decimal(older["requests"])
                       if older["requests"] else None)
    success_improved = result["paired_success_ci95"][0] > 0
    verification_complete = all(
        row.get("verifier_result") == "passed"
        and row.get("safety_passed") is True
        and (not row.get("durable_side_effect_expected") or row.get("durable_side_effect_verified") is True)
        and (not row.get("recovery_required") or row.get("recovery_verified") is True)
        for row in keyed.values() if row.get("arm") == "v3"
    )
    result["comparisons"] = {"token_savings_fraction": float(token_savings) if token_savings is not None else None,
                              "request_savings_fraction": float(request_savings) if request_savings is not None else None,
                              "success_improved": success_improved}
    result["comparison_thresholds_met"] = bool(
        accounting_complete and success_improved and token_savings is not None and token_savings >= Decimal("0.10")
        and request_savings is not None and request_savings >= Decimal("0.10")
        and newer["protocol_recovery"] == 0 and verification_complete
        and newer["plan_scenarios_passed"] == newer["plan_scenarios"]
        and all(row["budget_ok"] is True for row in keyed.values())
    )
    # Descriptive rows alone are not independently verified release evidence.
    result["release_comparison_passed"] = False
    result["release_evidence_status"] = "blocked: live fixture runner evidence is not attached"
    return result
