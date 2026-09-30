from __future__ import annotations



from .models import CAPSULE_PROTOCOL, DEFAULT_CONSTITUTION, EVOLUTION_PROFILES, NEGATIVE_FEEDBACK, POSITIVE_FEEDBACK
from openkyrozen.memory.models import _SECRET_RE

def retire_artifact(self, proposal_id: str) -> bool:
    proposal = next((item for item in self.store.list_proposals(
        workspace_id=self.memory.workspace_id, limit=10000) if item["id"] == proposal_id), None)
    validation = (proposal or {}).get("validation", {})
    skill_id = validation.get("skill_id")
    if not proposal or not skill_id or not validation.get("omission_trial", {}).get("retirement_eligible"):
        return False
    if not self.registry or not self.registry.set_learned_status(skill_id, "retired"):
        return False
    self.store.update_proposal(proposal_id, status="retired",
                               validation={**validation, "stage": "retired", "preimage_status": proposal["status"]})
    self.store.append_event("learning.artifact_retired", {"proposal_id": proposal_id, "skill_id": skill_id},
                            user_id=self.memory.user_id, workspace_id=self.memory.workspace_id)
    return True


def restore_retired(self, proposal_id: str) -> bool:
    proposal = next((item for item in self.store.list_proposals(
        workspace_id=self.memory.workspace_id, limit=10000) if item["id"] == proposal_id), None)
    skill_id = (proposal or {}).get("validation", {}).get("skill_id")
    if not proposal or proposal.get("status") != "retired" or not skill_id or not self.registry:
        return False
    if not self.registry.set_learned_status(skill_id, "canary"):
        return False
    self.store.update_proposal(proposal_id, status="canary",
                               validation={**proposal["validation"], "stage": "canary",
                                           "revalidation_status": "pending"})
    return True


def rollback(self, proposal_id: str) -> bool:
    proposal = next((p for p in self.store.list_proposals(workspace_id=self.memory.workspace_id, limit=10000)
                     if p["id"] == proposal_id), None)
    if not proposal:
        return False
    skill_id = proposal.get("validation", {}).get("skill_id")
    if skill_id and self.registry:
        changed = self.registry.rollback_learned(skill_id)
    else:
        rows = self.store.list_memories(kind=proposal["kind"], status="active", limit=10000,
                                        workspace_id=self.memory.workspace_id)
        changed = True
        self.memory.delete_logs([row["id"] for row in rows if row["content"] == proposal["content"]])
    if not changed:
        return False
    self.store.update_proposal(proposal_id, status="rolled_back",
                               validation={**proposal.get("validation", {}), "success": False,
                                           "reason": "manual rollback"})
    self.store.append_event("learning.proposal_rolled_back", {"proposal_id": proposal_id},
                            user_id=self.memory.user_id, workspace_id=self.memory.workspace_id,
                            session_id=self.memory.session_id)
    return True
