from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path


def _update_url_available(self, url: str) -> bool:
    try:
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "OpenKyrozen updater"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(getattr(response, "status", 200)) < 400
    except (OSError, urllib.error.URLError):
        return False


def _release_tui_asset_available(self) -> bool:
    return self._update_url_available(self.TUI_SOURCE_URL) and self._update_url_available(self.TUI_CHECKSUM_URL)


def _resolve_update_revision(self, ref: str = "refs/heads/main", timeout: int = 30) -> str | None:
    try:
        result = subprocess.run(
            ["git", "ls-remote", self.UPDATE_REPOSITORY_URL, ref],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        fields = line.split()
        if fields and re.fullmatch(r"[0-9a-f]{40}", fields[0]):
            return fields[0]
    return None


def _available_update(self) -> str | None:
    """Check the same immutable main source used by /update, without installing."""
    try:
        revision = self._resolve_update_revision(timeout=5)
        if not revision:
            return None
        request = urllib.request.Request(
            f"https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/{revision}/pyproject.toml",
            headers={"User-Agent": "OpenKyrozen updater"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            latest = tomllib.loads(response.read(128 * 1024).decode("utf-8"))["project"]["version"]
        current = self.__version__
        if not all(re.fullmatch(r"\d+\.\d+\.\d+", value) for value in (latest, current)):
            return None
        latest_parts, current_parts = (tuple(map(int, value.split("."))) for value in (latest, current))
        if latest_parts > current_parts:
            return latest
        if latest_parts < current_parts:
            return None
        source_root = Path(__file__).resolve().parents[2]
        if (source_root / ".git").exists():
            # A checkout can already contain main plus unpublished local commits.
            contained = subprocess.run(
                ["git", "-C", str(source_root), "merge-base", "--is-ancestor", revision, "HEAD"],
                capture_output=True, timeout=5, check=False,
            )
            if contained.returncode == 0:
                return None
            installed = subprocess.run(
                ["git", "-C", str(source_root), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5, check=False,
            ).stdout.strip()
        else:
            direct_url = importlib.metadata.distribution("openkyrozen").read_text("direct_url.json")
            installed = json.loads(direct_url or "{}").get("vcs_info", {}).get("commit_id")
            if not installed:
                installed = self._resolve_update_revision(f"refs/tags/v{current}^{{}}", timeout=5)
                installed = installed or self._resolve_update_revision(f"refs/tags/v{current}", timeout=5)
        if installed and installed != revision:
            return f"{latest} · {revision[:7]}"
    except (OSError, ValueError, KeyError, TypeError, importlib.metadata.PackageNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _download_update_file(self, url: str, destination: Path, timeout: int = 180) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "OpenKyrozen updater"})
    with urllib.request.urlopen(request, timeout=timeout) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def _update_sha256(self, path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _update_platform(self) -> tuple[str, str] | None:
    system = "windows" if self._IS_WINDOWS else "darwin" if self._IS_MACOS else "linux" if self._IS_LINUX else ""
    machine = platform.machine().lower()
    arch = "amd64" if machine in {"x86_64", "amd64"} else "arm64" if machine in {"arm64", "aarch64"} else ""
    return (system, arch) if (system, arch) in self.GO_SHA256 else None


def _update_go_version(self, candidate: str) -> tuple[int, int, int] | None:
    try:
        result = subprocess.run(
            [candidate, "version"], capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"\bgo(\d+)\.(\d+)(?:\.(\d+))?", result.stdout or "")
    if result.returncode or not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def _compatible_update_go(self, candidate: str) -> bool:
    version = self._update_go_version(candidate)
    return bool(version and version >= (1, 25, 8))


def _ensure_update_go(self, state_dir: Path) -> str | None:
    system_arch = self._update_platform()
    if system_arch is None:
        return None
    candidate = shutil.which("go")
    if candidate and self._compatible_update_go(candidate):
        return candidate

    local_name = "go.exe" if self._IS_WINDOWS else "go"
    toolchain = state_dir / "toolchains" / "go" / self.GO_VERSION
    local_go = toolchain / "bin" / local_name
    if local_go.is_file() and self._compatible_update_go(str(local_go)):
        return str(local_go)
    if toolchain.exists():
        return None

    system, arch = system_arch
    archive_suffix = "zip" if system == "windows" else "tar.gz"
    archive_url = f"https://go.dev/dl/go{self.GO_VERSION}.{system}-{arch}.{archive_suffix}"
    expected = self.GO_SHA256[system_arch]
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".go-update-", dir=state_dir) as temporary:
            temporary_path = Path(temporary)
            archive = temporary_path / f"go.{archive_suffix}"
            self._download_update_file(archive_url, archive)
            if self._update_sha256(archive) != expected:
                return None
            extract_dir = temporary_path / "extract"
            extract_dir.mkdir()
            if system == "windows":
                with zipfile.ZipFile(archive) as source:
                    source.extractall(extract_dir)
            else:
                with tarfile.open(archive, "r:gz") as source:
                    source.extractall(extract_dir, filter="data")
            extracted = extract_dir / "go"
            if not (extracted / "bin" / local_name).is_file():
                return None
            toolchain.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(extracted), str(toolchain))
    except (OSError, tarfile.TarError, zipfile.BadZipFile, urllib.error.URLError):
        return None
    return str(local_go) if local_go.is_file() and self._compatible_update_go(str(local_go)) else None


def _prepare_tui_source(self, temporary: Path, source_url: str, checksum_url: str | None) -> Path:
    archive = temporary / "tui-source.tar.gz"
    self._download_update_file(source_url, archive)
    if checksum_url:
        checksum_file = temporary / "tui-source.sha256"
        self._download_update_file(checksum_url, checksum_file)
        expected = checksum_file.read_text(encoding="utf-8").split()[0].lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or self._update_sha256(archive) != expected:
            raise ValueError("TUI source checksum verification failed")
    source_root = temporary / "source"
    source_root.mkdir()
    with tarfile.open(archive, "r:gz") as source:
        source.extractall(source_root, filter="data")
    candidates = [path.parent for path in source_root.rglob("go.mod") if (path.parent / "main.go").is_file()]
    if not candidates:
        raise ValueError("TUI source archive does not contain a Go module")
    return candidates[0]


def _update_tui_binary(self, source_url: str, checksum_url: str | None) -> tuple[bool, str]:
    state_dir = Path.home() / ".kyrozen"
    target_name = "openkyrozen-tui.exe" if self._IS_WINDOWS else "openkyrozen-tui"
    target = state_dir / "bin" / target_name
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        go_bin = self._ensure_update_go(state_dir)
        if not go_bin:
            return False, "A compatible Go toolchain was unavailable; the existing TUI binary was kept."
        with tempfile.TemporaryDirectory(prefix=".tui-update-", dir=state_dir) as temporary:
            temporary_path = Path(temporary)
            source_dir = self._prepare_tui_source(temporary_path, source_url, checksum_url)
            output = temporary_path / f"{target_name}.new"
            result = subprocess.run(
                [go_bin, "build", "-trimpath", "-ldflags", f"-s -w -X main.version={self.RELEASE_VERSION}", "-o", str(output), "."],
                cwd=source_dir, capture_output=True, text=True, timeout=300, check=False,
            )
            if result.returncode or not output.is_file():
                detail = self._fix_safe_text(result.stderr or result.stdout or "no build diagnostics", 400)
                return False, f"Bubble Tea build failed; the existing TUI binary was kept: {detail}"
            target.parent.mkdir(parents=True, exist_ok=True)
            if self._IS_WINDOWS and target.exists():
                os.replace(output, target.with_name(target.name + ".next"))
                return True, "Bubble Tea UI staged; quit and relaunch kyrozen to activate it."
            os.replace(output, target)
            try:
                target.chmod(0o700)
            except OSError:
                pass
    except (OSError, ValueError, tarfile.TarError, urllib.error.URLError):
        return False, "Bubble Tea source download or verification failed; the existing TUI binary was kept."
    return True, "Bubble Tea UI installed atomically."


def _self_update(self) -> str:
    """Upgrade the package and matching TUI without touching the active project."""
    uv_path = shutil.which("uv")
    if uv_path is None:
        return (
            "OpenKyrozen is package-managed. Install or update it with the official "
            f"{self.RELEASE_TAG} installer (uv is required)."
        )
    requested_python = (
        f"{sys.version_info.major}.{sys.version_info.minor}"
        if sys.version_info[:2] in {(3, 12), (3, 13)} else "3.12"
    )
    # Prefer the immutable current main commit so /update delivers fixes that
    # landed after the last tagged release. Fall back to the verified release
    # asset when the repository revision cannot be resolved.
    revision = self._resolve_update_revision()
    release_tui = revision is None and self._release_tui_asset_available()
    if revision is None and not release_tui:
        return "No verified update source was available; the existing installation was kept."
    package_spec = self.RELEASE_WHEEL_URL
    source_url, checksum_url = self.TUI_SOURCE_URL, self.TUI_CHECKSUM_URL
    if revision:
        package_spec = f"git+{self.UPDATE_REPOSITORY_URL}@{revision}"
        source_url, checksum_url = f"https://github.com/EvanProgramming/OpenKyrozen/archive/{revision}.tar.gz", None
    command = [
        uv_path, "tool", "install", "--python", requested_python, "--force",
        "--with", "fastapi", "--with", "uvicorn", package_spec,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
        if result.returncode != 0:
            result = subprocess.run(
                [uv_path, "--no-cache", *command[1:]],
                capture_output=True, text=True, timeout=300, check=False,
            )
        diagnostics = self._fix_safe_text(
            "\n".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip()), 1200,
        )
        if result.returncode != 0:
            return f"Update failed (uv exit {result.returncode}):\n{diagnostics or 'uv returned no diagnostics.'}"
        tui_ok, tui_message = self._update_tui_binary(source_url, checksum_url)
        gh_result = (self._github_cli or self.GitHubCLI(self._get_workspace_root(), self._state_root())).install_managed()
        gh_message = str(gh_result.get("message", "GitHub CLI setup skipped."))
        if revision:
            origin = f"source revision {revision[:12]}"
        else:
            origin = f"GitHub release {self.RELEASE_TAG}"
        return (
            f"Updated OpenKyrozen from {origin}:\n"
            f"{diagnostics or 'uv completed successfully.'}\n"
            f"{tui_message}\n"
            f"{gh_message}\n"
            "Restart kyrozen to use the updated process."
        )
    except subprocess.TimeoutExpired:
        return "Update timed out; the existing installation was kept."
    except FileNotFoundError:
        return "Error: uv or the update toolchain is not installed; the existing installation was kept."
    except Exception as exc:
        return f"Error during update: {self._fix_safe_text(exc, 400)}"


def _outdated_packages(self) -> list[str]:
    result = subprocess.run([sys.executable, "-m", "pip", "list", "--outdated", "--format=columns"],
                            capture_output=True, text=True, timeout=30)
    return result.stdout.strip().split("\n")[2:5] if result.returncode == 0 else []
