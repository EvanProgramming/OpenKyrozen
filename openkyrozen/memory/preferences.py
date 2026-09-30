from __future__ import annotations

import re
import uuid
from typing import Any


def _preference_fields(self, content: str) -> dict[str, Any]:
    """Parse one durable PREF record without applying untrusted text."""
    if not str(content).lstrip().upper().startswith("PREF:"):
        return {}
    fields: dict[str, Any] = {}
    for part in str(content).split(":", 1)[1].split(";"):
        key, separator, value = part.strip().partition("=")
        if not separator or key not in self._user_preferences:
            continue
        value = value.strip()
        if not value:
            continue
        if isinstance(self._user_preferences[key], bool):
            if value.lower() not in {"true", "false"}:
                continue
            fields[key] = value.lower() == "true"
        else:
            fields[key] = value
    return fields


def _restore_user_preferences(self) -> dict[str, Any]:
    """Hydrate only active, visible, conflict-free preferences for this scope."""
    profile = getattr(self, "_agent_profile_mode", None)
    profile = profile if profile in {"coder", "researcher"} else None
    scope = (self.memory_bank.user_id, self.memory_bank.workspace_id, self.memory_bank.session_id, profile)
    if scope != self._preference_scope:
        for key, value in self._hydrated_preferences.items():
            if self._user_preferences.get(key) == value:
                self._user_preferences[key] = False if isinstance(value, bool) else ""
        self._hydrated_preferences.clear()
        self._preference_scope = scope
    try:
        rows = self.memory_bank.store.list_memories(
            kind="preference", status="active", limit=10000,
            workspace_id=self.memory_bank.workspace_id, session_id=self.memory_bank.session_id,
            user_id=self.memory_bank.user_id,
        )
        if self.memory_bank.session_id is None:
            rows = [row for row in rows if not row.get("session_id")]
        rows = self.memory_bank.filter_records(
            rows, profile=profile, authorized_speakers={self.memory_bank.user_id},
        )
    except Exception:
        return {}
    candidates: dict[str, set[str]] = {}
    values: dict[str, Any] = {}
    for row in rows:
        for key, value in self._preference_fields(row.get("content", "")).items():
            candidates.setdefault(key, set()).add(repr(value))
            values[key] = value
    restored: dict[str, Any] = {}
    for key, representations in candidates.items():
        if len(representations) != 1 or self._user_preferences.get(key):
            continue
        self._user_preferences[key] = values[key]
        self._hydrated_preferences[key] = values[key]
        restored[key] = values[key]
    return restored


def _detect_user_preferences(self, user_input: str) -> None:
    """Detect implicit user preferences from their messages."""
    low = user_input.lower()

    # Language preference
    lang_signals = {
        "python": "python", "py": "python",
        "javascript": "javascript", "js": "javascript",
        "typescript": "typescript", "ts": "typescript",
        "rust": "rust", "go": "go", "golang": "go",
        "java": "java", "c++": "c++", "cpp": "c++",
        "ruby": "ruby", "swift": "swift",
    }
    for signal, lang in lang_signals.items():
        if re.search(rf"\b{signal}\b", low):
            self._user_preferences["language"] = lang
            break

    # Naming style
    if re.search(r"\b[a-z]+_[a-z]+\b", user_input) and "rust" not in low:
        self._user_preferences["naming_style"] = "snake_case"
    elif re.search(r"\b[a-z]+[A-Z][a-z]+\b", user_input):
        self._user_preferences["naming_style"] = "camelCase"

    # Verbosity
    if any(w in low for w in ["brief", "short", "concise", "quick"]):
        self._user_preferences["verbosity"] = "concise"
    elif any(w in low for w in ["detailed", "explain fully", "in depth", "comprehensive"]):
        self._user_preferences["verbosity"] = "detailed"

    # Code-first
    if any(w in low for w in ["show me the code", "just the code", "code only"]):
        self._user_preferences["code_first"] = True

    # Tables
    if any(w in low for w in ["table", "comparison table", "tabular"]):
        self._user_preferences["prefers_tables"] = True

    # Store detected preferences
    detected = {k: v for k, v in self._user_preferences.items() if v}
    if detected:
        pref_str = "; ".join(f"{k}={v}" for k, v in detected.items())
        # Each user turn is an independent observation.  Reusing a content hash
        # (or suppressing a repeated message) prevents the evidence threshold
        # from ever promoting a preference across a process restart.
        self.learning_engine.submit(
            "preference", f"PREF: {pref_str}", evidence_id=f"turn-{uuid.uuid4().hex}",
            confidence=0.5, metadata={"source": "preference_detection"},
        )


def _build_preference_context(self) -> str:
    """Build a system message describing known user preferences."""
    self._restore_user_preferences()
    active = [f"{k}={v}" for k, v in self._user_preferences.items()
              if v and v not in ("", False)]
    if not active:
        return ""
    return (
        "## Known user preferences (apply unless overridden)\n"
        + "\n".join(f"- {p}" for p in active)
    )
