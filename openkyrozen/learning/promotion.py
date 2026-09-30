from __future__ import annotations

from itertools import combinations
from typing import Any

from .models import CAPSULE_PROTOCOL, DEFAULT_CONSTITUTION, EVOLUTION_PROFILES, NEGATIVE_FEEDBACK, POSITIVE_FEEDBACK
from openkyrozen.memory.models import _SECRET_RE

def record_outcome(self, run: dict[str, str], receipts: list[dict[str, Any]], *, verified: bool,
                   success: bool, correction: bool = False, source: str = "runtime") -> list[str]:
    payload = {**run, "receipts": receipts, "verified": bool(verified), "success": bool(success),
               "correction": bool(correction), "source": source}
    self.store.append_event("learning.outcome", payload, user_id=self.memory.user_id,
                            workspace_id=self.memory.workspace_id, session_id=self.memory.session_id)
    if correction or (verified and not success):
        self.store.append_event("learning.review_requested", {"run_id": run["run_id"]},
                                user_id=self.memory.user_id, workspace_id=self.memory.workspace_id,
                                session_id=self.memory.session_id)
    for left, right in combinations(sorted(str(item.get("skill_id")) for item in receipts if item.get("skill_id")), 2):
        self.store.append_event("learning.artifact_pair", {
            "pair": [left, right], "run_id": run["run_id"], "verified": bool(verified),
            "success": bool(success), "correction": bool(correction),
        }, user_id=self.memory.user_id, workspace_id=self.memory.workspace_id,
           session_id=self.memory.session_id)
    if verified and success:
        for preflight_id in run.get("preflight_ids", []):
            self.store.append_event("learning.near_miss_prevented", {
                "run_id": run["run_id"], "regression_event_id": preflight_id,
            }, user_id=self.memory.user_id, workspace_id=self.memory.workspace_id,
               session_id=self.memory.session_id)
    if correction:
        dependencies = []
        for receipt in receipts:
            proposal = self._proposal_for_skill(str(receipt.get("skill_id", "")))
            dependencies.extend((proposal or {}).get("validation", {}).get("dependencies", []))
        self.store.append_event("learning.regression_case_created", {
            "run_id": run["run_id"], "profile": run["profile"],
            "task_signature": run["task_signature"], "task": self._clean(run.get("task", "")),
            "receipts": receipts, "dependencies": list(dict.fromkeys(dependencies)),
            "required_outcome": "must not repeat corrected behavior",
        }, user_id=self.memory.user_id, workspace_id=self.memory.workspace_id,
           session_id=self.memory.session_id)
    changes = []
    for receipt in receipts:
        change = self._reconcile_artifact(str(receipt.get("skill_id", "")))
        if change:
            changes.append(change)
    return changes


def _artifact_outcomes(self, skill_id: str) -> list[dict[str, Any]]:
    events = self.store.list_events("learning.outcome", limit=10000, workspace_id=self.memory.workspace_id)
    return [event["payload"] for event in reversed(events)
            if any(item.get("skill_id") == skill_id for item in event["payload"].get("receipts", []))]


def _proposal_for_skill(self, skill_id: str) -> dict[str, Any] | None:
    return next((proposal for proposal in self.store.list_proposals(
        workspace_id=self.memory.workspace_id, limit=10000)
        if proposal.get("validation", {}).get("skill_id") == skill_id), None)


def _reconcile_artifact(self, skill_id: str) -> str | None:
    if not skill_id or self.registry is None:
        return None
    skill = next((item for item in self.registry.list() if item["id"] == skill_id), None)
    if not skill or skill.get("source") != "learned":
        return None
    verified = [item for item in self._artifact_outcomes(skill_id) if item.get("verified")]
    failures = [item for item in verified[-5:] if not item.get("success")]
    any_failure = any(not item.get("success") for item in verified)
    corrected = any(item.get("correction") for item in verified)
    proposal = self._proposal_for_skill(skill_id)
    if corrected or (skill["status"] == "active" and len(failures) >= 2):
        if self.registry.rollback_learned(skill_id):
            if proposal:
                self.store.update_proposal(proposal["id"], status="rolled_back",
                                           validation={**proposal["validation"], "stage": "rolled_back"})
            self.store.append_event("learning.artifact_rolled_back", {"skill_id": skill_id},
                                    user_id=self.memory.user_id, workspace_id=self.memory.workspace_id)
            return f"Rolled back learned {skill['name']} after verified regression."
    successful_runs = {item["run_id"] for item in verified if item.get("success")}
    replay = (proposal or {}).get("validation", {}).get("shadow_replay") or {}
    verifier_ids = {item.get("source") for item in verified if item.get("success") and item.get("source")}
    verifiers_reliable = all(
        self.verifier_reliability(verifier)["reliability"] in {None, 1.0}
        or self.verifier_reliability(verifier)["reliability"] >= 0.5
        for verifier in verifier_ids
    )
    if (skill["status"] == "canary" and len(successful_runs) >= 2 and not any_failure and not corrected
            and replay.get("non_regressing") is True and verifiers_reliable):
        if self.registry.activate_learned(skill_id):
            if proposal:
                self.store.update_proposal(proposal["id"], status="active", confidence=0.9,
                                           validation={**proposal["validation"], "stage": "active",
                                                       "verified_successes": len(successful_runs)})
            self.store.append_event("learning.artifact_promoted", {"skill_id": skill_id,
                                                                     "verified_successes": len(successful_runs)},
                                    user_id=self.memory.user_id, workspace_id=self.memory.workspace_id)
            return f"Promoted learned {skill['name']} after {len(successful_runs)} verified uses."
    return None


def validate_and_activate(self, proposal_id: str, *, validation: dict[str, Any]) -> bool:
    if validation.get("success") is not True:
        self.store.update_proposal(proposal_id, status="rejected", validation=validation)
        return False
    proposal = next((p for p in self.store.list_proposals(workspace_id=self.memory.workspace_id, limit=10000)
                     if p["id"] == proposal_id), None)
    if not proposal:
        return False
    skill_id = proposal.get("validation", {}).get("skill_id")
    if skill_id:
        return bool(self.registry and self.registry.activate_learned(skill_id))
    self.store.update_proposal(proposal_id, status="active", confidence=max(0.8, proposal["confidence"]),
                               validation=validation)
    self.memory.add_log(proposal["content"], kind=proposal["kind"], status="active",
                        confidence=max(0.8, proposal["confidence"]),
                        metadata={"proposal_id": proposal_id, "validation": validation})
    return True
