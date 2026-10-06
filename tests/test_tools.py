import json
import hashlib
import re
import tempfile
import subprocess
import unittest
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from pathlib import Path

from openkyrozen.tools import ToolAdapters
tools = ToolAdapters()
AVAILABLE_TOOLS = tools.AVAILABLE_TOOLS
allowed_tool_names = tools.allowed_tool_names
git_status = tools.git_status
git_remote = tools.git_remote
git_branch = tools.git_branch
read_file = tools.read_file
read_webpage = tools.read_webpage
run_cmd = tools.run_cmd
set_workspace_root = tools.set_workspace_root
write_file = tools.write_file

class WorkspaceToolTests(unittest.TestCase):
    def test_structured_read_returns_hash_and_truncation_metadata(self):
        original = tools._WORKSPACE_ROOT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "module.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
            tools.set_workspace_root(root)
            try:
                result = json.loads(tools.read_file(json.dumps({"path": "module.py"})))
                excerpt = json.loads(tools.read_file(json.dumps(
                    {"path": "module.py", "start_line": 2, "max_lines": 1},
                )))
            finally:
                tools.set_workspace_root(original)
        self.assertEqual(result["content"], "one\ntwo\nthree\n")
        self.assertEqual(result["start_line"], 1)
        self.assertFalse(result["truncated"])
        self.assertRegex(result["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual((excerpt["content"], excerpt["start_line"], excerpt["end_line"]), ("two\n", 2, 2))
        self.assertTrue(excerpt["truncated"])

    def test_structured_read_streams_large_file_and_preserves_full_hash(self):
        original = tools._WORKSPACE_ROOT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "large.txt"
            data = ("x" * 100_000 + "\nselected\n") * 100
            target.write_text(data, encoding="utf-8")
            tools.set_workspace_root(root)
            try:
                with patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read")):
                    result = json.loads(tools.read_file(json.dumps(
                        {"path": "large.txt", "start_line": 2, "max_lines": 1}
                    )))
            finally:
                tools.set_workspace_root(original)
        self.assertEqual(result["content"], "selected\n")
        self.assertEqual(result["sha256"], hashlib.sha256(data.encode()).hexdigest())
        self.assertEqual(result["total_lines"], 200)

    def test_structured_read_keeps_prefix_of_line_spanning_chunks(self):
        original = tools._WORKSPACE_ROOT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "long.txt").write_text("z" * 200_000 + "\n", encoding="utf-8")
            tools.set_workspace_root(root)
            try:
                result = json.loads(tools.read_file(json.dumps(
                    {"path": "long.txt", "max_chars": 100}
                )))
            finally:
                tools.set_workspace_root(original)
        self.assertEqual(result["content"], "z" * 100)
        self.assertTrue(result["truncated"])

    def test_edit_file_requires_matching_hash_and_unique_old_text(self):
        original = tools._WORKSPACE_ROOT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "module.py"
            target.write_text("value = 1\n", encoding="utf-8")
            tools.set_workspace_root(root)
            try:
                digest = __import__("hashlib").sha256(target.read_bytes()).hexdigest()
                args = {"path": "module.py", "old_text": "value = 1", "new_text": "value = 2",
                        "expected_sha256": digest}
                self.assertIn("Updated", tools.edit_file(json.dumps(args)))
                args["expected_sha256"] = "0" * 64
                self.assertIn("stale", tools.edit_file(json.dumps(args)).lower())
                self.assertEqual(target.read_text(encoding="utf-8"), "value = 2\n")
            finally:
                tools.set_workspace_root(original)

    def test_search_files_is_literal_and_returns_relative_line_numbers(self):
        original = tools._WORKSPACE_ROOT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "module.py").write_text("if a == b:\n    return 1\n", encoding="utf-8")
            tools.set_workspace_root(root)
            try:
                result = tools.search_files(json.dumps({"query": "a == b", "glob": "*.py"}))
            finally:
                tools.set_workspace_root(original)
        self.assertIn("module.py:1:", result)
        self.assertIn("a == b", result)

    def test_search_ripgrep_includes_hidden_files_and_skips_runtime_directories(self):
        import shutil
        rg = shutil.which("rg")
        if not rg:
            self.skipTest("ripgrep is not installed")
        original = tools._WORKSPACE_ROOT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in (
                ".config/needed.txt", ".git/needed.txt", "venv/needed.txt",
                ".venv/needed.txt", "node_modules/needed.txt", "__pycache__/needed.txt",
            ):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("needle\n", encoding="utf-8")
            tools.set_workspace_root(root)
            try:
                results = []
                for rg_path in (rg, None):
                    with patch("openkyrozen.tools.filesystem.shutil.which", return_value=rg_path):
                        results.append(tools.search_files(json.dumps({"query": "needle", "glob": "*.txt"})))
            finally:
                tools.set_workspace_root(original)
        for result in results:
            self.assertIn(".config/needed.txt:1:needle", result)
            for directory in (".git", "venv", ".venv", "node_modules", "__pycache__"):
                self.assertNotIn(f"{directory}/needed.txt", result)

    def test_search_ripgrep_scopes_exclusions_to_requested_directory(self):
        import shutil
        rg = shutil.which("rg")
        if not rg:
            self.skipTest("ripgrep is not installed")
        original = tools._WORKSPACE_ROOT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("docs/.config/needed.txt", "docs/.venv/needed.txt"):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("needle\n", encoding="utf-8")
            tools.set_workspace_root(root)
            try:
                with patch("openkyrozen.tools.filesystem.shutil.which", return_value=rg):
                    result = tools.search_files(json.dumps(
                        {"query": "needle", "path": "docs", "glob": "*.txt"}
                    ))
            finally:
                tools.set_workspace_root(original)
        self.assertIn("docs/.config/needed.txt:1:needle", result)
        self.assertNotIn("docs/.venv/needed.txt", result)

    def test_search_ripgrep_includes_filename_for_explicit_file(self):
        import shutil
        rg = shutil.which("rg")
        if not rg:
            self.skipTest("ripgrep is not installed")
        original = tools._WORKSPACE_ROOT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "module.py").write_text("needle\n", encoding="utf-8")
            tools.set_workspace_root(root)
            try:
                with patch("openkyrozen.tools.filesystem.shutil.which", return_value=rg):
                    result = tools.search_files(json.dumps({"query": "needle", "path": "module.py"}))
            finally:
                tools.set_workspace_root(original)
        self.assertTrue(result.startswith("module.py:1:"), result)

    def test_search_fallback_skips_symlinks_outside_workspace(self):
        original = tools._WORKSPACE_ROOT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            outside = Path(directory) / "outside.txt"
            outside.write_text("secret-marker", encoding="utf-8")
            (root / "linked.txt").symlink_to(outside)
            tools.set_workspace_root(root)
            try:
                with patch("openkyrozen.tools.filesystem.shutil.which", return_value=None):
                    result = tools.search_files(json.dumps({"query": "secret-marker"}))
            finally:
                tools.set_workspace_root(original)
        self.assertEqual(result, "No matches.")

    def test_search_reports_timeout_when_ripgrep_is_killed_without_output(self):
        original = tools._WORKSPACE_ROOT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_rg = root / "fake-rg"
            fake_rg.write_text("#!/bin/sh\nexec /bin/sleep 30\n", encoding="utf-8")
            fake_rg.chmod(0o755)
            tools.set_workspace_root(root)

            class ImmediateTimer:
                def __init__(self, _delay, function):
                    self.function = function

                def start(self):
                    self.function()

                def cancel(self):
                    pass

            try:
                with patch("openkyrozen.tools.filesystem.shutil.which", return_value=str(fake_rg)), \
                        patch("openkyrozen.tools.filesystem.threading.Timer", ImmediateTimer):
                    result = tools.search_files(json.dumps({"query": "absent"}))
            finally:
                tools.set_workspace_root(original)
        self.assertEqual(result, "Search timed out.")

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

    def test_documented_file_examples_work_inside_a_temporary_workspace(self):
        examples_path = Path(__file__).parents[1] / "prompts" / "examples.md"
        blocks = re.findall(r"```json\n(.*?)\n```", examples_path.read_text(encoding="utf-8"), re.DOTALL)
        actions = [json.loads(block) for block in blocks]
        write_action = next(item for item in actions if item.get("action") == "write_file")
        read_action = next(item for item in actions if item.get("action") == "read_file")
        write_path, write_content = write_action["args"].split("|", 1)
        self.assertFalse(Path(write_path).is_absolute())
        self.assertFalse(write_path.startswith("~"))
        self.assertEqual(read_action["args"], write_path)

        original_root = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            set_workspace_root(root)
            try:
                self.assertIn("Wrote", write_file(write_action["args"]))
                self.assertEqual(read_file(read_action["args"]), write_content)
                self.assertEqual((root / write_path).read_text(encoding="utf-8"), write_content)
            finally:
                set_workspace_root(original_root)

    def test_capability_profiles_keep_rich_access_without_irreversible_reset(self):
        workspace = allowed_tool_names(AVAILABLE_TOOLS, "workspace")
        full = allowed_tool_names(AVAILABLE_TOOLS, "full")
        readonly = allowed_tool_names(AVAILABLE_TOOLS, "readonly")
        self.assertIn("run_cmd", workspace)
        self.assertIn("write_file", workspace)
        self.assertIn("edit_file", workspace)
        self.assertIn("search_files", readonly)
        self.assertNotIn("git_reset", workspace)
        self.assertIn("git_reset", full)
        self.assertIn("graph_query", readonly)
        self.assertIn("graph_refresh", readonly)
        self.assertIn("github_read", readonly)
        self.assertNotIn("github_cli", readonly)
        self.assertNotIn("browser_click", readonly)

    def test_run_cmd_resolves_bare_python_from_active_environment(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False):
            result = run_cmd("python -c 'import sys; print(sys.executable)' ")
        self.assertEqual(Path(result).resolve(), Path(sys.executable).resolve())

    def test_shell_and_git_commands_use_the_active_workspace_from_any_caller_directory(self):
        original_root = tools._WORKSPACE_ROOT
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            set_workspace_root(root)
            try:
                self.assertEqual(Path(run_cmd("pwd")).resolve(), root)
                self.assertIn("On branch", git_status(""))
            finally:
                set_workspace_root(original_root)

    def test_run_cmd_preserves_explicit_interpreter_paths(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False):
            result = run_cmd(f"{sys.executable} -c 'print(\"explicit\")'")
        self.assertEqual(result, "explicit")

    def test_git_remote_add_list_and_remove_use_real_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            initialized = subprocess.run(
                ["git", "init", "--quiet", str(repository)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            remote_url = "https://example.com/org/repo.git?token=x&format=json"
            original_root = tools._WORKSPACE_ROOT
            set_workspace_root(repository)
            try:
                added = git_remote(f"add origin {remote_url}")
                listed = git_remote("")
                removed = git_remote("remove origin")
                after_remove = git_remote("")
            finally:
                set_workspace_root(original_root)
            self.assertEqual(added, "Remote 'origin' added.")
            self.assertIn(f"origin\t{remote_url} (fetch)", listed)
            self.assertIn(f"origin\t{remote_url} (push)", listed)
            self.assertEqual(removed, "Remote 'origin' removed.")
            self.assertEqual(after_remove, "(no remotes configured)")

    def test_git_remote_rejects_malformed_arguments_deterministically(self):
        self.assertEqual(
            git_remote("add origin"),
            "Error: git_remote add requires exactly: add <name> <url>.",
        )
        self.assertEqual(
            git_remote("remove origin extra"),
            "Error: git_remote remove requires exactly: remove <name>.",
        )
        self.assertEqual(
            git_remote("add origin 'unterminated"),
            "Error: git_remote arguments contain invalid shell quoting.",
        )

    def test_git_branch_mutations_report_success_and_change_repository_state(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "OpenKyrozen Test"], cwd=repository, check=True)
            (repository / "README.md").write_text("branch test\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "initial"], cwd=repository, check=True)
            original_root = tools._WORKSPACE_ROOT
            set_workspace_root(repository)
            try:
                created = git_branch("audit-branch")
                listed = git_branch("")
                deleted = git_branch("-d audit-branch")
                after_delete = git_branch("")
            finally:
                set_workspace_root(original_root)
            self.assertIn("audit-branch", created)
            self.assertIn("audit-branch", listed)
            self.assertIn("Deleted branch", deleted)
            self.assertNotIn("audit-branch", after_delete)

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
        with patch("openkyrozen.tools.web.socket.getaddrinfo", return_value=[(
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
