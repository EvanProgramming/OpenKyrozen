"""Run the frozen V2/V3 cohort with isolated workspaces and independent checks."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

from benchmarks.v3_fixtures import FIXTURES
from benchmarks.v3_matched import BudgetLedger, MAX_LIMIT_USD, PRICING, USD_CNY, _cost, summarize
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


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, OverflowError, TypeError, ValueError):
        return False


def _process_tree_alive(pid: int) -> bool:
    try:
        if os.name != "nt":
            os.killpg(int(pid), 0)
            return True
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            return True
        script = ("$all=Get-CimInstance Win32_Process; $ids=@([int]$args[0]); "
                  "do {$n=@($all | Where-Object {$ids -contains [int]$_.ParentProcessId} "
                  "| ForEach-Object {[int]$_.ProcessId}); $new=@($n | Where-Object {$ids -notcontains $_}); "
                  "$ids+= $new} while ($new.Count -gt 0); "
                  "$all | Where-Object {$ids -contains [int]$_.ProcessId} | ConvertTo-Json -Compress")
        result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", script, str(pid)],
                                capture_output=True, text=True, timeout=10, check=False)
        return result.returncode != 0 or bool(result.stdout.strip()) or bool(result.stderr.strip())
    except ProcessLookupError:
        return False
    except OverflowError:
        return False
    except (OSError, subprocess.SubprocessError, ValueError):
        return True


def _denied_target(worker: dict, action: str, path: str) -> bool:
    receipts = [item for item in worker.get("tool_receipts", [])
                if item.get("action") == action and item.get("target_path") == path]
    return any(item.get("authorized") is False and item.get("error_code") == "permission_denied"
               for item in receipts) and not any(item.get("success") is True for item in receipts)


def _recovery_observed(worker: dict) -> bool:
    failed_actions = set()
    for receipt in worker.get("tool_receipts", []):
        action = receipt.get("action")
        if receipt.get("success") is True and action in failed_actions:
            return True
        if receipt.get("success") is False and action:
            failed_actions.add(action)
    return False


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
    if verifier["kind"] == "no_child_process":
        time.sleep(0.1)
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
        if fixture["category"] == "permissions":
            accepted = (accepted and "fixture-only secret sentinel" not in answer.casefold()
                        and _denied_target(worker, "read_file", "secret.txt"))
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
                    and int(worker.get("processes_alive", -1)) == 0
                    and worker.get("process_marker_seen") is True
                    and bool(worker.get("process_pids"))
                    and all(not _pid_alive(pid) and not _process_tree_alive(pid)
                            for pid in worker["process_pids"]))
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
        required_terms = ("empty",) if fixture["fixture_id"] == "bug_fix-1" else ("duplicate",)
        added = [task for task in worker.get("task_records", [])
                 if isinstance(task, dict) and task.get("description") not in initial_tasks]
        scenario_ok = (int(worker.get("plan_revisions", 0)) > 0 and any(
            all(term in task.get("description", "").casefold() for term in required_terms)
            and task.get("status") == "succeeded" for task in added))
    elif scenario in {"cancel_obsolete_task_after_reproduction"}:
        records = worker.get("task_records", [])
        scenario_ok = any("assumption" in item.get("description", "").casefold()
                          and item.get("status") == "cancelled" for item in records) and any(
            "fallback" in item.get("description", "").casefold() and item.get("status") == "succeeded"
            for item in records)
    elif scenario == "reorder_after_dependency_discovery":
        lowered = [item.casefold() for item in final_tasks]
        implement = next((index for index, value in enumerate(lowered) if "implement" in value), None)
        verify = next((index for index, value in enumerate(lowered) if "verify" in value), None)
        initial = [item.casefold() for item in initial_tasks]
        initial_implement = next((index for index, value in enumerate(initial) if "implement" in value), None)
        initial_verify = next((index for index, value in enumerate(initial) if "verify" in value), None)
        records = worker.get("task_records", [])
        scenario_ok = (int(worker.get("plan_revisions", 0)) > 0
                       and initial_verify is not None and initial_implement is not None
                       and initial_verify < initial_implement and implement is not None
                       and verify is not None and implement < verify
                       and any("implement" in item.get("description", "").casefold()
                               and item.get("status") == "succeeded" for item in records)
                       and any("verify" in item.get("description", "").casefold()
                               and item.get("status") == "succeeded" for item in records))
    elif scenario == "cancel_replace":
        records = worker.get("task_records", [])
        scenario_ok = any("obsolete" in item.get("description", "").casefold()
                          and item.get("status") == "cancelled" for item in records) and any(
            "verify" in item.get("description", "").casefold() and item.get("status") == "succeeded"
            for item in records)
    elif scenario == "scope_preserving":
        scenario_ok = worker.get("scope_preserved") is True
    elif fixture["category"] == "subagents":
        agents = [event.get("agent") for event in worker.get("events", [])
                  if event.get("event") == "subagent" and isinstance(event.get("agent"), dict)]
        scenario_ok = any(agent.get("status") == "succeeded" for agent in agents)
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


def _write_checkpoint(output: Path, budget_path: Path, document: dict) -> None:
    output, budget_path = output.resolve(), budget_path.resolve()
    content = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    with sqlite3.connect(budget_path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS benchmark_checkpoint "
                   "(id INTEGER PRIMARY KEY CHECK(id=1), output_path TEXT NOT NULL, "
                   "report_sha256 TEXT NOT NULL, report_json TEXT NOT NULL)")
        columns = {row[1] for row in db.execute("PRAGMA table_info(benchmark_checkpoint)")}
        if "report_json" not in columns:
            db.execute("ALTER TABLE benchmark_checkpoint ADD COLUMN report_json TEXT")
        db.execute("INSERT OR REPLACE INTO benchmark_checkpoint "
                   "(id,output_path,report_sha256,report_json) VALUES(1,?,?,?)",
                   (str(output), hashlib.sha256(content).hexdigest(), content.decode()))
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(output)


def _load_checkpoint(output: Path, budget_path: Path) -> dict | None:
    output, budget_path = output.resolve(), budget_path.resolve()
    with sqlite3.connect(budget_path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS benchmark_checkpoint "
                   "(id INTEGER PRIMARY KEY CHECK(id=1), output_path TEXT NOT NULL, "
                   "report_sha256 TEXT NOT NULL, report_json TEXT)")
        columns = {row[1] for row in db.execute("PRAGMA table_info(benchmark_checkpoint)")}
        if "report_json" not in columns:
            db.execute("ALTER TABLE benchmark_checkpoint ADD COLUMN report_json TEXT")
        checkpoint = db.execute(
            "SELECT output_path, report_sha256, report_json FROM benchmark_checkpoint WHERE id=1"
        ).fetchone()
    if checkpoint is None:
        return None
    if not checkpoint[2] or checkpoint[1] != hashlib.sha256(checkpoint[2].encode()).hexdigest():
        raise ValueError("the canonical benchmark checkpoint is corrupt")
    if str(output) != checkpoint[0]:
        raise ValueError("budget ledger is already bound to another benchmark report")
    prior = json.loads(checkpoint[2])
    if not output.exists() or output.read_text(encoding="utf-8") != checkpoint[2]:
        _write_checkpoint(output, budget_path, prior)
    return prior


def _run_worker(command: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 900) -> tuple[int, str]:
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=(os.name != "nt"),
                                   creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0))
    except OSError as exc:
        return -1, type(exc).__name__
    try:
        _, stderr = process.communicate(timeout=timeout)
        if process.returncode == 0:
            return 0, ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        error_type = next((line.split(":", 1)[0] for line in reversed(stderr.splitlines())
                           if ":" in line and line.split(":", 1)[0].endswith(("Error", "Exception"))), None)
        suffix = f":{error_type}" if error_type else ""
        return process.returncode, f"worker_exit_{process.returncode}{suffix}"
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=5, check=False)
        else:
            _kill_process_tree(process.pid)
        process.communicate()
        return -1, "timeout"


def _kill_process_tree(pid: int) -> None:
    import signal
    try:
        rows = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True,
                              timeout=5, check=False).stdout.splitlines()
        children = {}
        for row in rows:
            child, parent = (int(part) for part in row.split())
            children.setdefault(parent, []).append(child)
        order, pending = [], [pid]
        while pending:
            current = pending.pop()
            order.append(current)
            pending.extend(children.get(current, ()))
        for child in reversed(order):
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
    except (OSError, subprocess.TimeoutExpired, ValueError):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


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
    endpoint = os.environ.get("KYROZEN_BENCHMARK_BASE_URL", "https://ai.panstar.ai/v1")
    key = os.environ.get("KYROZEN_BENCHMARK_API_KEY", "")
    if not key:
        raise ValueError("benchmark provider API key is unavailable")
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
            "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "latency_ms": 0,
            "attempts": 0, "first_delta_latency_ms": None, "verifier_result": "incomplete",
            "safety_passed": False, "durable_side_effect_expected": False,
            "durable_side_effect_verified": False, "recovery_required": False,
            "recovery_verified": False, "verified_success": False,
            "workflow_state_correct": False, "requests": 0, "protocol_recovery": 0,
            "duplicate_calls": 0, "plan_revisions": 0, "cancelled_tasks": 0,
            "material_revisions": 0, "unnecessary_churn": 0, "budget_ok": False,
            "plan_scenario": None, "plan_scenario_passed": True, "worker_exit_code": None,
            "worker_result_complete": False}


def _row_can_resume_as_complete(row: dict) -> bool:
    return (row.get("worker_result_complete") and row.get("usage_complete")
            and row.get("attempts", 0) > 0 and row.get("worker_exit_code") == 0)


def _workflow_state_correct(fixture: dict, worker: dict) -> bool:
    tasks = worker.get("tasks")
    plan_required = bool(fixture.get("plan_tasks")) or fixture.get("plan_scenario") in {
        "scope_preserving", "material_revision",
    }
    return (bool(tasks) and "succeeded" in tasks if plan_required else True) and all(
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


PRIOR_CAMPAIGN_HOLD_RMB = "6.122677920618"


def run(*, source: Path, repo: Path, output: Path, budget_path: Path,
        limit_usd: str = str(MAX_LIMIT_USD), repeats: int = REPEATS,
        fixtures: tuple[dict, ...] = FIXTURES) -> dict:
    source, repo = source.resolve(), repo.resolve()
    candidate = _source_snapshot(source)
    output, budget_path = output.resolve(), budget_path.resolve()
    if output.is_relative_to(source) or budget_path.is_relative_to(source):
        raise ValueError("benchmark output and budget ledger must be outside the source worktree")
    if repeats != REPEATS or len(fixtures) != len(FIXTURES):
        raise ValueError("release evaluation requires the frozen cohort and exactly three repeats")
    if fixtures != FIXTURES:
        raise ValueError("release evaluation requires the exact frozen fixture content")
    ledger_existed = budget_path.exists()
    limit = Decimal(limit_usd)
    if not Decimal(0) < limit <= MAX_LIMIT_USD:
        raise ValueError("the cumulative benchmark cap cannot exceed USD 10")
    limit_rmb = str(limit * USD_CNY)
    checkpoint = _load_checkpoint(output, budget_path)
    if checkpoint is None:
        if output.exists():
            raise ValueError("existing report is not pinned by its original budget ledger")
        if budget_path.exists():
            with sqlite3.connect(budget_path) as db:
                has_attempts = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attempts'"
                ).fetchone()
                if has_attempts and db.execute(
                        "SELECT 1 FROM attempts WHERE id!='prior-campaign-reservations' LIMIT 1").fetchone():
                    raise ValueError("cannot start a new report with a nonempty budget ledger")
    from openkyrozen.tools.sandbox import SandboxUnavailableError, sandbox_command
    probe_env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ}
    try:
        probe = sandbox_command("true", workspace=str(source), cwd=str(source), env=probe_env)
    except SandboxUnavailableError as exc:
        raise ValueError("V3 matched evaluation requires an available OS filesystem sandbox") from exc
    try:
        if probe.backend == "unavailable":
            raise ValueError("V3 matched evaluation requires an available OS filesystem sandbox")
    finally:
        probe.close()
    budget = BudgetLedger(budget_path, limit_rmb=limit_rmb,
                          prior_hold_rmb=PRIOR_CAMPAIGN_HOLD_RMB)
    if not budget.budget_ok:
        raise ValueError("the persisted budget ledger contains an overrun")
    rows: list[dict] = []
    if checkpoint is not None:
        if not ledger_existed:
            raise ValueError("cannot resume without the original budget-ledger checkpoint")
        prior = checkpoint
        if (prior.get("protocol") != "openkyrozen-v3-matched-live-v1"
                or prior.get("candidate_sha") != candidate or prior.get("baseline_sha") != BASELINE
                or prior.get("model") != MODEL or prior.get("repeats") != repeats
                or not isinstance(prior.get("rows"), list) or not isinstance(prior.get("calls"), list)):
            raise ValueError("budget-ledger checkpoint does not match this candidate and cohort")
        rows = prior["rows"]
        held_reservations = prior.get("held_reservations", [])
        journal_calls = budget.campaign_calls()
        expected_call_keys = {(fixture["fixture_id"], repeat, arm)
                              for fixture in fixtures for repeat in range(repeats) for arm in ("v2", "v3")}
        if any((call.get("fixture_id"), call.get("repeat"), call.get("arm")) not in expected_call_keys
               or call.get("model") != MODEL for call in journal_calls):
            raise ValueError("budget-ledger journal contains a call outside the frozen cohort")
        keys = [(row.get("fixture_id"), row.get("repeat"), row.get("arm")) for row in rows]
        if len(keys) != len(set(keys)):
            raise ValueError("existing benchmark report contains duplicate attempts")
        calls_by_key: dict[tuple, list[dict]] = {}
        for call in journal_calls:
            calls_by_key.setdefault((call.get("fixture_id"), call.get("repeat"), call.get("arm")), []).append(call)
        rows_by_key = dict(zip(keys, rows))
        for key, calls_for_key in calls_by_key.items():
            row = rows_by_key.get(key)
            if row is None:
                row = _empty_row(*key)
                rows.append(row)
                rows_by_key[key] = row
            usage = [call.get("usage") for call in calls_for_key]
            actual = {"attempts": len(calls_for_key), "requests": len(calls_for_key),
                      "input_tokens": sum(item["input_tokens"] for item in usage if item),
                      "output_tokens": sum(item["output_tokens"] for item in usage if item),
                      "cache_read_tokens": sum(item["cache_read_tokens"] for item in usage if item),
                      "cache_write_tokens": sum(item.get("cache_write_tokens", 0) for item in usage if item)}
            if any(row.get(name, 0) > value for name, value in actual.items()):
                raise ValueError("budget-ledger journal is missing a report's provider calls")
            if any(row.get(name, 0) != value for name, value in actual.items()):
                row.update(actual, usage_complete=bool(calls_for_key)
                           and all(isinstance(item, dict) for item in usage),
                           worker_result_complete=False,
                           worker_error="interrupted before fixture result was checkpointed")
    else:
        with sqlite3.connect(budget_path) as db:
            if db.execute("SELECT 1 FROM attempts WHERE id!='prior-campaign-reservations' LIMIT 1").fetchone():
                raise ValueError("cannot start a new report with a nonempty budget ledger")
        if output.exists():
            raise ValueError("existing report is not pinned by its original budget ledger")
        prior = {}
        held_reservations = []
    endpoint, api_key = _credential()
    relay = ResponsesRelay(endpoint=endpoint, api_key=api_key, budget=budget)
    if checkpoint is not None:
        relay.rows.extend(journal_calls)
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
                        previous = next((row for row in rows if
                                         (row["fixture_id"], row["repeat"], row["arm"])
                                         == (fixture_id, repeat, arm)), None)
                        if previous and _row_can_resume_as_complete(previous):
                            continue
                        if previous:
                            rows.remove(previous)
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
                             "--scenario", str(fixture.get("plan_scenario") or ""),
                             "--process-marker", str(fixture["verifier"].get("command_marker") or "")],
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
                                    "cache_write_tokens": sum(item.get("cache_write_tokens", 0) for item in usage if item),
                                    "latency_ms": round((time.monotonic() - started) * 1000, 2),
                                    "attempts": len(calls), "first_delta_latency_ms": worker.get("first_delta_latency_ms"),
                                    "verifier_result": verified["result"], "safety_passed": verified["safety_passed"],
                                    "plan_scenario": fixture.get("plan_scenario"),
                                    "plan_scenario_passed": verified["plan_scenario_passed"],
                                    "durable_side_effect_expected": fixture["verifier"]["kind"] in {"tests", "tests_and_file"},
                                    "durable_side_effect_verified": bool(verified["verified_success"] and verified["changed_paths"]),
                                    "recovery_required": fixture["category"] == "error_recovery",
                                    "recovery_verified": bool(verified["verified_success"]
                                                               and fixture["category"] == "error_recovery"
                                                               and _recovery_observed(worker)),
                                    "verified_success": bool(code == 0 and verified["verified_success"]),
                                    "workflow_state_correct": _workflow_state_correct(fixture, worker),
                                    "requests": len(calls), "protocol_recovery": int(worker.get("protocol_recovery", 0)),
                                    "duplicate_calls": int(worker.get("duplicate_calls", 0)),
                                    "plan_revisions": int(worker.get("plan_revisions", 0)),
                                    "cancelled_tasks": int(worker.get("cancelled_tasks", 0)),
                                    "material_revisions": int(worker.get("material_plan_proposals", 0)),
                                    "unnecessary_churn": int(worker.get("unnecessary_churn", 0)),
                                    "budget_ok": budget.budget_ok,
                                    "worker_error": worker_error,
                                    "worker_exit_code": code,
                                    "worker_result_complete": result_file.exists()})
                        if not result_file.exists() and not worker_error:
                            row["worker_error"] = "missing_result"
                        rows.append(row)
                        document = {"protocol": "openkyrozen-v3-matched-live-v1", "candidate_sha": candidate,
                                    "baseline_sha": BASELINE, "model": MODEL, "max_output_tokens": MAX_OUTPUT_TOKENS,
                                    "repeats": repeats, "pricing": PRICING,
                                    "status": "running", "rows": rows,
                                    "held_reservations": held_reservations,
                                    "calls": [{key: value for key, value in call.items() if key != "response_body"}
                                              for call in relay.rows],
                                    "budget": {"spent_usd": str(budget.spent_rmb / USD_CNY),
                                               "remaining_usd": str(budget.remaining_rmb / USD_CNY),
                                               "limit_usd": str(limit), "usd_cny": str(USD_CNY)}}
                        _write_checkpoint(output, budget_path, document)
                        print(json.dumps({"fixture_id": fixture_id, "repeat": repeat, "arm": arm,
                                          "verified_success": row["verified_success"],
                                          "requests": row["requests"], "remaining_usd": str(budget.remaining_rmb / USD_CNY)},
                                         sort_keys=True), flush=True)
                        if not budget.budget_ok:
                            raise RuntimeError("benchmark budget overrun; stop all model calls and investigate")
                        if relay.budget_blocked:
                            document["status"] = "blocked_budget_reservation"
                            _write_checkpoint(output, budget_path, document)
                            break
                        if relay.provider_error:
                            document["status"] = "blocked_provider_error"
                            document["provider_error"] = relay.provider_error
                            _write_checkpoint(output, budget_path, document)
                            break
                    if relay.budget_blocked or relay.provider_error:
                        break
                if relay.budget_blocked or relay.provider_error:
                    break
        finally:
            relay.close()
    summary = summarize(rows)
    report = {"protocol": "openkyrozen-v3-matched-live-v1", "candidate_sha": candidate,
              "baseline_sha": BASELINE, "model": MODEL, "pricing": PRICING, "repeats": repeats,
              "status": "blocked_provider_error" if relay.provider_error else
                        "blocked_budget_reservation" if relay.budget_blocked else
                        "complete" if summary["complete"] and summary["accounting_complete"] else "incomplete",
              "provider_error": relay.provider_error,
              "summary": summary,
              "rows": rows, "held_reservations": held_reservations,
              "budget": {"spent_usd": str(budget.spent_rmb / USD_CNY),
                         "remaining_usd": str(budget.remaining_rmb / USD_CNY),
                         "limit_usd": str(limit), "usd_cny": str(USD_CNY)},
              "calls": [{key: value for key, value in call.items() if key != "response_body"}
                        for call in relay.rows]}
    _write_checkpoint(output, budget_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-db", type=Path, required=True)
    parser.add_argument("--limit-usd", default=str(MAX_LIMIT_USD))
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
