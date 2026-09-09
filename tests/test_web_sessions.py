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


class WebSessionBrowserTests(unittest.TestCase):
    @staticmethod
    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _request(base_url: str, path: str) -> dict:
        request = urllib.request.Request(base_url + path)
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def _start_server(self, workspace: Path, db_path: Path, port: int) -> tuple[subprocess.Popen, str]:
        repository = Path(__file__).parents[1]
        env = os.environ.copy()
        env.update({
            "HOME": str(workspace / "home"),
            "KYROZEN_DB_PATH": str(db_path),
            "KYROZEN_DISABLE_VECTOR_INDEX": "1",
            "KYROZEN_PROVIDER": "ollama",
            "KYROZEN_BASE_URL": "http://127.0.0.1:11434/v1",
        })
        for variable in ("KYROZEN_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                         "KYROZEN_SERVER_TOKEN"):
            env.pop(variable, None)
        process = subprocess.Popen(
            [sys.executable, str(repository / "server.py"), "--project", str(workspace),
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=workspace, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                self.fail(f"Web server exited before becoming ready: {output}")
            try:
                if self._request(base_url, "/api/health").get("status") in {"ok", "degraded"}:
                    return process, base_url
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        process.terminate()
        process.wait(timeout=5)
        self.fail("Web server did not become ready")

    @staticmethod
    def _stop_server(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()

    @unittest.skipUnless(
        os.environ.get("KYROZEN_BROWSER_TESTS") == "1",
        "browser integration requires `make install` (Playwright and Chromium)",
    )
    def test_browser_restores_switches_and_retains_durable_sessions_after_restart(self):
        with tempfile.TemporaryDirectory(prefix="openkyrozen-web-sessions-") as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "home").mkdir()
            db_path = root / "state.sqlite3"
            store = EventStore(db_path)
            for session_id, messages in {
                "session-one": [("user", "first user"), ("assistant", "UI_你好_✅")],
                "session-two": [("user", "second user"), ("assistant", "Second_会话_✅")],
            }.items():
                for role, content in messages:
                    store.append_event(
                        "session.message", {"role": role, "content": content},
                        user_id="local", workspace_id="default", session_id=session_id,
                    )

            port = self._free_port()
            process, base_url = self._start_server(workspace, db_path, port)
            playwright = browser = None
            try:
                from playwright.sync_api import sync_playwright

                playwright = sync_playwright().start()
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                page.add_init_script(
                    "if (!localStorage.getItem('openkyrozen.active-session')) "
                    "localStorage.setItem('openkyrozen.active-session', 'session-one');"
                )
                page.goto(base_url, wait_until="domcontentloaded")
                page.wait_for_function("document.body.innerText.includes('UI_你好_✅')")
                self.assertEqual(
                    page.locator("#session-select").evaluate("element => element.labels[0].textContent"),
                    "Saved conversation",
                )
                self.assertIn("durable history is never deleted", page.locator("#session-limit").text_content())

                page.select_option("#session-select", "session-two")
                page.wait_for_function("document.body.innerText.includes('Second_会话_✅')")
                self.assertNotIn("first user", page.locator("#chat").inner_text())
                page.reload(wait_until="domcontentloaded")
                page.wait_for_function("document.body.innerText.includes('Second_会话_✅')")

                self._stop_server(process)
                process, base_url = self._start_server(workspace, db_path, port)
                page.reload(wait_until="domcontentloaded")
                page.wait_for_function("document.body.innerText.includes('Second_会话_✅')")
                page.select_option("#session-select", "session-one")
                page.wait_for_function("document.body.innerText.includes('UI_你好_✅')")

                page.evaluate(
                    "localStorage.setItem('openkyrozen.active-session', 'invalid/session'); location.reload();"
                )
                page.wait_for_function(
                    "document.getElementById('status').textContent === 'Could not load this conversation.'"
                )
            finally:
                if browser is not None:
                    browser.close()
                if playwright is not None:
                    playwright.stop()
                self._stop_server(process)


if __name__ == "__main__":
    unittest.main()
