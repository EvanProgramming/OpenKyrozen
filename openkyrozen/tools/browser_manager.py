"""Optional isolated browser control with per-agent profiles."""

from __future__ import annotations

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

    def open(self, url: str) -> str:
        valid, reason = validate_browser_url(url)
        if not valid:
            return f"Error: {reason}"
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return "Error: browser support requires playwright; install it with `pip install playwright && playwright install chromium`."
        session_id = f"browser_{uuid.uuid4().hex[:12]}"
        try:
            playwright = sync_playwright().start()
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.root / session_id), headless=True,
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            self._sessions[session_id] = (playwright, context, page)
            return f"Browser session {session_id} opened {page.url}\nTitle: {page.title()}"
        except Exception as exc:
            try:
                playwright.stop()
            except Exception:
                pass
            return f"Error opening browser: {exc}"

    def snapshot(self, session_id: str) -> str:
        session = self._sessions.get(session_id.strip())
        if not session:
            return "Error: browser session not found"
        _, _, page = session
        try:
            return (
                f"URL: {page.url}\nTitle: {page.title()}\n\n"
                f"{page.locator('body').inner_text(timeout=BROWSER_SNAPSHOT_TIMEOUT_MS)[:12000]}"
            )
        except Exception as exc:
            return f"Error reading browser page: {exc}"

    def click(self, args: str) -> str:
        parts = args.split("|", 1)
        if len(parts) != 2:
            return "Error: browser_click args format is session_id|selector"
        session = self._sessions.get(parts[0].strip())
        if not session:
            return "Error: browser session not found"
        try:
            session[2].locator(parts[1].strip()).click(timeout=10_000)
            return self.snapshot(parts[0])
        except Exception as exc:
            return f"Error clicking browser element: {exc}"

    def type_text(self, args: str) -> str:
        parts = args.split("|", 2)
        if len(parts) != 3:
            return "Error: browser_type args format is session_id|selector|text"
        session = self._sessions.get(parts[0].strip())
        if not session:
            return "Error: browser session not found"
        try:
            session[2].locator(parts[1].strip()).fill(parts[2])
            return "Text entered."
        except Exception as exc:
            return f"Error typing in browser: {exc}"

    def close(self, session_id: str) -> str:
        session = self._sessions.pop(session_id.strip(), None)
        if not session:
            return "Error: browser session not found"
        try:
            session[1].close()
            session[0].stop()
            return f"Browser session {session_id} closed."
        except Exception as exc:
            return f"Error closing browser: {exc}"

    def close_all(self) -> None:
        for session_id in list(self._sessions):
            self.close(session_id)
