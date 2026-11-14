"""Offline routing-policy ablations; no provider or application is initialized.

Run ``python -m benchmarks.v3_routing_ablation --candidate-sha FULL_SHA`` on a
clean candidate to bind the observations to that commit. Without that argument,
dirty-tree observations remain useful diagnostics, not release certification.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import time
from types import MethodType, SimpleNamespace

from benchmarks.system_one import CASES as SYSTEM_ONE_CASES, _p95
from openkyrozen.learning.engine import LearningEngine
from openkyrozen.memory.retrieval import _has_cjk, _cjk_approximate_words
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.routing.router import _classify_complexity, _select_model, decide_request


# Planning labels concern dependent work; profiles concern engineering versus
# information work. These are hand labels, never outputs from the router.
PLANNING_PROFILES = {
    "route_fact": (False, "researcher"),
    "route_code_change": (True, "coder"),
    "route_summary": (False, "researcher"),
    "route_migration": (True, "coder"),
    "route_documentation": (False, "coder"),
    "route_security_audit": (True, "coder"),
    "route_json_transform": (False, "researcher"),
    "route_incident": (True, "coder"),
}
EDGE_CASES = (
    ("greeting_code", "Hello, refactor the parser and add regression tests.",
     True, "reasoning", "coder"),
    ("question_migration", "What is needed to migrate the database safely? First back it up, then migrate and verify.",
     True, "reasoning", "coder"),
    ("single_repo_read", "Read the repository README.md.", False, "simple", "coder"),
    ("cjk_code_change", "请重构代码并添加测试，最后验证结果。", True, "reasoning", "coder"),
    ("cjk_fact", "地球有几个月亮？", False, "simple", "researcher"),
    ("network_comparison", "Compare TCP and UDP in two short sentences.",
     False, "simple", "researcher"),
)
VARIANTS = {
    "default": (None, None),
    "planning_always": ("planning", True),
    "planning_never": ("planning", False),
    "model_simple": ("model", "simple"),
    "model_reasoning": ("model", "reasoning"),
    "profile_coder": ("profile", "coder"),
    "profile_researcher": ("profile", "researcher"),
}


def _cases() -> list[dict]:
    cases = []
    for case_id, kind, state, _questions, _question_id, expected in SYSTEM_ONE_CASES:
        if kind == "route_model":
            planning, profile = PLANNING_PROFILES[case_id]
            cases.append({"case": case_id, "request": state["request"],
                          "expected": {"planning": planning, "model": expected, "profile": profile},
                          "source": "benchmarks/system_one.py"})
    for case_id, request, planning, model, profile in EDGE_CASES:
        cases.append({"case": case_id, "request": request,
                      "expected": {"planning": planning, "model": model, "profile": profile},
                      "source": "greeting, question, single-action, CJK and comparison edge cases"})
    return cases


def _identity(candidate_sha: str | None) -> dict:
    root = Path(__file__).resolve().parents[1]
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=normal"], cwd=root, text=True,
    ).splitlines()
    if candidate_sha is not None and (candidate_sha != sha or dirty):
        raise ValueError("candidate binding requires the exact full HEAD SHA and a clean worktree")
    sources = ("benchmarks/v3_routing_ablation.py", "benchmarks/system_one.py",
               "openkyrozen/routing/router.py", "openkyrozen/providers/config.py",
               "openkyrozen/providers/registry.py", "openkyrozen/learning/engine.py",
               "openkyrozen/memory/retrieval.py", "openkyrozen/tasks/models.py")
    return {"git_sha": sha, "dirty_paths": dirty, "commit_bound": not dirty,
            "source_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                              for name in sources}}


def _route(runtime: SimpleNamespace, request: str, variant: str) -> dict:
    feature, value = VARIANTS[variant]
    advice = {"model": value} if feature == "model" else {}
    classify = ((lambda _text: "complex" if value else "medium") if feature == "planning"
                else lambda text: _classify_complexity(runtime, text))
    override = value if feature == "profile" else "auto"
    profile = LearningEngine.route_profile(request, override)
    decision, complexity = decide_request(
        runtime._provider_config, advice, request, lambda text: _select_model(runtime, text),
        classify, profile, override, "agent",
    )
    return {"planning": decision.planning_required,
            "model": {"offline-simple-slot": "simple", "offline-reasoning-slot": "reasoning"}[decision.model],
            "profile": decision.profile, "complexity": complexity}


def run(*, repeats: int = 100, candidate_sha: str | None = None) -> dict:
    if type(repeats) is not int or repeats < 1:
        raise ValueError("repeats must be a positive integer")
    identity = _identity(candidate_sha)
    cases = _cases()
    runtime = SimpleNamespace(_provider_config=ProviderConfig(
        provider="deepseek", model_simple="offline-simple-slot", model_complex="offline-reasoning-slot",
    ))
    runtime._has_cjk = MethodType(_has_cjk, runtime)
    runtime._cjk_approximate_words = MethodType(_cjk_approximate_words, runtime)
    rows = [{**case, "variants": {variant: _route(runtime, case["request"], variant)
                                 for variant in VARIANTS}} for case in cases]
    timings = defaultdict(list)
    for repeat in range(repeats):
        # Alternate order to reduce systematic warm-up/order bias.
        variants = tuple(VARIANTS) if repeat % 2 == 0 else tuple(reversed(VARIANTS))
        for case in cases:
            for variant in variants:
                started = time.perf_counter_ns()
                _route(runtime, case["request"], variant)
                timings[variant].append((time.perf_counter_ns() - started) / 1000)

    def metrics(feature: str, variant: str) -> dict:
        values = [row["variants"][variant][feature] for row in rows]
        correct = sum(value == row["expected"][feature] for value, row in zip(values, rows))
        return {"correct": correct, "incorrect": len(rows) - correct,
                "accuracy": correct / len(rows), "selected": dict(Counter(map(str, values))),
                "routing_median_us": statistics.median(timings[variant]),
                "routing_p95_us": _p95(timings[variant])}

    features = {}
    for feature in ("planning", "model", "profile"):
        baseline = {variant: metrics(feature, variant) for variant, (changed, _) in VARIANTS.items()
                    if changed == feature}
        default = metrics(feature, "default")
        improvement = default["correct"] - max(item["correct"] for item in baseline.values())
        features[feature] = {"default": default, "constant_ablations": baseline,
                             "correct_gain_over_best_constant": improvement,
                             "policy_accuracy_benefit": improvement > 0}
    return {
        "protocol": "openkyrozen-offline-routing-ablation-v1", "identity": identity,
        "dataset_sha256": hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "cases": len(cases), "repeats": repeats, "features": features, "rows": rows,
        "provider_calls": 0, "provider_cost_cny": "0", "model_quality_evaluated": False,
        "release_requirements_certified": False,
        "method": "One routing feature replaced at a time by each constant alternative; other routing outputs retained.",
        "limitations": [
            "Routing-policy labels are hand judgments on a small fixed diagnostic corpus, not independently verified task outcomes.",
            "Resource slots are symbolic: no real model quality, token usage, billed cost savings or end-to-end latency is measured.",
            "Latency is local routing-function wall time including benchmark dispatch overhead, not provider or turn latency.",
            "R124/R126 gain routing-policy observations; R132 per-feature end-to-end benefit remains unproven. Optional Jev/Kev remain outside this default-on audit.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=100)
    parser.add_argument("--candidate-sha")
    args = parser.parse_args()
    try:
        report = run(repeats=args.repeats, candidate_sha=args.candidate_sha)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
