import hashlib
import io
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import github_cli
from github_cli import GitHubCLI


class Runner:
    def __init__(self):
        self.calls = []
        self.authenticated = True

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, "git@ghe.example:team/repo.git\n", "")
        if command[1:3] == ["auth", "status"]:
            code = 0 if self.authenticated else 1
            return subprocess.CompletedProcess(command, code, "", "token=ghp_abcdefghijklmnopqrstuvwxyz123456")
        if command[1:3] == ["auth", "login"]:
            return subprocess.CompletedProcess(command, 1, "", "cancelled")
        return subprocess.CompletedProcess(command, 0, "ok", "")


class GitHubCLITests(unittest.TestCase):
    def test_enterprise_status_redacts_tokens_and_read_allowlist_is_shell_free(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "gh"
            binary.write_text("", encoding="utf-8")
            runner = Runner()
            with patch.dict("os.environ", {"KYROZEN_GH_BINARY": str(binary)}, clear=False):
                client = GitHubCLI(root, root / "state" / "v2", runner=runner)
                self.assertEqual(client.hostname(), "ghe.example")
                status = client.status()
                self.assertTrue(status["authenticated"])
                self.assertNotIn("ghp_", status["message"])
                self.assertEqual(client.run(["pr", "view"], read_only=True), "ok")
                before = len(runner.calls)
                self.assertIn("allowlist", client.run(["pr", "merge"], read_only=True))
                self.assertEqual(len(runner.calls), before)
                self.assertIn("blocked", client.run(["auth", "token"]))
                self.assertIn("allowlist", client.run(["repo", "view", "--web"], read_only=True))
                self.assertIn("credential", client.run(["api", "user", "--token", "never-log-me"]))

    def test_auth_required_and_cancelled_login_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "gh"
            binary.write_text("", encoding="utf-8")
            runner = Runner()
            runner.authenticated = False
            with patch.dict("os.environ", {"KYROZEN_GH_BINARY": str(binary)}, clear=False):
                client = GitHubCLI(root, root / "state" / "v2", runner=runner)
                required = client.run(["repo", "view"], read_only=True)
                self.assertIn("AUTH_REQUIRED", required)
                self.assertIn("gh auth login --hostname ghe.example --web", required)
                self.assertIn("exited with 1", client.login_interactive("ghe.example"))

    def test_managed_install_verifies_checksum_and_extracts_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("gh/bin/gh", b"binary")
            payload = archive.getvalue()
            key = (github_cli._system(), github_cli._machine())
            with patch.dict(github_cli.GH_ARCHIVES, {key: ("gh.zip", hashlib.sha256(payload).hexdigest())}, clear=True), \
                 patch("github_cli.urllib.request.urlopen", return_value=io.BytesIO(payload)):
                client = GitHubCLI(directory, Path(directory) / ".kyrozen" / "v2")
                result = client.install_managed()
            self.assertTrue(result["success"])
            self.assertEqual(client.managed_binary.read_bytes(), b"binary")

            client.managed_binary.unlink()
            with patch.dict(github_cli.GH_ARCHIVES, {key: ("gh.zip", "0" * 64)}, clear=True), \
                 patch("github_cli.urllib.request.urlopen", return_value=io.BytesIO(payload)):
                result = client.install_managed()
            self.assertFalse(result["success"])
            self.assertFalse(client.managed_binary.exists())


if __name__ == "__main__":
    unittest.main()
