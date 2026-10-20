"""In-memory process handles owned by one agent session."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field

_MAX_OUTPUT_CHARS = 1_000_000
_MAX_PROCESS_SECONDS = 30 * 60


def new_process_manager(self, owner_id: str, workspace_id: str, session_id: str):
    return SessionProcessManager(owner_id, workspace_id, session_id)


@dataclass
class ProcessHandle:
    process_id: str
    process: subprocess.Popen
    owner_id: str
    workspace_id: str
    session_id: str
    timeout_seconds: float
    started_at: float = field(default_factory=time.monotonic)
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    finished_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class SessionProcessManager:
    """Keep child processes and output scoped to their creating session."""

    def __init__(self, owner_id: str, workspace_id: str, session_id: str):
        self.owner_id = str(owner_id)
        self.workspace_id = str(workspace_id)
        self.session_id = str(session_id)
        self._handles: dict[str, ProcessHandle] = {}
        self._lock = threading.Lock()
        self._closed = False

    def start(self, command: str, *, cwd: str, env: dict[str, str], timeout_seconds: float) -> ProcessHandle:
        with self._lock:
            if self._closed:
                raise RuntimeError("session process manager is closed")
            if len(self._handles) >= 32:
                raise RuntimeError("session process handle limit reached")
            process = subprocess.Popen(
                command, shell=True, cwd=cwd, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=(os.name != "nt"),
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            )
            handle = ProcessHandle(uuid.uuid4().hex, process, self.owner_id,
                                   self.workspace_id, self.session_id, timeout_seconds)
            self._handles[handle.process_id] = handle
        for name in ("stdout", "stderr"):
            threading.Thread(target=self._collect, args=(handle, name), daemon=True).start()
        threading.Thread(target=self._expire, args=(handle,), daemon=True).start()
        return handle

    def _expire(self, handle: ProcessHandle) -> None:
        try:
            handle.process.wait(timeout=min(handle.timeout_seconds, _MAX_PROCESS_SECONDS))
        except subprocess.TimeoutExpired:
            if handle.process.poll() is None:
                handle.timed_out = True
                self.stop(handle.process_id)
        finally:
            handle.finished_at = time.monotonic()

    @staticmethod
    def _collect(handle: ProcessHandle, name: str) -> None:
        stream = getattr(handle.process, name)
        try:
            while chunk := stream.read1(4096):
                decoded = chunk.decode("utf-8", errors="replace")
                with handle.lock:
                    value = getattr(handle, name)
                    combined = value + decoded
                    if len(combined) > _MAX_OUTPUT_CHARS:
                        setattr(handle, f"{name}_truncated", True)
                    setattr(handle, name, combined[:_MAX_OUTPUT_CHARS])
        finally:
            stream.close()

    def read(self, process_id: str, stream: str = "stdout", offset: int = 0,
             limit: int = 4000) -> dict | None:
        handle = self._handles.get(process_id)
        if (handle is None or handle.owner_id != self.owner_id
                or handle.workspace_id != self.workspace_id or handle.session_id != self.session_id):
            return None
        if stream not in {"stdout", "stderr"} or type(offset) is not int or offset < 0:
            return None
        if type(limit) is not int or not 1 <= limit <= 4000:
            return None
        with handle.lock:
            output = getattr(handle, stream)
            page = output[offset:offset + limit]
            truncated = getattr(handle, f"{stream}_truncated")
            timed_out = handle.timed_out
            duration = (handle.finished_at or time.monotonic()) - handle.started_at
        return {
            "process_id": process_id, "stream": stream, "offset": offset,
            "output": page, "next_offset": offset + len(page),
            "output_truncated": truncated, "timed_out": timed_out,
            "duration_seconds": duration,
            "running": handle.process.poll() is None,
            "exit_code": handle.process.poll(),
        }

    def stop(self, process_id: str) -> bool | None:
        handle = self._handles.get(process_id)
        if handle is None:
            return None
        if handle.process.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(handle.process.pid), "/T", "/F"],
                                   capture_output=True, timeout=5, check=False)
                    handle.process.wait(timeout=2)
                else:
                    os.killpg(handle.process.pid, signal.SIGTERM)
                    handle.process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                if handle.process.poll() is None:
                    try:
                        if os.name == "nt":
                            subprocess.run(["taskkill", "/PID", str(handle.process.pid), "/T", "/F"],
                                           capture_output=True, timeout=5, check=False)
                        else:
                            os.killpg(handle.process.pid, signal.SIGKILL)
                        handle.process.wait(timeout=2)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
        if handle.process.poll() is not None and handle.finished_at is None:
            handle.finished_at = time.monotonic()
        return handle.process.poll() is not None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            process_ids = tuple(self._handles)
        for process_id in process_ids:
            self.stop(process_id)
