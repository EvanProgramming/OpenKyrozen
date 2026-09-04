#!/usr/bin/env python3
"""Build a wheel and prove its console script works outside the checkout."""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str], *, cwd: Path, env: dict[str, str], input_text: str = "") -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode:
        detail = result.stdout + result.stderr
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}\n{detail}")
    return result


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="openkyrozen-wheel-smoke-") as directory:
        root = Path(directory)
        dist = root / "dist"
        venv = root / "venv"
        caller = root / "caller"
        home = root / "home"
        dist.mkdir()
        caller.mkdir()
        home.mkdir()

        base_env = os.environ.copy()
        base_env["PYTHONHASHSEED"] = "0"
        _run([sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)], cwd=ROOT, env=base_env)
        wheels = sorted(Path(path) for path in glob.glob(str(dist / "*.whl")))
        if len(wheels) != 1:
            raise RuntimeError(f"expected one wheel, found {wheels}")

        _run([sys.executable, "-m", "venv", str(venv)], cwd=ROOT, env=base_env)
        bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        kyrozen = bin_dir / ("kyrozen.exe" if os.name == "nt" else "kyrozen")
        _run([str(python), "-m", "pip", "install", str(wheels[0])], cwd=ROOT, env=base_env)

        env = base_env.copy()
        env.update({
            "HOME": str(home),
            "USERPROFILE": str(home),
            "KYROZEN_PROVIDER": "ollama",
            "KYROZEN_DISABLE_VECTOR_INDEX": "1",
        })
        version = _run([str(kyrozen), "--version"], cwd=caller, env=env)
        if "OpenKyrozen" not in version.stdout:
            raise RuntimeError(f"unexpected version output: {version.stdout!r}")
        help_result = _run([str(kyrozen), "--help"], cwd=caller, env=env)
        if "--project" not in help_result.stdout:
            raise RuntimeError("installed CLI help did not expose --project")
        launched = _run([str(kyrozen)], cwd=caller, env=env, input_text="/quit\n")
        compact_output = launched.stdout.replace("\n", "")
        expected_root = str((home / ".kyrozen" / "workspace").resolve())
        if "Global mode:" not in launched.stdout or expected_root not in compact_output:
            raise RuntimeError(f"installed CLI did not bind the global root:\n{launched.stdout}{launched.stderr}")

    print("Wheel installation smoke passed: kyrozen ran from an unrelated directory.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
