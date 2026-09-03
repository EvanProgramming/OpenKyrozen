import tempfile
import unittest
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from pathlib import Path

import tools
from tools import AVAILABLE_TOOLS, allowed_tool_names, read_file, read_webpage, run_cmd, set_workspace_root, write_file


class WorkspaceToolTests(unittest.TestCase):
    def test_file_tools_stay_inside_workspace(self):
        original_root = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            set_workspace_root(root)
            try:
                self.assertIn("Wrote", write_file("inside.txt|hello"))
                self.assertEqual(read_file("inside.txt"), "hello")
                self.assertTrue(read_file(str(root.parent / "outside.txt")).startswith("Error"))
                self.assertTrue(write_file(str(root.parent / "outside.txt") + "|nope").startswith("Error"))
            finally:
                set_workspace_root(original_root)

    def test_capability_profiles_keep_rich_access_without_irreversible_reset(self):
        workspace = allowed_tool_names(AVAILABLE_TOOLS, "workspace")
        full = allowed_tool_names(AVAILABLE_TOOLS, "full")
        self.assertIn("run_cmd", workspace)
        self.assertIn("write_file", workspace)
        self.assertNotIn("git_reset", workspace)
        self.assertIn("git_reset", full)

    def test_run_cmd_resolves_bare_python_from_active_environment(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False):
            result = run_cmd("python -c 'import sys; print(sys.executable)' ")
        self.assertEqual(Path(result).resolve(), Path(sys.executable).resolve())

    def test_run_cmd_preserves_explicit_interpreter_paths(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False):
            result = run_cmd(f"{sys.executable} -c 'print(\"explicit\")'")
        self.assertEqual(result, "explicit")

    def test_read_webpage_blocks_private_and_reserved_destinations_before_connecting(self):
        class PrivateHandler(BaseHTTPRequestHandler):
            requests = 0

            def do_GET(self):
                self.__class__.requests += 1
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"loopback-only secret")

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), PrivateHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            local_url = f"http://127.0.0.1:{server.server_port}/private"
            result = read_webpage(local_url)
            self.assertIn("URL blocked", result)
            self.assertEqual(PrivateHandler.requests, 0)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        for url in (
            "http://10.0.0.1/",
            "http://192.168.1.1/",
            "http://172.16.0.1/",
            "http://169.254.169.254/",
            "http://224.0.0.1/",
            "http://0.0.0.0/",
            "http://[::1]/",
            "http://[fe80::1]/",
            "http://[::ffff:127.0.0.1]/",
        ):
            with self.subTest(url=url):
                self.assertIn("URL blocked", read_webpage(url))

    def test_hostname_resolution_to_private_address_is_blocked(self):
        with patch("tools.socket.getaddrinfo", return_value=[(
            tools.socket.AF_INET, tools.socket.SOCK_STREAM, tools.socket.IPPROTO_TCP,
            "", ("10.20.30.40", 80),
        )]):
            result = read_webpage("http://service.example/private")
        self.assertIn("URL blocked", result)
        self.assertIn("10.20.30.40", result)

    def test_redirect_is_revalidated_and_response_size_is_bounded(self):
        class RedirectHandler(BaseHTTPRequestHandler):
            requests = []
            oversized = False

            def do_GET(self):
                self.__class__.requests.append(self.path)
                if self.path == "/redirect":
                    location = f"http://127.0.0.1:{self.server.server_port}/private"
                    self.send_response(302)
                    self.send_header("Location", location)
                    self.end_headers()
                    return
                body = b"x" * (tools._MAX_WEBPAGE_BYTES + 1) if self.oversized else b"safe body"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        real_resolve = tools._resolve_web_destination
        real_connect = tools.socket.create_connection

        def resolve(url):
            parsed = tools.urlsplit(url)
            if parsed.hostname == "public.example":
                return parsed, "public.example", parsed.port or 80, "192.0.2.10"
            return real_resolve(url)

        def connect(address, timeout=None, source_address=None):
            if address[0] == "192.0.2.10":
                return real_connect(("127.0.0.1", httpd.server_port), timeout, source_address)
            return real_connect(address, timeout, source_address)

        try:
            with patch.object(tools, "_resolve_web_destination", side_effect=resolve):
                with patch.object(tools.socket, "create_connection", side_effect=connect):
                    result = read_webpage(f"http://public.example:{httpd.server_port}/redirect")
            self.assertIn("URL blocked", result)
            self.assertEqual(RedirectHandler.requests, ["/redirect"])

            RedirectHandler.oversized = True
            with patch.object(tools, "_resolve_web_destination", side_effect=resolve):
                with patch.object(tools.socket, "create_connection", side_effect=connect):
                    result = read_webpage(f"http://public.example:{httpd.server_port}/large")
            self.assertIn("exceeds", result)
        finally:
            httpd.shutdown()
            thread.join(timeout=2)
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
