"""Local risk classification for optional TUI permission checks."""

from __future__ import annotations

import re
import shlex

from openkyrozen.security.tool_policy import tool_capability


_PRIVATE = re.compile(r"(?i)(?:^|[\s/\\.\"'=|])(?:\.env(?:[./\\\s|\"']|$)|\.ssh|\.aws|\.config|keychain|credentials?|secrets?|private[_-]?key|id_rsa|id_ed25519)")
_HIGH_IMPACT = frozenset({
    "git_push", "git_pull", "git_reset", "git_checkout", "git_stash", "git_remote",
    "git_commit", "git_add", "git_clone", "github_cli", "define_tool",
})
_SAFE_COMMANDS = frozenset({
    "pwd", "ls", "find", "rg", "grep", "cat", "head", "tail", "wc", "file",
    "git status", "git diff", "git log", "git show", "git branch --show-current",
    "python -m unittest", "pytest", "go test", "go vet", "make check", "make test",
    "make lint", "npm test", "npm run test", "cargo test", "cargo check",
})
_SHELL_META = re.compile(r"(?:\$\(|`|>|<|\{|\}|\*|\?|\[|\]|\\\\|\n)")
_COMPOUND = re.compile(r"(?:&&|\|\||[;|])")


def _arguments(value: object) -> str:
    if isinstance(value, dict):
        return " ".join(str(item) for item in value.values())[:4000]
    return str(value or "")[:4000]


def _safe_shell_command(args: str) -> bool:
    if _SHELL_META.search(args):
        return False
    segments = _COMPOUND.split(args)
    if not segments:
        return False
    for segment in segments:
        try:
            words = shlex.split(segment)
        except ValueError:
            return False
        if not words:
            continue
        if words[0] in {"sh", "bash", "zsh", "fish", "eval", "sudo", "doas"}:
            return False
        if " ".join(words[:2]) == "git status" or " ".join(words[:2]) in {
            "git diff", "git log", "git show",
        }:
            command = " ".join(words[:2])
        elif words[:3] == ["git", "branch", "--show-current"]:
            command = "git branch --show-current"
        elif words[:3] == ["python", "-m", "unittest"]:
            command = "python -m unittest"
        elif words[:2] == ["npm", "run"]:
            command = "npm run " + (words[2] if len(words) > 2 else "")
        elif words[0] in {"go", "make", "npm", "cargo"}:
            command = " ".join(words[:2])
        else:
            command = words[0]
        if command not in _SAFE_COMMANDS:
            return False
        if _PRIVATE.search(" ".join(words[1:])):
            return False
    return True


def risk_category(action: str, args: object) -> str | None:
    """Return a bounded risk label requiring Jev or local approval."""
    action = str(action or "").strip()
    text = _arguments(args)
    if _PRIVATE.search(text) or re.search(r"(?i)\b(api[_-]?key|password|credential|secret|private key|token)\b", text):
        return "private_data_access"
    if action in _HIGH_IMPACT:
        return "external_or_irreversible_change"
    if action in {"search_memory", "check_stored_data", "browser_snapshot"}:
        return "private_data_access"
    if action == "read_file":
        return "private_data_access" if _PRIVATE.search(text) else None
    if action in {"browser_click", "browser_type", "browser_open", "browser_close"}:
        return "opaque_or_high_impact_command"
    if action in {"run_cmd", "execute_terminal_command"}:
        return None if _safe_shell_command(text) else "opaque_or_high_impact_command"
    if action == "write_file":
        return None
    if tool_capability(action) in {"shell", "git", "destructive", "dynamic"}:
        return "external_or_irreversible_change"
    return None


def requires_ask_approval(action: str, args: object) -> bool:
    action = str(action or "").strip()
    if risk_category(action, args):
        return True
    return tool_capability(action) in {"write", "shell", "git", "browser", "destructive", "dynamic"}
