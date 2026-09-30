from __future__ import annotations

import json
import platform
import re
import sys
import uuid
from typing import Any
from openkyrozen.persistence.models import stable_hash
from .ports import LearningStore
from openkyrozen.memory.service import MemoryBank

from .models import CAPSULE_PROTOCOL, DEFAULT_CONSTITUTION, EVOLUTION_PROFILES, NEGATIVE_FEEDBACK, POSITIVE_FEEDBACK
from openkyrozen.memory.models import CLAIM_SCOPES, SCOPE_RANK, _SECRET_RE

class LearningEngine:
    """Coordinates profile learning with injected memory and repositories."""
    from .context import constitution, artifact_context, negative_preflight
    from .candidate import propose_artifact, submit
    from .evidence import record_shadow_replay, verifier_reliability, evidence_card
    from .promotion import record_outcome, _artifact_outcomes, _proposal_for_skill, _reconcile_artifact, validate_and_activate
    from .canary import record_omission_trial
    from .rollback import retire_artifact, restore_retired, rollback
    from .capsules import export_capsule, import_capsule
    from .reviews import pending_reviews, mark_reviewed
    from .metrics import status, metrics
    from openkyrozen.memory.claims import ClaimService

    def __init__(self, memory: MemoryBank, store: LearningStore | None = None,
                 registry: "SkillRegistry | None" = None):
        self.memory = memory
        self.store = store or memory.store
        self.registry = registry


    def attach_registry(self, registry: "SkillRegistry") -> None:
        self.registry = registry


    @staticmethod
    def _clean(content: str) -> str:
        return re.sub(r"\s+", " ", str(content)).strip()[:4000]


    @staticmethod
    def route_profile(task: str, override: str = "auto") -> str:
        if override in EVOLUTION_PROFILES:
            return override
        text = str(task).lower()
        coder_terms = {
            "code", "repo", "repository", "bug", "test", "build", "commit", "git", "python", "javascript",
            "typescript", "swift", "rust", "function", "class", "terminal", "shell", "api", "database",
            "代码", "仓库", "修复", "测试", "构建", "提交", "终端", "数据库",
        }
        tokens = set(re.findall(r"[\w+#.-]+", text, re.UNICODE))
        return "coder" if coder_terms & tokens else "researcher"


    @staticmethod
    def task_signature(profile: str, task: str) -> str:
        terms = sorted(set(re.findall(r"[\w.+#-]{2,40}", str(task).lower(), re.UNICODE)))[:24]
        return stable_hash(f"{profile}:{' '.join(terms)}")[:20]


    @staticmethod
    def feedback_signal(text: str) -> str | None:
        lowered = str(text).lower()
        if any(term in lowered for term in NEGATIVE_FEEDBACK):
            return "failure"
        if any(term in lowered for term in POSITIVE_FEEDBACK):
            return "success"
        return None


    def begin_run(self, profile: str, task: str, *, provider_model: str | None = None) -> dict[str, str]:
        if profile not in EVOLUTION_PROFILES:
            raise ValueError("profile must be coder or researcher")
        environment = self.environment_fingerprint()
        run = {"run_id": f"learnrun_{uuid.uuid4().hex}", "profile": profile,
               "task_signature": self.task_signature(profile, task), "task": str(task)[:4000],
               "environment_hash": environment["hash"], "provider_model": provider_model or "unspecified"}
        self.store.append_event("learning.run_started", run, user_id=self.memory.user_id,
                                workspace_id=self.memory.workspace_id, session_id=self.memory.session_id)
        return run


    def environment_fingerprint(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return a stable, secret-free applicability fingerprint."""
        values = {"system": platform.system(), "machine": platform.machine(),
                  "python": f"{sys.version_info.major}.{sys.version_info.minor}",
                  "workspace": self.memory.workspace_id, **(extra or {})}
        return {"values": values, "hash": stable_hash(json.dumps(values, sort_keys=True, default=str))}


    def complete_run(self, run: dict[str, str], *, result: str, receipts: list[dict[str, Any]],
                     tools: list[dict[str, Any]], tokens: int | None, latency: float | None,
                     acceptance: list[dict[str, Any]] | None = None,
                     provider_error: bool = False, task_statuses: list[dict[str, Any]] | None = None,
                     eligible: bool | None = None) -> None:
        errors = sum(1 for item in tools if not item.get("success"))
        contains_secret = bool(_SECRET_RE.search(json.dumps(
            {"task": run.get("task"), "result": result, "tools": tools}, ensure_ascii=False,
        )))
        status_rows = [
            {"id": str(item.get("id", "")), "status": str(item.get("status", "")),
             "description": str(item.get("description", ""))[:500]}
            for item in (task_statuses or [])
        ]
        durable_complete = (all(item["status"] == "succeeded" for item in status_rows)
                            if task_statuses is not None else None)
        payload = {**run, "result": str(result)[:4000], "receipts": receipts, "tools": tools[:50],
                   "tool_calls": len(tools), "errors": errors,
                   "tokens": None if tokens is None else max(0, int(tokens)),
                   "latency": None if latency is None else max(0.0, float(latency)),
                   "provider_error": bool(provider_error),
                   "acceptance_evidence": (acceptance or [])[:20],
                   "contains_secret": contains_secret,
                   "task_statuses": status_rows,
                   "durable_complete": durable_complete,
                   "eligible": (not provider_error and not contains_secret and len(tools) >= 2
                                if eligible is None else bool(eligible) and not provider_error
                                and not contains_secret)}
        self.store.append_event("learning.run_completed", payload, user_id=self.memory.user_id,
                                workspace_id=self.memory.workspace_id, session_id=self.memory.session_id)


    def remember_claim(self, *args, **kwargs):
        return self.memory.claims(registry=self.registry).remember_claim(*args, **kwargs)

    def resolve_claim(self, *args, **kwargs):
        return self.memory.claims(registry=self.registry).resolve_claim(*args, **kwargs)

    def explain_claim(self, *args, **kwargs):
        return self.memory.claims(registry=self.registry).explain_claim(*args, **kwargs)

    def forget_claim(self, *args, **kwargs):
        return self.memory.claims(registry=self.registry).forget_claim(*args, **kwargs)

    def _review_claim_evidence(self, *args, **kwargs):
        return self.memory.claims(registry=self.registry)._review_claim_evidence(*args, **kwargs)
