from __future__ import annotations

import re
from typing import Any
from openkyrozen.persistence.models import stable_hash

from .models import CAPSULE_PROTOCOL, DEFAULT_CONSTITUTION, EVOLUTION_PROFILES, NEGATIVE_FEEDBACK, POSITIVE_FEEDBACK
from openkyrozen.memory.models import _SECRET_RE

def propose_artifact(self, run_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
    if self.registry is None:
        return {"status": "ignored", "reason": "skill registry unavailable"}
    if any(event["payload"].get("run_id") == run_id for event in self.store.list_events(
            "learning.artifact_created", limit=10000, workspace_id=self.memory.workspace_id)):
        return {"status": "ignored", "reason": "run already produced an artifact"}
    profile, body = str(artifact.get("profile", "")), str(artifact.get("body", "")).strip()
    if profile not in EVOLUTION_PROFILES or not body:
        return {"status": "ignored", "reason": "invalid artifact"}
    raw_name = str(artifact.get("name", "")).strip()
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw_name).strip("-.")[:64]
    name = name if len(name) >= 2 else f"learned-{stable_hash(body)[:12]}"
    parent_version = artifact.get("parent_version")
    if not parent_version:
        parent = next((item for item in self.registry.list("active")
                       if item["name"] == name and item.get("source") == "learned"
                       and item.get("manifest", {}).get("profiles") == [profile]), None)
        parent_version = parent["version"] if parent else None
    manifest = {
        "name": name,
        "description": str(artifact.get("description", "")).strip()[:500],
        "artifact_type": str(artifact.get("artifact_type", "skill")), "profiles": [profile],
        "triggers": [str(item).lower() for item in artifact.get("triggers", [])],
        "verification": artifact.get("verification", []), "parent_version": parent_version,
        "verification_contract": {
            "requirements": artifact.get("verification", []),
            "side_effects": "forbidden_during_replay",
        },
        "applicability": artifact.get("applicability") or {
            "environment_hash": self.environment_fingerprint()["hash"],
            "profile": profile,
        },
    }
    constitution = self.constitution()
    if manifest["artifact_type"] not in constitution.get("allowed_artifact_types", []):
        return {"status": "rejected", "reason": "learning constitution forbids artifact type"}
    source_run = next((event["payload"] for event in self.store.list_events(
        "learning.run_started", limit=10000, workspace_id=self.memory.workspace_id)
        if event["payload"].get("run_id") == run_id), None)
    if source_run and source_run.get("provider_model") != "unspecified":
        manifest["applicability"]["provider_model"] = source_run["provider_model"]
    try:
        installed = self.registry.install_learned(body, manifest, status="canary")
    except (OSError, ValueError) as exc:
        self.store.append_event("learning.artifact_rejected", {"run_id": run_id, "reason": str(exc)[:500]},
                                user_id=self.memory.user_id, workspace_id=self.memory.workspace_id,
                                session_id=self.memory.session_id)
        return {"status": "rejected", "reason": str(exc)}
    if installed.get("existing"):
        return {"status": "ignored", "reason": "artifact already exists", "skill_id": installed["id"]}
    if installed.get("status") == "rejected":
        return {"status": "rejected", "reason": installed.get("validation", {}).get("error", "validation failed")}
    proposal_id = self.store.create_proposal(manifest["artifact_type"], body, confidence=0.5,
                                             evidence=[run_id], workspace_id=self.memory.workspace_id,
                                             user_id=self.memory.user_id)
    validation = {"success": True, "stage": "canary", "profile": profile,
                  "skill_id": installed["id"], "manifest": installed["manifest"],
                  "verification_contract": manifest["verification_contract"],
                  "applicability": manifest["applicability"],
                  "revalidation_status": "pending", "shadow_replay": None,
                  "dependencies": [str(item) for item in artifact.get("dependencies", []) if item]}
    self.store.update_proposal(proposal_id, status="canary", validation=validation, confidence=0.5)
    self.store.append_event("learning.artifact_created", {"run_id": run_id, "proposal_id": proposal_id,
                                                           "skill_id": installed["id"], "profile": profile},
                            user_id=self.memory.user_id, workspace_id=self.memory.workspace_id,
                            session_id=self.memory.session_id)
    return {"status": "canary", "proposal_id": proposal_id, "skill_id": installed["id"]}


def submit(self, kind: str, content: str, *, evidence_id: str | None = None,
           confidence: float = 0.4, metadata: dict[str, Any] | None = None,
           evidence_text: str | None = None) -> dict[str, Any]:
    """Backward-compatible fact proposal path; executable kinds never auto-promote."""
    content = self._clean(content)
    if not content or content in {"—", "-"}:
        return {"status": "ignored", "reason": "empty"}
    digest = stable_hash(f"{kind}:{content}")
    proposals = self.store.list_proposals(workspace_id=self.memory.workspace_id, limit=10000)
    existing = next((p for p in proposals if stable_hash(f"{p['kind']}:{p['content']}") == digest
                     and p["status"] in {"candidate", "active"}), None)
    if existing:
        count = self.store.add_proposal_evidence(existing["id"], evidence_id or digest)
        confidence = min(0.95, max(existing.get("confidence", 0.0), confidence) + 0.2)
        if kind in {"skill", "tool", "code_patch", "policy"} or count < 2:
            return {"status": "candidate", "proposal_id": existing["id"], "evidence_count": count}
        evidence_review = self._review_claim_evidence(
            content, list(existing.get("evidence", [])),
            private=(metadata or {}).get("visibility") != "public",
            evidence_text=evidence_text,
        )
        if evidence_review is False:
            self.store.update_proposal(
                existing["id"], status="candidate",
                validation={**existing.get("validation", {}), "evidence_count": count,
                            "decision_review": "not_supported"},
            )
            return {"status": "candidate", "proposal_id": existing["id"],
                    "evidence_count": count, "needs_clarification": True}
        validation = {"success": True, "checks": ["repeated independent observations"], "evidence_count": count}
        if evidence_review is True:
            validation["decision_review"] = "supported"
        self.store.update_proposal(existing["id"], status="active", confidence=confidence, validation=validation)
        memory_id = self.memory.add_log(content, kind=kind, status="active", confidence=confidence,
                                        metadata={**(metadata or {}), "proposal_id": existing["id"],
                                                  "validation": validation})
        return {"status": "active", "proposal_id": existing["id"], "memory_id": memory_id,
                "evidence_count": count}
    proposal_id = self.store.create_proposal(kind, content, confidence=confidence,
                                             evidence=[evidence_id or digest], workspace_id=self.memory.workspace_id,
                                             user_id=self.memory.user_id)
    self.store.append_event("learning.proposal_created", {"proposal_id": proposal_id, "kind": kind},
                            user_id=self.memory.user_id, workspace_id=self.memory.workspace_id,
                            session_id=self.memory.session_id)
    return {"status": "candidate", "proposal_id": proposal_id, "evidence_count": 1}
