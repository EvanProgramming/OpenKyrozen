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
        self.assertEqual(project["version"], "2.0.4")
        self.assertEqual(project["requires-python"], ">=3.12,<3.14")
        self.assertEqual(project["scripts"]["kyrozen"], "tui_launcher:main")
        self.assertEqual(project["scripts"]["kyrozen-backend"], "tui_backend:main")
        self.assertEqual(project["scripts"]["kyrozen-web"], "server:main_entry")
        self.assertEqual(project["scripts"]["kyrozen-bootstrap-gh"], "github_cli:main")
        self.assertIn("tui_launcher", document["tool"]["setuptools"]["py-modules"])
        self.assertIn("tui_backend", document["tool"]["setuptools"]["py-modules"])
        self.assertIn("workspace_context", document["tool"]["setuptools"]["py-modules"])
        self.assertIn("learning_worker", document["tool"]["setuptools"]["py-modules"])
        self.assertIn("project_graph", document["tool"]["setuptools"]["py-modules"])
        self.assertIn("github_cli", document["tool"]["setuptools"]["py-modules"])

    def test_full_development_setup_includes_browser_and_core_test_path(self):
        with (ROOT / "pyproject.toml").open("rb") as handle:
            document = tomllib.load(handle)
        self.assertIn("playwright>=1.40", document["project"]["optional-dependencies"]["all"])

        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("install: install-core", makefile)
        self.assertIn("pip install -e '.[web]'", makefile)
        self.assertIn("pip install -e '.[all]'", makefile)
        self.assertIn("python -m playwright install chromium", makefile)
        self.assertIn("learning_worker.py", makefile)
        self.assertIn("test-core:", makefile)
        self.assertIn("MAKEFLAGS += --no-print-directory", makefile)

        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("pip install -e '.[all]'", workflow)
        self.assertIn("make test PYTHON=python", workflow)
        self.assertIn("make install-core PYTHON=python", workflow)
        self.assertIn("make test-core", workflow)
        self.assertIn("actions/setup-go@v5", workflow)
        self.assertIn("go test ./...", workflow)

    def test_one_shot_release_workflow_is_retired_after_v2_release(self):
        self.assertFalse((ROOT / ".github" / "workflows" / "publish.yml").exists())

    def test_tag_release_workflow_validates_source_and_installed_artifacts(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn('tags:', workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("RELEASE_TAG", workflow)
        self.assertIn('test "$tag_version" = "$package_version"', workflow)
        self.assertIn('python -m pip install -e ".[web]"', workflow)
        self.assertIn("tests.test_server.ServerBoundaryTests.test_browser_token_bootstrap", workflow)
        self.assertIn("tests.test_task_consistency.TaskConsistencyTests.test_multi_task_plan", workflow)
        self.assertIn("python scripts/wheel_smoke.py", workflow)
        self.assertIn("openkyrozen-tui-", workflow)
        self.assertIn("sha256sum", workflow)
        self.assertIn("runs-on: windows-latest", workflow)
        smoke = (ROOT / "scripts" / "wheel_smoke.py").read_text(encoding="utf-8")
        self.assertIn("/api/auth/session", smoke)
        self.assertIn("_task_status_counts", smoke)

    def test_public_install_paths_pin_the_verified_release(self):
        release_url = (
            "https://github.com/EvanProgramming/OpenKyrozen/releases/download/"
            "v2.0.4/openkyrozen-2.0.4-py3-none-any.whl"
        )
        for readme in sorted(ROOT.glob("README*.md")):
            text = readme.read_text(encoding="utf-8")
            with self.subTest(readme=readme.name):
                self.assertIn("raw.githubusercontent.com/EvanProgramming/OpenKyrozen/v2.0.4/", text)
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
        self.assertIn("release_version='2.0.4'", text)
        self.assertIn("releases/download", text)
        self.assertIn("--with fastapi --with uvicorn", text)
        self.assertIn("--no-cache", text)
        self.assertNotIn("pypi.org", text)
        self.assertIn("installed_version=", text)
        self.assertIn("go_version='1.27.1'", text)
        self.assertIn("Bubble Tea source asset", text)
        self.assertIn("mv \"$build_output\" \"$state_dir/bin/openkyrozen-tui\"", text)

    def test_installers_use_plain_canonical_banner(self):
        for name in ("install.sh", "install.ps1"):
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(installer=name):
                self.assertIn("OPENKYROZEN", text)
                self.assertIn("computer-native installer", text)
                self.assertNotIn("____", text)

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
        self.assertIn("v2.0.4", text)
        self.assertIn("releases/download", text)
        self.assertIn("--with fastapi --with uvicorn", text)
        self.assertIn("--no-cache", text)
        self.assertIn('$goVersion = "1.27.1"', text)
        self.assertIn("Get-FileHash -Algorithm SHA256", text)
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

    def test_posix_installer_retries_with_uncached_uv_install(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as bin_dir:
            home_path = Path(home)
            bin_path = Path(bin_dir)
            log_path = home_path / "uv.log"
            uv_path = bin_path / "uv"
            uv_path.write_text(
                r"""#!/bin/sh
printf '%s\n' "$*" >> "$UV_TEST_LOG"
if [ "$1" = "python" ] && [ "$2" = "find" ]; then
    exit 0
fi
if [ "$1" = "tool" ] && [ "$2" = "install" ]; then
    exit 1
fi
if [ "$1" = "--no-cache" ] && [ "$2" = "tool" ] && [ "$3" = "install" ]; then
    mkdir -p "$HOME/.local/bin"
    printf '%s\n' '#!/bin/sh' 'if [ "$1" = "--version" ]; then printf "%s\n" "OpenKyrozen 2.0.4"; fi' > "$HOME/.local/bin/kyrozen"
    chmod +x "$HOME/.local/bin/kyrozen"
    exit 0
fi
exit 0
""",
                encoding="utf-8",
            )
            curl_path = bin_path / "curl"
            curl_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(uv_path, 0o755)
            os.chmod(curl_path, 0o755)
            env = os.environ.copy()
            env.update({
                "HOME": str(home_path),
                "PATH": f"{bin_path}:{env.get('PATH', '')}",
                "SHELL": "/bin/sh",
                "UV_TEST_LOG": str(log_path),
            })
            result = subprocess.run(
                ["sh", str(ROOT / "install.sh")],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            install_calls = [
                line for line in log_path.read_text(encoding="utf-8").splitlines()
                if "tool install" in line
            ]
            self.assertEqual(len(install_calls), 2)
            self.assertTrue(install_calls[0].startswith("tool install "))
            self.assertTrue(install_calls[1].startswith("--no-cache tool install "))
            self.assertIn("Installation complete.", result.stdout)

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
        self.assertEqual(server.app.version, "2.0.4")

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
             patch("main._release_tui_asset_available", return_value=True), \
             patch("main._update_tui_binary", return_value=(True, "Bubble Tea UI installed atomically.")), \
             patch("main.GitHubCLI.install_managed", return_value={"success": True, "message": "GitHub CLI installed."}), \
             patch("main.subprocess.run", return_value=completed) as run:
            result = main._self_update()
        self.assertIn("upgraded", result)
        command = run.call_args.args[0]
        expected_python = (
            f"{sys.version_info.major}.{sys.version_info.minor}"
            if sys.version_info[:2] in {(3, 12), (3, 13)} else "3.12"
        )
        self.assertEqual(command[:6], ["/usr/local/bin/uv", "tool", "install", "--python", expected_python, "--force"])
        self.assertIn("--with", command)
        self.assertIn("fastapi", command)
        self.assertIn("uvicorn", command)
        self.assertTrue(command[-1].endswith("/v2.0.4/openkyrozen-2.0.4-py3-none-any.whl"))

    def test_update_retries_without_uv_cache_after_install_failure(self):
        import main

        failed = subprocess.CompletedProcess(
            ["uv", "tool", "install"], 1, stdout="", stderr="missing archive",
        )
        completed = subprocess.CompletedProcess(
            ["uv", "--no-cache", "tool", "install"], 0, stdout="upgraded", stderr="",
        )
        with patch("main.shutil.which", return_value="/usr/local/bin/uv"), \
             patch("main._release_tui_asset_available", return_value=True), \
             patch("main._update_tui_binary", return_value=(True, "Bubble Tea UI installed atomically.")), \
             patch("main.GitHubCLI.install_managed", return_value={"success": True, "message": "GitHub CLI installed."}), \
             patch("main.subprocess.run", side_effect=[failed, completed]) as run:
            result = main._self_update()
        self.assertIn("upgraded", result)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            run.call_args_list[1].args[0][:3],
            ["/usr/local/bin/uv", "--no-cache", "tool"],
        )

    def test_update_preserves_stdout_only_failure_diagnostics(self):
        import main

        failed = subprocess.CompletedProcess(
            ["/usr/local/bin/uv", "tool", "install"], 1,
            stdout="release asset unavailable", stderr="",
        )
        retry_failed = subprocess.CompletedProcess(
            ["/usr/local/bin/uv", "--no-cache", "tool", "install"], 1,
            stdout="retry also failed", stderr="",
        )
        with patch("main.shutil.which", return_value="/usr/local/bin/uv"), \
             patch("main._release_tui_asset_available", return_value=True), \
             patch("main.subprocess.run", side_effect=[failed, retry_failed]):
            result = main._self_update()
        self.assertIn("uv exit 1", result)
        self.assertIn("retry also failed", result)

    def test_update_bootstraps_main_revision_when_release_predates_tui(self):
        import main

        completed = subprocess.CompletedProcess(
            ["uv", "tool", "install"], 0, stdout="installed from source", stderr="",
        )
        revision = "a" * 40
        with patch("main.shutil.which", return_value="/usr/local/bin/uv"), \
             patch("main._release_tui_asset_available", return_value=False), \
             patch("main._resolve_update_revision", return_value=revision), \
             patch("main._update_tui_binary", return_value=(True, "Bubble Tea UI installed atomically.")), \
             patch("main.GitHubCLI.install_managed", return_value={"success": True, "message": "GitHub CLI installed."}), \
             patch("main.subprocess.run", return_value=completed) as run:
            result = main._self_update()
        command = run.call_args.args[0]
        self.assertEqual(command[-1], f"git+{main.UPDATE_REPOSITORY_URL}@{revision}")
        self.assertIn(f"source revision {revision[:12]}", result)

    def test_launcher_promotes_pending_tui_before_launch(self):
        import tui_launcher

        with tempfile.TemporaryDirectory() as home:
            state_bin = Path(home) / ".kyrozen" / "bin"
            state_bin.mkdir(parents=True)
            pending = state_bin / "openkyrozen-tui.next"
            target = state_bin / "openkyrozen-tui"
            pending.write_text("new tui", encoding="utf-8")
            pending.chmod(0o700)
            with patch("tui_launcher.Path.home", return_value=Path(home)), \
                 patch("tui_launcher.shutil.which", return_value=None):
                self.assertEqual(tui_launcher._tui_binary(), str(target))
            self.assertEqual(target.read_text(encoding="utf-8"), "new tui")
            self.assertFalse(pending.exists())

    def test_launcher_restarts_tui_after_successful_update(self):
        import tui_launcher

        results = [
            subprocess.CompletedProcess(["old-tui"], tui_launcher.TUI_RESTART_EXIT_CODE),
            subprocess.CompletedProcess(["new-tui"], 0),
        ]
        with patch("tui_launcher._is_terminal", return_value=True), \
             patch("tui_launcher._tui_binary", side_effect=["old-tui", "new-tui"]), \
             patch("tui_launcher._backend_command", return_value=("backend", None)), \
             patch("tui_launcher.subprocess.run", side_effect=results) as run, \
             patch("tui_launcher._legacy") as legacy, \
             patch("tui_launcher.sys.argv", ["kyrozen"]):
            tui_launcher.main()

        self.assertEqual([call.args[0][0] for call in run.call_args_list], ["old-tui", "new-tui"])
        legacy.assert_not_called()

    def test_docker_starts_server_in_explicit_project_mode(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn('"server.py", "--project", "/app"', dockerfile)


if __name__ == "__main__":
    unittest.main()
