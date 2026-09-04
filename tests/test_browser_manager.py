import builtins
import http.server
import os
import re
import threading
import tempfile
import unittest
from unittest.mock import patch

from browser_manager import BrowserManager, validate_browser_url


class BrowserManagerTests(unittest.TestCase):
    def test_browser_url_boundary(self):
        with patch("browser_manager.socket.gethostbyname", return_value="93.184.216.34"):
            self.assertTrue(validate_browser_url("https://example.com")[0])
        self.assertFalse(validate_browser_url("file:///tmp/a")[0])
        with patch.dict("os.environ", {"KYROZEN_BROWSER_ALLOW_PRIVATE": ""}):
            self.assertFalse(validate_browser_url("http://127.0.0.1:8000")[0])

    def test_missing_playwright_is_actionable(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = BrowserManager(root=directory)
            real_import = builtins.__import__

            def import_without_playwright(name, *args, **kwargs):
                if name == "playwright.sync_api":
                    raise ImportError("simulated missing playwright")
                return real_import(name, *args, **kwargs)

            with patch("browser_manager.socket.gethostbyname", return_value="93.184.216.34"), \
                    patch("builtins.__import__", side_effect=import_without_playwright):
                result = manager.open("https://example.com")
            self.assertIn("playwright", result.lower())

    def test_real_open_snapshot_click_and_close_flow(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
                body = b"""<!doctype html>
<html><head><title>Browser smoke</title>
<script>
window.addEventListener('DOMContentLoaded', () => {
  const original = document.body;
  original.remove();
  window.setTimeout(() => {
    const body = document.createElement('body');
    body.innerHTML = '<button id="advance">Advance</button><p id="status">initial</p>';
    document.documentElement.append(body);
    document.getElementById('advance').addEventListener('click', () => {
      document.getElementById('status').textContent = 'updated';
    });
  }, 100);
});
</script></head><body></body></html>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        manager = None
        try:
            with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"KYROZEN_BROWSER_ALLOW_PRIVATE": "1"}
            ):
                manager = BrowserManager(root=directory)
                opened = manager.open(f"http://127.0.0.1:{httpd.server_port}/")
                self.assertNotIn("Error", opened)
                match = re.search(r"Browser session (browser_[0-9a-f]+) opened", opened)
                self.assertIsNotNone(match, opened)
                session_id = match.group(1)

                immediate = manager.snapshot(session_id)
                self.assertIn("initial", immediate)
                clicked = manager.click(f"{session_id}|#advance")
                self.assertIn("updated", clicked)
                closed = manager.close(session_id)
                self.assertIn("closed", closed)
        finally:
            if manager is not None:
                manager.close_all()
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
