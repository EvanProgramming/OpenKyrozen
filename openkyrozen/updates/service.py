from __future__ import annotations

from contextlib import contextmanager
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
from .models import UpdateResult


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


@contextmanager
def _update_lock(self):
    state_dir = Path.home() / ".kyrozen"
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "update.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _stage_update_tui(self, temporary: Path, source_url: str, checksum_url: str | None,
                      revision: str | None) -> Path:
    go_bin = self._ensure_update_go(Path.home() / ".kyrozen")
    if not go_bin:
        raise RuntimeError("A compatible Go toolchain is unavailable; rerun the official installer.")
    source_dir = self._prepare_tui_source(temporary, source_url, checksum_url)
    identity = revision or self.RELEASE_TAG
    name = "openkyrozen-tui.exe" if self._IS_WINDOWS else "openkyrozen-tui"
    output = temporary / name
    result = subprocess.run(
        [go_bin, "build", "-trimpath", "-ldflags", f"-s -w -X main.revision={identity}",
         "-o", str(output), "."], cwd=source_dir, capture_output=True, text=True,
        timeout=300, check=False,
    )
    if result.returncode or not output.is_file():
        raise RuntimeError("TUI build failed: " + self._fix_safe_text(result.stderr or result.stdout, 400))
    probe = subprocess.run([str(output), "--revision"], capture_output=True, text=True,
                           timeout=15, check=False)
    if probe.returncode or probe.stdout.strip() != identity:
        # Older verified release assets have no revision flag. Their checksum
        # and exact release-version probe establish the fallback identity.
        if revision is None and checksum_url:
            legacy = subprocess.run([str(output), "--version"], capture_output=True,
                                    text=True, timeout=15, check=False)
            if legacy.returncode == 0 and legacy.stdout.strip() == f"OpenKyrozen {self.RELEASE_VERSION}":
                return output
        raise RuntimeError("The prepared TUI failed revision verification.")
    return output


def _activate_update_tui(self, staged: Path, revision: str | None) -> str:
    state_bin = Path.home() / ".kyrozen" / "bin"
    state_bin.mkdir(parents=True, exist_ok=True)
    target = state_bin / ("openkyrozen-tui.exe" if self._IS_WINDOWS else "openkyrozen-tui")
    destination = target.with_name(target.name + ".next") if self._IS_WINDOWS else target
    staged.chmod(0o700)
    os.replace(staged, destination)
    return "TUI staged for activation on relaunch." if self._IS_WINDOWS else "TUI installed atomically."


def _verify_update_package(self, uv_path: str, revision: str | None) -> bool:
    directory = subprocess.run([uv_path, "tool", "dir"], capture_output=True, text=True,
                               timeout=30, check=False)
    if directory.returncode:
        return False
    root = Path(directory.stdout.strip()) / "openkyrozen"
    python = root / ("Scripts/python.exe" if self._IS_WINDOWS else "bin/python")
    # Probe a fresh interpreter outside the source checkout, including the
    # entrypoint imports that must work before the running process is restarted.
    script = """import importlib.metadata as m, json, openkyrozen
from openkyrozen.interfaces.cli import launcher
from openkyrozen.interfaces.tui import backend
from openkyrozen.interfaces.web import app
d=m.distribution('openkyrozen')
print(json.dumps({'version':d.version,'source':json.loads(d.read_text('direct_url.json') or '{}')}))
"""
    result = subprocess.run([str(python), "-c", script], cwd=root, capture_output=True,
                            text=True, timeout=60, check=False)
    if result.returncode:
        return False
    data = json.loads(result.stdout.strip().splitlines()[-1])
    if revision:
        return data["source"].get("vcs_info", {}).get("commit_id") == revision
    return (data["version"] == self.RELEASE_VERSION and
            data["source"].get("url") == self.RELEASE_WHEEL_URL)


def _update_tui_binary(self, source_url: str, checksum_url: str | None) -> tuple[bool, str]:
    """Compatibility helper for installation callers; transactions stage separately."""
    try:
        state_dir = Path.home() / ".kyrozen"
        state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".tui-update-", dir=state_dir) as directory:
            staged = self._stage_update_tui(Path(directory), source_url, checksum_url, None)
            return True, self._activate_update_tui(staged, None)
    except (OSError, ValueError, RuntimeError, tarfile.TarError, subprocess.TimeoutExpired) as exc:
        return False, self._fix_safe_text(exc, 400)


def _self_update(self) -> UpdateResult:
    """Prepare matching components, verify the installed package, then activate."""
    components = {"python": "unchanged", "tui": "unchanged", "github_cli": "skipped"}
    revision = None
    def outcome(status, message, restart=False):
        return UpdateResult(status, message, revision, dict(components), restart)
    uv_path = shutil.which("uv")
    if not uv_path:
        return outcome("failed", "uv is required. Rerun the official OpenKyrozen installer.")
    requested_python = (f"{sys.version_info.major}.{sys.version_info.minor}"
                        if sys.version_info[:2] in {(3, 12), (3, 13)} else "3.12")
    revision = self._resolve_update_revision()
    if not revision and not self._release_tui_asset_available():
        return outcome("failed", "No verified update source was available; the existing installation was kept.")
    package_spec = f"git+{self.UPDATE_REPOSITORY_URL}@{revision}" if revision else self.RELEASE_WHEEL_URL
    source_url = f"https://github.com/EvanProgramming/OpenKyrozen/archive/{revision}.tar.gz" if revision else self.TUI_SOURCE_URL
    checksum_url = None if revision else self.TUI_CHECKSUM_URL
    command = [uv_path, "tool", "install", "--python", requested_python, "--force",
               "--with", "fastapi", "--with", "uvicorn", "--with", "anthropic",
               "--with", "google-genai", "--with", "perplexityai", "--with", "boto3",
               "--with", "azure-identity", "--with", "playwright", package_spec]
    recovery = "Rerun /update when connectivity is restored, or rerun the official installer. Check the recovery CLI with kyrozen --help; use the installer if it cannot start."
    stage = "prepare"
    try:
        state_dir = Path.home() / ".kyrozen"
        state_dir.mkdir(parents=True, exist_ok=True)
        with self._update_lock(), tempfile.TemporaryDirectory(prefix=".tui-update-", dir=state_dir) as directory:
            staged = self._stage_update_tui(Path(directory), source_url, checksum_url, revision)
            components["tui"] = "prepared"
            stage = "install"
            components["python"] = "unknown"
            result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
            if result.returncode:
                result = subprocess.run([uv_path, "--no-cache", *command[1:]], capture_output=True,
                                        text=True, timeout=300, check=False)
            diagnostics = self._fix_safe_text("\n".join(part.strip() for part in
                (result.stdout, result.stderr) if part and part.strip()), 1200)
            if result.returncode:
                return outcome("partial", f"Update failed (uv exit {result.returncode}):\n{diagnostics}\nPython installation state is uncertain; TUI was not activated. {recovery}")
            stage = "verify"
            if not self._verify_update_package(uv_path, revision):
                return outcome("partial", "Python installation could not be verified; TUI was not activated. " + recovery)
            components["python"] = "verified"
            stage = "activate"
            tui_message = self._activate_update_tui(staged, revision)
            components["tui"] = "staged" if self._IS_WINDOWS else "verified"
        try:
            gh = (self._github_cli or self.GitHubCLI(self._get_workspace_root(), self._state_root())).install_managed()
            components["github_cli"] = "ready" if gh.get("success", True) else "failed"
            gh_message = str(gh.get("message", "GitHub CLI setup skipped."))
        except Exception as exc:
            components["github_cli"] = "failed"
            gh_message = "Optional GitHub CLI setup failed: " + self._fix_safe_text(exc, 400)
        origin = f"source revision {revision[:12]}" if revision else f"GitHub release {self.RELEASE_TAG}"
        return outcome("success", f"Updated OpenKyrozen from {origin}:\n{diagnostics}\n{tui_message}\n{gh_message}\nRestart kyrozen to use the verified update.", True)
    except (Exception, KeyboardInterrupt) as exc:
        status = "failed" if stage == "prepare" else "partial"
        detail = "Another update is running" if isinstance(exc, BlockingIOError) else "Update interrupted" if isinstance(exc, KeyboardInterrupt) else self._fix_safe_text(exc, 400)
        state = "The existing installation was kept." if stage == "prepare" else "Python may have changed; the running process has not been restarted."
        return outcome(status, f"Update {stage} failed: {detail}. {state} {recovery}")


def _outdated_packages(self) -> list[str]:
    result = subprocess.run([sys.executable, "-m", "pip", "list", "--outdated", "--format=columns"],
                            capture_output=True, text=True, timeout=30)
    return result.stdout.strip().split("\n")[2:5] if result.returncode == 0 else []
