from __future__ import annotations

import re

CLAIM_SCOPES = {"global", "profile", "project", "task", "speaker", "audience", "channel"}


SCOPE_RANK = {"global": 0, "profile": 1, "project": 2, "channel": 3, "audience": 4,
              "speaker": 5, "task": 6}


_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|secret|password|token)\s*[:=]\s*[^\s]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
