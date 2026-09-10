import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from event_store import EventStore


class WebAuthBrowserTests(unittest.TestCase):
    @staticmethod
    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _health(base_url: str) -> dict:
        request = urllib.request.Request(
            base_url + "/api/health",
            headers={"Authorization": "Bearer browser-ui-token"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _stop_server(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()

    @staticmethod
    def _start_server(workspace: Path, database: Path, port: int) -> tuple[subprocess.Popen, str]:
        repository = Path(__file__).parents[1]
        environment = os.environ.copy()
        environment.update({
            "HOME": str(workspace / "home"),
            "KYROZEN_DB_PATH": str(database),
            "KYROZEN_DISABLE_VECTOR_INDEX": "1",
            "KYROZEN_PROVIDER": "ollama",
            "KYROZEN_BASE_URL": "http://127.0.0.1:11434/v1",
            "KYROZEN_SERVER_TOKEN": "browser-ui-token",
        })
        for variable in ("KYROZEN_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            environment.pop(variable, None)
        process = subprocess.Popen(
            [sys.executable, str(repository / "server.py"), "--project", str(workspace),
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=workspace, env=environment, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
        )
        base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                raise AssertionError(f"Web server exited before becoming ready: {output}")
            try:
                if WebAuthBrowserTests._health(base_url).get("status") in {"ok", "degraded"}:
                    return process, base_url
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        WebAuthBrowserTests._stop_server(process)
        raise AssertionError("Web server did not become ready")

    @unittest.skipUnless(
        os.environ.get("KYROZEN_BROWSER_TESTS") == "1",
        "browser integration requires `make install` (Playwright and Chromium)",
    )
    def test_token_enabled_ui_authenticates_loads_sessions_streams_and_reports_invalid_token(self):
        with tempfile.TemporaryDirectory(prefix="openkyrozen-web-auth-") as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "home").mkdir()
            database = root / "state.sqlite3"
            store = EventStore(database)
            store.append_event(
                "session.message", {"role": "assistant", "content": "AUTH_UI_HISTORY"},
                user_id="local", workspace_id="default", session_id="auth-ui-session",
            )
            process, base_url = self._start_server(workspace, database, self._free_port())
            playwright = browser = bad_context = None
            try:
                from playwright.sync_api import sync_playwright

                playwright = sync_playwright().start()
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                page.add_init_script(
                    "localStorage.setItem('openkyrozen.active-session', 'auth-ui-session');"
                )
                stream_headers = {}

                def stream_route(route):
                    stream_headers.update(route.request.all_headers())
                    route.fulfill(
                        status=200,
                        headers={"Content-Type": "text/event-stream"},
                        body='data: {"chunk":"AUTH_UI_STREAM"}\n\ndata: [DONE]\n\n',
                    )

                page.route("**/api/chat/stream", stream_route)
                page.goto(base_url, wait_until="domcontentloaded")
                page.wait_for_function(
                    "document.getElementById('auth-controls').hidden === false"
                )
                page.fill("#server-token", "browser-ui-token")
                page.click("#authenticate")
                page.wait_for_function("document.body.innerText.includes('AUTH_UI_HISTORY')")
                self.assertTrue(page.locator("#auth-controls").is_hidden())

                page.fill("#user-input", "stream through the authenticated UI")
                page.click("#send-btn")
                page.wait_for_function("document.body.innerText.includes('AUTH_UI_STREAM')")
                page.wait_for_function("document.getElementById('status').textContent === 'Ready'")
                self.assertIn("openkyrozen_browser_session=", stream_headers.get("cookie", ""))

                bad_context = browser.new_context()
                bad_page = bad_context.new_page()
                bad_page.goto(base_url, wait_until="domcontentloaded")
                bad_page.wait_for_function(
                    "document.getElementById('auth-controls').hidden === false"
                )
                bad_page.fill("#server-token", "wrong-token")
                bad_page.click("#authenticate")
                bad_page.wait_for_function(
                    "document.getElementById('auth-status').textContent.includes('Invalid server token')"
                )
            finally:
                if bad_context is not None:
                    bad_context.close()
                if browser is not None:
                    browser.close()
                if playwright is not None:
                    playwright.stop()
                self._stop_server(process)


if __name__ == "__main__":
    unittest.main()
