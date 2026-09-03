#!/usr/bin/env python3
"""Check pip extras in user-facing files and execute them safely under zsh."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PIP_INSTALL = re.compile(r"\bpip(?:3)?\s+install\b[^\n]*")
LOCAL_EXTRA = re.compile(r"\.\[(?:web|all)\]")
README_FILES = sorted(ROOT.glob("README*.md"))


def _install_files() -> list[Path]:
    """Return README and shell/install files that can be copied into zsh."""
    paths = list(README_FILES)
    paths.extend(ROOT.glob("*.sh"))
    paths.extend(ROOT.glob("*.zsh"))
    paths.extend(ROOT.glob("setup.*"))
    paths.extend(ROOT.glob("run.*"))
    paths.extend(ROOT.glob("push.*"))
    paths.append(ROOT / "Makefile")
    paths.extend((ROOT / "scripts").glob("*.sh"))
    paths.extend((ROOT / "scripts").glob("*.zsh"))
    return sorted({path for path in paths if path.is_file()})


def _quoted_extra(line: str, start: int, end: int) -> bool:
    if start == 0 or end == len(line):
        return False
    quote = line[start - 1]
    return quote in {"'", '"'} and line[end] == quote


def _check_install_files() -> list[str]:
    errors: list[str] = []
    command_count = 0
    for path in _install_files():
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            command = PIP_INSTALL.search(line)
            if not command:
                continue
            for extra in LOCAL_EXTRA.finditer(command.group(0)):
                command_count += 1
                if not _quoted_extra(command.group(0), extra.start(), extra.end()):
                    errors.append(
                        f"{path.relative_to(ROOT)}:{line_number}: quote local extra {extra.group(0)}"
                    )
    if not errors:
        print(f"Checked {command_count} local pip extra references; all are zsh-safe.")
    return errors


def _run_zsh(args: list[str], script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["zsh", "-f", *args, "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _check_zsh() -> list[str]:
    if shutil.which("zsh") is None:
        return ["zsh executable not found; install zsh before running this check"]
    syntax = "pip install '.[web]'\npip install '.[all]'\n"
    parsed = _run_zsh(["-n"], syntax)
    if parsed.returncode:
        return [f"zsh syntax check failed: {parsed.stderr.strip()}"]

    dry_run = r'''
set -eu
pip() {
    if [[ "$#" != 3 || "$1" != install || "$2" != --dry-run ]]; then
        print -u2 "unexpected pip arguments: $*"
        return 1
    fi
    case "$3" in
        '.[web]'|'.[all]')
            print -r -- "$3"
            ;;
        *)
            print -u2 "unquoted or unexpected extra: $3"
            return 1
            ;;
    esac
}
pip install --dry-run '.[web]'
pip install --dry-run '.[all]'
'''
    executed = _run_zsh([], dry_run)
    if executed.returncode:
        detail = executed.stderr.strip() or executed.stdout.strip()
        return [f"zsh dry-run failed: {detail}"]
    if executed.stdout.splitlines() != [".[web]", ".[all]"]:
        return [f"zsh dry-run returned unexpected arguments: {executed.stdout!r}"]
    print("Clean zsh syntax and pip --dry-run execution passed without glob expansion.")
    return []


def main() -> int:
    errors = _check_install_files()
    errors.extend(_check_zsh())
    if errors:
        print("zsh extras check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
