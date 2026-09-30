from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from openkyrozen.persistence.models import stable_hash

from .models import CAPSULE_PROTOCOL, DEFAULT_CONSTITUTION, EVOLUTION_PROFILES, NEGATIVE_FEEDBACK, POSITIVE_FEEDBACK
from openkyrozen.memory.models import _SECRET_RE

def export_capsule(self, proposal_id: str) -> dict[str, Any] | None:
    proposal = next((item for item in self.status(10000) if item["id"] == proposal_id), None)
    if not proposal:
        return None
    capsule = {"protocol": CAPSULE_PROTOCOL, "exported_at": datetime.now(timezone.utc).isoformat(),
               "kind": proposal["kind"], "content": proposal["content"],
               "content_hash": stable_hash(proposal["content"]), "profile": proposal.get("profile"),
               "evidence_card": self.evidence_card(proposal_id)}
    if _SECRET_RE.search(json.dumps(capsule, ensure_ascii=False)):
        raise ValueError("capsule contains secret-like content")
    return capsule


def import_capsule(self, capsule: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(capsule, dict) or capsule.get("protocol") != CAPSULE_PROTOCOL:
        raise ValueError("unsupported experience capsule")
    content = str(capsule.get("content", "")).strip()
    if not content or stable_hash(content) != capsule.get("content_hash") or _SECRET_RE.search(content):
        raise ValueError("invalid or secret-bearing capsule")
    proposal_id = self.store.create_proposal(
        str(capsule.get("kind", "skill")), content, confidence=0.0,
        evidence=[], workspace_id=self.memory.workspace_id, user_id=self.memory.user_id,
    )
    self.store.update_proposal(proposal_id, status="candidate", validation={
        "stage": "imported", "source": "imported", "active": False,
        "profile": capsule.get("profile"), "imported_evidence_card": capsule.get("evidence_card"),
    })
    self.store.append_event("learning.capsule_imported", {"proposal_id": proposal_id, "active": False},
                            user_id=self.memory.user_id, workspace_id=self.memory.workspace_id)
    return {"proposal_id": proposal_id, "status": "candidate", "active": False}
