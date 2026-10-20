"""OS filesystem restrictions for managed shell commands."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


class SandboxUnavailableError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass
class SandboxedCommand:
    command: str | list[str]
    shell: bool
    env: dict[str, str]
    backend: str
    temporary_directory: tempfile.TemporaryDirectory | None = None
    unavailable_reason: str | None = None

    def close(self) -> None:
        if self.temporary_directory is not None:
            self.temporary_directory.cleanup()
            self.temporary_directory = None


def _required() -> bool:
    return os.environ.get("KYROZEN_SANDBOX_REQUIRED", "").strip() == "1"


def _sbpl_path(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _bwrap_command(executable: str, workspace: str, cwd: str,
                   command: str) -> list[str]:
    workspace_mounts: list[str] = []
    if workspace != "/tmp" and os.path.commonpath([workspace, "/tmp"]) == "/tmp":
        parent = Path(workspace).parent
        missing: list[str] = []
        while str(parent) != "/tmp":
            missing.append(str(parent))
            parent = parent.parent
        for directory in reversed(missing):
            workspace_mounts.extend(("--dir", directory))
    workspace_mounts.extend(("--bind", workspace, workspace))
    return [executable, "--die-with-parent", "--new-session", "--unshare-pid",
            "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev",
            "--proc", "/proc", "--tmpfs", "/tmp", *workspace_mounts,
            "--chdir", cwd, "--setenv", "TMPDIR", "/tmp", "--", "/bin/sh", "-c", command]


def _profile(workspace: str, temp_path: str) -> str:
    return (
        "(version 1)(deny default)(import \"system.sb\")"
        "(allow process*)(allow file-read*)(allow network*)"
        f"(allow file-write* (subpath {_sbpl_path(workspace)})"
        f" (subpath {_sbpl_path(temp_path)}))"
    )


def _probe_backend(backend: str, executable: str) -> str | None:
    """Return a reason when a backend cannot enforce the workspace boundary."""
    try:
        with tempfile.TemporaryDirectory(prefix="kyrozen-sandbox-probe-", dir=Path.home()) as root:
            root_path = Path(root).resolve()
            workspace = root_path / "workspace"
            workspace.mkdir()
            inside = workspace / "inside-probe"
            outside = root_path / "outside-probe"
            command = (
                f"touch {shlex.quote(str(inside))} && "
                f"! touch {shlex.quote(str(outside))} 2>/dev/null"
            )
            if backend == "bubblewrap":
                argv = _bwrap_command(executable, str(workspace), str(workspace), command)
                result = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)
            else:
                with tempfile.TemporaryDirectory(prefix="kyrozen-sandbox-probe-") as temp:
                    profile = _profile(str(workspace), str(Path(temp).resolve()))
                    result = subprocess.run(
                        [executable, "-p", profile, "/bin/sh", "-c", command],
                        capture_output=True, text=True, timeout=5, check=False,
                    )
            if result.returncode == 0 and inside.is_file() and not outside.exists():
                return None
            detail = (result.stderr or result.stdout or "sandbox containment probe failed").strip()
            return detail[:300]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)[:300]


def sandbox_command(command: str, *, workspace: str, cwd: str,
                    env: dict[str, str]) -> SandboxedCommand:
    """Wrap a shell command in the available filesystem sandbox."""
    workspace = str(Path(workspace).resolve())
    cwd = str(Path(cwd).resolve())
    if workspace == os.path.abspath(os.sep):
        raise SandboxUnavailableError("The filesystem root cannot be used as a writable sandbox workspace.")

    if sys.platform == "darwin":
        executable = shutil.which("sandbox-exec")
        backend = "sandbox-exec"
    elif sys.platform.startswith("linux"):
        executable = shutil.which("bwrap")
        backend = "bubblewrap"
    else:
        executable = None
        backend = "unavailable"

    if not executable:
        reason = "no supported OS filesystem sandbox backend was found"
        if _required():
            raise SandboxUnavailableError(reason)
        return SandboxedCommand(command, True, env, "unavailable", unavailable_reason=reason)

    unavailable_reason = _probe_backend(backend, executable)
    if unavailable_reason:
        reason = f"{backend} could not enforce filesystem restrictions: {unavailable_reason}"
        if _required():
            raise SandboxUnavailableError(reason)
        return SandboxedCommand(command, True, env, "unavailable", unavailable_reason=reason)

    if backend == "sandbox-exec":
        temporary_directory = tempfile.TemporaryDirectory(prefix="kyrozen-sandbox-")
        temp_path = str(Path(temporary_directory.name).resolve())
        profile = _profile(workspace, temp_path)
        sandbox_env = dict(env)
        sandbox_env["TMPDIR"] = temp_path
        return SandboxedCommand(
            [executable, "-p", profile, "/bin/sh", "-c", command],
            False, sandbox_env, backend, temporary_directory,
        )

    sandbox_env = dict(env)
    sandbox_env["TMPDIR"] = "/tmp"
    return SandboxedCommand(
        _bwrap_command(executable, workspace, cwd, command),
        False, sandbox_env, backend,
    )
