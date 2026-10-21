"""Run the frozen V2/V3 cohort with isolated workspaces and independent checks."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from benchmarks.v3_fixtures import FIXTURES
from benchmarks.v3_matched import BudgetLedger, _cost, summarize
from benchmarks.v3_responses_relay import MODEL, MAX_OUTPUT_TOKENS, ResponsesRelay

BASELINE = "1bfc82c793df49e76097061ec5adfef0e4e2e883"
REPEATS = 3


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True,
                                   stderr=subprocess.DEVNULL, env=env).strip()


def _write_files(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def prepare_fixture(root: Path, fixture: dict) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    _write_files(root, fixture["files"])
    metadata = {"expected_commit": None}
    verifier = fixture["verifier"]
    if verifier["kind"] == "git_subject":
        path = root / verifier["path"]
        current = path.read_text(encoding="utf-8")
        if verifier["new"] not in current:
            raise ValueError("Git fixture's final value is not present in its source file")
        path.write_text(current.replace(verifier["new"], verifier["old"], 1), encoding="utf-8")
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_AUTHOR_DATE="2026-10-01T00:00:00Z", GIT_COMMITTER_DATE="2026-10-01T00:00:00Z")
        _git(root, "init", "-q", env=env)
        _git(root, "config", "user.name", "OpenKyrozen V3 benchmark", env=env)
        _git(root, "config", "user.email", "benchmark@example.invalid", env=env)
        _git(root, "add", "--", verifier["path"], env=env)
        _git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "initial fixture", env=env)
        path.write_text(current, encoding="utf-8")
        _git(root, "add", "--", verifier["path"], env=env)
        _git(root, "-c", "commit.gpgsign=false", "commit", "-qm", verifier["subject"], env=env)
        metadata["expected_commit"] = _git(root, "rev-parse", "--short", "HEAD", env=env)
        metadata["expected_commit_full"] = _git(root, "rev-parse", "HEAD", env=env)
    metadata["initial_files"] = snapshot_workspace(root)
    return metadata


def snapshot_workspace(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file() and ".git" not in path.parts
            and "__pycache__" not in path.parts}


def _run_fixture_tests(root: Path, command: list[str]) -> bool:
    if (len(command) < 4 or command[:3] != ["python", "-m", "unittest"]
            or any(not item.endswith(".py") or "/" in item or "\\" in item for item in command[3:])):
        return False
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ}
    env.update(HOME=str(root), PYTHONPATH="", PYTHONDONTWRITEBYTECODE="1",
               KYROZEN_SANDBOX_REQUIRED=os.environ.get("KYROZEN_SANDBOX_REQUIRED", "0"))
    from openkyrozen.tools.sandbox import SandboxUnavailableError, sandbox_command

    invocation = None
    try:
        command_line = shlex.join([sys.executable, "-m", "unittest", *[item[:-3] for item in command[3:]]])
        invocation = sandbox_command(command_line, workspace=str(root), cwd=str(root), env=env)
        if invocation.backend == "unavailable":
            return False
        result = subprocess.run(invocation.command, cwd=root, env=invocation.env,
                                shell=invocation.shell, capture_output=True, timeout=60, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired, SandboxUnavailableError):
        return False
    finally:
        if invocation:
            invocation.close()


def verify_fixture(fixture: dict, root: Path, answer: str, worker: dict, prepared: dict) -> dict:
    verifier = fixture["verifier"]
    before = prepared["initial_files"]
    after = snapshot_workspace(root)
    changed = {path for path, digest in before.items() if after.get(path) != digest}
    changed.update(set(after) - set(before))
    test_files_unchanged = all(after.get(path) == digest for path, digest in before.items()
                               if Path(path).name.startswith("test_"))
    declared = set(fixture["files"])
    scope_safe = (changed <= declared and test_files_unchanged
                  and ("write" in fixture["capabilities"] or not changed))
    kind = verifier["kind"]
    test_ok = None
    if kind in {"tests", "tests_and_file"}:
        test_ok = _run_fixture_tests(root, verifier["command"])
    if kind == "answer_contains":
        accepted = all(term.casefold() in answer.casefold() for term in verifier["all"])
        accepted = accepted and not changed
    elif kind == "tests":
        accepted = bool(test_ok and scope_safe)
    elif kind == "tests_and_file":
        source = (root / verifier["path"]).read_text(encoding="utf-8") if (root / verifier["path"]).exists() else ""
        accepted = bool(test_ok and scope_safe and verifier["must_contain"] in source)
    elif kind == "git_subject":
        log = _git(root, "log", "--format=%h %s")
        accepted = (verifier["subject"].casefold() in answer.casefold()
                    and prepared["expected_commit"] in answer
                    and _git(root, "rev-parse", "HEAD") == prepared["expected_commit_full"]
                    and verifier["subject"].casefold() in log.casefold()
                    and verifier["old"] in answer and verifier["new"] in answer
                    and not changed)
    elif kind == "no_child_process":
        accepted = (int(worker.get("processes_started", 0)) > 0
                    and int(worker.get("processes_cancelled", 0)) > 0
                    and int(worker.get("processes_alive", -1)) == 0)
        if "unchanged" in verifier:
            accepted = accepted and all(after.get(path) == before.get(path) for path in verifier["unchanged"])
    elif kind == "unchanged":
        accepted = all(after.get(path) == before.get(path) for path in verifier["paths"])
    else:
        accepted = False
    scenario = fixture.get("plan_scenario")
    initial_tasks = [f"{title}: {description}" for title, description in fixture.get("plan_tasks", [])]
    final_tasks = [task.get("description", "") for task in worker.get("task_records", [])
                   if isinstance(task, dict)]
    cancelled = int(worker.get("cancelled_tasks", 0))
    if scenario in {"add_discovered_edge_case", "add_discovered_verification_task"}:
        scenario_ok = len(final_tasks) > len(initial_tasks) and int(worker.get("plan_revisions", 0)) > 0
    elif scenario in {"cancel_obsolete_task_after_reproduction"}:
        scenario_ok = cancelled > 0
    elif scenario == "reorder_after_dependency_discovery":
        lowered = [item.casefold() for item in final_tasks]
        implement = next((index for index, value in enumerate(lowered) if "implement" in value), None)
        verify = next((index for index, value in enumerate(lowered) if "verify" in value), None)
        scenario_ok = implement is not None and verify is not None and implement < verify
    elif scenario == "cancel_replace":
        scenario_ok = cancelled > 0 and len(final_tasks) > len(initial_tasks)
    elif scenario == "scope_preserving":
        scenario_ok = worker.get("scope_preserved") is True
    elif scenario == "material_revision":
        scenario_ok = (worker.get("pending_material_revision") is True
                       and worker.get("accepted_scope_preserved") is True
                       and int(worker.get("material_plan_proposals", 0)) > 0)
    elif scenario == "unnecessary_churn":
        scenario_ok = int(worker.get("plan_revisions", 0)) == 0 and int(worker.get("unnecessary_churn", 0)) == 0
    else:
        scenario_ok = True
    accepted = bool(accepted and scenario_ok)
    safe = scope_safe and not worker.get("provider_error", False) and worker.get("processes_alive", 0) == 0
    return {"result": "passed" if accepted else "failed", "verified_success": bool(accepted),
            "safety_passed": bool(safe), "test_files_unchanged": test_files_unchanged,
            "changed_paths": sorted(changed), "test_result": test_ok,
            "plan_scenario": scenario, "plan_scenario_passed": bool(scenario_ok)}


def _source_snapshot(source: Path) -> str:
    if subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True).strip():
        raise ValueError("matched live benchmark requires clean source worktrees")
    return subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()


def _run_worker(command: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 900) -> tuple[int, str]:
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=(os.name != "nt"),
                                   creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0))
    except OSError as exc:
        return -1, type(exc).__name__
    try:
        process.communicate(timeout=timeout)
        return process.returncode, ""
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=5, check=False)
        else:
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.communicate()
        return -1, "timeout"


def _extract_baseline(root: Path, repo: Path, commit: str) -> None:
    subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"], check=True)
    archive = subprocess.Popen(["git", "-C", str(repo), "archive", "--format=tar", commit], stdout=subprocess.PIPE)
    try:
        import tarfile
        with tarfile.open(fileobj=archive.stdout, mode="r|*") as tar:
            tar.extractall(root, filter="data")
        if archive.wait() != 0:
            raise subprocess.CalledProcessError(archive.returncode, archive.args)
    finally:
        if archive.stdout:
            archive.stdout.close()


def _credential() -> tuple[str, str]:
    from openkyrozen.providers.custom import load_custom_provider_profile

    endpoint = os.environ.get("PANSTAR_BASE_URL", "https://ai.panstar.ai/v1")
    key = os.environ.get("PANSTAR_API_KEY", "")
    if not key:
        profile = load_custom_provider_profile("Panster")
        if profile.base_url.rstrip("/") != endpoint.rstrip("/"):
            raise ValueError("Panster profile endpoint does not match PANSTAR_BASE_URL")
        key = profile.api_key
    if not key:
        raise ValueError("Panstar API key is unavailable")
    return endpoint, key


def _worker_env(base: Path, fixture: dict, relay: ResponsesRelay) -> dict[str, str]:
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL") if key in os.environ}
    capabilities = ",".join(fixture["capabilities"]) or "none"
    env.update(HOME=str(base / "home"), KYROZEN_DB_PATH=str(base / "state.sqlite3"),
               KYROZEN_DISABLE_VECTOR_INDEX="1", KYROZEN_CLI_CAPABILITIES=capabilities,
               KYROZEN_BENCHMARK_API_KEY=relay.client_key,
               KYROZEN_SANDBOX_REQUIRED=os.environ.get("KYROZEN_SANDBOX_REQUIRED", "0"),
               PYTHONDONTWRITEBYTECODE="1", PYTHONPATH="")
    return env


def _empty_row(fixture_id: str, repeat: int, arm: str) -> dict:
    return {"fixture_id": fixture_id, "repeat": repeat, "arm": arm, "usage_complete": False,
            "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "latency_ms": 0,
            "attempts": 0, "first_delta_latency_ms": None, "verifier_result": "incomplete",
            "safety_passed": False, "durable_side_effect_expected": False,
            "durable_side_effect_verified": False, "recovery_required": False,
            "recovery_verified": False, "verified_success": False,
            "workflow_state_correct": False, "requests": 0, "protocol_recovery": 0,
            "duplicate_calls": 0, "plan_revisions": 0, "cancelled_tasks": 0,
            "material_revisions": 0, "unnecessary_churn": 0, "budget_ok": False,
            "plan_scenario": None, "plan_scenario_passed": True}


def _workflow_state_correct(fixture: dict, worker: dict) -> bool:
    tasks = worker.get("tasks")
    plan_required = bool(fixture.get("plan_tasks")) or fixture.get("plan_scenario") in {
        "scope_preserving", "material_revision",
    }
    return (bool(tasks) if plan_required else True) and all(
        status in {"succeeded", "cancelled"} for status in (tasks or [])
    )


def _initial_plan(fixture: dict) -> list[str] | dict:
    if fixture.get("plan_scenario") == "scope_preserving":
        step = {"id": "objective", "title": "Refactor labels",
                "description": "Extract shared formatting without changing outputs.",
                "acceptance": ["Both formatters preserve their outputs."]}
    elif fixture.get("plan_scenario") == "material_revision":
        step = {"id": "objective", "title": "Fix page offset",
                "description": "Make page 1 start at offset zero while preserving later pages.",
                "acceptance": ["Page 1 starts at zero.", "Page 2 starts at one page size."]}
    else:
        return [f"{title}: {description}" for title, description in fixture.get("plan_tasks", [])]
    return {"title": step["title"], "summary": step["description"], "assumptions": [],
            "steps": [step],
            "bounds": {"workspace_paths": ["."], "capabilities": ["read", "write"],
                       "effects": ["read_only", "filesystem", "internal"], "risk_ceiling": "normal"}}


def run(*, source: Path, repo: Path, output: Path, budget_path: Path,
        limit_usd: str = "20", repeats: int = REPEATS, fixtures: tuple[dict, ...] = FIXTURES) -> dict:
    source, repo = source.resolve(), repo.resolve()
    candidate = _source_snapshot(source)
    output, budget_path = output.resolve(), budget_path.resolve()
    if output.is_relative_to(source) or budget_path.is_relative_to(source):
        raise ValueError("benchmark output and budget ledger must be outside the source worktree")
    if repeats != REPEATS or len(fixtures) != len(FIXTURES):
        raise ValueError("release evaluation requires the frozen cohort and exactly three repeats")
    budget = BudgetLedger(budget_path, limit_usd=limit_usd)
    endpoint, api_key = _credential()
    relay = ResponsesRelay(endpoint=endpoint, api_key=api_key, budget=budget)
    rows: list[dict] = []
    if output.exists():
        prior = json.loads(output.read_text(encoding="utf-8"))
        if (prior.get("protocol") != "openkyrozen-v3-matched-live-v1"
                or prior.get("candidate_sha") != candidate or prior.get("baseline_sha") != BASELINE
                or prior.get("model") != MODEL or prior.get("repeats") != repeats
                or not isinstance(prior.get("rows"), list) or not isinstance(prior.get("calls"), list)):
            raise ValueError("existing benchmark report does not match this candidate and cohort")
        rows = prior["rows"]
        keys = [(row.get("fixture_id"), row.get("repeat"), row.get("arm")) for row in rows]
        if len(keys) != len(set(keys)):
            raise ValueError("existing benchmark report contains duplicate attempts")
        relay.rows.extend(prior["calls"])
    else:
        prior = {}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="openkyrozen-v3-matched-") as temporary:
        temporary_root = Path(temporary)
        baseline_source = temporary_root / "baseline"
        _extract_baseline(baseline_source, repo, BASELINE)
        relay.start()
        try:
            for repeat in range(repeats):
                for fixture_index, fixture in enumerate(fixtures):
                    ordered_arms = (("v2", baseline_source), ("v3", source))
                    if (repeat + fixture_index) % 2:
                        ordered_arms = tuple(reversed(ordered_arms))
                    for arm, arm_source in ordered_arms:
                        fixture_id = fixture["fixture_id"]
                        if any((row["fixture_id"], row["repeat"], row["arm"]) == (fixture_id, repeat, arm)
                               for row in rows):
                            continue
                        base = temporary_root / f"{repeat}-{fixture_id}-{arm}"
                        workspace, home = base / "workspace", base / "home"
                        workspace.mkdir(parents=True)
                        home.mkdir(parents=True)
                        prepared = prepare_fixture(workspace, fixture)
                        prompt_file, plan_file, result_file = base / "prompt.txt", base / "plan.json", base / "result.json"
                        prompt_file.write_text(fixture["prompt"], encoding="utf-8")
                        plan_file.write_text(json.dumps(_initial_plan(fixture)),
                                              encoding="utf-8")
                        relay.label = (arm, fixture_id, repeat)
                        started = time.monotonic()
                        worker_env = _worker_env(base, fixture, relay)
                        code, worker_error = _run_worker(
                            [sys.executable, str(repo / "benchmarks" / "prompt_worker.py"),
                             "--source", str(arm_source), "--workspace", str(workspace),
                             "--url", relay.url, "--model", MODEL, "--protocol", "responses",
                             "--prompt-file", str(prompt_file), "--plan-file", str(plan_file),
                             "--result", str(result_file),
                             "--scenario", str(fixture.get("plan_scenario") or "")],
                            cwd=workspace, env=worker_env,
                        )
                        worker = json.loads(result_file.read_text(encoding="utf-8")) if result_file.exists() else {}
                        answer = worker.pop("answer", "")
                        verified = verify_fixture(fixture, workspace, answer, worker, prepared)
                        calls = [row for row in relay.rows if (row["arm"], row["fixture_id"], row["repeat"]) == (arm, fixture_id, repeat)]
                        usage = [call.get("usage") for call in calls]
                        usage_complete = bool(calls) and all(isinstance(item, dict) for item in usage)
                        row = _empty_row(fixture_id, repeat, arm)
                        row.update({"usage_complete": usage_complete,
                                    "input_tokens": sum(item["input_tokens"] for item in usage if item),
                                    "output_tokens": sum(item["output_tokens"] for item in usage if item),
                                    "cache_read_tokens": sum(item["cache_read_tokens"] for item in usage if item),
                                    "latency_ms": round((time.monotonic() - started) * 1000, 2),
                                    "attempts": len(calls), "first_delta_latency_ms": worker.get("first_delta_latency_ms"),
                                    "verifier_result": verified["result"], "safety_passed": verified["safety_passed"],
                                    "plan_scenario": fixture.get("plan_scenario"),
                                    "plan_scenario_passed": verified["plan_scenario_passed"],
                                    "durable_side_effect_expected": fixture["verifier"]["kind"] in {"tests", "tests_and_file"},
                                    "durable_side_effect_verified": bool(verified["verified_success"] and verified["changed_paths"]),
                                    "recovery_required": fixture["category"] == "error_recovery",
                                    "recovery_verified": bool(verified["verified_success"] and fixture["category"] == "error_recovery"),
                                    "verified_success": bool(code == 0 and verified["verified_success"]),
                                    "workflow_state_correct": _workflow_state_correct(fixture, worker),
                                    "requests": len(calls), "protocol_recovery": int(worker.get("protocol_recovery", 0)),
                                    "duplicate_calls": int(worker.get("duplicate_calls", 0)),
                                    "plan_revisions": int(worker.get("plan_revisions", 0)),
                                    "cancelled_tasks": int(worker.get("cancelled_tasks", 0)),
                                    "material_revisions": int(worker.get("material_plan_proposals", 0)),
                                    "unnecessary_churn": int(worker.get("unnecessary_churn", 0)),
                                    "budget_ok": budget.budget_ok,
                                    "worker_error": worker_error})
                        rows.append(row)
                        document = {"protocol": "openkyrozen-v3-matched-live-v1", "candidate_sha": candidate,
                                    "baseline_sha": BASELINE, "model": MODEL, "max_output_tokens": MAX_OUTPUT_TOKENS,
                                    "repeats": repeats, "status": "running", "rows": rows,
                                    "calls": [{key: value for key, value in call.items() if key != "response_body"}
                                              for call in relay.rows],
                                    "budget": {"spent_usd": str(budget.spent_usd),
                                               "remaining_usd": str(budget.remaining_usd),
                                               "limit_usd": limit_usd}}
                        output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                        print(json.dumps({"fixture_id": fixture_id, "repeat": repeat, "arm": arm,
                                          "verified_success": row["verified_success"],
                                          "requests": row["requests"], "remaining_usd": str(budget.remaining_usd)},
                                         sort_keys=True), flush=True)
                        if not budget.budget_ok:
                            raise RuntimeError("benchmark budget overrun; stop all model calls and investigate")
                        if relay.budget_blocked:
                            document["status"] = "blocked_budget_reservation"
                            output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                            break
                    if relay.budget_blocked:
                        break
                if relay.budget_blocked:
                    break
        finally:
            relay.close()
    summary = summarize(rows)
    report = {"protocol": "openkyrozen-v3-matched-live-v1", "candidate_sha": candidate,
              "baseline_sha": BASELINE, "model": MODEL,
              "status": "blocked_budget_reservation" if relay.budget_blocked else
                        "complete" if summary["complete"] and summary["accounting_complete"] else "incomplete",
              "summary": summary,
              "rows": rows, "budget": {"spent_usd": str(budget.spent_usd),
                                      "remaining_usd": str(budget.remaining_usd),
                                      "limit_usd": limit_usd},
              "calls": [{key: value for key, value in call.items() if key != "response_body"}
                        for call in relay.rows]}
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-db", type=Path, required=True)
    parser.add_argument("--limit-usd", default="20")
    parser.add_argument("--repeats", type=int, default=REPEATS)
    args = parser.parse_args()
    if args.repeats != REPEATS:
        parser.error("release evaluation requires exactly three matched repeats")
    report = run(source=args.source, repo=args.repo, output=args.output,
                 budget_path=args.budget_db, limit_usd=args.limit_usd, repeats=args.repeats)
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    summary = report["summary"]
    raise SystemExit(0 if (summary["complete"] and summary["accounting_complete"]
                          and summary["comparison_thresholds_met"]) else 1)


if __name__ == "__main__":
    main()
