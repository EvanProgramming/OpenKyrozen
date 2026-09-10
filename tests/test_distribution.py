import os
import inspect
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import tomllib


ROOT = Path(__file__).parents[1].resolve()


class DistributionTests(unittest.TestCase):
    def test_pyproject_exposes_both_entry_points_and_launch_module(self):
        with (ROOT / "pyproject.toml").open("rb") as handle:
            document = tomllib.load(handle)
        project = document["project"]
        self.assertEqual(project["version"], "2.0.2")
        self.assertEqual(project["requires-python"], ">=3.12,<3.14")
        self.assertEqual(project["scripts"]["kyrozen"], "main:main")
        self.assertEqual(project["scripts"]["kyrozen-web"], "server:main_entry")
        self.assertIn("workspace_context", document["tool"]["setuptools"]["py-modules"])

    def test_full_development_setup_includes_browser_and_core_test_path(self):
        with (ROOT / "pyproject.toml").open("rb") as handle:
            document = tomllib.load(handle)
        self.assertIn("playwright>=1.40", document["project"]["optional-dependencies"]["all"])

        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("install: install-core", makefile)
        self.assertIn("pip install -e '.[web]'", makefile)
        self.assertIn("pip install -e '.[all]'", makefile)
        self.assertIn("python -m playwright install chromium", makefile)
        self.assertIn("test-core:", makefile)
        self.assertIn("MAKEFLAGS += --no-print-directory", makefile)

        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("pip install -e '.[all]'", workflow)
        self.assertIn("make test PYTHON=python", workflow)
        self.assertIn("make install-core PYTHON=python", workflow)
        self.assertIn("make test-core", workflow)

    def test_one_shot_release_workflow_is_retired_after_v2_release(self):
        self.assertFalse((ROOT / ".github" / "workflows" / "publish.yml").exists())

    def test_tag_release_workflow_validates_source_and_installed_artifacts(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn('tags:', workflow)
        self.assertIn('test "$tag_version" = "$package_version"', workflow)
        self.assertIn("tests.test_server.ServerBoundaryTests.test_browser_token_bootstrap", workflow)
        self.assertIn("tests.test_task_consistency.TaskConsistencyTests.test_multi_task_plan", workflow)
        self.assertIn("python scripts/wheel_smoke.py", workflow)
        self.assertIn("runs-on: windows-latest", workflow)
        smoke = (ROOT / "scripts" / "wheel_smoke.py").read_text(encoding="utf-8")
        self.assertIn("/api/auth/session", smoke)
        self.assertIn("_task_status_counts", smoke)

    def test_public_install_paths_pin_the_verified_release(self):
        release_url = (
            "https://github.com/EvanProgramming/OpenKyrozen/releases/download/"
            "v2.0.2/openkyrozen-2.0.2-py3-none-any.whl"
        )
        for readme in sorted(ROOT.glob("README*.md")):
            text = readme.read_text(encoding="utf-8")
            with self.subTest(readme=readme.name):
                self.assertIn("raw.githubusercontent.com/EvanProgramming/OpenKyrozen/v2.0.2/", text)
                self.assertIn(release_url, text)
                self.assertNotIn("pypi.org", text.lower())
                self.assertNotIn("uv tool upgrade openkyrozen", text)

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
        self.assertIn("release_version='2.0.2'", text)
        self.assertIn("releases/download", text)
        self.assertIn("--with fastapi --with uvicorn", text)
        self.assertNotIn("pypi.org", text)
        self.assertIn("installed_version=", text)

    def test_powershell_installer_is_static_safe_and_parses_when_available(self):
        installer = ROOT / "install.ps1"
        text = installer.read_text(encoding="utf-8")
        self.assertIn('[Alias("Help")]', text)
        self.assertIn("$ShowHelp", text)
        self.assertIn("$homeDirectory = if ($env:HOME)", text)
        self.assertNotIn('Join-Path $HOME ".kyrozen"', text)
        self.assertIn("$env:UV_INSTALL_DIR = $localBin", text)
        self.assertIn("[Environment]::GetEnvironmentVariable(\"Path\", \"User\")", text)
        self.assertIn("SetEnvironmentVariable(\"Path\"", text)
        self.assertIn("tool install --python", text)
        self.assertIn("v2.0.2", text)
        self.assertIn("releases/download", text)
        self.assertIn("--with fastapi --with uvicorn", text)
        self.assertNotIn("pypi.org", text)
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

        import server
        self.assertEqual(server.app.version, "2.0.2")

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

    def test_custom_web_launch_does_not_claim_a_fixed_endpoint(self):
        import server

        self.assertNotIn("http://127.0.0.1:8000", inspect.getsource(server.startup))
        with patch.object(server, "_parse_server_args", return_value=(
                SimpleNamespace(host="127.0.0.1", port=8876, reload=False), None)), \
             patch.object(server.uvicorn, "run") as run:
            server.main_entry()
        run.assert_called_once_with(server.app, host="127.0.0.1", port=8876, reload=False)

    def test_update_uses_the_package_manager_instead_of_git_pull(self):
        import main

        completed = subprocess.CompletedProcess(
            ["uv", "tool", "install"], 0, stdout="upgraded", stderr="",
        )
        with patch("main.shutil.which", return_value="/usr/local/bin/uv"), \
             patch("main.subprocess.run", return_value=completed) as run:
            result = main._self_update()
        self.assertIn("upgraded", result)
        command = run.call_args.args[0]
        expected_python = (
            f"{sys.version_info.major}.{sys.version_info.minor}"
            if sys.version_info[:2] in {(3, 12), (3, 13)} else "3.12"
        )
        self.assertEqual(command[:6], ["uv", "tool", "install", "--python", expected_python, "--force"])
        self.assertIn("--with", command)
        self.assertIn("fastapi", command)
        self.assertIn("uvicorn", command)
        self.assertTrue(command[-1].endswith("/v2.0.2/openkyrozen-2.0.2-py3-none-any.whl"))

    def test_docker_starts_server_in_explicit_project_mode(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn('"server.py", "--project", "/app"', dockerfile)


if __name__ == "__main__":
    unittest.main()
