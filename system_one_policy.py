"""Risk-specific policies and data-driven threshold calibration for System One."""

from __future__ import annotations

import hashlib
import json
import os
import random
from collections import defaultdict
from pathlib import Path
from typing import Any


POLICY_VERSION = "system-one-policy-v1"
CALIBRATION_PATH = "system_one_calibration.json"

# These are deliberately conservative bootstrap values. A persisted calibrated
# policy replaces them only after a held-out quality gate passes.
DEFAULT_POLICIES: dict[str, dict[str, float | str]] = {
    "route_model": {"confidence": 0.80, "probability": 0.85, "margin": 0.15, "minimum_coverage": 0.05, "target_precision": 0.95, "fallback": "llm"},
    "route_complexity": {"confidence": 0.85, "probability": 0.90, "margin": 0.20, "minimum_coverage": 0.05, "target_precision": 0.95, "fallback": "llm"},
    "route_profile": {"confidence": 0.85, "probability": 0.90, "margin": 0.20, "minimum_coverage": 0.05, "target_precision": 0.95, "fallback": "llm"},
    "clarification": {"confidence": 0.90, "probability": 0.90, "margin": 0.25, "minimum_coverage": 0.05, "target_precision": 0.99, "fallback": "user"},
    "learning_evidence": {"confidence": 0.99, "probability": 0.99, "margin": 0.30, "minimum_coverage": 0.01, "target_precision": 0.99, "fallback": "candidate"},
    "memory_relevance": {"confidence": 0.70, "probability": 0.70, "margin": 0.10, "minimum_coverage": 0.25, "target_precision": 0.90, "fallback": "existing_order"},
    "tool_output_review": {"confidence": 0.995, "probability": 0.995, "margin": 0.50, "minimum_coverage": 0.10, "target_precision": 0.995, "fallback": "advisory"},
}


def _path() -> Path:
    return Path.home() / ".kyrozen" / CALIBRATION_PATH


def _clean_policy(value: Any, fallback: dict[str, float | str]) -> dict[str, Any]:
    result = dict(fallback)
    result["validated"] = False
    if isinstance(value, dict):
        for key in ("confidence", "probability", "margin", "minimum_coverage", "target_precision"):
            try:
                number = float(value[key])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= number <= 1:
                result[key] = number
        result["validated"] = bool(value.get("validated"))
        result["calibration_id"] = str(value.get("calibration_id") or "")[:120]
        result["fallback"] = str(value.get("fallback") or fallback.get("fallback") or "current_path")[:40]
    result["version"] = POLICY_VERSION
    return result


def load(backend: str, action: str) -> dict[str, Any]:
    fallback = _clean_policy(DEFAULT_POLICIES.get(action, DEFAULT_POLICIES["clarification"]), {})
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        policy = data.get("backends", {}).get(str(backend), {}).get(action, {})
        return _clean_policy(policy, fallback)
    except (OSError, TypeError, ValueError):
        return fallback


def status() -> dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {"version": POLICY_VERSION, "backends": {}}
    if not isinstance(data, dict):
        return {"version": POLICY_VERSION, "backends": {}}
    return {"version": str(data.get("version") or POLICY_VERSION),
            "backends": data.get("backends") if isinstance(data.get("backends"), dict) else {}}


def _threshold_candidates(rows: list[dict[str, Any]]) -> list[float]:
    # Do not synthesize a zero threshold.  With a small all-correct sample it
    # would accept every future decision and erase the risk-specific bootstrap
    # gate.  The observed scores plus the hard upper bound are sufficient.
    values = {1.0}
    for row in rows:
        try:
            value = float(row["score"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= value <= 1:
            values.add(round(value, 6))
    return sorted(values)


def split_cases(rows: list[dict[str, Any]], *, calibration_fraction: float = 0.8,
                seed: int = 7, stratify_field: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split repeated measurements by case, optionally preserving labels."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, row in enumerate(rows):
        groups[str(row.get("case") or row.get("id") or index)].append(row)
    if not groups:
        return [], []
    group_names = sorted(groups, key=lambda name: hashlib.sha256(
        f"{seed}:{name}".encode()).hexdigest())
    if not stratify_field:
        strata = {"all": group_names}
    else:
        strata: dict[str, list[str]] = defaultdict(list)
        for name in group_names:
            strata[str(groups[name][0].get(stratify_field))].append(name)
    calibration_names: list[str] = []
    holdout_names: list[str] = []
    for names in strata.values():
        names = list(names)
        holdout_count = int(round(len(names) * (1 - calibration_fraction)))
        if len(names) >= 2:
            holdout_count = max(1, holdout_count)
        holdout_count = min(max(0, holdout_count), max(0, len(names) - 1))
        holdout_names.extend(names[-holdout_count:] if holdout_count else [])
        calibration_names.extend(names[:-holdout_count] if holdout_count else names)
    calibration = [row for name in calibration_names for row in groups[name]]
    holdout = [row for name in holdout_names for row in groups[name]]
    return calibration, holdout


def calibrate(rows: list[dict[str, Any]], *, backend: str, model_version: str,
              dataset_id: str = "", target_precision: float = 0.95,
              minimum_coverage: float = 0.0, calibration_fraction: float = 0.8,
              seed: int = 7, stratify_field: str | None = None) -> dict[str, Any]:
    """Fit a threshold on 80% of rows and gate it on the held-out 20%."""
    ordered = [row for row in rows if isinstance(row, dict) and "score" in row and "correct" in row]
    if not dataset_id:
        dataset_id = hashlib.sha256(json.dumps(ordered, sort_keys=True, default=str).encode()).hexdigest()[:16]
    # Split whole labeled cases so repeated measurements of one case cannot
    # leak into both calibration and holdout.
    calibration_rows, holdout_rows = split_cases(
        ordered, calibration_fraction=calibration_fraction, seed=seed,
        stratify_field=stratify_field,
    )
    unique_case_count = len({str(row.get("case") or row.get("id")) for row in ordered})
    if not holdout_rows:
        holdout_rows = calibration_rows
    best: dict[str, Any] | None = None
    for threshold in _threshold_candidates(calibration_rows):
        accepted = [row for row in calibration_rows if float(row["score"]) >= threshold]
        if not accepted or len(accepted) / max(1, len(calibration_rows)) < minimum_coverage:
            continue
        precision = sum(bool(row["correct"]) for row in accepted) / len(accepted)
        if precision < target_precision:
            continue
        candidate = {"threshold": threshold, "precision": round(precision, 6),
                     "coverage": round(len(accepted) / len(calibration_rows), 6)}
        # Prefer the lowest threshold that still meets the calibration target.
        # This maximizes useful coverage; the action-specific holdout gate is
        # still authoritative for high-risk actions such as quarantine.
        if best is None or candidate["threshold"] < best["threshold"]:
            best = candidate
    if best is None:
        best = {"threshold": 1.0, "precision": 0.0, "coverage": 0.0}
    holdout_accepted = [row for row in holdout_rows if float(row["score"]) >= best["threshold"]]
    holdout_precision = (sum(bool(row["correct"]) for row in holdout_accepted) / len(holdout_accepted)
                         if holdout_accepted else 0.0)
    holdout_coverage = len(holdout_accepted) / max(1, len(holdout_rows))
    holdout_correct = [float(row["correct"]) for row in holdout_accepted]
    best.update({"holdout_precision": round(holdout_precision, 6),
                 "holdout_coverage": round(holdout_coverage, 6),
                 "holdout_count": len(holdout_rows),
                 "calibration_count": len(calibration_rows),
                 "target_precision": target_precision,
                 "minimum_coverage": minimum_coverage,
                 "validated": bool(unique_case_count >= 3 and holdout_accepted
                                   and holdout_precision >= target_precision),
                 "unique_case_count": unique_case_count,
                 "bootstrap_precision_95": bootstrap_interval(holdout_correct),
                 "seed": seed})
    digest = hashlib.sha256(json.dumps(ordered, sort_keys=True, default=str).encode()).hexdigest()[:16]
    best.update({"backend": backend, "model_version": model_version[:120],
                 "dataset_id": dataset_id[:120], "calibration_id": f"{backend}:{digest}"})
    return best


def write(backend: str, policies: dict[str, dict[str, Any]], *, model_version: str,
          dataset_id: str) -> dict[str, Any]:
    current = status()
    current.update({"version": POLICY_VERSION, "model_version": model_version[:120],
                    "dataset_id": dataset_id[:120]})
    backends = current.setdefault("backends", {})
    backends[backend] = policies
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return current


def calibration_metrics(scores: list[float], labels: list[bool], *, bins: int = 10) -> dict[str, Any]:
    """Return Brier/ECE/reliability data for benchmark reports."""
    pairs = [(max(0.0, min(1.0, float(score))), bool(label)) for score, label in zip(scores, labels)]
    if not pairs:
        return {"brier": None, "ece": None, "reliability": []}
    brier = sum((score - int(label)) ** 2 for score, label in pairs) / len(pairs)
    reliability = []
    ece = 0.0
    for index in range(bins):
        lower, upper = index / bins, (index + 1) / bins
        bucket = [pair for pair in pairs if lower <= pair[0] < upper or (index == bins - 1 and pair[0] == 1)]
        if not bucket:
            continue
        average_score = sum(pair[0] for pair in bucket) / len(bucket)
        accuracy = sum(pair[1] for pair in bucket) / len(bucket)
        weight = len(bucket) / len(pairs)
        ece += weight * abs(average_score - accuracy)
        reliability.append({"lower": lower, "upper": upper, "count": len(bucket),
                            "mean_score": round(average_score, 6), "accuracy": round(accuracy, 6)})
    return {"brier": round(brier, 6), "ece": round(ece, 6), "reliability": reliability}


def bootstrap_interval(values: list[float], *, seed: int = 7, repeats: int = 1000) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    rng = random.Random(seed)
    samples = [sum(rng.choice(values) for _ in values) / len(values) for _ in range(repeats)]
    samples.sort()
    return round(samples[int(repeats * 0.025)], 6), round(samples[int(repeats * 0.975)], 6)
