from __future__ import annotations

import re

_BLOCKED_PATTERNS = [
    r"\brm\s+(-rf?|-\s*rf?)\s",
    r"\brm\s+.*-r",
    r"^\s*rm\s+-rf",
    r"\brm\s+-rf\b",
    r"\bmkfs\.\w+",
    r">\s*/dev/sd",
    r':\(\)\s*\{\s*:\s*\|\s*:\s*&',
    r"wget\s+.*\|\s*sh\s*$",
    r"curl\s+.*\|\s*sh\s*$",
    # Windows-specific dangerous patterns
    r"\bdel\s+/[fs](?:\s+/[sq])?\s+\S:\\",
    r"\brd\s+/[sq]\s+\S:\\",
    r"\brmdir\s+/[sq]\s+\S:\\",
    r"\bformat\s+\w:",
    r"\bdiskpart\b",
    r"\bwmic\s+process\s+where.*delete\b",
    r"\btaskkill\s+/f\s+/im\s+(?:svchost|winlogon|csrss|lsass|smss|wininit|services)\b",
    r"\breg\s+delete\s+HKLM",
    r"\bicacls\s+\S+\s+/deny",
    r"\bdeltree\b",
]


_BLOCKED_RE = re.compile("|".join(_BLOCKED_PATTERNS), re.IGNORECASE)
