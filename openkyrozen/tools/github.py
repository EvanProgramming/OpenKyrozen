from __future__ import annotations

import json

from openkyrozen.tools.models import CommandResult
from openkyrozen.security.command_policy import _BLOCKED_RE

def github_status(self, args: str = "") -> str:
    """Inspect GitHub CLI installation and authentication without exposing tokens."""
    if self._GITHUB_CLI is None:
        return "Error: GitHub CLI is not configured."
    return json.dumps(self._GITHUB_CLI.status(str(args).strip() or None), ensure_ascii=False, indent=2)


def github_read(self, args: str | list[str]) -> str:
    """Run an allowlisted read-only GitHub CLI command."""
    return self._GITHUB_CLI.run(args, read_only=True) if self._GITHUB_CLI is not None else "Error: GitHub CLI is not configured."


def github_cli(self, args: str | list[str]) -> str:
    """Run GitHub CLI arguments directly without a shell. Agent mode and approval are required."""
    return self._GITHUB_CLI.run(args) if self._GITHUB_CLI is not None else "Error: GitHub CLI is not configured."
