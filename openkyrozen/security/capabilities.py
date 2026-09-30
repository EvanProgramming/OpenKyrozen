"""Short-lived capability tokens for tool invocation."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilityToken:
    subject: str
    capabilities: frozenset[str]
    issued_at: float
    expires_at: float
    token_id: str

    def allows(self, capability: str) -> bool:
        return time.time() < self.expires_at and capability in self.capabilities

    def as_dict(self) -> dict[str, object]:
        return {
            "token_id": self.token_id, "subject": self.subject,
            "capabilities": sorted(self.capabilities), "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }


def issue_capability_token(subject: str, capabilities: set[str] | frozenset[str], *, ttl_seconds: float = 900) -> CapabilityToken:
    issued = time.time()
    return CapabilityToken(
        subject=subject,
        capabilities=frozenset(capabilities),
        issued_at=issued,
        expires_at=issued + max(1.0, min(float(ttl_seconds), 86_400)),
        token_id=f"cap_{secrets.token_urlsafe(18)}",
    )
