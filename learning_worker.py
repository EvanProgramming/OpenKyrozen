"""Detached, singleton worker for durable self-learning cycles."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import Event

from workspace_context import source_scope_id


WORKER_INTERVAL_SECONDS = 30.0
ACTIVE_CLI_GRACE_SECONDS = 60.0


def _state_root() -> Path:
    path = Path.home() / ".kyrozen" / "v2"
    path.mkdir(parents=True, exist_ok=True)
    return path


def worker_paths(workspace_root: str | os.PathLike[str]) -> tuple[Path, Path]:
    """Return the singleton lock and CLI heartbeat paths for a workspace."""
    scope = source_scope_id(workspace_root)
    root = _state_root()
    return root / f"learning-worker-{scope}.pid", root / f"learning-worker-{scope}.heartbeat"


def touch_cli_heartbeat(workspace_root: str | os.PathLike[str]) -> Path:
    """Record that an interactive CLI is still active for this workspace."""
    _, heartbeat = worker_paths(workspace_root)
    heartbeat.write_text(str(time.time()), encoding="utf-8")
    return heartbeat


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def worker_is_running(workspace_root: str | os.PathLike[str]) -> bool:
    """Check and clean a stale worker pid file."""
    lock, _ = worker_paths(workspace_root)
    try:
        pid = int(lock.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        if lock.exists():
            lock.unlink(missing_ok=True)
        return False
    if _pid_is_alive(pid):
        return True
    lock.unlink(missing_ok=True)
    return False


def start_worker(*, workspace_root: str | os.PathLike[str], launch_mode: str) -> bool:
    """Start one detached worker, returning False only when spawning fails."""
    workspace_root = str(Path(workspace_root).expanduser().resolve())
    touch_cli_heartbeat(workspace_root)
    if worker_is_running(workspace_root):
        return True

    env = os.environ.copy()
    env.update({
        "KYROZEN_EXECUTION_SURFACE": "worker",
        "KYROZEN_WORKSPACE_ROOT": workspace_root,
        "KYROZEN_LAUNCH_MODE": launch_mode,
        "KYROZEN_LEARNING_WORKER": "1",
    })
    module_root = str(Path(__file__).resolve().parent)
    env["PYTHONPATH"] = module_root + os.pathsep + env.get("PYTHONPATH", "")
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, "-m", "learning_worker"], **kwargs)
    except OSError:
        return False
    return True


def _claim_worker_lock(lock: Path) -> bool:
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if worker_is_running_from_lock(lock):
            return False
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except (FileExistsError, OSError):
            return False
    except OSError:
        return False
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    finally:
        os.close(fd)
    return True


def worker_is_running_from_lock(lock: Path) -> bool:
    try:
        pid = int(lock.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        lock.unlink(missing_ok=True)
        return False
    if _pid_is_alive(pid):
        return True
    lock.unlink(missing_ok=True)
    return False


def _parent_cli_is_active(heartbeat: Path) -> bool:
    try:
        return (time.time() - heartbeat.stat().st_mtime) < ACTIVE_CLI_GRACE_SECONDS
    except FileNotFoundError:
        return False


def worker_main() -> int:
    workspace_root = os.environ.get("KYROZEN_WORKSPACE_ROOT", "") or str(
        Path.home() / ".kyrozen" / "workspace"
    )
    lock, heartbeat = worker_paths(workspace_root)
    if not _claim_worker_lock(lock):
        return 0

    stop = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    for signum in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if signum is not None:
            try:
                signal.signal(signum, request_stop)
            except (OSError, RuntimeError):
                pass

    try:
        import main as agent

        agent.configure_launch_context()
        agent._prompt_and_init_deepseek(interactive=False)
        if agent.learning_runtime()["status"] != "ready":
            agent._record_learning_event("learning.worker_skipped", {
                "reason": "learning runtime is not ready",
            })
            return 0
        agent._record_learning_event("learning.worker_started", {
            "workspace_root": str(Path(workspace_root).resolve()),
            "pid": os.getpid(),
        })
        while not stop.wait(WORKER_INTERVAL_SECONDS):
            if _parent_cli_is_active(heartbeat):
                continue
            agent._restore_self_learning_flags()
            if not any(agent._SELF_LEARNING_FLAGS.values()):
                continue
            try:
                agent.dispatch_learning_cycle(
                    surface="worker", trigger="detached", max_features=4,
                )
            except Exception as exc:
                agent.memory_bank.store.append_event(
                    "learning.worker_cycle_failed", {"error": str(exc)[:1000]},
                    user_id=agent.memory_bank.user_id,
                    workspace_id=agent.memory_bank.workspace_id,
                    session_id=agent.memory_bank.session_id,
                )
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(worker_main())
