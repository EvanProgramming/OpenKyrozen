"""Evidence-gated automatic learning and rollback."""

from __future__ import annotations

import re
from typing import Any

from event_store import EventStore, stable_hash
from memory import MemoryBank


class LearningEngine:
    """Promotes repeated observations automatically and executable changes only after validation."""

    def __init__(self, memory: MemoryBank, store: EventStore | None = None):
        self.memory = memory
        self.store = store or memory.store

    @staticmethod
    def _clean(content: str) -> str:
        content = re.sub(r"\s+", " ", str(content)).strip()
        return content[:4000]

    def submit(self, kind: str, content: str, *, evidence_id: str | None = None,
               confidence: float = 0.4, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        content = self._clean(content)
        if not content or content in {"—", "-"}:
            return {"status": "ignored", "reason": "empty"}
        digest = stable_hash(f"{kind}:{content}")
        proposals = self.store.list_proposals(workspace_id=self.memory.workspace_id, limit=10000)
        existing = next((p for p in proposals if stable_hash(f"{p['kind']}:{p['content']}") == digest and p["status"] in {"candidate", "active"}), None)
        if existing:
            count = self.store.add_proposal_evidence(existing["id"], evidence_id or digest)
            confidence = min(0.95, max(existing.get("confidence", 0.0), confidence) + 0.2)
            if kind in {"skill", "tool", "code_patch"}:
                return {"status": "candidate", "proposal_id": existing["id"], "evidence_count": count}
            if count < 2:
                return {"status": "candidate", "proposal_id": existing["id"], "evidence_count": count}
            validation = {"success": True, "checks": ["repeated independent observations"], "evidence_count": count}
            self.store.update_proposal(existing["id"], status="active", confidence=confidence, validation=validation)
            memory_id = self.memory.add_log(content, kind=kind, status="active", confidence=confidence,
                                            metadata={**(metadata or {}), "proposal_id": existing["id"], "validation": validation})
            return {"status": "active", "proposal_id": existing["id"], "memory_id": memory_id, "evidence_count": count}

        proposal_id = self.store.create_proposal(kind, content, confidence=confidence,
                                                 evidence=[evidence_id or digest],
                                                 workspace_id=self.memory.workspace_id, user_id=self.memory.user_id)
        self.store.append_event("learning.proposal_created", {"proposal_id": proposal_id, "kind": kind},
                                user_id=self.memory.user_id, workspace_id=self.memory.workspace_id,
                                session_id=self.memory.session_id)
        return {"status": "candidate", "proposal_id": proposal_id, "evidence_count": 1}

    def validate_and_activate(self, proposal_id: str, *, validation: dict[str, Any]) -> bool:
        if validation.get("success") is not True:
            self.store.update_proposal(proposal_id, status="rejected", validation=validation)
            return False
        proposals = [p for p in self.store.list_proposals(workspace_id=self.memory.workspace_id, limit=10000)
                     if p["id"] == proposal_id]
        if not proposals:
            return False
        proposal = proposals[0]
        self.store.update_proposal(proposal_id, status="active", confidence=max(0.8, proposal["confidence"]), validation=validation)
        self.memory.add_log(proposal["content"], kind=proposal["kind"], status="active",
                            confidence=max(0.8, proposal["confidence"]),
                            metadata={"proposal_id": proposal_id, "validation": validation})
        self.store.append_event("learning.proposal_activated", {"proposal_id": proposal_id, "validation": validation},
                                user_id=self.memory.user_id, workspace_id=self.memory.workspace_id,
                                session_id=self.memory.session_id)
        return True

    def rollback(self, proposal_id: str) -> bool:
        proposals = [p for p in self.store.list_proposals(workspace_id=self.memory.workspace_id, limit=10000)
                     if p["id"] == proposal_id]
        if not proposals:
            return False
        proposal = proposals[0]
        self.store.update_proposal(proposal_id, status="rolled_back", validation={"success": False, "reason": "manual rollback"})
        rows = self.store.list_memories(kind=proposal["kind"], status="active", limit=10000,
                                        workspace_id=self.memory.workspace_id)
        ids = [row["id"] for row in rows if row["content"] == proposal["content"]]
        if ids:
            self.memory.delete_logs(ids)
        self.store.append_event("learning.proposal_rolled_back", {"proposal_id": proposal_id},
                                user_id=self.memory.user_id, workspace_id=self.memory.workspace_id,
                                session_id=self.memory.session_id)
        return True

    def status(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.list_proposals(workspace_id=self.memory.workspace_id, limit=limit)
