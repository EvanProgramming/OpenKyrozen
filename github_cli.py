"""Shell-free GitHub CLI integration with managed, verified binaries."""

from __future__ import annotations

import hashlib
import argparse
import os
import platform
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable


GH_VERSION = "2.101.0"
GH_RELEASE_BASE = f"https://github.com/cli/cli/releases/download/v{GH_VERSION}"
GH_ARCHIVES = {
    ("darwin", "amd64"): (f"gh_{GH_VERSION}_macOS_amd64.zip", "a6fd66c88e2f07d6e4e058173db341d07dd74d58cf8f19ae668293d2bb614ca3"),
    ("darwin", "arm64"): (f"gh_{GH_VERSION}_macOS_arm64.zip", "e4303e39d8f07141c4bad4b99b01079f05029c59b27076e8fbc825c985ecdd8b"),
    ("linux", "amd64"): (f"gh_{GH_VERSION}_linux_amd64.tar.gz", "9bca2d1c16825f109907a23307628a2f0698fbf99662b73a5cf0b020293072b8"),
    ("linux", "arm64"): (f"gh_{GH_VERSION}_linux_arm64.tar.gz", "b57e8063f18862647c9d22727c32e9da1b963f8bf9db648fe123a6975695640f"),
    ("windows", "amd64"): (f"gh_{GH_VERSION}_windows_amd64.zip", "bc6c814367b193cd8e713611d61e36013c0ef843b8f516458fe3eda039192794"),
    ("windows", "arm64"): (f"gh_{GH_VERSION}_windows_arm64.zip", "e6cbb2d4afdad3e70f3d38b8d1ebaa3a0870a897cfc0e4cf569826710b96b4fd"),
}
READ_ONLY = {
    ("auth", "status"), ("repo", "list"), ("repo", "view"),
    ("issue", "list"), ("issue", "status"), ("issue", "view"),
    ("pr", "checks"), ("pr", "diff"), ("pr", "list"), ("pr", "status"), ("pr", "view"),
    ("run", "list"), ("run", "view"), ("workflow", "list"), ("workflow", "view"),
    ("release", "list"), ("release", "view"),
}
SENSITIVE_RE = re.compile(r"(?i)(token|password|secret|api[_-]?key)(\s*[:=]\s*)\S+")
SENSITIVE_FLAGS = {"--token", "--password", "--secret", "--with-token"}
READ_ONLY_BLOCKED_FLAGS = {"--web"}


def _machine() -> str:
    value = platform.machine().lower()
    return "arm64" if value in {"arm64", "aarch64"} else "amd64" if value in {"x86_64", "amd64"} else value


def _system() -> str:
    return "windows" if os.name == "nt" else "darwin" if sys_platform() == "darwin" else "linux"


def sys_platform() -> str:
    import sys
    return sys.platform


class GitHubCLI:
    def __init__(self, workspace: str | os.PathLike[str], state_root: str | os.PathLike[str], *,
                 runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.state_root = Path(state_root).expanduser().resolve()
        self.runner = runner

    @property
    def managed_binary(self) -> Path:
        name = "gh.exe" if os.name == "nt" else "gh"
        return self.state_root.parent / "tools" / "gh" / GH_VERSION / "bin" / name

    def binary(self) -> str | None:
        explicit = os.environ.get("KYROZEN_GH_BINARY", "").strip()
        if explicit and Path(explicit).is_file():
            return explicit
        if self.managed_binary.is_file():
            return str(self.managed_binary)
        installed = self.install_managed()
        if installed.get("success") and self.managed_binary.is_file():
            return str(self.managed_binary)
        return shutil.which("gh")

    @staticmethod
    def auth_command_text(hostname: str) -> str:
        return shlex.join(["gh", "auth", "login", "--hostname", hostname, "--web", "--git-protocol", "https"])

    def hostname(self) -> str:
        try:
            result = self.runner(
                ["git", "-C", str(self.workspace), "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            remote = (result.stdout or "").strip()
        except (OSError, subprocess.TimeoutExpired):
            remote = ""
        match = re.match(r"git@([^:]+):", remote)
        if match:
            return match.group(1)
        match = re.match(r"https?://([^/]+)/", remote)
        return match.group(1) if match else "github.com"

    @staticmethod
    def _redact(text: str) -> str:
        text = SENSITIVE_RE.sub(r"\1\2<redacted>", str(text))
        text = re.sub(r"\b(?:gh[opsu]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]+)\b", "<redacted>", text)
        return text[:32_000]

    def status(self, hostname: str | None = None) -> dict[str, Any]:
        binary = self.binary()
        host = hostname or self.hostname()
        if not binary:
            return {"installed": False, "authenticated": False, "hostname": host,
                    "auth_required": True, "command": self.auth_command_text(host),
                    "message": "GitHub CLI is not installed."}
        try:
            result = self.runner(
                [binary, "auth", "status", "--hostname", host], capture_output=True,
                text=True, timeout=20, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"installed": True, "authenticated": False, "hostname": host, "message": str(exc)[:500]}
        authenticated = result.returncode == 0
        return {"installed": True, "authenticated": authenticated, "hostname": host,
                "auth_required": not authenticated, "command": "" if authenticated else self.auth_command_text(host),
                "message": self._redact((result.stdout or result.stderr or "").strip())}

    @staticmethod
    def parse_args(value: str | list[str]) -> list[str]:
        if isinstance(value, list):
            values = [str(item) for item in value]
        else:
            values = shlex.split(str(value), posix=os.name != "nt")
        if any("\x00" in item or len(item) > 4000 for item in values) or len(values) > 100:
            raise ValueError("invalid GitHub CLI arguments")
        return values

    @staticmethod
    def is_read_only(argv: list[str]) -> bool:
        if any(value in READ_ONLY_BLOCKED_FLAGS for value in argv):
            return False
        values = [value for value in argv if not value.startswith("-")]
        if not values:
            return False
        if values[0] in {"status", "search"}:
            return True
        return len(values) >= 2 and (values[0], values[1]) in READ_ONLY

    def run(self, value: str | list[str], *, read_only: bool = False) -> str:
        binary = self.binary()
        if not binary:
            return "AUTH_REQUIRED: GitHub CLI is not installed. Use /github login to install and authenticate it."
        try:
            argv = self.parse_args(value)
        except ValueError as exc:
            return f"Error: {exc}"
        if not argv:
            return "Error: github_cli requires arguments."
        if (argv[:2] == ["auth", "token"] or "--show-token" in argv
                or any(value.split("=", 1)[0] in SENSITIVE_FLAGS for value in argv)):
            return "Error: credential arguments are blocked; let gh own authentication."
        if argv[:2] == ["auth", "login"]:
            return "AUTH_REQUIRED: Interactive login is available through /github login."
        if read_only and not self.is_read_only(argv):
            return "Error: this GitHub command is not in the read-only allowlist."
        status = self.status()
        if not status["authenticated"] and argv[:2] != ["auth", "status"]:
            return f"AUTH_REQUIRED: {status['command']}"
        try:
            result = self.runner(
                [binary, *argv], cwd=str(self.workspace), capture_output=True, text=True,
                timeout=120, check=False, env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired:
            return "Error: GitHub CLI timed out after 120 seconds."
        except OSError as exc:
            return f"Error: GitHub CLI could not start: {exc}"
        output = self._redact((result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")).strip()
        if result.returncode:
            return f"Error: gh exited with {result.returncode}: {output or '(no output)'}"
        return output or "GitHub CLI completed successfully."

    def login_command(self, hostname: str | None = None) -> list[str] | None:
        binary = self.binary()
        return [binary, "auth", "login", "--hostname", hostname or self.hostname(), "--web", "--git-protocol", "https"] if binary else None

    def login_interactive(self, hostname: str | None = None) -> str:
        command = self.login_command(hostname)
        if not command:
            installed = self.install_managed()
            if not installed["success"]:
                return f"Error: {installed['message']}"
            command = self.login_command(hostname)
        try:
            result = self.runner(command, cwd=str(self.workspace), check=False)
        except OSError as exc:
            return f"Error: GitHub login could not start: {exc}"
        return "GitHub authentication completed." if result.returncode == 0 else f"GitHub authentication exited with {result.returncode}."

    def install_managed(self) -> dict[str, Any]:
        key = (_system(), _machine())
        asset = GH_ARCHIVES.get(key)
        if asset is None:
            return {"success": False, "message": f"Unsupported GitHub CLI platform: {key[0]}/{key[1]}"}
        name, expected = asset
        target = self.managed_binary
        if target.is_file():
            return {"success": True, "message": f"GitHub CLI {GH_VERSION} is installed.", "path": str(target)}
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix=".gh-install-", dir=target.parent.parent) as temporary:
                temporary_path = Path(temporary)
                archive = temporary_path / name
                with urllib.request.urlopen(f"{GH_RELEASE_BASE}/{name}", timeout=60) as response, archive.open("wb") as output:
                    shutil.copyfileobj(response, output)
                actual = hashlib.sha256(archive.read_bytes()).hexdigest()
                if actual != expected:
                    return {"success": False, "message": "GitHub CLI checksum verification failed."}
                extract = temporary_path / "extract"
                extract.mkdir()
                if name.endswith(".zip"):
                    with zipfile.ZipFile(archive) as bundle:
                        for member in bundle.infolist():
                            destination = (extract / member.filename).resolve()
                            destination.relative_to(extract.resolve())
                        bundle.extractall(extract)
                else:
                    with tarfile.open(archive, "r:gz") as bundle:
                        for member in bundle.getmembers():
                            destination = (extract / member.name).resolve()
                            destination.relative_to(extract.resolve())
                        bundle.extractall(extract, filter="data")
                found = next((path for path in extract.rglob(target.name) if path.is_file()), None)
                if found is None:
                    return {"success": False, "message": "GitHub CLI archive did not contain the expected executable."}
                staged = target.with_suffix(target.suffix + ".new")
                shutil.copy2(found, staged)
                staged.chmod(0o700)
                os.replace(staged, target)
        except (OSError, ValueError, urllib.error.URLError, zipfile.BadZipFile, tarfile.TarError) as exc:
            return {"success": False, "message": f"GitHub CLI installation failed: {exc}"}
        return {"success": True, "message": f"GitHub CLI {GH_VERSION} installed.", "path": str(target)}


def main() -> None:
    parser = argparse.ArgumentParser(description=f"Install OpenKyrozen's verified GitHub CLI {GH_VERSION}")
    parser.parse_args()
    state = Path.home() / ".kyrozen" / "v2"
    result = GitHubCLI(Path.cwd(), state).install_managed()
    print(result["message"])
    raise SystemExit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()
