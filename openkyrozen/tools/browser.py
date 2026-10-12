from __future__ import annotations
from openkyrozen.tools.models import ToolError, ToolResult, command_tool_result, invoke_tool_result, legacy_text_api, tool_failure, tool_json_result, tool_success



from openkyrozen.security.command_policy import _BLOCKED_RE

@legacy_text_api
def browser_open(self, args: str) -> str:
    """Open a URL in an isolated headless browser profile. Args: URL."""
    return invoke_tool_result(self._BROWSER.open, args if isinstance(args, dict) else args.strip())


@legacy_text_api
def browser_snapshot(self, args: str) -> str:
    """Read the current page text. Args: browser session ID."""
    return invoke_tool_result(self._BROWSER.snapshot, args if isinstance(args, dict) else args.strip())


@legacy_text_api
def browser_click(self, args: str) -> str:
    """Click a selector. Args format: session_id|CSS selector."""
    return invoke_tool_result(self._BROWSER.click, args)


@legacy_text_api
def browser_type(self, args: str) -> str:
    """Fill a selector. Args format: session_id|CSS selector|text."""
    return invoke_tool_result(self._BROWSER.type_text, args)


@legacy_text_api
def browser_close(self, args: str) -> str:
    """Close an isolated browser session. Args: browser session ID."""
    return invoke_tool_result(self._BROWSER.close, args if isinstance(args, dict) else args.strip())
