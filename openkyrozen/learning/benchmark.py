"""Harness-neutral clean-vs-evolved learning benchmark."""

from __future__ import annotations

import argparse
import json
import math
import shlex
import subprocess
from pathlib import Path
from typing import Any


PROTOCOL = "openkyrozen-learning-benchmark-v1"
RESULT_FIELDS = {"verified_success", "corrections", "repeated_errors", "tool_calls", "tokens", "latency"}
DEFAULT_EVIDENCE_STATUS = "verified"


def _wilson(successes: int, total: int) -> tuple[float, float]:
    if total == 0:
        return 0.0, 0.0
    z = 1.96
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((proportion * (1 - proportion) + z * z / (4 * total)) / total) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _load_cases(path: Path) -> list[dict[str, Any]]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for case in cases:
        if not isinstance(case, dict) or not case.get("id") or case.get("profile") not in {"coder", "researcher"} or not case.get("task"):
            raise ValueError("each case requires id, coder|researcher profile, and task")
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("benchmark case ids must be unique")
    return cases


def _run(command: str, case: dict[str, Any], timeout: float) -> dict[str, Any]:
    completed = subprocess.run(shlex.split(command), input=json.dumps(case), text=True,
                               capture_output=True, timeout=timeout, check=False)
    if completed.returncode:
        raise RuntimeError(f"runner exited {completed.returncode}: {completed.stderr[:500]}")
    result = json.loads(completed.stdout)
    if not isinstance(result, dict) or not RESULT_FIELDS <= result.keys():
        raise ValueError(f"runner output must contain {sorted(RESULT_FIELDS)}")
    result_row = {"case_id": case["id"], "profile": case["profile"],
                  "verified_success": bool(result["verified_success"]),
                  **{field: max(0, int(result[field])) for field in ("corrections", "repeated_errors", "tool_calls", "tokens")},
                  "latency": max(0.0, float(result["latency"]))}
    for field in ("provider", "model", "evidence_status"):
        value = result.get(field, DEFAULT_EVIDENCE_STATUS if field == "evidence_status" else "unknown")
        result_row[field] = str(value)[:120]
    evidence = result.get("evidence")
    if isinstance(evidence, dict):
        result_row["evidence"] = {
            str(key)[:80]: (value if isinstance(value, (bool, int, float)) else str(value)[:500])
            for key, value in list(evidence.items())[:20]
        }
    return result_row


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    successes = sum(item["verified_success"] for item in results)
    total = len(results)
    low, high = _wilson(successes, total)
    return {"cases": total, "verified_successes": successes,
            "completion_rate": successes / total if total else None,
            "completion_rate_95ci": [low, high],
            **{field: sum(item[field] for item in results)
               for field in ("corrections", "repeated_errors", "tool_calls", "tokens", "latency")},
            "providers": sorted({str(item.get("provider", "unknown")) for item in results}),
            "models": sorted({str(item.get("model", "unknown")) for item in results}),
            "evidence_statuses": sorted({str(item.get("evidence_status", DEFAULT_EVIDENCE_STATUS)) for item in results})}


def _sign_test_improvement(before: list[float], after: list[float]) -> dict[str, Any]:
    wins = sum(right < left for left, right in zip(before, after))
    losses = sum(right > left for left, right in zip(before, after))
    compared = wins + losses
    p_value = (sum(math.comb(compared, k) for k in range(wins, compared + 1)) / (2 ** compared)
               if compared else 1.0)
    return {"wins": wins, "losses": losses, "p_value": p_value,
            "credible_improvement": wins > losses and p_value <= 0.05}


def compare(clean: dict[str, Any], evolved: dict[str, Any],
            clean_results: list[dict[str, Any]] | None = None,
            evolved_results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    clean_rate, evolved_rate = clean["completion_rate"], evolved["completion_rate"]
    secondary = {}
    for field in ("corrections", "repeated_errors", "tool_calls", "tokens", "latency"):
        before, after = clean[field], evolved[field]
        secondary[field] = {"clean": before, "evolved": after,
                            "improvement": ((before - after) / before) if before else None}
        if clean_results is not None and evolved_results is not None:
            secondary[field].update(_sign_test_improvement(
                [float(item[field]) for item in clean_results],
                [float(item[field]) for item in evolved_results],
            ))
    credible_secondary_gain = any(item.get("credible_improvement", False) for item in secondary.values())
    evidence_sufficient = True
    if clean_results is not None and evolved_results is not None:
        evidence_sufficient = all(
            item.get("evidence_status", DEFAULT_EVIDENCE_STATUS) == DEFAULT_EVIDENCE_STATUS
            for item in [*clean_results, *evolved_results]
        )
    return {"completion_non_regressing": evolved_rate is not None and clean_rate is not None and evolved_rate >= clean_rate,
            "credible_secondary_gain": credible_secondary_gain,
            "public_superiority_claim_supported": evolved_rate is not None and clean_rate is not None
                                                   and evolved_rate >= clean_rate and credible_secondary_gain
                                                   and evidence_sufficient,
            "paired_evidence_status": "verified" if evidence_sufficient else "insufficient",
            "secondary": secondary}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True, type=Path, help="JSONL cases with id, profile, and task")
    parser.add_argument("--clean-runner", required=True, help="Command reading one case JSON from stdin")
    parser.add_argument("--evolved-runner", required=True, help="Command reading one case JSON from stdin")
    parser.add_argument("--ablation", action="append", default=[], metavar="NAME=COMMAND",
                        help="Additional no-memory, candidate, predecessor, or omission runner")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)
    cases = _load_cases(args.cases)
    clean = [_run(args.clean_runner, case, args.timeout) for case in cases]
    evolved = [_run(args.evolved_runner, case, args.timeout) for case in cases]
    clean_summary, evolved_summary = summarize(clean), summarize(evolved)
    report = {"protocol": PROTOCOL, "case_order": [case["id"] for case in cases],
              "clean": {"summary": clean_summary, "results": clean},
              "evolved": {"summary": evolved_summary, "results": evolved},
              "comparison": compare(clean_summary, evolved_summary, clean, evolved)}
    report["metadata"] = {
        "cases": str(args.cases.resolve()),
        "timeout_seconds": args.timeout,
        "runners": {
            "clean": {"command": args.clean_runner, "providers": clean_summary["providers"],
                       "models": clean_summary["models"], "evidence_statuses": clean_summary["evidence_statuses"]},
            "evolved": {"command": args.evolved_runner, "providers": evolved_summary["providers"],
                         "models": evolved_summary["models"], "evidence_statuses": evolved_summary["evidence_statuses"]},
        },
        "evidence_policy": (
            "A public superiority claim requires verified paired evidence from the same fixture, "
            "provider, model, configuration, and independently observable product behavior."
        ),
    }
    report["ablations"] = {}
    for spec in args.ablation:
        name, separator, command = spec.partition("=")
        if not separator or not name.strip() or not command.strip():
            raise ValueError("--ablation requires NAME=COMMAND")
        results = [_run(command, case, args.timeout) for case in cases]
        report["ablations"][name.strip()] = {"summary": summarize(results), "results": results}
    output = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
