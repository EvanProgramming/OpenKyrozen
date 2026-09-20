"""Launch the optional Bubble Tea client without breaking the Rich recovery CLI."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

TUI_RESTART_EXIT_CODE = 75


def _legacy() -> None:
    import main

    main.main()


def _is_terminal() -> bool:
    return bool(getattr(sys.stdin, "isatty", lambda: False)() and
                getattr(sys.stdout, "isatty", lambda: False)())


def _tui_binary() -> str | None:
    explicit = os.environ.get("KYROZEN_TUI_BINARY", "").strip()
    state_bin = Path.home() / ".kyrozen" / "bin"
    for name in ("openkyrozen-tui", "openkyrozen-tui.exe"):
        target = state_bin / name
        pending = target.with_name(target.name + ".next")
        if pending.is_file():
            try:
                os.replace(pending, target)
            except OSError:
                pass
    candidates = [Path(explicit)] if explicit else []
    candidates.extend([
        state_bin / "openkyrozen-tui",
        state_bin / "openkyrozen-tui.exe",
        Path(__file__).resolve().parent / "tui" / "openkyrozen-tui",
    ])
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("openkyrozen-tui")


def _backend_command() -> tuple[str | None, str | None]:
    command = shutil.which("kyrozen-backend")
    if command:
        return command, None
    return None, sys.executable


def main() -> None:
    argv = sys.argv[1:]
    # These paths are intentionally handled by the legacy entry point: they
    # are one-shot commands, not interactive terminal sessions.
    if (not _is_terminal() or os.environ.get("KYROZEN_DISABLE_TUI") == "1" or
            any(flag in argv for flag in ("--help", "--version", "--init")) or
            argv[:1] in (["migrate"], ["learning"])):
        _legacy()
        return

    binary = _tui_binary()
    if not binary:
        print(
            "OpenKyrozen TUI is unavailable; continuing with the Rich recovery interface.",
            file=sys.stderr,
        )
        _legacy()
        return

    backend, python = _backend_command()
    env = os.environ.copy()
    env["KYROZEN_EXECUTION_SURFACE"] = "tui"
    env.setdefault("KYROZEN_TUI_CAPABILITIES", "full")
    if backend:
        env["KYROZEN_BACKEND_COMMAND"] = backend
    else:
        env["KYROZEN_BACKEND_PYTHON"] = python or sys.executable
        env["KYROZEN_BACKEND_MODULE"] = "tui_backend"
    while True:
        try:
            completed = subprocess.run([binary, *argv], env=env, check=False)
        except OSError as exc:
            print(f"OpenKyrozen TUI could not start ({exc}); using Rich fallback.", file=sys.stderr)
            _legacy()
            return
        if completed.returncode == TUI_RESTART_EXIT_CODE:
            binary = _tui_binary() or binary
            continue
        if completed.returncode:
            print(
                f"OpenKyrozen TUI exited with status {completed.returncode}; using Rich fallback.",
                file=sys.stderr,
            )
            _legacy()
        return


if __name__ == "__main__":
    main()
