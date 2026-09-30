from __future__ import annotations

import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
import requests
import openkyrozen.routing.models as routing_models
import openkyrozen.routing.transport as _routing_transport

def _kev_root() -> Path:
    return Path.home() / ".kyrozen" / "kev"


def _kev_python() -> Path:
    return _kev_root() / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _kev_key() -> str:
    return (_kev_root() / "api_key").read_text(encoding="utf-8").strip()


def _kev_model_version() -> str:
    cache = Path(os.environ.get("HF_HUB_CACHE") or
                 Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub")
    try:
        revision = (cache / "models--jaredpalmer--kev-0.8b" / "refs" / "main").read_text().strip()
    except OSError:
        revision = "unknown"
    return f"{routing_models.KEV_RUN}@{revision}"


def _supported_local_device() -> bool:
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return True
    for name in ("nvidia-smi", "rocminfo"):
        if shutil.which(name):
            try:
                if subprocess.run([name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=5, check=False).returncode == 0:
                    return True
            except (OSError, subprocess.TimeoutExpired):
                pass
    return False


def _kev_ready() -> bool:
    try:
        response = requests.get(routing_models.KEV_URL + "/v1/models", headers={"Authorization": f"Bearer {_kev_key()}"}, timeout=2)
        response.raise_for_status()
        models = response.json().get("models", [])
        for model in models:
            if model.get("name") == routing_models.KEV_MODEL and model.get("run") == routing_models.KEV_RUN:
                if model.get("device") not in {"mps", "cuda"}:
                    raise RuntimeError("Kev is using the CPU; a supported GPU runtime is required")
                return True
        return False
    except (OSError, ValueError, requests.RequestException):
        return False


def setup_kev() -> float:
    """Install only after explicit selection, then prove a live model decision."""
    started = time.perf_counter()
    if not _supported_local_device():
        raise RuntimeError("Kev-0.8B setup needs Apple Silicon or a CUDA/ROCm GPU")
    root = _kev_root()
    root.mkdir(parents=True, exist_ok=True)
    key_path = root / "api_key"
    if not key_path.exists():
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(secrets.token_urlsafe(32))
    os.chmod(key_path, 0o600)
    if not _kev_ready():
        if shutil.disk_usage(Path.home()).free < 8 * 1024**3:
            raise RuntimeError("Kev-0.8B setup needs at least 8 GB free disk space")
        try:
            with socket.create_connection(("127.0.0.1", 8009), timeout=1):
                raise RuntimeError("Port 8009 is occupied by another service")
        except ConnectionRefusedError:
            pass
        uv = shutil.which("uv")
        python = _kev_python()
        try:
            if not python.exists():
                command = ([uv, "venv", str(python.parent.parent), "--python", sys.executable]
                           if uv else [sys.executable, "-m", "venv", str(python.parent.parent)])
                subprocess.run(command, check=True, timeout=120, capture_output=True, text=True)
            command = ([uv, "pip", "install", "--python", str(python), routing_models.KEV_PACKAGE]
                       if uv else [str(python), "-m", "pip", "install", routing_models.KEV_PACKAGE])
            subprocess.run(command, check=True, timeout=900, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError("Kev install failed: " + str(exc.stderr or exc.stdout or exc)[-500:]) from exc
        log = (root / "server.log").open("a", encoding="utf-8")
        try:
            process = subprocess.Popen(
                [str(python), "-m", "kev.serve", "--run", routing_models.KEV_RUN, "--host", "127.0.0.1", "--port", "8009"],
                cwd=root, env={**os.environ, "KEV_API_KEY": _kev_key(), "PYTHONUNBUFFERED": "1",
                               "HF_HUB_DISABLE_XET": "1"},
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
            )
        finally:
            log.close()
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            try:
                if _kev_ready():
                    break
            except RuntimeError:
                process.terminate()
                raise
            if process.poll() is not None:
                raise RuntimeError("Kev exited during startup; see ~/.kyrozen/kev/server.log")
            time.sleep(2)
        else:
            raise RuntimeError("Kev did not become ready; see ~/.kyrozen/kev/server.log")
    result = _routing_transport._request("kev", {"smoke": {"type": "choice", "instructions": "Choose the matching word.",
                                               "criteria": {"ready": "ready", "missing": "missing"}}},
                      "The service is ready.", timeout=20)
    if result["answers"].get("smoke", {}).get("choice") != "ready":
        raise RuntimeError("Kev smoke decision failed")
    return round((time.perf_counter() - started) * 1000, 2)
