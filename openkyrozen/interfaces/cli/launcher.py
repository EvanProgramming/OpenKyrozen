"""Launch the optional Bubble Tea client without breaking the Rich recovery CLI."""

from __future__ import annotations

import os
import json
import shutil
import subprocess
import sys
from pathlib import Path

TUI_RESTART_EXIT_CODE = 75


def _legacy() -> None:
    from .main import main
    main()


def _is_terminal() -> bool:
    return bool(getattr(sys.stdin, "isatty", lambda: False)() and
                getattr(sys.stdout, "isatty", lambda: False)())


def _tui_binary() -> str | None:
    from openkyrozen.updates.locking import update_lock
    with update_lock(Path.home() / ".kyrozen"):
        return _verified_tui_binary()


def _verified_tui_binary() -> str | None:
    explicit = os.environ.get("KYROZEN_TUI_BINARY", "").strip()
    state_bin = Path.home() / ".kyrozen" / "bin"
    manifest = state_bin.parent / "update-state.json"
    if manifest.is_file():
        state = json.loads(manifest.read_text(encoding="utf-8"))
        recovery = "Update installation is incomplete. Rerun the official installer; kyrozen --help remains available for recovery."
        if state.get("status") != "success":
            raise RuntimeError(recovery)
        from openkyrozen.updates.models import INSTALL_PROBE
        probe = subprocess.run([sys.executable, "-I", "-c", INSTALL_PROBE],
                               capture_output=True, text=True, timeout=60, check=False)
        try:
            data = json.loads(probe.stdout.strip().splitlines()[-1])
            paths = data.get("paths", [])
            provenance = bool(paths) and all(Path(path).resolve().is_relative_to(Path(sys.prefix).resolve()) for path in paths)
            source = data.get("source", {})
            revision = state.get("revision")
            matched = (source.get("vcs_info", {}).get("commit_id") == revision if revision else
                       data.get("version") == state.get("version") and source.get("url") == state.get("source_url"))
            if probe.returncode or not provenance or not matched:
                raise ValueError("installed package provenance differs")
        except (ValueError, IndexError, TypeError) as exc:
            raise RuntimeError(recovery) from exc
        name = "openkyrozen-tui.exe" if os.name == "nt" else "openkyrozen-tui"
        target = state_bin / name
        pending = target.with_name(target.name + ".next")
        prepared = pending if pending.is_file() else target
        check = subprocess.run([str(prepared), "--revision"], capture_output=True,
                               text=True, timeout=15, check=False)
        if check.returncode or check.stdout.strip() != state.get("tui_revision"):
            if revision:
                raise RuntimeError(recovery)
            legacy = subprocess.run([str(prepared), "--version"], capture_output=True,
                                    text=True, timeout=15, check=False)
            if legacy.returncode or legacy.stdout.strip() != "OpenKyrozen " + state.get("version", ""):
                raise RuntimeError(recovery)
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
        Path(__file__).resolve().parents[3] / "tui" / "openkyrozen-tui",
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
    onboarding_requested = bool(argv and argv[0].lower() == "onboarding")
    # These paths are intentionally handled by the legacy entry point: they
    # are one-shot commands, not interactive terminal sessions.
    if onboarding_requested and (not _is_terminal() or os.environ.get("KYROZEN_DISABLE_TUI") == "1"):
        print("kyrozen onboarding requires an interactive Bubble Tea terminal.", file=sys.stderr)
        return
    if (not _is_terminal() or os.environ.get("KYROZEN_DISABLE_TUI") == "1" or
            any(flag in argv for flag in ("--help", "--version", "--init")) or
            argv[:1] in (["migrate"], ["learning"])):
        _legacy()
        return

    try:
        binary = _tui_binary()
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"OpenKyrozen update handoff blocked: {exc}", file=sys.stderr)
        return
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
        env["KYROZEN_BACKEND_MODULE"] = "openkyrozen.interfaces.tui.backend"
    while True:
        try:
            completed = subprocess.run([binary, *argv], env=env, check=False)
        except OSError as exc:
            print(f"OpenKyrozen TUI could not start ({exc}); using Rich fallback.", file=sys.stderr)
            _legacy()
            return
        if completed.returncode == TUI_RESTART_EXIT_CODE:
            try:
                binary = _tui_binary() or binary
            except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                print(f"OpenKyrozen update handoff blocked: {exc}", file=sys.stderr)
                return
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
