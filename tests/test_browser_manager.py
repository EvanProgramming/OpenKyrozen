import builtins
import http.server
import os
import re
import threading
import tempfile
import unittest
from unittest.mock import patch

from browser_manager import BrowserManager, validate_browser_url
import server


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

                class ChatHandler(http.server.BaseHTTPRequestHandler):
                    def do_GET(self):  # noqa: N802 - stdlib HTTP server contract
                        if self.path == "/":
                            body = server.CHAT_HTML.encode("utf-8")
                            content_type = "text/html; charset=utf-8"
                        elif self.path == "/api/cost":
                            body = b'{"summary":"initial-cost"}'
                            content_type = "application/json"
                        else:
                            self.send_error(404)
                            return
                        self.send_response(200)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)

                    def log_message(self, *_args):
                        return

                chat_httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ChatHandler)
                chat_thread = threading.Thread(target=chat_httpd.serve_forever, daemon=True)
                chat_thread.start()
                playwright = None
                browser = None
                try:
                    from playwright.sync_api import sync_playwright

                    playwright = sync_playwright().start()
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page()
                    page.add_init_script(r"""
const originalFetch = window.fetch.bind(window);
let streamCall = 0;
window.fetch = async (input, init) => {
  const url = typeof input === 'string' ? input : input.url;
  if (url.endsWith('/api/cost')) {
    return new Response(JSON.stringify({summary: 'initial-cost'}), {
      status: 200, headers: {'Content-Type': 'application/json'}
    });
  }
  if (!url.endsWith('/api/chat/stream')) return originalFetch(input, init);
  streamCall += 1;
  const frames = streamCall === 1 ? [
    'data: {"chunk":"Hello "}\n\n',
    'data: {"chunk":"世界"}\n\n',
    'data: {"cost":"Cost: split-stream"}\n\n',
    'data: [DONE]\n\n'
  ] : ['data: {"error":"mock provider failure"}\n\n'];
  const bytes = new TextEncoder().encode(frames.join(''));
  return new Response(new ReadableStream({
    start(controller) {
      let offset = 0;
      let part = 0;
      const sizes = [1, 4, 2, 7, 3, 5];
      function enqueueNext() {
        if (offset >= bytes.length) {
          controller.close();
          return;
        }
        const end = Math.min(bytes.length, offset + sizes[part % sizes.length]);
        controller.enqueue(bytes.slice(offset, end));
        offset = end;
        part += 1;
        setTimeout(enqueueNext, 20);
      }
      enqueueNext();
    }
  }), {status: 200, headers: {'Content-Type': 'text/event-stream'}});
};
""")
                    page.goto(
                        f"http://127.0.0.1:{chat_httpd.server_port}/",
                        wait_until="domcontentloaded",
                    )
                    page.fill("#user-input", "split boundary")
                    page.click("#send-btn")
                    page.wait_for_function(
                        "document.querySelector('.msg.assistant .content').textContent.includes('Hello')"
                    )
                    self.assertEqual(page.locator("#status").text_content(), "Thinking...")
                    page.wait_for_function(
                        "document.getElementById('status').textContent === 'Ready'"
                    )
                    assistant = page.locator(".msg.assistant .content").nth(0)
                    self.assertEqual(assistant.text_content(), "Hello 世界")
                    self.assertEqual(
                        page.locator("#cost-display").text_content(), "Cost: split-stream"
                    )
                    self.assertNotIn("parse error", assistant.text_content().lower())

                    page.fill("#user-input", "error boundary")
                    page.click("#send-btn")
                    page.wait_for_function(
                        "document.querySelectorAll('.msg.assistant .content')[1].textContent.includes('mock provider failure')"
                    )
                    page.wait_for_function(
                        "document.getElementById('status').textContent === 'Error'"
                    )
                    self.assertEqual(page.locator("#status").text_content(), "Error")
                    self.assertEqual(
                        page.locator(".msg.assistant .content").nth(1).text_content(),
                        "Error: mock provider failure",
                    )
                finally:
                    if browser is not None:
                        browser.close()
                    if playwright is not None:
                        playwright.stop()
                    chat_httpd.shutdown()
                    chat_httpd.server_close()
                    chat_thread.join(timeout=5)
        finally:
            if manager is not None:
                manager.close_all()
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
