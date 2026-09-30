"""Local learning runtime installation and machine probes."""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from openkyrozen.providers.config import ProviderConfig

def _local_learning_resources_ok(self) -> tuple[bool, str]:
    """Keep the small local model usable on ordinary low-memory computers."""
    try:
        free_disk = shutil.disk_usage(Path.home()).free
        if free_disk < 8 * 1024**3:
            return False, "Local learning needs at least 8 GB of free disk space."
        if sys.platform == "darwin":
            memory_bytes = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip())
        elif os.name == "nt":
            import ctypes
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong),
                             ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                             ("total_page_file", ctypes.c_ulonglong), ("avail_page_file", ctypes.c_ulonglong),
                             ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                             ("avail_extended_virtual", ctypes.c_ulonglong)]
            status = MemoryStatus(); status.length = ctypes.sizeof(status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                raise OSError("GlobalMemoryStatusEx failed")
            memory_bytes = int(status.total_phys)
        else:
            memory_bytes = 0
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    memory_bytes = int(line.split()[1]) * 1024
                    break
        if memory_bytes and memory_bytes < 12 * 1024**3:
            return False, "Local learning needs at least 12 GB of system RAM."
    except Exception:
        pass
    return True, ""


def _ollama_command(self) -> str | None:
    return shutil.which("ollama")


def _install_ollama(self) -> str:
    """Use Ollama's documented installer only after the user chose Local."""
    if sys.platform == "win32":
        command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                   "irm https://ollama.com/install.ps1 | iex"]
    else:
        command = ["/bin/sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"]
    completed = subprocess.run(command, text=True, capture_output=True, timeout=600, check=False)
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout or "Ollama installer failed")[:500])
    executable = self._ollama_command()
    if not executable:
        raise RuntimeError("Ollama installed but its command is not available; restart OpenKyrozen and try Local again.")
    return executable


def _ollama_ready(self, executable: str) -> bool:
    try:
        return subprocess.run([executable, "list"], text=True, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _bootstrap_local_learning(self) -> None:
    """Install, pull, and smoke-test the explicitly selected free learning runtime."""
    try:
        okay, detail = self._local_learning_resources_ok()
        if not okay:
            raise RuntimeError(detail)
        executable = self._ollama_command() or self._install_ollama()
        if not self._ollama_ready(executable):
            kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                                      "stderr": subprocess.DEVNULL}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen([executable, "serve"], **kwargs)
            for _ in range(10):
                time.sleep(1)
                if self._ollama_ready(executable):
                    break
        if not self._ollama_ready(executable):
            raise RuntimeError("Ollama did not start its local service.")
        pulled = subprocess.run([executable, "pull", self._LOCAL_LEARNING_MODEL], text=True,
                                capture_output=True, timeout=900, check=False)
        if pulled.returncode:
            raise RuntimeError((pulled.stderr or pulled.stdout or "Model download failed")[:500])
        listed = subprocess.run([executable, "list"], text=True, capture_output=True, timeout=15, check=False)
        if listed.returncode or self._LOCAL_LEARNING_MODEL not in listed.stdout:
            raise RuntimeError(f"Ollama did not report {self._LOCAL_LEARNING_MODEL} after download.")
        config = ProviderConfig(provider="ollama_native", model_simple=self._LOCAL_LEARNING_MODEL,
                                model_complex=self._LOCAL_LEARNING_MODEL)
        reply, _ = self.get_provider(config).chat([{"role": "user", "content": "Reply with OK."}], self._LOCAL_LEARNING_MODEL)
        if not reply.strip():
            raise RuntimeError("Local model smoke test returned no response.")
        self._set_learning_runtime("local", "ready", model=self._LOCAL_LEARNING_MODEL,
                              detail="Local Qwen learning is ready with no API cost.")
        self._record_learning_event("learning.local_ready", {"model": self._LOCAL_LEARNING_MODEL})
    except Exception as exc:
        self._set_learning_runtime("local", "failed", model=self._LOCAL_LEARNING_MODEL,
                              detail=self._learning_safe_text(exc, 500))
        self._record_learning_event("learning.local_setup_failed", {"error": self._learning_safe_text(exc, 500)})
