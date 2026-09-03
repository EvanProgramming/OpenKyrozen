"""Deterministic local runner for the frozen multi-party memory fixture.

This runner exercises the real SQLite-backed memory boundary without an
external provider or API key. It is deliberately a harness fixture, not a
claim that the product is superior to another model or provider.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

os.environ.setdefault("KYROZEN_DISABLE_VECTOR_INDEX", "1")

from memory import MemoryBank  # noqa: E402


PROVIDER = "deterministic-fixture"
MODEL = "openkyrozen-memory-policy-v1"
WORKSPACE_ID = "multi-party-memory-fixture"


def _database_for(mode: str) -> Path:
    root = os.environ.get("KYROZEN_BENCHMARK_ROOT", "").strip()
    if not root:
        raise RuntimeError(
            "KYROZEN_BENCHMARK_ROOT is required; run `make benchmark` "
            "or set it to an isolated temporary directory"
        )
    state_root = Path(root).expanduser().resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    return state_root / f"{mode}.sqlite3"


def _remember(bank: MemoryBank, content: str, **metadata: Any) -> str:
    return bank.add_log(content, kind="fact", confidence=1.0, metadata=metadata)


def _records(bank: MemoryBank, query: str, **context: Any) -> list[dict[str, Any]]:
    return bank.recall_records(query, n_results=20, **context)


def _speaker_belief_isolation(bank: MemoryBank) -> tuple[bool, int, list[str]]:
    _remember(bank, "The release date is Monday.", speaker="alice", claim_type="belief")
    _remember(bank, "The release date is Tuesday.", speaker="bob", claim_type="belief")
    alice = _records(bank, "release date", speaker="alice", authorized_speakers={"alice"})
    bob = _records(bank, "release date", speaker="bob", authorized_speakers={"bob"})
    passed = (
        len(alice) == 1 and alice[0]["metadata"].get("speaker") == "alice"
        and len(bob) == 1 and bob[0]["metadata"].get("speaker") == "bob"
    )
    return passed, 4, ["speaker-scoped recall preserved both attributed beliefs"]


def _speaker_update(bank: MemoryBank) -> tuple[bool, int, list[str]]:
    old_alice = _remember(bank, "Alice says the release date is Monday.", speaker="alice", claim_type="belief")
    _remember(bank, "Bob says the release date is Tuesday.", speaker="bob", claim_type="belief")
    bank.store.set_memory_status(old_alice, "superseded", workspace_id=bank.workspace_id, user_id=bank.user_id)
    bank.store.append_event(
        "memory.superseded",
        {"memory_id": old_alice, "replacement": "Wednesday", "speaker": "alice"},
        user_id=bank.user_id, workspace_id=bank.workspace_id, session_id=bank.session_id,
    )
    _remember(bank, "Alice says the release date is Wednesday.", speaker="alice", claim_type="belief")
    alice = _records(bank, "release date", speaker="alice", authorized_speakers={"alice"})
    bob = _records(bank, "release date", speaker="bob", authorized_speakers={"bob"})
    alice_text = " ".join(row["content"] for row in alice)
    bob_text = " ".join(row["content"] for row in bob)
    passed = "Wednesday" in alice_text and "Monday" not in alice_text and bob_text.endswith("Tuesday.")
    return passed, 7, ["Alice's old claim was superseded while Bob's claim remained active"]


def _private_leakage(bank: MemoryBank) -> tuple[bool, int, list[str]]:
    _remember(
        bank, "Alice's private budget is 7000 dollars.", speaker="alice",
        visibility="private", claim_type="private_fact",
    )
    bob = _records(bank, "private budget", speaker="bob", authorized_speakers={"bob"})
    alice = _records(bank, "private budget", speaker="alice", authorized_speakers={"alice"})
    passed = not bob and len(alice) == 1 and "7000" in alice[0]["content"]
    return passed, 4, ["private memory was visible to its owner and hidden from Bob"]


def _audience_language(bank: MemoryBank) -> tuple[bool, int, list[str]]:
    _remember(
        bank, "The release agreement uses blue-green deployment.", speaker="team",
        visibility="group", audiences=["engineering"], claim_type="group_agreement",
    )
    engineering = _records(bank, "release agreement", audience="engineering")
    sales = _records(bank, "release agreement", audience="sales")
    passed = len(engineering) == 1 and not sales and "blue-green" in engineering[0]["content"]
    return passed, 4, ["group agreement was visible only to the engineering audience"]


def _term_ambiguity(bank: MemoryBank) -> tuple[bool, int, list[str]]:
    _remember(bank, "For Alice, Orion means the public launch date.", speaker="alice", claim_type="term")
    _remember(bank, "For Bob, Orion means the internal project codename.", speaker="bob", claim_type="term")
    records = _records(bank, "Orion means", authorized_speakers={"alice", "bob"})
    speakers = {row["metadata"].get("speaker") for row in records}
    passed = len(records) == 2 and speakers == {"alice", "bob"}
    return passed, 3, ["same term retained both speaker attributions for clarification"]


_CASE_HANDLERS: dict[str, Callable[[MemoryBank], tuple[bool, int, list[str]]]] = {
    "speaker-belief-isolation": _speaker_belief_isolation,
    "speaker-update": _speaker_update,
    "private-leakage": _private_leakage,
    "audience-language": _audience_language,
    "term-ambiguity": _term_ambiguity,
}


def run_case(mode: str, case: dict[str, Any]) -> dict[str, Any]:
    """Run one fixture case and return the benchmark runner contract."""
    case_id = str(case.get("id", "")).strip()
    handler = _CASE_HANDLERS.get(case_id)
    if handler is None:
        raise ValueError(f"unsupported fixture case: {case_id}")
    started = time.perf_counter()
    bank = MemoryBank(
        _database_for(mode), user_id=f"benchmark-{mode}",
        workspace_id=WORKSPACE_ID, session_id=case_id,
    )
    bank.store.append_event(
        "benchmark.case_started",
        {"case_id": case_id, "mode": mode, "provider": PROVIDER, "model": MODEL},
        user_id=bank.user_id, workspace_id=bank.workspace_id, session_id=bank.session_id,
    )
    passed, tool_calls, checks = handler(bank)
    events = bank.store.list_events(
        workspace_id=bank.workspace_id, session_id=bank.session_id, user_id=bank.user_id,
        limit=100,
    )
    memories = bank.store.list_memories(
        workspace_id=bank.workspace_id, session_id=bank.session_id, user_id=bank.user_id,
        status="active", limit=100,
    )
    if passed and events and memories:
        evidence_status = "fixture_verified"
    elif not passed:
        evidence_status = "failed"
    else:
        evidence_status = "insufficient"
    return {
        "verified_success": passed,
        "corrections": 1 if case_id == "speaker-update" else 0,
        "repeated_errors": 0,
        "tool_calls": tool_calls,
        "tokens": max(1, len(str(case.get("task", ""))) + sum(len(row["content"]) for row in memories)),
        "latency": max(0.0, time.perf_counter() - started),
        "provider": PROVIDER,
        "model": MODEL,
        "evidence_status": evidence_status,
        "evidence": {
            "event_count": len(events),
            "active_memory_count": len(memories),
            "scope": {"user_id": bank.user_id, "workspace_id": bank.workspace_id, "session_id": case_id},
            "checks": checks if passed else [],
        },
    }


def run_stdin_case(mode: str) -> None:
    case = json.load(sys.stdin)
    if not isinstance(case, dict):
        raise ValueError("benchmark input must be a JSON object")
    result = run_case(mode, case)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
