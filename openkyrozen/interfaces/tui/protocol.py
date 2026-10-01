from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from typing import Any

PROTOCOL_VERSION = 1


MAX_LINE_BYTES = 64 * 1024


MAX_TEXT_CHARS = 12_000


MAX_ARGS_CHARS = 4_000


MAX_REQUEST_ID_CHARS = 100


APPROVAL_TIMEOUT_SECONDS = 15 * 60


MAX_ATTACHMENTS = 10


MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


_SENSITIVE_KEY_RE = re.compile(r"(?i)(api[_-]?key|secret|password|(?:access|refresh|auth|bearer|api|session|csrf)[_-]?token|^token$)")


_OUTPUT_LOCK = threading.Lock()


def _redact(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    text = str(value if value is not None else "").replace("\x00", "").replace("\r", " ")
    text = re.sub(
        r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>", text,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "sk-<redacted>", text)
    return text[:limit]


def _safe_json(value: Any) -> Any:
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if _SENSITIVE_KEY_RE.search(str(key)) else _safe_json(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact(value)
