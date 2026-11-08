"""DeepSeek matched evaluation with a genuine cumulative 15 CNY cap.

No credential files are consulted. Paid execution is explicit through this CLI;
importing this module and reconstructing old ledgers are offline operations.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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


PLANNING_PREFLIGHT_CONTENT = "planning-preflight-ok\n"


def planning_preflight_task() -> dict:
    return {"description": "Read and verify proof.txt", "acceptance": [{
        "criterion": "The complete proof file has the expected marker",
        "verification": {"action": "read_file", "arguments": {"path": "proof.txt"},
                         "expect": {"equals": PLANNING_PREFLIGHT_CONTENT}},
    }]}


def planning_preflight_fixture(candidate: str) -> dict:
    creation = {"complexity": "simple", "expected_revision": 0, "reason": "Verify the planning preflight",
        "operations": [{"op": "add", "ref": "proof", "task": planning_preflight_task()},
                       {"op": "start", "task": "proof"}]}
    return {"fixture_id": candidate, "category": "planning_preflight",
        "prompt": "This is a planning preflight, separate from the formal evaluation. Do not write files or run commands.\n"
        "1. Create and explicitly start the Working Plan with update_plan using this object: "
        + json.dumps(creation, ensure_ascii=False) + "\n"
        "2. Call read_file with {\"path\":\"proof.txt\"}. The explicitly selected task owns this native receipt; "
        "do not add unsupported task_id fields to read_file. The complete content must satisfy the typed equals predicate. "
        "The verified receipt completes the task.\n"
        "3. Confirm using update_plan with the returned actual plan_id, its unchanged current expected_revision, "
        "and operations [{\"op\":\"suggest_complete\",\"task\":\"<returned actual task ID>\"}]. "
        "The pinned current Working Plan state retains these IDs after the read completes it; "
        "if an earlier result is archived, the permitted read_tool_artifact reader can retrieve it. "
        "After the actual completed plan is confirmed, return the file marker as your final answer.",
        "files": {"proof.txt": PLANNING_PREFLIGHT_CONTENT},
        "verifier": {"kind": "answer_contains", "all": [PLANNING_PREFLIGHT_CONTENT.strip()]},
        "plan_tasks": [], "plan_scenario": None, "turns": None,
        "capabilities": ["read", "write"], "context_window_tokens": 1_000_000,
        "output_reserve_tokens": 4096}


def _preflight_arguments(receipt: dict) -> dict:
    arguments = receipt.get("arguments", receipt.get("args"))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return {}
    return arguments if isinstance(arguments, dict) else {}


def planning_preflight_semantics(worker: dict) -> bool:
    """Require actual ordered native receipts and exact durable task identity."""
    if (not isinstance(worker, dict) or not isinstance(worker.get("answer"), str)
            or PLANNING_PREFLIGHT_CONTENT.strip() not in worker["answer"]
            or worker.get("protocol_recovery") != 0 or worker.get("duplicate_calls") != 0
            or worker.get("tasks") != ["succeeded"]):
        return False
    plan = worker.get("working_plan")
    if not isinstance(plan, dict):
        return False
    tasks = plan.get("tasks", [])
    if (plan.get("status") != "completed" or not isinstance(tasks, list) or len(tasks) != 1
            or not isinstance(tasks[0], dict) or tasks[0].get("status") != "succeeded"):
        return False
    task_id, plan_id = tasks[0].get("id"), plan.get("plan_id")
    if not task_id or not plan_id:
        return False
    records = worker.get("task_records") or []
    if (not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict)
            or records[0].get("id") != task_id or records[0].get("status") != "succeeded"
            or records[0].get("description") != planning_preflight_task()["description"]
            or records[0].get("dependencies") != [] or not isinstance(records[0].get("checkpoint"), dict)
            or not isinstance(records[0].get("evidence"), list)
            or any(not isinstance(item, dict) for item in records[0]["evidence"])):
        return False
    criterion = planning_preflight_task()["acceptance"][0]
    criterion_id = "read-ac-" + hashlib.sha256(json.dumps(criterion, sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:12]
    expected = {"id": criterion_id, "criterion": criterion["criterion"], "action": "read_file",
        "args": '{"path":"proof.txt"}', "expect": {"equals": PLANNING_PREFLIGHT_CONTENT}}
    if (records[0].get("acceptance") != [expected]
            or (records[0].get("checkpoint") or {}).get("native_execution_selected") is not True):
        return False
    allowed = {"update_plan", "read_file", "get_file_hash", "list_dir", "list_tree",
               "find_files", "search_files", "discover_tools", "read_tool_artifact"}
    observed = worker.get("tool_receipts")
    if not isinstance(observed, list) or any(not isinstance(item, dict) for item in observed):
        return False
    receipts = [item.get("evidence") for item in observed]
    if not receipts or any(not isinstance(receipt, dict) or receipt.get("action") not in allowed
            or receipt.get("call_id_source") != "native" or receipt.get("authorized") is not True
            or receipt.get("success") is not True
            or not isinstance(receipt.get("verification"), dict)
            or receipt.get("side_effects") not in {"internal", "read_only", "read", "none"}
            for receipt in receipts):
        return False
    for field in ("call_id", "receipt_id"):
        values = [receipt.get(field) for receipt in receipts]
        if any(not isinstance(value, str) or not value for value in values) or len(set(values)) != len(values):
            return False
    stage = 0
    scope = None
    turn_id = None
    for receipt in receipts:
        arguments = _preflight_arguments(receipt)
        result = receipt.get("result_data") or {}
        if not isinstance(result, dict):
            return False
        current_scope = tuple(receipt.get(field) for field in ("user_id", "workspace_id", "session_id"))
        if not all(isinstance(value, str) and value for value in current_scope):
            return False
        scope = scope or current_scope
        if current_scope != scope:
            return False
        if receipt["action"] == "update_plan":
            operations = arguments.get("operations")
            verification = receipt["verification"]
            observed = verification.get("observed")
            if (verification.get("status") != "verified" or not isinstance(observed, dict)
                    or observed.get("plan_id") != plan_id or observed.get("revision") != 1
                    or not isinstance(observed.get("turn_id"), str) or not observed["turn_id"]):
                return False
            turn_id = turn_id or observed["turn_id"]
            if observed["turn_id"] != turn_id:
                return False
            if (stage == 0 and isinstance(operations, list) and len(operations) == 2
                    and isinstance(operations[0], dict) and operations[0].get("op") == "add"
                    and operations[0].get("ref") == "proof"
                    and operations[1] == {"op": "start", "task": "proof"}):
                if (arguments.get("expected_revision") != 0 or result.get("plan_id") != plan_id
                        or result.get("task_ids") != [task_id] or result.get("status") != "active"
                        or verification.get("method") != "working_plan_commit"):
                    return False
                stage = 1
            elif stage == 2 and operations == [{"op": "suggest_complete", "task": task_id}]:
                if (arguments.get("plan_id") != plan_id or arguments.get("expected_revision") != 1
                        or result.get("plan_id") != plan_id or result.get("status") != "completed"
                        or result.get("revision") != 1 or plan.get("revision") != 1
                        or (receipt.get("verification") or {}).get("method") != "working_plan_confirmation"):
                    return False
                stage = 3
            else:
                return False
        elif receipt["action"] == "read_file":
            if (stage != 1 or arguments != {"path": "proof.txt"} or receipt.get("task_id") != task_id
                    or receipt.get("plan_id") != plan_id or result.get("content") != PLANNING_PREFLIGHT_CONTENT
                    or result.get("sha256") != hashlib.sha256(PLANNING_PREFLIGHT_CONTENT.encode()).hexdigest()
                    or result.get("truncated") is not False or not receipt.get("acceptance")
                    or (receipt.get("verification") or {}).get("status") not in {"verified", "not_applicable"}):
                return False
            if not any(item.get("receipt_id") == receipt.get("receipt_id") and item.get("success") is True
                       and item.get("task_id") == task_id
                       and item.get("acceptance_ids") == [criterion_id]
                       for item in records[0].get("evidence", [])):
                return False
            stage = 2
    events = worker.get("events")
    if not isinstance(events, list):
        return False
    snapshots = [event.get("working_plan") for event in events if isinstance(event, dict)]
    return stage == 3 and any(isinstance(snapshot, dict) and snapshot.get("plan_id") == plan_id
        and snapshot.get("revision") == 1 and snapshot.get("status") == "active"
        and isinstance(snapshot.get("tasks"), list)
        and any(task.get("id") == task_id and task.get("status") == "running"
                for task in snapshot["tasks"] if isinstance(task, dict)) for snapshot in snapshots)


def _planning_preflight_identity(candidate: str) -> str:
    return hashlib.sha256(json.dumps(planning_preflight_fixture(candidate), sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def validate_planning_preflight(output: Path, record: dict, calls: list[dict],
                                candidate: str, identity: dict) -> None:
    from benchmarks.v3_matched_live import _validate_worker_evidence, _safety_failure_status
    label = ("planning_preflight", candidate, -1)
    from benchmarks.v3_matched_live import _fixture_prompt
    prompt_sha256 = hashlib.sha256(_fixture_prompt(planning_preflight_fixture(candidate)).encode()).hexdigest()
    phase_calls = [call for call in calls if call.get("arm") == label[0]]
    if (not isinstance(record, dict) or record.get("status") != "passed"
            or record.get("fixture_sha256") != _planning_preflight_identity(candidate)
            or record.get("prompt_sha256") != prompt_sha256
            or not isinstance(record.get("attempt_ids"), list)
            or not 1 <= len(record["attempt_ids"]) <= 6
            or any(not isinstance(item, str) or not item for item in record["attempt_ids"])
            or len(set(record["attempt_ids"])) != len(record["attempt_ids"])
            or set(record["attempt_ids"]) != {call.get("attempt_id") for call in phase_calls}
            or len(phase_calls) != len(record["attempt_ids"])):
        raise ValueError("planning preflight checkpoint or journal is incomplete")
    if any((call.get("arm"), call.get("fixture_id"), call.get("repeat")) != label
            or call.get("model") != DEEPSEEK_RELAY_CONFIG.model
            or call.get("max_output_tokens") != 4096 or call.get("reserved_rmb") != "2.032768"
            or call.get("status") != "settled" or not isinstance(call.get("usage"), dict)
            for call in phase_calls):
        raise ValueError("planning preflight calls lack exact settled CNY lineage")
    usage_fields = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    for call in phase_calls:
        usage = call["usage"]
        if (any(type(usage.get(key)) is not int or usage[key] < 0 for key in usage_fields)
                or usage["cache_read_tokens"] + usage["cache_write_tokens"] > usage["input_tokens"]
                or usage["input_tokens"] > 1_000_000 or usage["output_tokens"] > 4096):
            raise ValueError("planning preflight usage is incomplete or invalid")
        try:
            actual = Decimal(call.get("actual_rmb"))
            expected = DEEPSEEK_PRICING.cost(*(usage[key] for key in usage_fields))
            if not actual.is_finite() or actual < 0 or actual != expected:
                raise ValueError("planning preflight actual charge differs from its pinned usage")
        except (InvalidOperation, TypeError):
            raise ValueError("planning preflight actual charge is invalid") from None
    row = record.get("row")
    if (not isinstance(row, dict) or (row.get("arm"), row.get("fixture_id"), row.get("repeat")) != label
            or row.get("worker_exit_code") != 0 or row.get("worker_result_complete") is not True
            or row.get("usage_complete") is not True or row.get("verified_success") is not True
            or row.get("requests") != len(phase_calls) or _safety_failure_status(row) is not None):
        raise ValueError("planning preflight worker did not complete safely")
    totals = {key: sum(call["usage"][key] for call in phase_calls) for key in usage_fields}
    totals.update(attempts=len(phase_calls), requests=len(phase_calls))
    if any(type(row.get(key)) is not int or row[key] != value for key, value in totals.items()):
        raise ValueError("planning preflight row differs from its exact journal totals")
    worker = _validate_worker_evidence(output, row, candidate, identity, return_worker=True)
    conversation = worker.get("conversation_records") if isinstance(worker, dict) else None
    if (not isinstance(conversation, list) or len(conversation) != 1 or not isinstance(conversation[0], dict)
            or conversation[0].get("turn") != 0 or conversation[0].get("prompt_sha256") != prompt_sha256
            or conversation[0].get("completed") is not True or not planning_preflight_semantics(worker)):
        raise ValueError("planning preflight lacks actual verified completion")


def run_planning_preflight(**options) -> dict:
    from benchmarks.v3_matched_live import _execute_cell, _fixture_prompt
    candidate, relay = options["candidate"], options["relay"]
    record = {"status": "running", "fixture_sha256": _planning_preflight_identity(candidate),
        "prompt_sha256": hashlib.sha256(_fixture_prompt(planning_preflight_fixture(candidate)).encode()).hexdigest()}
    # Keep the finite label for this relay's lifetime: queued requests may
    # already be authorized when the parent advances to a different phase.
    if relay.planning_preflight_closed:
        raise ValueError("planning preflight admission is already closed")
    relay.planning_preflight_label = ("planning_preflight", candidate, -1)
    try:
        record["row"] = _execute_cell(**options, repeat=-1, fixture=planning_preflight_fixture(candidate),
                                     arm="planning_preflight")
    except BaseException:
        # Interrupted work cannot succeed; close admission without delaying cancellation.
        relay.planning_preflight_closed = True
        raise
    with relay._upstream_lock:
        # Drain reserve/append admission before closing and validating a stable
        # snapshot. A still-unsettled stream fails validation conservatively.
        relay.planning_preflight_closed = True
        calls = json.loads(json.dumps(relay.rows))
        record["attempt_ids"] = [call["attempt_id"] for call in calls
                                 if call.get("arm") == "planning_preflight"]
        record["status"] = "passed"
        try:
            validate_planning_preflight(options["output"], record, calls, candidate, options["identity"])
        except ValueError:
            record["status"] = "failed"
    return record


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
    parser.add_argument("--planning-preflight-only", action="store_true",
                        help="Run the protected planning check, retain costs, and stop before the formal cohort.")
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
                 endpoint=DEEPSEEK_ENDPOINT, planning_preflight_only=args.planning_preflight_only)
    if args.planning_preflight_only:
        print(json.dumps({"status": report["status"], "planning_preflight": report.get("planning_preflight"),
                          "budget": report["budget"]}, indent=2, sort_keys=True))
        raise SystemExit(0 if report["status"] == "planning_preflight_passed" else 1)
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    summary = report["summary"]
    raise SystemExit(0 if summary["complete"] and summary["accounting_complete"]
                     and summary["comparison_thresholds_met"] else 1)


if __name__ == "__main__":
    main()
