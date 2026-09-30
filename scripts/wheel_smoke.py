#!/usr/bin/env python3
"""Build a wheel and prove its console script works outside the checkout."""

from __future__ import annotations

import glob
import json
import os
import shutil
import socket
import time
import urllib.error
import urllib.request
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
        timeout=300,
        check=False,
    )
    if result.returncode:
        detail = result.stdout + result.stderr
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}\n{detail}")
    return result


def _install(python: Path, artifact: Path, extras: str, *, cwd: Path, env: dict[str, str]) -> None:
    uv = shutil.which("uv")
    command = [uv, "pip", "install", "--python", str(python)] if uv else [str(python), "-m", "pip", "install"]
    _run(command + [str(artifact) + extras], cwd=cwd, env=env)


def _web_smoke(python: Path, *, cwd: Path, env: dict[str, str]) -> None:
    with socket.socket() as socket_probe:
        socket_probe.bind(("127.0.0.1", 0))
        port = socket_probe.getsockname()[1]
    web_env = env | {"KYROZEN_SERVER_TOKEN": "installed-smoke-token"}
    url = f"http://127.0.0.1:{port}"
    with (cwd / "web.log").open("w+") as log:
        process = subprocess.Popen([str(python), "-m", "openkyrozen.interfaces.web.app",
                                    "--global", "--host", "127.0.0.1", "--port", str(port)],
                                   cwd=cwd, env=web_env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    request = urllib.request.Request(url + "/api/health", headers={"Authorization": "Bearer installed-smoke-token"})
                    with urllib.request.urlopen(request, timeout=2) as response:
                        assert json.load(response)["status"] in {"ok", "degraded"}
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.2)
            else:
                log.seek(0)
                raise RuntimeError("Installed web startup failed:\n" + log.read())
            with urllib.request.urlopen(url, timeout=5) as response:
                assert b"OpenKyrozen" in response.read()
            try:
                urllib.request.urlopen(url + "/api/health", timeout=5)
            except urllib.error.HTTPError as error:
                assert error.code in {401, 403}
            else:
                raise AssertionError("Installed web API did not enforce authentication")
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


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
        _run([sys.executable, "-m", "build", "--outdir", str(dist)], cwd=ROOT, env=base_env)
        sdists = sorted(dist.glob("*.tar.gz"))
        if len(sdists) != 1:
            raise RuntimeError(f"expected one sdist, found {sdists}")
        wheels = sorted(Path(path) for path in glob.glob(str(dist / "*.whl")))
        if len(wheels) != 1:
            raise RuntimeError(f"expected one wheel, found {wheels}")

        _run([sys.executable, "-m", "venv", str(venv)], cwd=ROOT, env=base_env)
        bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        kyrozen = bin_dir / ("kyrozen.exe" if os.name == "nt" else "kyrozen")
        _install(python, wheels[0], "[all]", cwd=ROOT, env=base_env)

        env = base_env.copy()
        env.update({
            "HOME": str(home),
            "USERPROFILE": str(home),
            "KYROZEN_PROVIDER": "ollama",
            "KYROZEN_DISABLE_VECTOR_INDEX": "1",
        })
        entry_points = json.loads(_run([
            str(python), "-c",
            "import importlib.metadata, json; print(json.dumps({e.name: e.value for e in importlib.metadata.entry_points(group='console_scripts') if e.name in {'kyrozen', 'kyrozen-backend'}}))",
        ], cwd=caller, env=env).stdout)
        if entry_points.get("kyrozen") != "openkyrozen.interfaces.cli.launcher:main" or entry_points.get("kyrozen-backend") != "openkyrozen.interfaces.tui.backend:main":
            raise RuntimeError(f"installed entry points did not target the TUI bridge: {entry_points}")
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

        # Every shipped command and detached-worker module must resolve outside the source tree.
        for command in ("kyrozen-backend", "kyrozen-web", "kyrozen-bootstrap-gh"):
            executable = bin_dir / (command + ".exe" if os.name == "nt" else command)
            _run([str(executable), "--help"], cwd=caller, env=env)
        backend = _run([str(bin_dir / ("kyrozen-backend.exe" if os.name == "nt" else "kyrozen-backend"))],
                       cwd=caller, env=env, input_text='{"command":"start","request_id":"installed-start"}\n{"command":"shutdown"}\n')
        events = [json.loads(line) for line in backend.stdout.splitlines()]
        if not any(event.get("event") == "ready" for event in events):
            raise RuntimeError(f"installed JSONL backend did not become ready: {events}")
        _run([str(python), "-c", "import openkyrozen.learning.worker; import server; assert server.app.state.service._application is None"], cwd=caller, env=env)
        _run([str(python), "-m", "openkyrozen.learning.worker"], cwd=caller, env=env)
        _web_smoke(python, cwd=caller, env=env)

        probe_code = """
import tempfile
from pathlib import Path
from openkyrozen.app.bootstrap import build_application
_application = build_application(surface="cli")
main = _application.runtime
from openkyrozen.interfaces.web.service import WebService
server = WebService(_application)
assert any(r.path == "/api/auth/session" and "POST" in (r.methods or set()) for r in server.app.routes)
from openkyrozen.persistence.store import EventStore
from openkyrozen.tasks.engine import TaskManager

with tempfile.TemporaryDirectory() as directory:
    store = EventStore(Path(directory) / "state.sqlite3")
    manager = TaskManager(store, workspace_id="smoke", session_id="release")
    index = manager.add_task("blocked smoke task")
    manager.set_status(index, "blocked")
    previous = main.tasks
    main.tasks = manager
    try:
        assert main._task_status_counts()["blocked"] == 1
        assert "All tasks complete" not in main._tasks_panel_content()
    finally:
        main.tasks = previous
print("installed artifact auth/task behavior passed")
"""
        probe = _run([str(python), "-c", probe_code], cwd=caller, env=env)
        if "installed artifact auth/task behavior passed" not in probe.stdout:
            raise RuntimeError(f"installed artifact behavior probe failed:\n{probe.stdout}{probe.stderr}")

        acceptance_env = env.copy()
        acceptance_env["KYROZEN_ACCEPTANCE_INSTALLED"] = "1"
        acceptance = _run(
            [str(python), str(ROOT / "scripts" / "agent_workflow_acceptance.py")],
            cwd=caller, env=acceptance_env,
        )
        if "Agent workflow acceptance passed." not in acceptance.stdout:
            raise RuntimeError(f"installed agent workflow acceptance failed:\n{acceptance.stdout}{acceptance.stderr}")

        sdist_venv = root / "sdist-venv"
        _run([sys.executable, "-m", "venv", str(sdist_venv)], cwd=caller, env=base_env)
        sdist_bin = sdist_venv / ("Scripts" if os.name == "nt" else "bin")
        sdist_python = sdist_bin / ("python.exe" if os.name == "nt" else "python")
        _install(sdist_python, sdists[0], "[web]", cwd=caller, env=base_env)
        _run([str(sdist_bin / ("kyrozen.exe" if os.name == "nt" else "kyrozen")), "--help"], cwd=caller, env=env)
        _run([str(sdist_python), "-c", probe_code], cwd=caller, env=env)
        _web_smoke(sdist_python, cwd=caller, env=env)

    print("Wheel and sdist installation smoke passed: all commands, resources and runtime contracts ran outside the checkout.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
