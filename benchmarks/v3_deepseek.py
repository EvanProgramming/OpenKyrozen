"""DeepSeek matched evaluation with a genuine cumulative 15 CNY cap.

No credential files are consulted. Paid execution is explicit through this CLI;
importing this module and reconstructing old ledgers are offline operations.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
import json
import hashlib
from pathlib import Path
import sqlite3
import secrets

from benchmarks.v3_matched import BudgetLedger, Pricing, _picormb
from benchmarks.v3_responses_relay import RelayConfig

DEEPSEEK_ENDPOINT = "https://api.deepseek.com"
DEEPSEEK_PRICING = Pricing(
    input_rate=Decimal("2"), output_rate=Decimal("8"),
    cache_read_rate=Decimal("0.04"), cache_write_rate=Decimal("2"),
    max_limit_cny=Decimal("15"),
    metadata_json=json.dumps({
        "currency": "CNY", "source_currency": "CNY", "provider": "DeepSeek official",
        "requested_model": "deepseek-flash", "served_model": "DeepSeek-V4.1-Flash-1017",
        "pricing_source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        "input_per_million": "2", "output_per_million": "8",
        "cache_read_per_million": "0.04", "cache_write_per_million": "2",
        "reservation": "peak rates, 1M input tokens and 4096 output tokens per call",
    }, sort_keys=True),
)
DEEPSEEK_RELAY_CONFIG = RelayConfig(
    model="deepseek-flash", max_output_tokens=4096,
    fixed_input_token_bound=1_000_000, serial_requests=True,
)
# The previous campaign used the same CNY prices under this retired model alias.
_LEGACY_PRICING_SHA256 = "70cf92ad796b1061598eb1d5ac16aa19d473299758bcfdde86f789b2d40aabc4"


@dataclass(frozen=True)
class PriorExposure:
    settled_cny: Decimal
    held_cny: Decimal
    attempt_count: int
    records: tuple[str, ...] = ()

    @property
    def total_cny(self) -> Decimal:
        return self.settled_cny + self.held_cny


def read_prior_exposure(paths: list[Path] | tuple[Path, ...]) -> PriorExposure:
    """Reconstruct old costs read-only; duplicate journal attempts count once."""
    if not paths:
        raise ValueError("at least one original prior campaign ledger is required")
    seen: dict[str, str] = {}
    amounts: dict[str, tuple[int, int]] = {}
    sources: list[tuple[set[str], list[set[str]]]] = []
    opaque_anchors: list[set[str]] = []

    def add(record: dict) -> tuple[int, int]:
        attempt_id = record.get("id")
        reserved, actual = record.get("reserved"), record.get("actual")
        status, usage = record.get("status"), record.get("usage")
        if (not isinstance(attempt_id, str) or not attempt_id or type(reserved) is not int
                or reserved < 0 or status not in {"settled", "reserved", "unknown"}):
            raise ValueError("invalid or overrun attempt in prior campaign ledger")
        normalized = json.dumps({"id": attempt_id, "reserved": reserved, "actual": actual,
                                 "status": status, "usage": usage}, sort_keys=True)
        if attempt_id in seen:
            if seen[attempt_id] != normalized:
                raise ValueError("conflicting copies of a prior campaign attempt")
            return amounts[attempt_id]
        if status == "settled":
            if type(actual) is not int or actual < 0 or actual > reserved or not isinstance(usage, dict):
                raise ValueError("settled prior attempt lacks valid usage accounting")
            required = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
            if any(field not in usage for field in required):
                raise ValueError("settled prior attempt has incomplete usage accounting")
            reconstructed = _picormb(DEEPSEEK_PRICING.cost(*(usage[field] for field in required)))
            if actual != reconstructed:
                raise ValueError("prior attempt charge differs from reconstructed CNY usage")
            amount = (reconstructed, 0)
        else:
            if actual is not None or usage is not None:
                raise ValueError("unsettled prior attempts must retain their full reservation")
            amount = (0, reserved)
        seen[attempt_id], amounts[attempt_id] = normalized, amount
        return amount

    try:
        for path in paths:
            with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
                db.execute("PRAGMA query_only=ON")
                config = db.execute("SELECT currency_code,pricing_sha256 FROM budget_config WHERE id=1").fetchone()
                if config not in {("CNY", _LEGACY_PRICING_SHA256), ("CNY", DEEPSEEK_PRICING.sha256)}:
                    raise ValueError("prior ledger does not pin the approved DeepSeek CNY prices")
                attempts = db.execute(
                    "SELECT id,reserved_picormb,actual_picormb,status,usage_json,metadata_json FROM attempts"
                ).fetchall()
            source_ids: set[str] = set()
            manifests: list[set[str]] = []
            direct_opaque = False
            for attempt_id, reserved, actual, status, usage_json, metadata_json in attempts:
                metadata = json.loads(metadata_json) if metadata_json else {}
                if attempt_id == "prior-campaign-reservations" and "prior_attempts" in metadata:
                    manifest = metadata["prior_attempts"]
                    if (status != "unknown" or actual is not None or usage_json is not None
                            or not isinstance(manifest, list) or not manifest
                            or hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
                            != metadata.get("prior_attempts_sha256")):
                        raise ValueError("carried prior campaign provenance is invalid")
                    represented = [json.loads(item) for item in manifest]
                    if len({item["id"] for item in represented}) != len(represented):
                        raise ValueError("carried prior campaign contains duplicate attempts")
                    manifest_ids = {item["id"] for item in represented}
                    manifests.append(manifest_ids)
                    source_ids.update(manifest_ids)
                    if "prior-campaign-reservations" in manifest_ids:
                        opaque_anchors.append(manifest_ids)
                    reconstructed = sum(sum(add(item)) for item in represented)
                    if reconstructed != reserved:
                        raise ValueError("carried prior campaign hold differs from its underlying attempts")
                    continue
                add({"id": attempt_id, "reserved": reserved, "actual": actual,
                     "status": status, "usage": json.loads(usage_json) if usage_json else None})
                source_ids.add(attempt_id)
                direct_opaque = direct_opaque or attempt_id == "prior-campaign-reservations"
            sources.append((source_ids, manifests))
            if direct_opaque:
                opaque_anchors.append(source_ids)
    except (sqlite3.Error, OSError, TypeError, KeyError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("cannot reconstruct the required prior campaign ledger") from exc
    # Opaque holds can accompany copies or proven descendants, not unrelated
    # source histories whose costs may already be included in that aggregate.
    for anchor in opaque_anchors:
        if any(not source <= anchor and not any(anchor <= manifest for manifest in manifests)
               for source, manifests in sources):
            raise ValueError("ambiguous opaque prior campaign aggregate requires original provenance")
    settled = sum(amount[0] for amount in amounts.values())
    held = sum(amount[1] for amount in amounts.values())
    return PriorExposure(Decimal(settled) / 1_000_000_000_000,
                         Decimal(held) / 1_000_000_000_000, len(seen),
                         tuple(seen[key] for key in sorted(seen)))


def create_budget(path: Path, *, prior_ledgers: list[Path] | tuple[Path, ...],
                  limit_cny: str | Decimal = "15") -> BudgetLedger:
    exposure = read_prior_exposure(prior_ledgers)
    return BudgetLedger(path, limit_rmb=limit_cny, prior_hold_rmb=exposure.total_cny,
                        pricing=DEEPSEEK_PRICING, prior_hold_records=exposure.records)


def run_smoke(relay, candidate: str) -> bool:
    """Verify typed tool continuation through the same metered campaign relay."""
    if getattr(relay, "smoke_verified_candidate", None) == candidate:
        return True
    import requests
    probe = secrets.token_hex(16)
    tools = [{"type": "function", "name": "echo_probe", "description": "Echo a benchmark probe value.",
              "parameters": {"type": "object", "properties": {
                  "value": {"type": "string", "enum": [probe]}},
                  "required": ["value"], "additionalProperties": False}, "strict": False}]
    history = [{"role": "user", "content":
                "Call echo_probe with the allowed value, then repeat the returned value exactly."}]
    relay.label = ("smoke", candidate, -1)
    attempt_ids = []

    def request(body: dict) -> dict | None:
        before = len(relay.rows)
        response = requests.post(relay.url + "/responses", json=body,
                                 headers={"Authorization": "Bearer " + relay.client_key}, timeout=190)
        if (response.status_code != 200 or len(relay.rows) != before + 1
                or relay.rows[-1].get("status") != "settled"
                or not isinstance(relay.rows[-1].get("usage"), dict) or not relay.budget.budget_ok):
            if not relay.budget_blocked and not relay.provider_error:
                relay.provider_error = "smoke_incomplete_usage"
            return None
        attempt_ids.append(relay.rows[-1]["attempt_id"])
        result = response.json()
        return result if isinstance(result, dict) else None

    try:
        first = request({"input": history, "tools": tools,
                         "tool_choice": "auto",
                         "parallel_tool_calls": False, "stream": False})
        if first is None:
            return False
        output = first.get("output")
        calls = [item for item in output if isinstance(item, dict)
                 and item.get("type") == "function_call"] if isinstance(output, list) else []
        if len(calls) != 1:
            relay.provider_error = "smoke_invalid_tool_call"
            return False
        call = calls[0]
        if (call.get("name") != "echo_probe" or not isinstance(call.get("call_id"), str)
                or not call["call_id"].strip() or not isinstance(call.get("arguments"), str)):
            relay.provider_error = "smoke_invalid_tool_call"
            return False
        arguments = json.loads(call["arguments"])
        if arguments != {"value": probe}:
            relay.provider_error = "smoke_invalid_tool_arguments"
            return False
        final = request({"input": history + output + [{"type": "function_call_output",
                         "call_id": call["call_id"], "output": json.dumps({"value": probe})}],
                         "tools": tools, "tool_choice": "none", "stream": False})
        if final is None:
            return False
        final_output = final.get("output")
        messages = final_output if isinstance(final_output, list) else []
        text = "".join(content.get("text", "") for item in messages if isinstance(item, dict)
                       and item.get("type") == "message" and item.get("role") == "assistant"
                       and isinstance(item.get("content"), list)
                       for content in item["content"] if isinstance(content, dict)
                       and content.get("type") == "output_text" and isinstance(content.get("text"), str))
        if (probe not in text or any(isinstance(item, dict) and item.get("type") == "function_call"
                                    for item in messages)):
            relay.provider_error = "smoke_invalid_continuation"
            return False
    except (requests.RequestException, ValueError):
        if not relay.provider_error:
            relay.provider_error = "smoke_invalid_response"
        return False
    relay.smoke_verified_candidate = candidate
    relay.smoke_verified_attempt_ids = tuple(attempt_ids)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-db", type=Path, required=True)
    parser.add_argument("--prior-ledger", type=Path, action="append", required=True)
    parser.add_argument("--limit-cny", default="15")
    args = parser.parse_args()
    from benchmarks.v3_matched_live import _source_snapshot, run
    from scripts.publish_v3_preview import _verify_candidate_signature
    if _source_snapshot(args.source.resolve()) != args.candidate_sha:
        raise ValueError("source does not match the configured frozen candidate")
    _verify_candidate_signature(args.source.resolve(), args.candidate_sha)
    exposure = read_prior_exposure(args.prior_ledger)
    # Validate the absolute cap before starting the runner; it probes protection before reading the key.
    limit = Decimal(args.limit_cny)
    if not limit.is_finite() or not 0 < limit <= 15 or exposure.total_cny > limit:
        raise ValueError("the cumulative DeepSeek cap must not exceed 15 CNY")
    report = run(source=args.source, repo=args.repo, output=args.output, budget_path=args.budget_db,
                 limit_cny=str(limit), prior_hold_cny=str(exposure.total_cny),
                 prior_hold_records=exposure.records,
                 pricing=DEEPSEEK_PRICING, relay_config=DEEPSEEK_RELAY_CONFIG,
                 endpoint=DEEPSEEK_ENDPOINT)
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    summary = report["summary"]
    raise SystemExit(0 if summary["complete"] and summary["accounting_complete"]
                     and summary["comparison_thresholds_met"] else 1)


if __name__ == "__main__":
    main()
