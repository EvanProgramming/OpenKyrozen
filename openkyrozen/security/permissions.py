from __future__ import annotations

import os
import re
import sys
import datetime
from pathlib import Path
from openkyrozen.workspace.context import LaunchContext


def _state_root(self) -> Path:
    """Return the private durable state directory used by this process."""
    context = getattr(self, "_launch_context", None)
    if isinstance(context, LaunchContext):
        return context.runtime_state_root
    return Path.home().expanduser() / ".kyrozen" / "v2"


def _approval_log_path(self) -> Path:
    explicit = os.environ.get("KYROZEN_AUDIT_LOG", "").strip()
    return Path(explicit).expanduser() if explicit else self._state_root() / "kyrozen_audit.log"


def _record_tool_approval(self, action: str, decision: str, args: str = "") -> None:
    """Write a minimal local audit record without persisting obvious secrets."""
    safe_args = re.sub(r"(?i)(token|password|secret|api[_-]?key)\s*[:=]\s*\S+", r"\1=<redacted>", str(args))
    safe_args = re.sub(r"\bsk-[A-Za-z0-9_-]+", "sk-<redacted>", safe_args)
    safe_args = safe_args.replace("\n", " ")[:240]
    try:
        ts = datetime.datetime.now(self.UTC).isoformat(timespec="seconds")
        audit_path = self._approval_log_path()
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8") as audit_file:
            audit_file.write(f"[{ts}] [local-cli] TOOL_{decision.upper()} | {action} | {safe_args}\n")
    except OSError:
        pass


def _confirm_tool_action(self, action: str, args: str = "") -> bool:
    """Confirm high-impact local CLI actions while keeping normal tools frictionless."""
    if action not in self._APPROVAL_REQUIRED_TOOLS or self._EXECUTION_SURFACE not in {"cli", "tui"}:
        return True
    mode = os.environ.get("KYROZEN_APPROVAL_MODE", "dangerous").strip().lower()
    if mode in {"never", "none", "off"}:
        self._record_tool_approval(action, "approved", args)
        return True
    callback = self._approval_callback.get()
    if callable(callback):
        try:
            approved = bool(callback(action, args))
        except Exception:
            approved = False
        self._record_tool_approval(action, "approved" if approved else "denied", args)
        return approved
    if not sys.stdin.isatty():
        self._record_tool_approval(action, "denied_noninteractive", args)
        return False
    prompt = (
        f"High-impact action: {action} {args[:180]}\n"
        "This may change remote or working-tree state. Continue? [y/N]: "
    )
    try:
        approved = self.console.input(prompt).strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        approved = False
    self._record_tool_approval(action, "approved" if approved else "denied", args)
    return approved
