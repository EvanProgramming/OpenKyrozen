from __future__ import annotations

import os
import re
import sys
import datetime
import math
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


def _confirm_tool_action(self, action: str, args: str = "", *, force: bool = False) -> bool:
    """Confirm high-impact local CLI actions while keeping normal tools frictionless."""
    if (not force and action not in self._APPROVAL_REQUIRED_TOOLS) or self._EXECUTION_SURFACE not in {"cli", "tui"}:
        return True
    mode = os.environ.get("KYROZEN_APPROVAL_MODE", "dangerous").strip().lower()
    if not force and mode in {"never", "none", "off"}:
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


def _jev_permission_check(self, action: str, category: str) -> bool:
    """Permit only a strongly confident safe judgment over a sanitized summary."""
    from openkyrozen.routing.transport import _request

    self._emit_stream_event({"event": "permission_check", "state": "checking", "category": category})
    questions = {"permission": {
        "type": "choice",
        "instructions": "Can this operation proceed without a separate user approval?",
        "criteria": {"allow": "The described category is safe to execute without user approval",
                     "deny": "This category is risky, sensitive, or uncertain"},
    }}
    safe_actions = {"read_file", "run_cmd", "write_file", "git_push", "git_reset", "github_cli",
                    "git_commit", "git_pull", "git_checkout", "git_remote", "git_clone", "git_add",
                    "git_stash", "define_tool", "search_memory", "check_stored_data"}
    summary = {"action": action if action in safe_actions else "other_tool",
               "risk_category": category, "surface": "local TUI"}
    try:
        response = _request("jev", questions, summary, timeout=3.0)
        answer = response.get("answers", {}).get("permission", {})
        probabilities = answer.get("probabilities", {}) if isinstance(answer, dict) else {}
        confidence = answer.get("confidence") if isinstance(answer, dict) else None
        allow_probability = probabilities.get("allow") if isinstance(probabilities, dict) else None
        deny_probability = probabilities.get("deny") if isinstance(probabilities, dict) else None
        numeric = (confidence, allow_probability, deny_probability)
        allowed = (
            category != "private_data_access" and isinstance(answer, dict)
            and answer.get("type") == "choice" and answer.get("choice") == "allow"
            and set(probabilities) == {"allow", "deny"}
            and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in numeric)
            and math.isfinite(float(confidence)) and float(confidence) >= 0.995
            and math.isfinite(float(allow_probability)) and float(allow_probability) >= 0.995
            and math.isfinite(float(deny_probability)) and 0 <= float(deny_probability) <= 1
            and abs(float(allow_probability) + float(deny_probability) - 1) <= 0.02
        )
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        allowed = False
    self._emit_stream_event({"event": "permission_check", "state": "allowed" if allowed else "approval_required",
                             "category": category})
    self._record_permission_decision(action, category, "jev_allow" if allowed else "local_approval")
    return allowed


def _record_permission_decision(self, action: str, category: str, outcome: str) -> None:
    try:
        self.memory_bank.store.append_event(
            "permission.decision", {"action": action[:64], "category": category[:64],
                                    "mode": self.permission_mode(), "outcome": outcome},
            user_id=self.memory_bank.user_id, workspace_id=self.interaction_workspace_id(),
        )
    except Exception:
        pass


def _authorize_tool_action(self, action: str, args: object, *, approve=None) -> bool:
    """Apply the selected TUI permission policy before executing a tool."""
    if self._EXECUTION_SURFACE != "tui":
        return True
    from openkyrozen.security.permission_gate import requires_ask_approval, risk_category

    mode = self.permission_mode()
    risk = risk_category(action, args)
    if mode == "full":
        if risk:
            self._record_permission_decision(action, risk, "unprotected_full")
        return True
    if mode == "full_jev" and not risk:
        return True
    if mode == "full_jev" and risk and self._jev_permission_check(action, risk):
        return True
    if mode == "ask" and not requires_ask_approval(action, args):
        return True
    callback = approve or (lambda current, details: self._confirm_tool_action(current, details, force=True))
    self._emit_stream_event({"event": "permission_check", "state": "approval_required",
                             "category": risk or "workspace_change"})
    approved = bool(callback(action, str(args)[:600]))
    self._emit_stream_event({"event": "permission_check", "state": "allowed" if approved else "denied",
                             "category": risk or "workspace_change"})
    self._record_permission_decision(action, risk or "workspace_change",
                                     "user_approved" if approved else "user_denied")
    return approved
