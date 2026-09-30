from __future__ import annotations

from typing import Any

from .models import CAPSULE_PROTOCOL, DEFAULT_CONSTITUTION, EVOLUTION_PROFILES, NEGATIVE_FEEDBACK, POSITIVE_FEEDBACK
from openkyrozen.memory.models import _SECRET_RE

def record_omission_trial(self, proposal_id: str, with_item: list[dict[str, Any]],
                          without_item: list[dict[str, Any]]) -> dict[str, Any]:
    proposal = next((item for item in self.store.list_proposals(
        workspace_id=self.memory.workspace_id, limit=10000) if item["id"] == proposal_id), None)
    if not proposal or not with_item or len(with_item) != len(without_item):
        raise ValueError("proposal and equal non-empty omission results are required")
    left = [str(item.get("case_id", "")) for item in with_item]
    right = [str(item.get("case_id", "")) for item in without_item]
    if not all(left) or left != right or len(set(left)) != len(left):
        raise ValueError("paired omission case ids must be unique and identical")
    with_successes = sum(bool(item.get("verified_success")) for item in with_item)
    without_successes = sum(bool(item.get("verified_success")) for item in without_item)
    result = {"case_ids": left, "with_successes": with_successes, "without_successes": without_successes,
              "context_chars_saved": len(proposal["content"]) * len(left),
              "retirement_eligible": without_successes >= with_successes}
    self.store.update_proposal(proposal_id, status=proposal["status"],
                               validation={**proposal["validation"], "omission_trial": result})
    self.store.append_event("learning.omission_trial", {"proposal_id": proposal_id, **result},
                            user_id=self.memory.user_id, workspace_id=self.memory.workspace_id,
                            session_id=self.memory.session_id)
    return result
