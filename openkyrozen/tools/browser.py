from __future__ import annotations



from openkyrozen.tools.models import CommandResult
from openkyrozen.security.command_policy import _BLOCKED_RE

def browser_open(self, args: str) -> str:
    """Open a URL in an isolated headless browser profile. Args: URL."""
    return self._BROWSER.open(args.strip())


def browser_snapshot(self, args: str) -> str:
    """Read the current page text. Args: browser session ID."""
    return self._BROWSER.snapshot(args.strip())


def browser_click(self, args: str) -> str:
    """Click a selector. Args format: session_id|CSS selector."""
    return self._BROWSER.click(args)


def browser_type(self, args: str) -> str:
    """Fill a selector. Args format: session_id|CSS selector|text."""
    return self._BROWSER.type_text(args)


def browser_close(self, args: str) -> str:
    """Close an isolated browser session. Args: browser session ID."""
    return self._BROWSER.close(args.strip())
