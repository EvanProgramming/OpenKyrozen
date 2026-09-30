from __future__ import annotations

from typing import Any

from .models import CAPSULE_PROTOCOL, DEFAULT_CONSTITUTION, EVOLUTION_PROFILES, NEGATIVE_FEEDBACK, POSITIVE_FEEDBACK
from openkyrozen.memory.models import _SECRET_RE

def record_shadow_replay(self, proposal_id: str, candidate: list[dict[str, Any]],
                         predecessor: list[dict[str, Any]]) -> dict[str, Any]:
    """Record paired, already-sandboxed replay results without executing commands."""
    proposal = next((item for item in self.store.list_proposals(
        workspace_id=self.memory.workspace_id, limit=10000) if item["id"] == proposal_id), None)
    if not proposal or not candidate or len(candidate) != len(predecessor):
        raise ValueError("proposal and equal non-empty paired replay results are required")
    candidate_ids = [str(item.get("case_id", "")) for item in candidate]
    predecessor_ids = [str(item.get("case_id", "")) for item in predecessor]
    if not all(candidate_ids) or candidate_ids != predecessor_ids or len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("paired replay case ids must be unique and identical")
    candidate_successes = sum(bool(item.get("verified_success")) for item in candidate)
    predecessor_successes = sum(bool(item.get("verified_success")) for item in predecessor)
    models = sorted({str(item.get("provider_model")) for item in candidate if item.get("provider_model")})
    result = {
        "case_ids": candidate_ids, "candidate_successes": candidate_successes,
        "predecessor_successes": predecessor_successes,
        "non_regressing": candidate_successes >= predecessor_successes,
        "environment": self.environment_fingerprint(),
        "model_scope": models, "cross_model_validated": len(models) >= 2,
    }
    validation = {**proposal.get("validation", {}), "shadow_replay": result,
                  "revalidation_status": "valid" if result["non_regressing"] else "failed"}
    self.store.update_proposal(proposal_id, status=proposal["status"], validation=validation)
    self.store.append_event("learning.shadow_replay", {"proposal_id": proposal_id, **result},
                            user_id=self.memory.user_id, workspace_id=self.memory.workspace_id,
                            session_id=self.memory.session_id)
    return result


def verifier_reliability(self, verifier_id: str) -> dict[str, Any]:
    outcomes = [event["payload"] for event in self.store.list_events(
        "learning.outcome", limit=10000, workspace_id=self.memory.workspace_id)]
    accepted = {item.get("run_id") for item in outcomes
                if item.get("source") == verifier_id and item.get("verified") and item.get("success")}
    corrected = {item.get("run_id") for item in outcomes if item.get("correction")}
    false_accepts = len(accepted & corrected)
    return {"verifier_id": verifier_id, "accepted": len(accepted), "false_accepts": false_accepts,
            "reliability": 1.0 - (false_accepts / len(accepted)) if accepted else None}


def evidence_card(self, proposal_id: str) -> dict[str, Any] | None:
    proposal = next((item for item in self.status(10000) if item["id"] == proposal_id), None)
    if not proposal:
        return None
    validation = proposal.get("validation", {})
    return {
        "proposal_id": proposal_id, "profile": proposal.get("profile"),
        "stage": proposal.get("lifecycle_stage"), "source_evidence": proposal.get("evidence", []),
        "verification_contract": validation.get("verification_contract"),
        "applicability": validation.get("applicability"),
        "revalidation_status": validation.get("revalidation_status"),
        "shadow_replay": validation.get("shadow_replay"),
        "outcomes": proposal.get("evidence_receipts", []),
        "metrics": proposal.get("artifact_metrics", {}),
        "predecessor": proposal.get("predecessor"),
    }
