"""Live, paired end-to-end Fast benchmark against one running OpenKyrozen server."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import uuid

import requests


CASES = (
    ("fact", "What is 7 times 8? Reply with the number only.", ("56",)),
    ("explanation", "Explain what a Python dictionary does in one sentence.", ("dictionary",)),
    ("comparison", "Compare TCP and UDP in two short sentences.", ("TCP", "UDP")),
    ("analysis", "Explain why unit tests help catch regressions in Python code in two sentences.", ("test", "regression")),
)


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, int((len(ordered) * 0.95 + 0.999999) - 1))]


def summarize(rows: list[dict], backend: str, first_activation_ms: float,
              later_activation_median_ms: float | None) -> dict:
    excluded = {(item["case"], item["repeat"]) for item in rows if item["provider_error"]}
    valid = [item for item in rows if (item["case"], item["repeat"]) not in excluded]
    if not valid:
        raise ValueError("No valid matched turns; check the LLM provider")
    summary = {}
    for selected in ("off", backend):
        group = [item for item in valid if item["backend"] == selected]
        latencies = [item["latency_ms"] for item in group]
        summary[selected] = {
            "turns": len(group), "median_ms": round(statistics.median(latencies), 2),
            "p95_ms": round(_p95(latencies), 2),
            "correct": sum(item["correct"] for item in group),
            "llm_calls": sum(item["llm_calls"] for item in group),
            "llm_tokens": sum(item["llm_tokens"] for item in group),
            "median_decision_ms": round(statistics.median(item["decision_ms"] for item in group), 2),
            "decisions": sum(item["decision_count"] for item in group),
            "fallbacks": sum(item["fallback_count"] for item in group),
            "provider_errors": sum(item["provider_error"] for item in rows if item["backend"] == selected),
        }
    baseline, fast = summary["off"]["median_ms"], summary[backend]["median_ms"]
    workloads = {}
    for case_id in dict.fromkeys(item["case"] for item in rows):
        off = [item["latency_ms"] for item in valid if item["case"] == case_id and item["backend"] == "off"]
        on = [item["latency_ms"] for item in valid if item["case"] == case_id and item["backend"] == backend]
        workloads[case_id] = {"pairs": len(off), "off_median_ms": round(statistics.median(off), 2) if off else None,
                              "fast_median_ms": round(statistics.median(on), 2) if on else None,
                              "speedup": round(statistics.median(off) / statistics.median(on), 3) if off and on else None}
    return {"protocol": "openkyrozen-fast-live-v1", "backend": backend,
            "correctness_method": "case keyword checks; not a semantic quality evaluation",
            "first_activation_ms": round(first_activation_ms, 2),
            "later_activation_median_ms": round(later_activation_median_ms, 2)
            if later_activation_median_ms is not None else None,
            "speedup": round(baseline / fast, 3) if fast else None,
            "excluded_pairs": [{"case": case, "repeat": repeat} for case, repeat in sorted(excluded)],
            "workloads": workloads, "summary": summary, "rows": rows}


def run(base_url: str, backend: str, token: str, repeats: int,
        cases: tuple = CASES, mode: str | None = None) -> dict:
    http = requests.Session()
    if token:
        http.headers["Authorization"] = f"Bearer {token}"
    base_url = base_url.rstrip("/")
    rows = []
    activation_ms = []
    for repeat in range(repeats):
        for case_id, prompt, expected in cases:
            for selected in (("off", backend) if repeat % 2 == 0 else (backend, "off")):
                session_id = f"fast-benchmark-{uuid.uuid4().hex}"
                if selected != "off" or mode:
                    control = {"session_id": session_id}
                    if selected != "off":
                        control["fast_backend"] = selected
                    if mode:
                        control["mode"] = mode
                    if selected == "jev" and os.environ.get("TYPESAFE_API_KEY"):
                        control["jev_api_key"] = os.environ["TYPESAFE_API_KEY"]
                    setup_started = time.perf_counter()
                    configured = http.post(base_url + "/api/chat", json=control, timeout=2400)
                    configured.raise_for_status()
                    if selected != "off":
                        activation_ms.append((time.perf_counter() - setup_started) * 1000)
                started = time.perf_counter()
                reply = http.post(base_url + "/api/chat", json={"session_id": session_id, "message": prompt},
                                  timeout=180)
                elapsed_ms = (time.perf_counter() - started) * 1000
                reply.raise_for_status()
                text = str(reply.json().get("reply", ""))
                diagnostics = http.get(base_url + "/api/v2/fast/diagnostics",
                                       params={"session_id": session_id}, timeout=30)
                diagnostics.raise_for_status()
                data = diagnostics.json()
                decisions = data["decisions"]
                usage = data["llm_usage"]
                rows.append({
                    "case": case_id, "backend": selected, "repeat": repeat,
                    "session_id": session_id,
                    "latency_ms": round(elapsed_ms, 2),
                    "provider_error": "[LLM Error]" in text or usage["attempts"] == 0,
                    "correct": all(value.casefold() in text.casefold() for value in expected),
                    "decision_ms": round(sum(float(item.get("latency_ms") or 0) for item in decisions), 2),
                    "decision_count": sum(bool(item.get("choices")) for item in decisions),
                    "fallback_count": sum(bool(item.get("fallback_reason")) for item in decisions),
                    "llm_calls": usage["attempts"],
                    "llm_tokens": usage["prompt_tokens"] + usage["completion_tokens"] + usage["reasoning_tokens"],
                })
    return summarize(rows, backend, activation_ms[0],
                     statistics.median(activation_ms[1:]) if len(activation_ms) > 1 else None)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--backend", choices=("kev", "jev"), default="kev")
    parser.add_argument("--token", default=os.environ.get("KYROZEN_SERVER_TOKEN", ""))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--cases", help="JSON list of [id, prompt, expected keywords] cases")
    parser.add_argument("--mode", choices=("auto", "ask", "plan", "agent"),
                        help="Set the same interaction mode for both sides")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 20:
        parser.error("repeats must be 1-20")
    cases = tuple(json.load(open(args.cases, encoding="utf-8"))) if args.cases else CASES
    print(json.dumps(run(args.url, args.backend, args.token, args.repeats, cases, args.mode), indent=2))


if __name__ == "__main__":
    main()
