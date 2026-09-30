from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from openkyrozen.persistence.models import stable_hash

from .models import CLAIM_SCOPES, SCOPE_RANK, _SECRET_RE

class ClaimService:
    """Scoped claim lifecycle over injected memory, storage and skill rollback."""
    def __init__(self, memory, registry=None):
        self.memory = memory
        self.store = memory.store
        self.registry = registry

    @staticmethod
    def _clean(content: str) -> str:
        return re.sub(r"\s+", " ", str(content)).strip()[:4000]


    def remember_claim(self, *, key: str, value: str, kind: str = "fact", authority: str = "inferred",
                       scope: str = "global", scope_value: str = "", evidence_id: str | None = None,
                       dependencies: list[str] | None = None, valid_until: str | None = None,
                       claim_type: str = "general", speaker: str | None = None,
                       audiences: list[str] | None = None, channel: str | None = None,
                       visibility: str = "public") -> dict[str, Any]:
        """Create a typed memory claim; inferred claims need repeated evidence."""
        key, value = self._clean(key), self._clean(value)
        if not key or not value or _SECRET_RE.search(f"{key}={value}"):
            raise ValueError("claim requires non-secret key and value")
        if authority not in {"owner", "inferred"} or scope not in CLAIM_SCOPES:
            raise ValueError("invalid claim authority or scope")
        if scope != "global" and not str(scope_value).strip():
            raise ValueError("non-global claims require scope_value")
        if claim_type not in {"general", "attributed_belief", "private_fact", "group_agreement"}:
            raise ValueError("invalid claim type")
        if visibility not in {"public", "private", "group"}:
            raise ValueError("invalid claim visibility")
        speaker = self._clean(speaker or "") or None
        audiences = [self._clean(item) for item in audiences or [] if self._clean(item)]
        if claim_type in {"attributed_belief", "private_fact"} and not speaker:
            raise ValueError("attributed and private claims require speaker")
        if claim_type == "private_fact":
            visibility = "private"
            if authority != "owner":
                raise ValueError("private facts must be explicitly owner-authored")
        if claim_type == "group_agreement":
            visibility = "group"
        metadata = {"claim": True, "claim_key": key, "claim_value": value,
                    "authority": authority, "scope": {"type": scope, "value": str(scope_value).strip()},
                    "valid_from": datetime.now(timezone.utc).isoformat(), "valid_until": valid_until,
                    "dependencies": [str(item) for item in dependencies or [] if item],
                    "claim_type": claim_type, "speaker": speaker, "audiences": audiences,
                    "channel": self._clean(channel or "") or None, "visibility": visibility}
        active = [row for row in self.store.list_memories(status="active", limit=10000,
                                                          workspace_id=self.memory.workspace_id,
                                                          user_id=self.memory.user_id)
                  if row.get("metadata", {}).get("claim_key") == key
                  and row.get("metadata", {}).get("scope") == metadata["scope"]
                  and row.get("metadata", {}).get("speaker") == speaker
                  and row.get("metadata", {}).get("claim_type", "general") == claim_type]
        if claim_type == "attributed_belief":
            display = f"{speaker} believes {key}: {value}"
        elif claim_type == "private_fact":
            display = f"Private fact from {speaker} — {key}: {value}"
        elif claim_type == "group_agreement":
            display = f"Group agreement — {key}: {value}"
        else:
            display = f"{key}: {value}"
        if authority == "owner":
            for row in active:
                if row.get("metadata", {}).get("claim_value") != value:
                    self.store.set_memory_status(row["id"], "superseded",
                                                 workspace_id=self.memory.workspace_id,
                                                 user_id=self.memory.user_id)
                    metadata["supersedes"] = row["id"]
            memory_id = self.memory.add_log(display, kind=kind, status="active", confidence=1.0,
                                            metadata=metadata)
            audit_metadata = ({**metadata, "claim_value": "[private]"}
                              if visibility == "private" else metadata)
            self.store.append_event("memory.claim_activated", {"memory_id": memory_id, **audit_metadata},
                                    user_id=self.memory.user_id, workspace_id=self.memory.workspace_id,
                                    session_id=self.memory.session_id)
            return {"status": "active", "memory_id": memory_id, "needs_clarification": False}
        proposals = self.store.list_proposals(workspace_id=self.memory.workspace_id, limit=10000)
        existing = next((item for item in proposals if item["status"] == "candidate"
                         and item.get("validation", {}).get("claim_key") == key
                         and item.get("validation", {}).get("claim_value") == value
                         and item.get("validation", {}).get("scope") == metadata["scope"]
                         and item.get("validation", {}).get("speaker") == speaker
                         and item.get("validation", {}).get("claim_type", "general") == claim_type), None)
        conflict = any(row.get("metadata", {}).get("claim_value") != value for row in active)
        if existing:
            count = self.store.add_proposal_evidence(existing["id"], evidence_id or stable_hash(f"{key}:{value}"))
            if count >= 2 and not conflict:
                evidence_review = self._review_claim_evidence(
                    display, list(existing.get("evidence", [])) + ([evidence_id] if evidence_id else []),
                    private=visibility != "public",
                )
                if evidence_review is False:
                    validation = {**existing["validation"], **metadata,
                                  "stage": "candidate", "decision_review": "not_supported"}
                    self.store.update_proposal(existing["id"], status="candidate", validation=validation)
                    return {"status": "candidate", "proposal_id": existing["id"],
                            "evidence_count": count, "needs_clarification": True}
                validation = {**existing["validation"], **metadata, "success": True,
                              "evidence_count": count, "stage": "active"}
                if evidence_review is True:
                    validation["decision_review"] = "supported"
                self.store.update_proposal(existing["id"], status="active", confidence=0.8, validation=validation)
                memory_id = self.memory.add_log(display, kind=kind, status="active", confidence=0.8,
                                                metadata={**metadata, "proposal_id": existing["id"]})
                return {"status": "active", "proposal_id": existing["id"], "memory_id": memory_id,
                        "needs_clarification": False}
            return {"status": "candidate", "proposal_id": existing["id"], "evidence_count": count,
                    "needs_clarification": conflict}
        proposal_id = self.store.create_proposal(kind, display, confidence=0.4,
                                                 evidence=[evidence_id or stable_hash(f"{key}:{value}")],
                                                 workspace_id=self.memory.workspace_id, user_id=self.memory.user_id)
        self.store.update_proposal(proposal_id, status="candidate",
                                   validation={**metadata, "stage": "candidate", "conflict": conflict})
        self.store.append_event("memory.claim_candidate", {"proposal_id": proposal_id, **metadata},
                                user_id=self.memory.user_id, workspace_id=self.memory.workspace_id,
                                session_id=self.memory.session_id)
        return {"status": "candidate", "proposal_id": proposal_id, "evidence_count": 1,
                "needs_clarification": conflict}


    def resolve_claim(self, key: str, *, profile: str | None = None, project: str | None = None,
                      task_signature: str | None = None, speaker: str | None = None,
                      audience: str | None = None, channel: str | None = None,
                      authorized_speakers: set[str] | None = None) -> dict[str, Any] | None:
        context = {"profile": profile, "project": project, "task": task_signature,
                   "speaker": speaker, "audience": audience, "channel": channel}
        authorized_speakers = authorized_speakers or set()
        matches = []
        for row in self.store.list_memories(status="active", limit=10000,
                                            workspace_id=self.memory.workspace_id,
                                            user_id=self.memory.user_id):
            meta = row.get("metadata", {})
            if not meta.get("claim") or meta.get("claim_key") != key:
                continue
            scope = meta.get("scope", {"type": "global", "value": ""})
            if scope["type"] != "global" and context.get(scope["type"]) != scope.get("value"):
                continue
            claim_speaker = meta.get("speaker")
            if speaker and claim_speaker and claim_speaker != speaker:
                continue
            if meta.get("channel") and meta.get("channel") != channel:
                continue
            if meta.get("visibility") == "private" and not (
                    claim_speaker == speaker and claim_speaker in authorized_speakers):
                continue
            if meta.get("visibility") == "group" and meta.get("audiences") and audience not in meta["audiences"]:
                continue
            matches.append((SCOPE_RANK[scope["type"]], int(meta.get("authority") == "owner"), row))
        if not matches:
            return None
        best_rank = max((rank, authority) for rank, authority, _ in matches)
        best = [row for rank, authority, row in matches if (rank, authority) == best_rank]
        values = {row["metadata"]["claim_value"] for row in best}
        attributed = {row["metadata"].get("speaker"): row["metadata"]["claim_value"] for row in best
                      if row["metadata"].get("claim_type") == "attributed_belief"}
        return {"conflict": len(values) > 1, "value": None if attributed else (
                    next(iter(values)) if len(values) == 1 else None),
                "attributed_values": attributed, "claims": best, "scope_rank": best_rank[0]}


    def explain_claim(self, claim_id: str, *, user_id: str | None = None,
                      workspace_id: str | None = None, session_id: str | None = None) -> dict[str, Any] | None:
        scoped_user = self.memory.user_id if user_id is None else str(user_id)
        scoped_workspace = self.memory.workspace_id if workspace_id is None else str(workspace_id)
        rows = self.store.list_memories(status=None, limit=10000,
                                        workspace_id=scoped_workspace, session_id=session_id,
                                        user_id=scoped_user)
        row = next((item for item in rows if item["id"] == claim_id and item.get("metadata", {}).get("claim")), None)
        return row


    def forget_claim(self, claim_id: str, *, user_id: str | None = None,
                     workspace_id: str | None = None, session_id: str | None = None) -> bool:
        scoped_user = self.memory.user_id if user_id is None else str(user_id)
        scoped_workspace = self.memory.workspace_id if workspace_id is None else str(workspace_id)
        scoped_session = self.memory.session_id if session_id is None else session_id
        claim = self.explain_claim(claim_id, user_id=scoped_user,
                                   workspace_id=scoped_workspace, session_id=scoped_session)
        if not claim:
            return False
        claim_memory = self.memory
        if (scoped_user, scoped_workspace, scoped_session) != (
                self.memory.user_id, self.memory.workspace_id, self.memory.session_id):
            claim_memory = self.memory.scoped( user_id=scoped_user,
                                      workspace_id=scoped_workspace, session_id=scoped_session)
        claim_memory.delete_logs([claim_id])
        for proposal in self.store.list_proposals(workspace_id=scoped_workspace, user_id=scoped_user, limit=10000):
            if claim_id not in proposal.get("validation", {}).get("dependencies", []):
                continue
            skill_id = proposal.get("validation", {}).get("skill_id")
            if skill_id and self.registry:
                self.registry.rollback_learned(skill_id)
            self.store.update_proposal(
                proposal["id"], status="rejected",
                validation={**proposal["validation"], "reason": "source claim deleted"},
                workspace_id=scoped_workspace, user_id=scoped_user,
            )
        self.store.append_event("memory.claim_forgotten", {"memory_id": claim_id},
                                user_id=scoped_user, workspace_id=scoped_workspace,
                                session_id=scoped_session)
        self.store.append_event("learning.regression_cases_deactivated", {"dependency": claim_id},
                                user_id=scoped_user, workspace_id=scoped_workspace,
                                session_id=scoped_session)
        return True


    def _review_claim_evidence(self, claim: str, evidence_ids: list[str], *, private: bool,
                               evidence_text: str | None = None) -> bool | None:
        """Return True/False for a confident typed review, None on advisory fallback."""
        try:
            fast_mode = self.memory.decisions
            decision_assist = fast_mode.decision_assist
            events = self.store.list_events(limit=5000, workspace_id=self.memory.workspace_id,
                                            user_id=self.memory.user_id)
            by_id = {event["id"]: event.get("payload", {}) for event in events}
            evidence = [by_id[item] for item in evidence_ids if item in by_id]
            if not evidence and evidence_text:
                evidence = [{"source": "learning_receipt", "text": self._clean(evidence_text)}]
            if not evidence:
                return None
            diagnostics: dict[str, object] = {}
            result = decision_assist(
                "learning_evidence", {"claim": claim[:1200], "evidence": evidence[:5]},
                {"verdict": {"type": "choice", "instructions": (
                             "For this candidate claim, classify only the supplied evidence: "
                                 f"{claim[:500]}. Mark support only when the evidence establishes it; "
                                 "mark contradict only when the evidence directly conflicts; "
                                 "a started, unknown, or incomplete status is insufficient. "
                                 "Example: a worker status=started does not prove completion or failure."),
                             "criteria": {"support": "Evidence supports the claim",
                                           "contradict": "Evidence explicitly conflicts with or disproves the claim",
                                           "insufficient": "Evidence is incomplete or neutral, including started, unknown, or missing final status"}}},
                private=private, diagnostics=diagnostics,
            )
            if not result:
                if diagnostics:
                    self.store.append_event(
                        "learning.evidence_review", {**diagnostics, "outcome": "fallback"},
                        user_id=self.memory.user_id, workspace_id=self.memory.workspace_id,
                        session_id=self.memory.session_id,
                    )
                return None
            answer = result["answers"].get("verdict")
            probabilities = answer.get("probabilities", {}) if isinstance(answer, dict) else {}
            confidence = float(answer.get("confidence", 0)) if isinstance(answer, dict) else 0
            choice = answer.get("choice") if isinstance(answer, dict) else None
            backend = str(result.get("backend") or "")
            accepted_choice = None
            try:
                accepted_choice = fast_mode.confident_choice(
                    "learning_evidence", answer, {"support", "contradict", "insufficient"}, backend,
                )
            except (TypeError, ValueError):
                accepted_choice = None
            accepted = (bool(result.get("quality_gate", True))
                        and choice in {"support", "contradict", "insufficient"}
                        and isinstance(probabilities, dict) and accepted_choice == choice)
            self.store.append_event(
                "learning.evidence_review", {
                    "backend": backend, "model_version": result.get("model_version"),
                    "model_release_date": result.get("model_release_date"),
                    "latency_ms": result.get("latency_ms"), "confidence": confidence,
                    "probability": probabilities.get(choice) if isinstance(probabilities, dict) else None,
                    "input_tokens": result.get("input_tokens"), "output_tokens": result.get("output_tokens"),
                    "fallback_reason": result.get("fallback_reason") or (
                        "quality_gate" if result.get("quality_gate") is False else None),
                    "policy": result.get("policy"),
                    "policy_version": result.get("policy_version"),
                    "fallback_behavior": result.get("fallback_behavior"),
                    "confidence_threshold": result.get("confidence_threshold"),
                    "probability_threshold": result.get("probability_threshold"),
                    "probability_margin": result.get("probability_margin"),
                    "outcome": choice if accepted else "fallback",
                }, user_id=self.memory.user_id, workspace_id=self.memory.workspace_id,
                session_id=self.memory.session_id,
            )
            if not accepted:
                return None
            return choice == "support"
        except (OSError, RuntimeError, ValueError, TypeError, KeyError):
            return None
