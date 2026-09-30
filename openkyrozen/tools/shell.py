from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from openkyrozen.tools.models import CommandResult
from openkyrozen.security.command_policy import _BLOCKED_RE

def _is_dangerous(self, cmd: str) -> bool:
    """Return True if the command looks dangerous and should be blocked."""
    return bool(_BLOCKED_RE.search(cmd))


def _active_command_env(self) -> dict[str, str]:
    """Make the interpreter running Kyrozen win bare ``python``/``pip`` lookups."""
    env = os.environ.copy()
    # Keep the visible venv ``bin`` directory before the resolved interpreter
    # target; on macOS a venv executable is commonly a symlink to Homebrew.
    interpreter_path = Path(sys.executable)
    interpreter_dir = str(interpreter_path.parent)
    resolved_dir = str(interpreter_path.resolve().parent)
    current_path = env.get("PATH", "")
    path_entries = current_path.split(os.pathsep) if current_path else []
    for directory in reversed((interpreter_dir, resolved_dir)):
        if directory and directory not in path_entries:
            path_entries.insert(0, directory)
    env["PATH"] = os.pathsep.join(path_entries)
    return env


def run_command(self, args: str) -> CommandResult:
    """
    Execute a shell command and retain its machine-readable outcome.
    """
    cmd = args.strip()
    if not cmd:
        return CommandResult("Error: run_cmd requires a command", False, failure="invalid_arguments")
    if self._is_dangerous(cmd):
        return CommandResult(
            "Error: command blocked for safety (e.g. rm -rf or similar).", False, failure="denied"
        )
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
            env=self._active_command_env(),
            cwd=str(self._WORKSPACE_ROOT),
        )
        out = result.stdout or ""
        err = result.stderr or ""
        if result.returncode != 0:
            return CommandResult(
                f"Exit code {result.returncode}\nstdout:\n{out}\nstderr:\n{err}".strip(),
                False, result.returncode, "nonzero_exit",
            )
        return CommandResult(out.strip() or "(no output)", True, result.returncode)
    except subprocess.TimeoutExpired:
        return CommandResult("Error: command timed out after 60s", False, failure="timeout")
    except Exception as e:
        return CommandResult(f"Error running command: {e}", False, failure="execution_error")


def run_cmd(self, args: str) -> str:
    """Execute a shell command. Args: the full command string."""
    return str(self.run_command(args))
