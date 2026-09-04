import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tomllib


ROOT = Path(__file__).parents[1].resolve()


class DistributionTests(unittest.TestCase):
    def test_pyproject_exposes_both_entry_points_and_launch_module(self):
        with (ROOT / "pyproject.toml").open("rb") as handle:
            document = tomllib.load(handle)
        project = document["project"]
        self.assertEqual(project["requires-python"], ">=3.12,<3.14")
        self.assertEqual(project["scripts"]["kyrozen"], "main:main")
        self.assertEqual(project["scripts"]["kyrozen-web"], "server:main_entry")
        self.assertIn("workspace_context", document["tool"]["setuptools"]["py-modules"])

    def test_posix_installer_has_valid_syntax_and_idempotent_user_path_logic(self):
        installer = ROOT / "install.sh"
        result = subprocess.run(
            ["sh", "-n", str(installer)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        text = installer.read_text(encoding="utf-8")
        self.assertIn('PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"', text)
        self.assertIn("grep -Fq '$HOME/.local/bin'", text)
        self.assertIn('tool install --python', text)
        self.assertIn("kyrozen_bin\" --version", text)

    def test_powershell_installer_is_static_safe_and_parses_when_available(self):
        installer = ROOT / "install.ps1"
        text = installer.read_text(encoding="utf-8")
        self.assertIn("[Environment]::GetEnvironmentVariable(\"Path\", \"User\")", text)
        self.assertIn("SetEnvironmentVariable(\"Path\"", text)
        self.assertIn("tool install --python", text)
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell is None:
            return
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-File", str(installer), "-Help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cli_and_web_help_version_flags_work_without_provider_setup(self):
        with tempfile.TemporaryDirectory() as home:
            env = os.environ.copy()
            env.update({
                "HOME": home,
                "KYROZEN_DISABLE_VECTOR_INDEX": "1",
            })
            commands = [
                ([sys.executable, str(ROOT / "main.py"), "--help"], "--project"),
                ([sys.executable, str(ROOT / "main.py"), "--version"], "OpenKyrozen"),
                ([sys.executable, str(ROOT / "server.py"), "--help"], "--reload"),
            ]
            for command, marker in commands:
                with self.subTest(command=command):
                    result = subprocess.run(
                        command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(marker, result.stdout)

    def test_web_parser_accepts_project_and_server_flags(self):
        import server

        args = server._server_parser().parse_args([
            "--project", ".", "--host", "0.0.0.0", "--port", "8123", "--reload",
        ])
        self.assertEqual(args.project, ".")
        self.assertFalse(args.global_mode)
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 8123)
        self.assertTrue(args.reload)

    def test_update_uses_the_package_manager_instead_of_git_pull(self):
        import main

        completed = subprocess.CompletedProcess(
            ["uv", "tool", "upgrade", "openkyrozen"], 0, stdout="upgraded", stderr="",
        )
        with patch("main.shutil.which", return_value="/usr/local/bin/uv"), \
             patch("main.subprocess.run", return_value=completed) as run:
            result = main._self_update()
        self.assertIn("upgraded", result)
        run.assert_called_once_with(
            ["uv", "tool", "upgrade", "openkyrozen"],
            capture_output=True, text=True, timeout=60,
        )

    def test_docker_starts_server_in_explicit_project_mode(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn('"server.py", "--project", "/app"', dockerfile)


if __name__ == "__main__":
    unittest.main()
