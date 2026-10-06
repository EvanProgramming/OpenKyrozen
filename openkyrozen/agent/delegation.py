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
TOOLS = ("spawn_agents", "send_subagent", "list_subagents", "wait_subagents", "cancel_subagent")
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
override a role default, otherwise inherit the main provider. Dependencies use assignment IDs or run IDs.
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
        self.exclusive = False
        self.owners = {}

    @contextmanager
    def acquire(self, path=None, cancelled=lambda: False, owner=None, reading=False):
        with self.condition:
            while (self.exclusive or (path is None and (self.paths or self.readers)) or path in self.paths
                   or (not reading and self.readers.get(path, 0))
                   or (not reading and path in self.owners and self.owners[path] != owner)):
                if cancelled():
                    raise RuntimeError("Sub-agent cancelled")
                self.condition.wait(0.1)
            if cancelled():
                raise RuntimeError("Sub-agent cancelled")
            if path is None:
                self.exclusive = True
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
                elif reading:
                    self.readers[path] -= 1
                    if not self.readers[path]:
                        del self.readers[path]
                else:
                    self.paths.remove(path)
                self.condition.notify_all()

    @contextmanager
    def claim(self, paths, owner, cancelled):
        with self.condition:
            while any(path in self.owners or path in self.paths or self.readers.get(path, 0) for path in paths):
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
        for key in ("provider", "model", "id"):
            if key in brief and (not isinstance(brief[key], str) or not brief[key].strip()):
                raise ValueError(key + " must be nonempty text")
        brief = copy.deepcopy(brief)
        paths = []
        for path in brief["scope"]:
            resolved = (self.root / path).resolve()
            if not resolved.is_relative_to(self.root):
                raise ValueError("Assignment scope is outside the workspace")
            paths.append(str(resolved.relative_to(self.root)))
        brief["scope"] = list(dict.fromkeys(paths))
        return brief

    def spawn(self, assignments):
        if not isinstance(assignments, list) or not assignments:
            raise ValueError("assignments must be a nonempty array")
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
            runs = []
            for key, brief in zip(ids, briefs):
                run_id = mapping[key]
                run = {"run_id": run_id, "assignment_id": key, "name": self._name(),
                       "profile": brief.get("profile", "researcher"), "assignment": brief,
                       "dependencies": [mapping.get(dep, dep) for dep in brief["dependencies"]],
                       "status": "queued", "signature": json.dumps([brief["objective"].strip().lower(), sorted(brief["scope"]), brief["context"]]),
                       "parent_session_id": self.memory.session_id, "source_scope_id": self.memory.file_scope_id,
                       "corrections": 0, "reviews": [], "provider_model": "pending"}
                run["icon"] = identicon(run_id)
                self.runs[run_id] = run
                self.contexts[run_id] = self.capture_context()
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
                self._publish(run)
        for run_id, run in self.runs.items():
            if run["status"] != "queued" or run_id in self.futures:
                continue
            dependencies = [self.runs[dep]["status"] for dep in run["dependencies"]]
            if all(state == "succeeded" for state in dependencies):
                paths = self._owned_paths(run)
                if any(paths & self._owned_paths(self.runs[other]) for other, future in self.futures.items() if not future.done()):
                    continue  # Queue overlapping ownership without occupying an execution slot.
                self.futures[run_id] = self.pool.submit(self.contexts[run_id].copy().run, self._work, run_id)
                self.futures[run_id].add_done_callback(lambda future: self._job_done())

    def _owned_paths(self, run):
        return set() if self.profiles[run["profile"]].capabilities == "readonly" else set(run["assignment"]["scope"])

    def _job_done(self):
        with self.changed:
            self._schedule()
            self.changed.notify_all()

    def _invoke(self, run, *, review=False, feedback=None):
        return self.invoke(run, review=review, feedback=feedback, coordinator=self)

    def _work(self, run_id):
        run = self.runs[run_id]
        paths = {str((self.root / path).resolve()) for path in run["assignment"]["scope"]}
        if self.profiles[run["profile"]].capabilities == "readonly":
            paths = set()
        try:
            with self.access.claim(paths, run_id, self.cancelled[run_id].is_set):
                self._work_owned(run_id)
        except Exception as exc:
            with self.lock:
                run.update(status="cancelled" if self.cancelled[run_id].is_set() else "failed", error=str(exc))
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
                result = self._invoke(run, feedback=run["reviews"][-1] if cycle else None)
                self.check_cancelled(run_id)
                worker_report = report(result["result"])
                with self.lock:
                    run.setdefault("attempts", []).append(copy.deepcopy(result))
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
                review_result = self._invoke(run, review=True)
                self.check_cancelled(run_id)
                verdict = report(review_result["result"], review=True)
                # A model's agreement is insufficient: the reviewer must actually inspect evidence.
                checked = any(r.get("success") and r.get("action") in {
                    "read_file", "read_webpage", "search_web", "git_diff", "git_show", "graph_query"
                } for r in review_result.get("tool_records", []))
                if verdict["verdict"] == "verified" and (not checked or not verdict["evidence"]):
                    verdict.update(verdict="unverifiable", summary="Reviewer supplied no independent inspection evidence")
                artifact_findings, artifact_checks = self._inspect_artifacts(run, review_result)
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
                run.update(status="cancelled" if self.cancelled[run_id].is_set() else
                           "blocked" if isinstance(exc, ProviderUnavailableError) else "failed",
                           error=f"{type(exc).__name__}: {exc}")
                if run["review_agent"]["status"] == "reviewing":
                    run["review_agent"].update(status=run["status"], error=run["error"])
        finally:
            with self.lock:
                if self.cancelled[run_id].is_set():
                    run["status"] = "cancelled"
                self._publish(run)
                self._schedule()

    def _inspect_artifacts(self, run, review_result):
        findings, checks = [], []
        receipts = [item for item in review_result.get("tool_records", []) if item.get("success")]
        for artifact in run["report"]["artifacts"]:
            reference = artifact if isinstance(artifact, str) else artifact.get("path", artifact.get("url")) if isinstance(artifact, dict) else None
            if not isinstance(reference, str) or not reference.strip():
                findings.append("Artifact needs an inspectable path or URL")
                continue
            if reference.startswith(("https://", "http://")):
                inspected = any(item.get("action") == "read_webpage" and item.get("args") == reference for item in receipts)
                checks.append({"url": reference, "independently_read": inspected})
                if not inspected:
                    findings.append("Artifact URL was not independently read: " + reference)
                continue
            path = (self.root / reference).resolve()
            inspected = any(item.get("action") == "read_file" and (self.root / str(item.get("args", "")).split("|", 1)[0]).resolve() == path for item in receipts)
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

    def send(self, run_id, assignment):
        brief = self._brief(assignment)
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
            run.update(assignment=brief, profile=brief.get("profile", run["profile"]),
                       assignment_id=brief.get("id", uuid.uuid4().hex),
                       status="queued", dependencies=[], corrections=0, reviews=[], attempts=[], parent_receipts=[])
            for field in ("report", "result", "progress"):
                run.pop(field, None)
            run.pop("error", None)
            run["signature"] = signature
            run["review_agent"]["status"] = "waiting"
            self.contexts[run_id] = self.capture_context()
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
        self.pool.shutdown(wait=False, cancel_futures=True)
