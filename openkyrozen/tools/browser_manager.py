"""Optional isolated browser control with per-agent profiles."""

from __future__ import annotations
from openkyrozen.tools.models import ToolError, ToolResult, command_tool_result, invoke_tool_result, legacy_text_api, tool_failure, tool_json_result, tool_success

import ipaddress
import os
import socket
import uuid
from pathlib import Path
from urllib.parse import urlparse


BROWSER_SNAPSHOT_TIMEOUT_MS = 10_000


def validate_browser_url(url: str) -> tuple[bool, str]:
    parsed = urlparse(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False, "Only http(s) URLs are supported"
    if parsed.username or parsed.password:
        return False, "Credentials in browser URLs are not allowed"
    if os.environ.get("KYROZEN_BROWSER_ALLOW_PRIVATE", "").lower() in {"1", "true", "yes"}:
        return True, ""
    try:
        address = ipaddress.ip_address(parsed.hostname)
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            return False, "Private and loopback browser destinations are disabled"
    except ValueError:
        try:
            resolved = socket.gethostbyname(parsed.hostname)
            address = ipaddress.ip_address(resolved)
            if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
                return False, "Browser hostname resolves to a private destination"
        except OSError:
            pass
    return True, ""


class BrowserManager:
    """Owns isolated Playwright contexts and never touches the user's browser profile."""

    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = Path(root or os.environ.get("KYROZEN_BROWSER_PROFILES", Path.home() / ".kyrozen" / "v2" / "browser_profiles")).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, tuple[object, object, object]] = {}

    @legacy_text_api
    def open(self, url: str) -> str:
        valid, reason = validate_browser_url(url)
        if not valid:
            return tool_failure('permission_denied' if 'private' in reason.lower() or 'credentials' in reason.lower() else 'invalid_arguments', f"Error: {reason}", retriable=False)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return tool_failure('dependency_unavailable', "Error: browser support requires playwright; install it with `pip install playwright && playwright install chromium`.", retriable=False)
        session_id = f"browser_{uuid.uuid4().hex[:12]}"
        try:
            playwright = sync_playwright().start()
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.root / session_id), headless=True,
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            self._sessions[session_id] = (playwright, context, page)
            return tool_success(str(f"Browser session {session_id} opened {page.url}\nTitle: {page.title()}"))
        except Exception as exc:
            try:
                playwright.stop()
            except Exception:
                pass
            return tool_failure('execution_error', f"Error opening browser: {exc}", retriable=False)

    @legacy_text_api
    def snapshot(self, session_id: str) -> str:
        session = self._sessions.get(session_id.strip())
        if not session:
            return tool_failure('not_found', "Error: browser session not found", retriable=False)
        _, _, page = session
        try:
            return (
                tool_success(str(f"URL: {page.url}\nTitle: {page.title()}\n\n"
                f"{page.locator('body').inner_text(timeout=BROWSER_SNAPSHOT_TIMEOUT_MS)[:12000]}"))
            )
        except Exception as exc:
            return tool_failure('execution_error', f"Error reading browser page: {exc}", retriable=False)

    @legacy_text_api
    def click(self, args: str) -> str:
        parts = args.split("|", 1)
        if len(parts) != 2:
            return tool_failure('invalid_arguments', "Error: browser_click args format is session_id|selector", retriable=False)
        session = self._sessions.get(parts[0].strip())
        if not session:
            return tool_failure('not_found', "Error: browser session not found", retriable=False)
        try:
            session[2].locator(parts[1].strip()).click(timeout=10_000)
            return invoke_tool_result(self.snapshot, parts[0])
        except Exception as exc:
            return tool_failure('execution_error', f"Error clicking browser element: {exc}", retriable=False)

    @legacy_text_api
    def type_text(self, args: str) -> str:
        parts = args.split("|", 2)
        if len(parts) != 3:
            return tool_failure('invalid_arguments', "Error: browser_type args format is session_id|selector|text", retriable=False)
        session = self._sessions.get(parts[0].strip())
        if not session:
            return tool_failure('not_found', "Error: browser session not found", retriable=False)
        try:
            session[2].locator(parts[1].strip()).fill(parts[2])
            return tool_success(str("Text entered."))
        except Exception as exc:
            return tool_failure('execution_error', f"Error typing in browser: {exc}", retriable=False)

    @legacy_text_api
    def close(self, session_id: str) -> str:
        session = self._sessions.pop(session_id.strip(), None)
        if not session:
            return tool_failure('not_found', "Error: browser session not found", retriable=False)
        try:
            session[1].close()
            session[0].stop()
            return tool_success(str(f"Browser session {session_id} closed."))
        except Exception as exc:
            return tool_failure('execution_error', f"Error closing browser: {exc}", retriable=False)

    def close_all(self) -> None:
        for session_id in list(self._sessions):
            self.close(session_id)
