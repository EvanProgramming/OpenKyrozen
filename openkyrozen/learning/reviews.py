from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import CAPSULE_PROTOCOL, DEFAULT_CONSTITUTION, EVOLUTION_PROFILES, NEGATIVE_FEEDBACK, POSITIVE_FEEDBACK
from openkyrozen.memory.models import _SECRET_RE

def pending_reviews(self, *, min_age_seconds: float = 30.0, limit: int = 1) -> list[dict[str, Any]]:
    completed = self.store.list_events("learning.run_completed", limit=1000, workspace_id=self.memory.workspace_id)
    reviewed = {event["payload"].get("run_id") for event in self.store.list_events(
        "learning.run_reviewed", limit=10000, workspace_id=self.memory.workspace_id)}
    requested = {event["payload"].get("run_id") for event in self.store.list_events(
        "learning.review_requested", limit=10000, workspace_id=self.memory.workspace_id)}
    now, candidates = datetime.now(timezone.utc), []
    for event in reversed(completed):
        payload = event["payload"]
        if payload["run_id"] in reviewed or payload.get("provider_error") or payload.get("contains_secret"):
            continue
        if (now - datetime.fromisoformat(event["created_at"])).total_seconds() < min_age_seconds:
            continue
        if not payload.get("eligible") and payload["run_id"] not in requested:
            continue
        recurrence = sum(item["payload"].get("task_signature") == payload.get("task_signature")
                         for item in completed)
        score = recurrence * 2 + int(payload["run_id"] in requested) * 5 + int(payload.get("errors", 0)) * 2
        score += min(5, int(payload.get("tokens") or 0) // 1000)
        candidates.append((score, event["created_at"], payload))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [payload for _, _, payload in candidates[:limit]]


def mark_reviewed(self, run_id: str, *, result: str) -> None:
    self.store.append_event("learning.run_reviewed", {"run_id": run_id, "result": str(result)[:500]},
                            user_id=self.memory.user_id, workspace_id=self.memory.workspace_id,
                            session_id=self.memory.session_id)
