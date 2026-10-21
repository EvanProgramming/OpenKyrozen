from __future__ import annotations

from typing import Any

from .models import CAPSULE_PROTOCOL, DEFAULT_CONSTITUTION, EVOLUTION_PROFILES, NEGATIVE_FEEDBACK, POSITIVE_FEEDBACK
from openkyrozen.memory.models import _SECRET_RE

def status(self, limit: int = 100, *, profile: str | None = None,
           status: str | None = None) -> list[dict[str, Any]]:
    proposals = self.store.list_proposals(workspace_id=self.memory.workspace_id, limit=limit)
    if status:
        proposals = [item for item in proposals if item["status"] == status]
    if profile:
        proposals = [item for item in proposals if item.get("validation", {}).get("profile") == profile]
    enriched = []
    skills = self.registry.list() if self.registry else []
    completed = {event["payload"].get("run_id"): event["payload"] for event in self.store.list_events(
        "learning.run_completed", limit=10000, workspace_id=self.memory.workspace_id)}
    for proposal in proposals:
        skill_id = proposal.get("validation", {}).get("skill_id")
        skill = next((item for item in skills if item["id"] == skill_id), None)
        outcomes = self._artifact_outcomes(skill_id) if skill_id else []
        verified = [item for item in outcomes if item.get("verified")]
        successful = [item for item in verified if item.get("success")]
        measured = [completed[item.get("run_id")] for item in outcomes
                    if item.get("run_id") in completed]
        token_values = [int(item["tokens"]) for item in measured if item.get("tokens") is not None]
        latency_values = [float(item["latency"]) for item in measured if item.get("latency") is not None]
        enriched.append({**proposal,
                         "profile": proposal.get("validation", {}).get("profile"),
                         "lifecycle_stage": skill.get("status") if skill else proposal.get("status"),
                         "predecessor": skill.get("manifest", {}).get("parent_version") if skill else None,
                         "evidence_receipts": [{"run_id": item.get("run_id"),
                                                "success": item.get("success"),
                                                "correction": item.get("correction"),
                                                "source": item.get("source")} for item in outcomes[-5:]],
                         "artifact_metrics": {
                             "uses": len(outcomes),
                             "verified_uses": len(verified),
                             "verification_rate": len(verified) / len(outcomes) if outcomes else None,
                             "successful_uses": len(successful),
                             "task_success_rate": len(successful) / len(verified) if verified else None,
                             "failed_uses": len([item for item in verified if not item.get("success")]),
                             "corrections": len([item for item in outcomes if item.get("correction")]),
                             "regression_rate": (len([item for item in verified if not item.get("success")])
                                                 / len(verified) if verified else None),
                             "measured_runs": len(measured), "eligible_runs": sum(bool(item.get("eligible")) for item in measured),
                             "tokens": sum(token_values),
                             "mean_tokens": sum(token_values) / len(token_values) if token_values else None,
                             "mean_latency_seconds": (sum(latency_values) / len(latency_values)
                                                      if latency_values else None),
                             "tool_calls": sum(int(item.get("tool_calls", 0)) for item in measured),
                             "repeated_error_rate": (sum(int(item.get("errors", 0)) > 1 for item in measured)
                                                     / len(measured) if measured else None),
                         }})
    return enriched


def metrics(self, profile: str | None = None) -> dict[str, Any]:
    completed = [event["payload"] for event in self.store.list_events(
        "learning.run_completed", limit=10000, workspace_id=self.memory.workspace_id)]
    outcomes = [event["payload"] for event in self.store.list_events(
        "learning.outcome", limit=10000, workspace_id=self.memory.workspace_id)]
    if profile:
        completed = [item for item in completed if item.get("profile") == profile]
        outcomes = [item for item in outcomes if item.get("profile") == profile]
    verified = [item for item in outcomes if item.get("verified")]
    families: dict[str, dict[str, Any]] = {}
    for item in verified:
        family = families.setdefault(str(item.get("task_signature", "unknown")), {"uses": 0, "successes": 0})
        family["uses"] += 1
        family["successes"] += int(bool(item.get("success")))
    for family in families.values():
        family["completion_rate"] = family["successes"] / family["uses"]
    return {
        "profile": profile or "all", "runs": len(completed), "verified_outcomes": len(verified),
        "completion_rate": (sum(bool(item.get("success")) for item in verified) / len(verified)) if verified else None,
        "correction_rate": (sum(bool(item.get("correction")) for item in verified) / len(verified)) if verified else None,
        "repeated_error_rate": (sum(item.get("errors", 0) > 1 for item in completed) / len(completed)) if completed else None,
        "tool_calls": sum(int(item.get("tool_calls", 0)) for item in completed),
        "tokens": sum(int(item.get("tokens") or 0) for item in completed),
        "latency": sum(float(item.get("latency") or 0.0) for item in completed),
        "task_families": families,
        "context_chars": sum(sum(int(receipt.get("chars", 0)) for receipt in item.get("receipts", []))
                             for item in outcomes),
        "prevented_near_misses": len(self.store.list_events(
            "learning.near_miss_prevented", limit=10000, workspace_id=self.memory.workspace_id)),
    }
