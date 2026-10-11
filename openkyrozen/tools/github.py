from __future__ import annotations
from openkyrozen.tools.models import ToolError, ToolResult, command_tool_result, legacy_text_api, tool_failure, tool_json_result, tool_success

import json

from openkyrozen.security.command_policy import _BLOCKED_RE

@legacy_text_api
def github_status(self, args: str = "") -> str:
    """Inspect GitHub CLI installation and authentication without exposing tokens."""
    if self._GITHUB_CLI is None:
        return tool_failure('not_found', "Error: GitHub CLI is not configured.", retriable=False)
    data = self._GITHUB_CLI.status(str(args).strip() or None)
    return tool_json_result(data, text=json.dumps(data, ensure_ascii=False, indent=2))


@legacy_text_api
def github_read(self, args: str | list[str]) -> str:
    """Run an allowlisted read-only GitHub CLI command."""
    return (self._GITHUB_CLI.run_result(args, read_only=True) if self._GITHUB_CLI is not None else tool_failure('not_found', "Error: GitHub CLI is not configured.", retriable=False))


@legacy_text_api
def github_cli(self, args: str | list[str]) -> str:
    """Run GitHub CLI arguments directly without a shell. Agent mode and approval are required."""
    return (self._GITHUB_CLI.run_result(args) if self._GITHUB_CLI is not None else tool_failure('not_found', "Error: GitHub CLI is not configured.", retriable=False))
