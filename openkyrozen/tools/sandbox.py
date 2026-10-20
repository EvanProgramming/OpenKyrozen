"""OS filesystem restrictions for managed shell commands."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


class SandboxUnavailableError(RuntimeError):
    pass


@dataclass
class SandboxedCommand:
    command: str | list[str]
    shell: bool
    env: dict[str, str]
    backend: str
    temporary_directory: tempfile.TemporaryDirectory | None = None

    def close(self) -> None:
        if self.temporary_directory is not None:
            self.temporary_directory.cleanup()
            self.temporary_directory = None


def _required() -> bool:
    return os.environ.get("KYROZEN_SANDBOX_REQUIRED", "").strip() == "1"


def _sbpl_path(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


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
        if _required():
            raise SandboxUnavailableError("Required OS filesystem sandbox is unavailable on this system.")
        return SandboxedCommand(command, True, env, "unavailable")

    if backend == "sandbox-exec":
        temporary_directory = tempfile.TemporaryDirectory(prefix="kyrozen-sandbox-")
        temp_path = str(Path(temporary_directory.name).resolve())
        profile = (
            "(version 1)(deny default)(import \"system.sb\")"
            "(allow process*)(allow file-read*)(allow network*)"
            f"(allow file-write* (subpath {_sbpl_path(workspace)})"
            f" (subpath {_sbpl_path(temp_path)}))"
        )
        sandbox_env = dict(env)
        sandbox_env["TMPDIR"] = temp_path
        return SandboxedCommand(
            [executable, "-p", profile, "/bin/sh", "-c", command],
            False, sandbox_env, backend, temporary_directory,
        )

    sandbox_env = dict(env)
    sandbox_env["TMPDIR"] = "/tmp"
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
    return SandboxedCommand(
        [executable, "--die-with-parent", "--new-session", "--unshare-pid",
         "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev",
         "--proc", "/proc", "--tmpfs", "/tmp", *workspace_mounts,
         "--chdir", cwd, "--setenv", "TMPDIR", "/tmp", "--", "/bin/sh", "-c", command],
        False, sandbox_env, backend,
    )
