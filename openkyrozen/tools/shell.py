from __future__ import annotations
from openkyrozen.tools.models import CommandResult, ToolError, ToolResult, command_tool_result, legacy_text_api, tool_failure, tool_json_result, tool_success

import os
import subprocess
import sys
import time
from pathlib import Path

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


def _validated_shell_options(self, args: str | dict):
    options = args if isinstance(args, dict) else {"command": args}
    cmd = options.get("command")
    timeout = options.get("timeout_seconds", 60)
    cwd = options.get("cwd", ".")
    overlay = options.get("env", {})
    background = options.get("background", False)
    if (not isinstance(cmd, str) or type(timeout) not in {int, float}
            or not 0 < timeout <= 1800 or not isinstance(cwd, str)
            or not isinstance(overlay, dict)
            or any(not isinstance(key, str) or not key or "=" in key or "\0" in key
                   or not isinstance(value, str) or "\0" in value for key, value in overlay.items())
            or type(background) is not bool):
        return CommandResult("Error: invalid shell options", False, failure="invalid_arguments")
    cmd = cmd.strip()
    if not cmd or "\0" in cmd:
        return CommandResult("Error: run_cmd requires a command", False, failure="invalid_arguments")
    if self._is_dangerous(cmd):
        return CommandResult("Error: command blocked for safety (e.g. rm -rf or similar).", False, failure="denied")
    try:
        working_directory = self._resolve_workspace_path(cwd)
    except (OSError, ValueError) as exc:
        return CommandResult(f"Error: invalid working directory: {exc}", False, failure="invalid_arguments")
    if not working_directory.is_dir():
        return CommandResult("Error: working directory must be a directory", False, failure="invalid_arguments")
    env = self._active_command_env()
    env.update(overlay)
    return cmd, timeout, str(working_directory), env, background


def run_command(self, args: str | dict) -> CommandResult:
    """
    Execute a shell command and retain its machine-readable outcome.
    """
    validated = self._validated_shell_options(args)
    if isinstance(validated, CommandResult):
        return validated
    cmd, timeout, working_directory, env, background = validated
    if background:
        return CommandResult("Error: background processes require a session runtime", False,
                             failure="invalid_arguments")
    try:
        started = time.monotonic()
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=working_directory,
        )
        out = result.stdout or ""
        err = result.stderr or ""
        elapsed = time.monotonic() - started
        if result.returncode != 0:
            return CommandResult(
                f"Exit code {result.returncode}\nstdout:\n{out}\nstderr:\n{err}".strip(),
                False, result.returncode, "nonzero_exit", out, err, elapsed,
            )
        return CommandResult(out.strip() or "(no output)", True, result.returncode,
                             stdout=out, stderr=err, duration_seconds=elapsed)
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started if "started" in locals() else 0.0
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return CommandResult(f"Error: command timed out after {timeout}s", False,
                             failure="timeout", stdout=out, stderr=err,
                             duration_seconds=elapsed, timed_out=True)
    except Exception as e:
        return CommandResult(f"Error running command: {e}", False, failure="execution_error")


@legacy_text_api
def run_cmd(self, args: str | dict) -> str:
    """Execute a shell command. Args: the full command string."""
    return command_tool_result(self.run_command(args))
