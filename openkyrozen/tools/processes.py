"""In-memory process handles owned by one agent session."""
from __future__ import annotations

import os
import json
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from openkyrozen.tools.sandbox import sandbox_command

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
    sandbox_backend: str = "unavailable"
    temporary_directory: object | None = None
    sandbox_reason: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    finished_at: float | None = None
    cancelled: bool = False
    stop_reason: str | None = None
    audit_error: bool = False
    audit_exit_recorded: bool = False
    expiry_thread: threading.Thread | None = None
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
        self._audit_store = None
        self._audit_lock = threading.Lock()
        self._audit_closed = False
        self._audit_workspace_id = None
        self._audit_manager_id = uuid.uuid4().hex
        self._prior_process_audit = []
        self._prior_process_total = 0
        self._prior_audit_unavailable = False

    def _bind_audit(self, store, workspace_id: str) -> None:
        """Bind the existing session journal; filesystem roots are not DB scope IDs."""
        self._audit_store = store
        self._audit_workspace_id = str(workspace_id)
        try:
            unresolved = [record for record in self._audit_records()
                          if record.get("manager_id") != self._audit_manager_id
                          and record["owned_process_state"] == "unknown"]
            self._prior_process_total = len(unresolved)
            self._prior_process_audit = [{
                "process_id": record["process_id"][:64],
                "status": record.get("status") if record.get("status") in {
                    "launch_intent", "started", "stop_requested", "termination_failed", "launch_failed"
                } else "unknown",
                "pid_observation": record.get("pid") if type(record.get("pid")) is int else None,
            } for record in unresolved[:5]]
        except Exception:
            self._prior_audit_unavailable = True

    def _recovery_context(self) -> str:
        if self._prior_audit_unavailable:
            return ("Prior process audit unavailable; previous process effects are unknown. "
                    "Do not replay, reattach or kill processes from historical PID metadata.")
        if not self._prior_process_total:
            return ""
        return ("Prior process outcomes are unknown: "
                f"total={self._prior_process_total}, omitted={self._prior_process_total - len(self._prior_process_audit)}. "
                "Inspect or request trusted reconciliation before repeating effects. "
                "PID observations are not authority to reattach or kill; do not replay these commands. "
                + json.dumps(self._prior_process_audit, separators=(",", ":")))

    def _audit_event(self, status, process_id, *, handle=None, **fields):
        if self._audit_store is None:
            return
        payload = {"process_id": process_id, "manager_id": self._audit_manager_id,
                   "status": status, "observed_at": datetime.now(timezone.utc).isoformat(),
                   "owned_process_state": "unknown", "descendant_state": "unknown", **fields}
        if handle is not None:
            pid = getattr(handle.process, "pid", None)
            payload.update(pid=pid if type(pid) is int else None,
                           sandbox_backend=handle.sandbox_backend)
        try:
            if self._audit_closed or not self._audit_lock.acquire(timeout=.1):
                raise RuntimeError("process audit is closed or busy")
            try:
                if self._audit_closed:
                    raise RuntimeError("process audit is closed")
                self._audit_store._append_control_audit("process.lifecycle", payload, user_id=self.owner_id,
                    workspace_id=self._audit_workspace_id, session_id=self.session_id)
            finally:
                self._audit_lock.release()
        except Exception:
            # Neither database errors nor command/environment values enter audit logs.
            raise RuntimeError("Process audit could not be persisted.") from None

    def _audit_records(self) -> list[dict]:
        """Enumerate scoped uncertainty, never reattach or signal an observed PID."""
        if self._audit_store is None:
            return []
        events = self._audit_store.list_events("process.lifecycle", limit=None,
            user_id=self.owner_id, workspace_id=self._audit_workspace_id, session_id=self.session_id)
        records = {}
        for event in reversed(events):
            payload = event["payload"]
            process_id = payload.get("process_id")
            if not isinstance(process_id, str):
                continue
            records.setdefault(process_id, {}).update(payload)
        for record in records.values():
            if record.get("owned_process_state") not in {"exited", "not_started"}:
                record["owned_process_state"] = "unknown"
            record["potentially_active"] = (record["owned_process_state"] == "unknown"
                                               or record.get("descendant_state") == "unknown")
        return list(records.values())

    def _audit_started(self, handle):
        try:
            self._audit_event("started", handle.process_id, handle=handle,
                owned_process_state="running", timeout_seconds=handle.timeout_seconds)
        except RuntimeError:
            handle.audit_error = True
            handle.stop_reason = "launch_audit_failed"
            try:
                self.stop(handle.process_id)
            finally:
                if handle.process.poll() is not None:
                    for name in ("stdout", "stderr"):
                        getattr(handle.process, name).close()
                    if handle.temporary_directory is not None:
                        handle.temporary_directory.cleanup()
                        handle.temporary_directory = None
            raise RuntimeError("Process audit failed after launch; owned cleanup was attempted.") from None

    def _audit_outcome(self, handle):
        code = handle.process.poll()
        if handle.audit_exit_recorded:
            return
        reason = handle.stop_reason or "exited"
        try:
            self._audit_event(reason if code is not None else "termination_failed",
                handle.process_id, handle=handle, reason=reason,
                owned_process_state="exited" if code is not None else "unknown",
                exit_code=code if type(code) is int else None)
            handle.audit_exit_recorded = code is not None
        except RuntimeError:
            # The prior intent/started record stays unresolved on restart.
            handle.audit_error = True

    def start(self, command: str, *, cwd: str, env: dict[str, str], timeout_seconds: float,
              cancelled=None) -> ProcessHandle:
        process_id = uuid.uuid4().hex
        self._audit_event("launch_intent", process_id, timeout_seconds=timeout_seconds)
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("session process manager is closed")
                if cancelled is not None and cancelled.is_set():
                    raise RuntimeError("turn was cancelled before process start")
                if sum(handle.process.poll() is None for handle in self._handles.values()) >= 32:
                    raise RuntimeError("session process handle limit reached")
                invocation = sandbox_command(
                    command, workspace=self.workspace_id, cwd=cwd,
                    env=os.environ.copy() if env is None else env,
                )
                try:
                    process = subprocess.Popen(
                        invocation.command, shell=invocation.shell, cwd=cwd, env=invocation.env,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        start_new_session=(os.name != "nt"),
                        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
                    )
                except Exception:
                    invocation.close()
                    raise
                handle = ProcessHandle(
                    process_id, process, self.owner_id, self.workspace_id,
                    self.session_id, timeout_seconds, invocation.backend,
                    invocation.temporary_directory, invocation.unavailable_reason,
                )
                self._handles[handle.process_id] = handle
        except BaseException:
            try:
                self._audit_event("launch_failed", process_id, reason="launch_not_confirmed")
            except RuntimeError:
                pass
            raise
        self._audit_started(handle)
        for name in ("stdout", "stderr"):
            threading.Thread(target=self._collect, args=(handle, name), daemon=True).start()
        handle.expiry_thread = threading.Thread(target=self._expire, args=(handle, cancelled), daemon=True)
        handle.expiry_thread.start()
        return handle

    def _expire(self, handle: ProcessHandle, cancelled=None) -> None:
        try:
            deadline = handle.started_at + min(handle.timeout_seconds, _MAX_PROCESS_SECONDS)
            while handle.process.poll() is None:
                if cancelled is not None and cancelled.is_set():
                    handle.stop_reason = "cancelled"
                    self.stop(handle.process_id)
                    handle.cancelled = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    handle.stop_reason = "timed_out"
                    self.stop(handle.process_id)
                    handle.timed_out = True
                    break
                try:
                    handle.process.wait(timeout=min(.1, remaining))
                except subprocess.TimeoutExpired:
                    pass
        finally:
            # A failed termination must remain visibly running, with its
            # sandbox retained while the process may still be using it.
            self._audit_outcome(handle)
            if handle.process.poll() is not None:
                handle.finished_at = time.monotonic()
                self._cleanup_sandbox(handle)

    @staticmethod
    def _cleanup_sandbox(handle):
        with handle.lock:
            temporary = handle.temporary_directory
            handle.temporary_directory = None
        if temporary is not None:
            temporary.cleanup()

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
            "output_truncated": truncated, "timed_out": timed_out, "cancelled": handle.cancelled,
            "sandbox_backend": handle.sandbox_backend,
            "sandbox_reason": handle.sandbox_reason,
            "duration_seconds": duration,
            "running": handle.process.poll() is None,
            "exit_code": handle.process.poll(),
        }

    def stop(self, process_id: str) -> bool | None:
        handle = self._handles.get(process_id)
        if handle is None:
            return None
        if handle.process.poll() is None:
            handle.stop_reason = handle.stop_reason or "stopped"
            try:
                self._audit_event("stop_requested", handle.process_id, handle=handle,
                                  reason=handle.stop_reason)
            except RuntimeError:
                handle.audit_error = True
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
        self._audit_outcome(handle)
        if handle.process.poll() is not None:
            self._cleanup_sandbox(handle)
        return handle.process.poll() is not None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            process_ids = tuple(self._handles)
        try:
            for process_id in process_ids:
                self.stop(process_id)
            for process_id in process_ids:
                thread = self._handles[process_id].expiry_thread
                if thread is not None and thread is not threading.current_thread():
                    thread.join(timeout=1)
        finally:
            # Late expiry can retain uncertainty, but cannot reopen a closed
            # application's journal. Drain bounded in-flight audit appends.
            self._audit_closed = True
            if self._audit_lock.acquire(timeout=.3):
                self._audit_lock.release()
            else:
                for handle in self._handles.values():
                    handle.audit_error = True
                raise RuntimeError("Process audit finalization did not complete.")
