from __future__ import annotations
from openkyrozen.tools.models import CommandResult, ToolError, ToolResult, command_tool_result, legacy_text_api, tool_failure, tool_json_result, tool_success

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from openkyrozen.security.command_policy import _BLOCKED_RE
from openkyrozen.tools.sandbox import SandboxUnavailableError, sandbox_command

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


def run_command(self, args: str | dict, *, cancelled=None) -> CommandResult:
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
    invocation = None
    process = None
    try:
        invocation = sandbox_command(
            cmd, workspace=str(self._WORKSPACE_ROOT), cwd=working_directory, env=env,
        )
        started = time.monotonic()
        process = subprocess.Popen(
            invocation.command, shell=invocation.shell, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=invocation.env, cwd=working_directory,
            start_new_session=(os.name != "nt"),
        )
        try:
            deadline = started + timeout
            while True:
                if cancelled is not None and cancelled.is_set():
                    raise InterruptedError
                try:
                    out, err = process.communicate(timeout=min(0.1, max(0.01, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise
        except InterruptedError:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=5, check=False)
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                out, err = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                out, err = process.communicate()
            return CommandResult("Error: command cancelled", False, process.returncode,
                                 "cancelled", out, err, time.monotonic() - started, True,
                                 sandbox_backend=invocation.backend,
                                 sandbox_reason=invocation.unavailable_reason)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=5, check=False)
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                out, err = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                out, err = process.communicate()
            elapsed = time.monotonic() - started
            message = f"Error: command timed out after {timeout}s"
            if invocation.backend == "unavailable":
                reason = f" Reason: {invocation.unavailable_reason}." if invocation.unavailable_reason else ""
                message = "OS filesystem sandbox unavailable; command ran without filesystem protection." + reason + "\n" + message
            return CommandResult(message, False,
                                 process.returncode, "timeout", out, err, elapsed, True,
                                 sandbox_backend=invocation.backend,
                                 sandbox_reason=invocation.unavailable_reason)
        elapsed = time.monotonic() - started
        message = ""
        if invocation.backend == "unavailable":
            message = "OS filesystem sandbox unavailable; command ran without filesystem protection."
            if invocation.unavailable_reason:
                message += f" Reason: {invocation.unavailable_reason}."
            message += "\n"
        if process.returncode != 0:
            return CommandResult(
                f"{message}Exit code {process.returncode}\nstdout:\n{out}\nstderr:\n{err}".strip(),
                False, process.returncode, "nonzero_exit", out, err, elapsed,
                sandbox_backend=invocation.backend,
                sandbox_reason=invocation.unavailable_reason,
            )
        return CommandResult(message + (out.strip() or "(no output)"), True, process.returncode,
                             stdout=out, stderr=err, duration_seconds=elapsed,
                             sandbox_backend=invocation.backend,
                             sandbox_reason=invocation.unavailable_reason)
    except SandboxUnavailableError as exc:
        return CommandResult(f"Error: required OS filesystem sandbox unavailable: {exc}", False,
                             failure="sandbox_unavailable", sandbox_backend="unavailable",
                             sandbox_reason=exc.reason)
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started if "started" in locals() else 0.0
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return CommandResult(f"Error: command timed out after {timeout}s", False,
                             failure="timeout", stdout=out, stderr=err,
                             duration_seconds=elapsed, timed_out=True,
                             sandbox_backend=invocation.backend if invocation else "unavailable",
                             sandbox_reason=invocation.unavailable_reason if invocation else None)
    except Exception as e:
        return CommandResult(f"Error running command: {e}", False, failure="execution_error",
                             sandbox_backend=invocation.backend if invocation else "unavailable",
                             sandbox_reason=invocation.unavailable_reason if invocation else None)
    finally:
        if process is not None and process.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   capture_output=True, timeout=5, check=False)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if invocation is not None:
            invocation.close()


@legacy_text_api
def run_cmd(self, args: str | dict) -> str:
    """Execute a shell command. Args: the full command string."""
    return command_tool_result(self.run_command(args))
