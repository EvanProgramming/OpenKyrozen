"""Chat-owned delegation and evidence review, using the existing runner and store."""
from __future__ import annotations

import copy
import hashlib
import json
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from pathlib import Path

NAMES = ("Evan", "Morning", "Joseph", "Addison", "Jerry", "Leo", "Obert",
         "Jason", "Justin", "Dewey", "Old Fool", "Lianto")
from openkyrozen.tools.catalog import builtin_names
TOOLS = builtin_names("orchestration")
TERMINAL = {"succeeded", "unverified", "failed", "blocked", "cancelled", "interrupted"}
REPORT_LISTS = ("findings", "evidence", "artifacts", "checks", "uncertainties", "suggested_followups")
GUIDANCE = '''Delegate automatically when independent work can run in parallel or a specialist can
materially improve the result. Do not delegate greetings, single lookups, or duplicate work.
The user never needs to mention agents, parallelism, or verification. Detect the need from the work.
For a project-wide review spanning distinct subsystems or different kinds of specialist reasoning,
automatically use spawn_agents for focused investigations once the relevant targets are known.
Inspect the project structure first if needed; discovering files is preparation, not a completed review.
Before taking tools, decide which investigations are independent and which require shared sequencing.
When the user explicitly requests parallel specialist investigations, honor that request too.
If the independent targets are already known, spawn before doing their inspections yourself.
Delegated assignments already own their task lifecycle. Do not duplicate spawn/wait/review as
parent execution steps; use run states and wait_subagents. Keep parent task rows for direct main work.
Output delegation Actions separately from sequential task lists. Start with the spawn Action alone.
For other complex work, delegate where separate contexts add coverage or reduce dependencies;
keep a simple direct task in the main context. Explain why each assignment helps in its reason field.
Use spawn_agents with a JSON string: {"assignments":[{"id":"inspect","profile":"researcher",
"objective":"Inspect the relevant subsystem","context":"Relevant facts and paths",
"scope":["path/to/file"],"dependencies":[],"acceptance":["Cite the actual source"],
"deliverables":["Evidence-backed findings"],"reason":"Independent specialist investigation"}]}.
Scope lists exact writable files for coders; other profiles are read-only. Optional provider/model
override a role default, otherwise inherit the main provider. A custom provider can name its saved
profile with custom_profile. Dependencies use assignment IDs or run IDs.
Start independent assignments together, work on other tasks while they run, then use wait_subagents
({"run_ids":["returned ID"],"timeout":30}) and inspect the structured results and reviews.
Reuse an agent via send_subagent ({"run_id":"ID","assignment":{...same brief fields...}}).
Only the main agent delegates: workers suggest followups. Add agents for distinct unexplored work,
not merely because another agent exists. Do not repeat completed exploration without new evidence.
Every substantive result receives automatic independent review. Queued/running/reviewing is not
completion. Never claim success while assigned work is unfinished or unverified; resolve review
findings and report unresolved failures. Ask/Plan delegation retains read-only permissions.'''
GUIDANCE += '\nCanonical spawn example (args is an escaped JSON string):\nAction: ' + json.dumps({
    "action": "spawn_agents", "args": json.dumps({"assignments": [{"profile": "researcher",
        "objective": "Inspect parser correctness", "context": "Check parser.py against its documented invariants",
        "scope": ["parser.py"], "dependencies": [], "acceptance": ["Read actual source and check invariant"],
        "deliverables": ["Structured findings with evidence"], "reason": "Independent specialist inspection"}]})})


def identicon(seed: str) -> list[str]:
    digest = hashlib.sha256(seed.encode()).digest()
    rows = []
    for y in range(5):
        half = ["1" if digest[y * 3 + x] & 1 else "0" for x in range(3)]
        rows.append("".join(half + half[-2::-1]))
    return rows


def assignment_key(brief):
    return brief["objective"].strip().lower(), tuple(sorted(brief["scope"]))


def report(value, *, review=False):
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("```"):
            value = value.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("Result must be a JSON object")
    if review:
        if value.get("verdict") not in {"verified", "changes_requested", "unverifiable"}:
            raise ValueError("Review requires a valid verdict")
        keys = ("findings", "evidence")
    else:
        if value.get("status") not in {"completed", "partial", "blocked", "failed"}:
            raise ValueError("Result requires a valid status")
        keys = REPORT_LISTS
    if not isinstance(value.get("summary"), str) or not value["summary"].strip():
        raise ValueError("Result requires a summary")
    if any(not isinstance(value.get(key), list) for key in keys):
        raise ValueError("Result requires arrays: " + ", ".join(keys))
    return value


class WorkspaceAccess:
    """Disjoint writes overlap; opaque commands acquire exclusive workspace access."""
    def __init__(self):
        self.condition = threading.Condition()
        self.paths = set()
        self.readers = {}
        self.recursive_readers = {}
        self.exclusive = False
        self.owners = {}

    @contextmanager
    def acquire(self, path=None, cancelled=lambda: False, owner=None, reading=False, recursive=False):
        with self.condition:
            overlaps = lambda left, right: Path(left).is_relative_to(Path(right)) or Path(right).is_relative_to(Path(left))
            while (self.exclusive or (path is None and (self.paths or self.readers)) or path in self.paths
                   or (not reading and self.readers.get(path, 0))
                   or (path is None and self.recursive_readers)
                   or (not reading and any(Path(path).is_relative_to(Path(scope))
                                           for scope in self.recursive_readers))
                   or (reading and recursive and any(Path(active).is_relative_to(Path(path))
                                                     for active in self.paths))
                   or (path is not None and any(overlaps(path, active) and active_owner != owner
                                                 for active, active_owner in self.owners.items()))):
                if cancelled():
                    raise RuntimeError("Sub-agent cancelled")
                self.condition.wait(0.1)
            if cancelled():
                raise RuntimeError("Sub-agent cancelled")
            if path is None:
                self.exclusive = True
            elif reading and recursive:
                self.recursive_readers[path] = self.recursive_readers.get(path, 0) + 1
            elif reading:
                self.readers[path] = self.readers.get(path, 0) + 1
            else:
                self.paths.add(path)
        try:
            yield
        finally:
            with self.condition:
                if path is None:
                    self.exclusive = False
                elif reading and recursive:
                    self.recursive_readers[path] -= 1
                    if not self.recursive_readers[path]:
                        del self.recursive_readers[path]
                elif reading:
                    self.readers[path] -= 1
                    if not self.readers[path]:
                        del self.readers[path]
                else:
                    self.paths.remove(path)
                self.condition.notify_all()

    @contextmanager
    def claim(self, paths, owner, cancelled):
        overlaps = lambda left, right: Path(left).is_relative_to(Path(right)) or Path(right).is_relative_to(Path(left))
        with self.condition:
            while self.exclusive or any(
                any(overlaps(path, active) for active in self.owners)
                or any(overlaps(path, active) for active in self.paths)
                or any(count and overlaps(path, active) for active, count in self.readers.items())
                or any(Path(path).is_relative_to(Path(scope)) for scope in self.recursive_readers)
                for path in paths
            ):
                if cancelled():
                    raise RuntimeError("Sub-agent cancelled")
                self.condition.wait(.1)
            if cancelled():
                raise RuntimeError("Sub-agent cancelled")
            for path in paths:
                self.owners[path] = owner
        try:
            yield
        finally:
            with self.condition:
                for path in paths:
                    self.owners.pop(path, None)
                self.condition.notify_all()


class DelegationBudget:
    """Reserve a bounded share of one parent turn for child and review work."""

    CHILD_RESERVATION = 100_000_000_000
    REVIEW_RESERVATION = 250_000_000_000
    TURN_LIMIT = 1_000_000_000_000

    def __init__(self):
        self._lock = threading.RLock()
        self._commitments: dict[str, int] = {}
        self._unknown: set[str] = set()
        self._spent: dict[str, int] = {}

    @property
    def remaining_picos(self) -> int:
        with self._lock:
            return self.TURN_LIMIT - sum(self._commitments.values())

    def reserve(self, run_id: str, *, review: bool) -> None:
        amount = self.CHILD_RESERVATION + (self.REVIEW_RESERVATION if review else 0)
        with self._lock:
            if run_id in self._commitments or amount > self.TURN_LIMIT - sum(self._commitments.values()):
                raise ValueError("Delegation budget is unavailable for this assignment")
            self._commitments[run_id] = amount

    def settle(self, run_id: str, *, cost_picos: int | None) -> None:
        with self._lock:
            if run_id not in self._commitments:
                return
            if cost_picos is None:
                self._unknown.add(run_id)
                return
            self._commitments[run_id] = max(0, int(cost_picos))

    def record_cost(self, run_id: str, cost_picos: int | None) -> None:
        with self._lock:
            if run_id not in self._commitments:
                return
            if cost_picos is None:
                self._unknown.add(run_id)
            else:
                self._spent[run_id] = self._spent.get(run_id, 0) + max(0, int(cost_picos))

    def can_continue(self, run_id: str, *, cost_picos: int | None) -> bool:
        with self._lock:
            if run_id not in self._commitments:
                return False
            # Unknown pricing consumes the reservation for the turn but cannot meter individual calls.
            if run_id in self._unknown or cost_picos is None:
                return True
            return self._spent.get(run_id, int(cost_picos)) < self._commitments[run_id]

    def release(self, run_id: str) -> None:
        with self._lock:
            self._commitments.pop(run_id, None)
            self._unknown.discard(run_id)
            self._spent.pop(run_id, None)


class Delegation:
    def __init__(self, memory, invoke, *, profiles, root, concurrency=4, emit=None, access=None, capture_context=copy_context, redact=str, on_verified=None):
        self.memory, self.invoke, self.profiles = memory, invoke, profiles
        self.root = Path(root).resolve()
        self.emit = emit or (lambda event: None)
        self.access = access or WorkspaceAccess()
        self.capture_context = capture_context
        self.redact = redact
        self.on_verified = on_verified or (lambda run: [])
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="kyrozen-agent")
        self.runs, self.messages, self.cancelled, self.futures = {}, {}, {}, {}
        self.contexts = {}
        self.budgets = {}
        self.names = set()
        self._restore()

    def _restore(self):
        for event in reversed(self.memory.store.list_events(
                "subagent.state", limit=None, user_id=self.memory.user_id,
                workspace_id=self.memory.file_scope_id, session_id=self.memory.session_id)):
            run = event["payload"]
            self.runs[run["run_id"]] = run
            self.names.add(run["name"])
            if run.get("reviewer"):
                self.names.add(run["reviewer"])
        for run in self.runs.values():
            self.cancelled[run["run_id"]] = threading.Event()
            if run["status"] not in TERMINAL:
                run.update(status="interrupted", error="Runtime restarted; mutations were not replayed.")
                run["review_agent"]["status"] = "interrupted"
                self._publish(run)

    def _name(self):
        cycle = 1
        while True:
            available = [name + (str(cycle) if cycle > 1 else "") for name in NAMES
                         if name + (str(cycle) if cycle > 1 else "") not in self.names]
            if available:
                name = random.SystemRandom().choice(available)
                self.names.add(name)
                return name
            cycle += 1

    def _publish(self, run):
        run["version"] = run.get("version", 0) + 1
        self.memory.store.append_event("subagent.state", self._safe(run),
            user_id=self.memory.user_id, workspace_id=self.memory.file_scope_id,
            session_id=self.memory.session_id)
        with self.changed:
            self.changed.notify_all()
        try:
            self.emit({"event": "subagent", "agent": self.summary(run),
                       "session_id": self.memory.session_id, "source_scope_id": self.memory.file_scope_id})
        except Exception:
            pass  # UI observers cannot change execution semantics.

    def add_message(self, run_id, message):
        with self.lock:
            message = self._safe(message)
            self.messages.setdefault(run_id, []).append(message)
            self.memory.store.append_event("subagent.message", {"run_id": run_id, "message": message},
                user_id=self.memory.user_id, workspace_id=self.memory.file_scope_id,
                session_id=self.memory.session_id)

    def _safe(self, value):
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {key: "<redacted>" if key.lower() in {"api_key", "password", "secret", "token", "authorization"}
                    else self._safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._safe(item) for item in value]
        return value

    def summary(self, run, *, results=False):
        value = {key: copy.deepcopy(run[key]) for key in (
            "run_id", "name", "profile", "assignment", "status", "icon", "version", "provider_model",
            "review_agent", "parent_session_id", "source_scope_id", "progress", "corrections", "error") if key in run}
        if results:
            value.update(report=run.get("report"), usage=run.get("usage", {}),
                reviews=[{key: item for key, item in review.items() if key != "tool_receipts"} for review in run["reviews"]],
                receipt_ids=[item.get("receipt_id") for item in run.get("result", {}).get("tool_records", [])])
        return self._safe(value)

    def snapshot(self):
        with self.lock:
            return [self.summary(run) for run in self.runs.values()]

    def record_receipt(self, run_id, child_id, receipt):
        with self.lock:
            run = self.runs[run_id]
            self.memory.store.append_event("subagent.receipt", self._safe({"run_id": run_id,
                "agent_session_id": child_id, "receipt": receipt}), user_id=self.memory.user_id,
                workspace_id=self.memory.file_scope_id, session_id=self.memory.session_id)
            run["progress"] = {"steps": run.get("progress", {}).get("steps", 0) + 1,
                "action": receipt["action"], "success": receipt["success"], "agent_session_id": child_id}
            self._publish(run)

    def _brief(self, brief):
        if not isinstance(brief, dict):
            raise ValueError("Assignment must be an object")
        for key in ("objective", "context", "reason"):
            if not isinstance(brief.get(key), str) or not brief[key].strip():
                raise ValueError("Assignment requires " + key)
        for key in ("scope", "dependencies", "acceptance", "deliverables"):
            if not isinstance(brief.get(key), list) or any(not isinstance(x, str) or not x.strip() for x in brief[key]):
                raise ValueError("Assignment requires a string array: " + key)
        if not brief["acceptance"] or not brief["deliverables"]:
            raise ValueError("Acceptance and deliverables cannot be empty")
        if brief.get("profile", "researcher") not in self.profiles:
            raise ValueError("Unknown sub-agent profile")
        if brief.get("risk", "normal") not in {"low", "normal", "high"}:
            raise ValueError("risk must be low, normal, or high")
        for key in ("provider", "model", "custom_profile", "id"):
            if key in brief and (not isinstance(brief[key], str) or not brief[key].strip()):
                raise ValueError(key + " must be nonempty text")
        if "task_id" in brief and (not isinstance(brief["task_id"], str) or not brief["task_id"].strip()):
            raise ValueError("task_id must be nonempty text")
        brief = copy.deepcopy(brief)
        paths = []
        for path in brief["scope"]:
            resolved = (self.root / path).resolve()
            if not resolved.is_relative_to(self.root):
                raise ValueError("Assignment scope is outside the workspace")
            paths.append(str(resolved.relative_to(self.root)))
        brief["scope"] = list(dict.fromkeys(paths))
        return brief

    def _requires_review(self, brief):
        profile = self.profiles[brief.get("profile", "researcher")]
        broad_scope = len(brief["scope"]) > 1 or "." in brief["scope"] or any(
            (self.root / path).is_dir() for path in brief["scope"]
        )
        return profile.capabilities != "readonly" or broad_scope or brief.get("risk", "normal") == "high"

    def spawn(self, assignments, *, budget=None):
        if not isinstance(assignments, list) or not assignments:
            raise ValueError("assignments must be a nonempty array")
        budget = budget or DelegationBudget()
        briefs = [self._brief(item) for item in assignments]
        with self.lock:
            ids = [b.get("id", uuid.uuid4().hex) for b in briefs]
            if len(set(ids)) != len(ids):
                raise ValueError("Duplicate assignment IDs")
            mapping = {key: "subagent_" + uuid.uuid4().hex for key in ids}
            known = set(self.runs) | set(mapping)
            signatures = set()
            active_keys = {assignment_key(run["assignment"]) for run in self.runs.values() if run["status"] not in TERMINAL}
            for key, brief in zip(ids, briefs):
                if key in self.runs or any(key == r.get("assignment_id") for r in self.runs.values()):
                    raise ValueError("Assignment ID already exists")
                if any(dep not in known for dep in brief["dependencies"]):
                    raise ValueError("Unknown dependency")
                signature = json.dumps([brief["objective"].strip().lower(), sorted(brief["scope"]), brief["context"]])
                if assignment_key(brief) in active_keys:
                    raise ValueError("Duplicate active assignment")
                active_keys.add(assignment_key(brief))
                if signature in signatures or any(r["signature"] == signature and r["status"] not in {"failed", "blocked", "cancelled", "interrupted"} for r in self.runs.values()):
                    raise ValueError("Duplicate exploration requires new evidence and a distinct objective")
                signatures.add(signature)
            edges = {key: set(b["dependencies"]) & set(mapping) for key, b in zip(ids, briefs)}
            pending = set(edges)
            while pending:
                ready = {key for key in pending if not edges[key] & pending}
                if not ready:
                    raise ValueError("Cyclic dependencies")
                pending -= ready
            reserved = []
            try:
                for key, brief in zip(ids, briefs):
                    run_id = mapping[key]
                    budget.reserve(run_id, review=self._requires_review(brief))
                    reserved.append(run_id)
            except Exception:
                for run_id in reserved:
                    budget.release(run_id)
                raise
            runs = []
            for key, brief in zip(ids, briefs):
                run_id = mapping[key]
                run = {"run_id": run_id, "assignment_id": key, "name": self._name(),
                       "profile": brief.get("profile", "researcher"), "assignment": brief,
                       "budget_id": run_id,
                       "dependencies": [mapping.get(dep, dep) for dep in brief["dependencies"]],
                       "status": "queued", "signature": json.dumps([brief["objective"].strip().lower(), sorted(brief["scope"]), brief["context"]]),
                       "parent_session_id": self.memory.session_id, "source_scope_id": self.memory.file_scope_id,
                       "corrections": 0, "reviews": [], "provider_model": "pending"}
                run["icon"] = identicon(run_id)
                self.runs[run_id] = run
                self.contexts[run_id] = self.capture_context()
                self.budgets[run_id] = budget
                self.cancelled[run_id] = threading.Event()
                runs.append(run)
            # Peer identities are fixed before any work starts; review contexts remain independent.
            for i, run in enumerate(runs):
                run["reviewer"] = runs[(i + 1) % len(runs)]["name"] if len(runs) > 1 else self._name()
                run["review_agent"] = {"name": run["reviewer"], "profile": "reviewer",
                    "icon": runs[(i + 1) % len(runs)]["icon"] if len(runs) > 1 else identicon(run["run_id"] + "_reviewer"), "status": "waiting",
                    "provider_model": "pending"}
                self._publish(run)
            self._schedule()
            return copy.deepcopy(runs)

    def _schedule(self):
        # Propagate failed dependencies in either order, including long chains.
        while True:
            blocked = [run for run in self.runs.values() if run["status"] == "queued" and
                       any(self.runs[dep]["status"] in TERMINAL - {"succeeded"} for dep in run["dependencies"])]
            if not blocked:
                break
            for run in blocked:
                run.update(status="blocked", error="Dependency did not pass independent verification")
                run["review_agent"]["status"] = "blocked"
                budget = self.budgets.get(run["run_id"])
                if budget is not None:
                    budget.settle(run.get("budget_id", run["run_id"]), cost_picos=0)
                self._publish(run)
        for run_id, run in self.runs.items():
            if run["status"] != "queued" or run_id in self.futures:
                continue
            dependencies = [self.runs[dep]["status"] for dep in run["dependencies"]]
            if all(state == "succeeded" for state in dependencies):
                paths = self._owned_paths(run)
                if any(any(Path(left).is_relative_to(Path(right)) or Path(right).is_relative_to(Path(left))
                           for left in paths for right in self._owned_paths(self.runs[other]))
                       for other, future in self.futures.items() if not future.done()):
                    continue  # Queue overlapping ownership without occupying an execution slot.
                self.futures[run_id] = self.pool.submit(self.contexts[run_id].copy().run, self._work, run_id)
                self.futures[run_id].add_done_callback(lambda future: self._job_done())

    def _owned_paths(self, run):
        return set() if self.profiles[run["profile"]].capabilities == "readonly" else {
            str(self.root / path) for path in run["assignment"]["scope"]
        }

    def _job_done(self):
        with self.changed:
            self._schedule()
            self.changed.notify_all()

    def _invoke(self, run, *, review=False, feedback=None, report_repair=False):
        return self.invoke(run, review=review, feedback=feedback, report_repair=report_repair, coordinator=self)

    def _work(self, run_id):
        run = self.runs[run_id]
        paths = {str(self.root / path) for path in run["assignment"]["scope"]}
        if self.profiles[run["profile"]].capabilities == "readonly":
            paths = set()
        try:
            with self.access.claim(paths, run_id, self.cancelled[run_id].is_set):
                self._work_owned(run_id)
        except Exception as exc:
            with self.lock:
                run.update(status="cancelled" if self.cancelled[run_id].is_set() else "failed", error=str(exc))
                budget = self.budgets.get(run_id)
                if budget is not None:
                    costs = [item.get("cost_picos") for item in run.get("usage", {}).values()]
                    budget.settle(run.get("budget_id", run_id),
                                  cost_picos=sum(costs) if costs and all(value is not None for value in costs) else (0 if not costs else None))
                self._publish(run)
                self._schedule()

    def _work_owned(self, run_id):
        try:
            with self.lock:
                run = self.runs[run_id]
                if self.cancelled[run_id].is_set():
                    return
                run["status"] = "running"
                self._publish(run)
            for cycle in range(3):
                self.check_cancelled(run_id)
                feedback = run["reviews"][-1] if cycle else None
                report_attempts = []
                for repair in range(3):
                    result = self._invoke(run, feedback=feedback, report_repair=repair > 0)
                    report_attempts.append(copy.deepcopy(result))
                    self.check_cancelled(run_id)
                    try:
                        worker_report = report(result["result"])
                        break
                    except (ValueError, json.JSONDecodeError) as exc:
                        if repair == 2:
                            raise ValueError("Malformed delegated report after two repairs") from exc
                        feedback = {"instruction": "Return only a valid JSON report with every required field. Do not call tools.",
                                    "invalid_response": result.get("result", "")[:4000]}
                with self.lock:
                    run.setdefault("attempts", []).extend(report_attempts)
                    result["tool_records"] = [receipt for attempt in run["attempts"] for receipt in attempt.get("tool_records", [])]
                    result["evidence"] = [evidence for attempt in run["attempts"] for evidence in attempt.get("evidence", [])]
                    run.update(result=result, report=worker_report,
                               provider_model=result.get("metrics", {}).get("provider_model", "unknown")
                                   if result.get("metrics", {}).get("provider_model", "unknown") != "unknown" else run["provider_model"],
                               status="reviewing", corrections=cycle)
                    self._publish(run)
                if worker_report["status"] not in {"completed", "partial"}:
                    with self.lock:
                        run["status"] = worker_report["status"]
                    break
                if not self._requires_review(run["assignment"]):
                    verified = self._verify_read_only_result(run, worker_report, result)
                    with self.lock:
                        run["reviews"].append({
                            "verdict": "verified" if verified else "unverifiable",
                            "summary": "Parent validated scoped read receipts" if verified else
                                "Read-only result did not bind its evidence to successful scoped receipts",
                            "findings": [] if verified else ["Evidence was not independently bound to tool receipts"],
                            "evidence": worker_report["evidence"] if verified else [],
                            "reviewer": "parent-runtime",
                            "verification": "receipt-bound-read-only",
                        })
                        if verified and worker_report["status"] == "completed":
                            run["parent_receipts"] = self.on_verified(run)
                            run["status"] = "succeeded"
                        else:
                            run.update(status="unverified", error="Read-only result did not pass parent verification")
                        run["review_agent"]["status"] = "skipped" if verified else "unverifiable"
                        self._publish(run)
                    break
                for inspection_attempt in range(3):
                    review_result = self._invoke(run, review=True, feedback=feedback if inspection_attempt else None)
                    self.check_cancelled(run_id)
                    verdict = report(review_result["result"], review=True)
                    checked = any(r.get("success") and r.get("action") in {
                        "read_file",
                    } for r in review_result.get("tool_records", []))
                    if verdict["verdict"] == "verified" and (not checked or not verdict["evidence"]):
                        verdict.update(verdict="unverifiable", summary="Reviewer supplied no independent inspection evidence")
                    artifact_findings, artifact_checks = self._inspect_artifacts(run, review_result)
                    if not artifact_findings or inspection_attempt == 2:
                        break
                    feedback = {"findings": artifact_findings,
                                "instruction": "Verification requires independent tool inspection. Read every declared artifact and return a JSON review."}
                if artifact_findings and verdict["verdict"] == "verified":
                    verdict.update(verdict="changes_requested", summary="Runtime artifact checks did not pass")
                    verdict["findings"].extend(artifact_findings)
                with self.lock:
                    run["reviews"].append({**verdict, "reviewer": run["reviewer"],
                        "session_id": review_result.get("session_id"),
                        "artifact_checks": artifact_checks,
                        "tool_receipts": review_result.get("tool_records", []), "metrics": review_result.get("metrics", {})})
                    run["review_agent"]["status"] = verdict["verdict"]
                    if review_result.get("metrics", {}).get("provider_model", "unknown") != "unknown":
                        run["review_agent"]["provider_model"] = review_result["metrics"]["provider_model"]
                    if verdict["verdict"] == "verified":
                        if worker_report["status"] == "completed":
                            run["parent_receipts"] = self.on_verified(run)
                            run["status"] = "succeeded"
                        else:
                            run.update(status="unverified", error="Partial findings reviewed; assignment acceptance remains incomplete")
                        break
                    run["status"] = "unverified" if cycle == 2 or verdict["verdict"] == "unverifiable" else "running"
                    self._publish(run)
                if run["status"] == "unverified":
                    break
        except (Exception, SystemExit) as exc:
            with self.lock:
                run = self.runs[run_id]
                from .types import ProviderUnavailableError
                error = (f"Malformed delegated report after two repairs: {exc}"
                         if isinstance(exc, json.JSONDecodeError) else f"{type(exc).__name__}: {exc}")
                run.update(status="cancelled" if self.cancelled[run_id].is_set() else
                           "blocked" if isinstance(exc, ProviderUnavailableError) else "failed",
                           error=error)
                if run["review_agent"]["status"] == "reviewing":
                    run["review_agent"].update(status=run["status"], error=run["error"])
        finally:
            with self.lock:
                if self.cancelled[run_id].is_set():
                    run["status"] = "cancelled"
                budget = self.budgets.get(run_id)
                if budget is not None:
                    costs = [item.get("cost_picos") for item in run.get("usage", {}).values()]
                    total_cost = sum(costs) if costs and all(value is not None for value in costs) else None
                    budget.settle(run.get("budget_id", run_id), cost_picos=total_cost)
                self._publish(run)
                self._schedule()

    def _verify_read_only_result(self, run, worker_report, result):
        if worker_report["status"] != "completed" or worker_report["artifacts"]:
            return False
        receipts = [item for item in result.get("tool_records", []) if item.get("success")]
        scopes = [(self.root / value).resolve() for value in run["assignment"]["scope"]]
        def in_scope(raw):
            try:
                path = Path(raw)
                path = (path if path.is_absolute() else self.root / path).resolve()
                return any(path == scope or path.is_relative_to(scope) for scope in scopes)
            except (OSError, TypeError, ValueError):
                return False
        inspected = {item.get("receipt_id") for item in receipts if item.get("action") == "read_file"
                     and in_scope((item.get("args") or {}).get("path") if isinstance(item.get("args"), dict) else
                                  str(item.get("args", "")).split("|", 1)[0])}
        evidence = worker_report["evidence"]
        if not inspected or not evidence or not worker_report.get("checks"):
            return False
        return all(isinstance(item, dict) and item.get("receipt_id") in inspected for item in evidence)

    def _inspect_artifacts(self, run, review_result):
        findings, checks = [], []
        receipts = [item for item in review_result.get("tool_records", []) if item.get("success")]
        for artifact in run["report"]["artifacts"]:
            reference = artifact if isinstance(artifact, str) else artifact.get("path", artifact.get("url")) if isinstance(artifact, dict) else None
            if not isinstance(reference, str) or not reference.strip():
                findings.append("Artifact needs an inspectable path or URL")
                continue
            def read_argument(item, field):
                args = item.get("args", "")
                if isinstance(args, dict):
                    return args.get(field, "")
                try:
                    parsed = json.loads(args)
                    if isinstance(parsed, dict): return parsed.get(field, "")
                except (json.JSONDecodeError, TypeError):
                    pass
                return str(args).split("|", 1)[0] if field == "path" else args
            if reference.startswith(("https://", "http://")):
                inspected = any(item.get("action") == "read_webpage" and read_argument(item, "url") == reference for item in receipts)
                checks.append({"url": reference, "independently_read": inspected})
                if not inspected:
                    findings.append("Artifact URL was not independently read: " + reference)
                continue
            path = (self.root / reference).resolve()
            inspected = any(item.get("action") == "read_file" and
                            (self.root / str(read_argument(item, "path"))).resolve() == path
                            for item in receipts)
            exists = path.is_relative_to(self.root) and path.is_file()
            check = {"path": reference, "exists": exists, "independently_read": inspected}
            if not exists:
                findings.append("Artifact missing or outside assigned workspace: " + reference)
            elif not inspected:
                findings.append("Artifact was not independently read: " + reference)
            else:
                with self.access.acquire(str(path), self.cancelled[run["run_id"]].is_set, reading=True):
                    with path.open("rb") as source:
                        check["sha256"] = hashlib.file_digest(source, "sha256").hexdigest()
                if isinstance(artifact, dict) and artifact.get("sha256") and artifact["sha256"] != check["sha256"]:
                    findings.append("Artifact hash disagrees with actual file: " + reference)
            checks.append(check)
        return findings, checks

    def check_cancelled(self, run_id):
        if self.cancelled[run_id].is_set():
            raise RuntimeError("Sub-agent cancelled")

    def list(self):
        with self.lock:
            return self._safe(list(self.runs.values()))

    def detail(self, run_id):
        with self.lock:
            if run_id not in self.runs:
                raise ValueError("Unknown sub-agent in this chat/project")
            message_events = list(reversed(self.memory.store.list_events(
                "subagent.message", limit=None, user_id=self.memory.user_id,
                workspace_id=self.memory.file_scope_id, session_id=self.memory.session_id)))
            messages = [event["payload"]["message"] for event in message_events if event["payload"].get("run_id") == run_id]
            review_messages = {review["session_id"]: [event["payload"]["message"] for event in message_events
                if event["payload"].get("run_id") == review["session_id"]]
                for review in self.runs[run_id]["reviews"] if review.get("session_id")}
            child_sessions = {run_id, self.runs[run_id].get("budget_id", run_id)}
            child_sessions.update(item.get("session_id") for item in self.runs[run_id].get("attempts", [])
                                  if item.get("session_id"))
            child_sessions.update(review.get("session_id") for review in self.runs[run_id]["reviews"]
                                  if review.get("session_id"))
            for child_session in child_sessions:
                from .conversation import ConversationHistory
                child_messages = ConversationHistory(
                    self.memory.store, user_id=self.memory.user_id,
                    workspace_id=self.memory.file_scope_id, session_id=child_session,
                ).display_messages()
                if child_session in {run_id, self.runs[run_id].get("budget_id", run_id)}:
                    messages = child_messages
                else:
                    review_messages[child_session] = child_messages
            # ponytail: scan scoped events; add an indexed projection when long chats justify it.
            receipts = [event["payload"] for event in reversed(self.memory.store.list_events(
                "subagent.receipt", limit=None, user_id=self.memory.user_id,
                workspace_id=self.memory.file_scope_id, session_id=self.memory.session_id))
                if event["payload"].get("run_id") == run_id]
            return {**self._safe(self.runs[run_id]), "messages": messages, "review_messages": review_messages, "receipts": receipts}

    def wait(self, run_ids=None, timeout=30):
        deadline = time.monotonic() + max(0, min(float(timeout), 60))
        with self.changed:
            selected = list(self.runs) if run_ids is None else run_ids
            if not isinstance(selected, list) or any(key not in self.runs for key in selected):
                raise ValueError("Unknown sub-agent in this chat/project")
            while any(self.runs[key]["status"] not in TERMINAL or
                      (self.runs[key]["status"] != "cancelled" and key in self.futures and not self.futures[key].done())
                      for key in selected):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.changed.wait(remaining)
            return copy.deepcopy([self.runs[key] for key in selected])

    def cancel(self, run_id):
        with self.lock:
            if run_id not in self.runs:
                raise ValueError("Unknown sub-agent in this chat/project")
            if self.runs[run_id]["status"] not in TERMINAL:
                self.cancelled[run_id].set()
                self.runs[run_id]["status"] = "cancelled"
                self.runs[run_id]["review_agent"]["status"] = "cancelled"
                self._publish(self.runs[run_id])
                self._schedule()
            return copy.deepcopy(self.runs[run_id])

    def send(self, run_id, assignment, *, budget=None):
        brief = self._brief(assignment)
        budget = budget or DelegationBudget()
        with self.lock:
            if run_id not in self.runs or self.runs[run_id]["status"] not in TERMINAL:
                raise ValueError("Reuse requires a finished agent in this chat")
            if run_id in self.futures and not self.futures[run_id].done():
                raise ValueError("Agent is still finishing; wait before reuse")
            run = self.runs[run_id]
            if brief.get("id") and any(brief["id"] == other["assignment_id"] for key, other in self.runs.items() if key != run_id):
                raise ValueError("Assignment ID already exists")
            if brief["dependencies"]:
                raise ValueError("Followup dependencies must be completed before sending")
            if brief["objective"] == run["assignment"]["objective"]:
                raise ValueError("Followup must supply a distinct objective and new context")
            signature = json.dumps([brief["objective"].strip().lower(), sorted(brief["scope"]), brief["context"]])
            if any(assignment_key(other["assignment"]) == assignment_key(brief) and other["status"] not in TERMINAL
                   for key, other in self.runs.items() if key != run_id):
                raise ValueError("Duplicate active assignment")
            budget_id = f"{run_id}_followup_{uuid.uuid4().hex}"
            budget.reserve(budget_id, review=self._requires_review(brief))
            run.update(assignment=brief, profile=brief.get("profile", run["profile"]),
                       budget_id=budget_id,
                       assignment_id=brief.get("id", uuid.uuid4().hex),
                       status="queued", dependencies=[], corrections=0, reviews=[], attempts=[], parent_receipts=[])
            run["usage"] = {}
            for field in ("report", "result", "progress"):
                run.pop(field, None)
            run.pop("error", None)
            run["signature"] = signature
            run["review_agent"]["status"] = "waiting"
            self.contexts[run_id] = self.capture_context()
            self.budgets[run_id] = budget
            if run_id not in self.messages:
                self.messages[run_id] = self.detail(run_id)["messages"]
            self.cancelled[run_id].clear()
            self.futures.pop(run_id, None)
            self._publish(run)
            self._schedule()
            return copy.deepcopy(run)

    def close(self):
        for run in self.list():
            self.cancel(run["run_id"])
        self.pool.shutdown(wait=True, cancel_futures=True)
