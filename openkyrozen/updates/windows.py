"""Stage uv environments without deleting a running Windows interpreter.

Generations deliberately keep their absolute paths: uv's executable entrypoints
refer to those paths. The previous tool environment remains usable for recovery.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

from .locking import update_lock


@dataclass
class WindowsPackageStage:
    root: Path
    python: Path
    bin_dir: Path
    environment: dict[str, str]
    process_result: subprocess.CompletedProcess
    token: str


def install_windows_package(uv_path: str, command: list[str], state_dir: Path) -> WindowsPackageStage:
    generations = state_dir / 'python-generations'
    generations.mkdir(parents=True, exist_ok=True)
    generation = Path(tempfile.mkdtemp(prefix='update-', dir=generations))
    tool_dir, bin_dir = generation / 'tools', generation / 'bin'
    tool_dir.mkdir(); bin_dir.mkdir()
    env = os.environ.copy()
    env.update(UV_TOOL_DIR=str(tool_dir), UV_TOOL_BIN_DIR=str(bin_dir))
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=300, check=False)
    if result.returncode:
        result = subprocess.run([uv_path, '--no-cache', *command[1:]], env=env,
                                capture_output=True, text=True, timeout=300, check=False)
    root = tool_dir / 'openkyrozen'
    return WindowsPackageStage(root, root / 'Scripts/python.exe', bin_dir, env, result, uuid.uuid4().hex)


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.update-' + uuid.uuid4().hex)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def activate_windows_package(bin_dir: Path, canonical_bin: Path, staged_tui: Path, tui_target: Path) -> None:
    """Replace entrypoints and TUI, restoring every prior file on failure."""
    entries = [(source, canonical_bin / source.name) for source in sorted(bin_dir.iterdir()) if source.is_file()]
    if not any(target.name == 'kyrozen.exe' for _, target in entries):
        raise RuntimeError('The staged package has no recovery CLI entrypoint.')
    entries.append((staged_tui, tui_target))
    with tempfile.TemporaryDirectory(prefix='.activation-backup-', dir=bin_dir.parent) as directory:
        backups = Path(directory)
        applied = []
        try:
            for index, (source, target) in enumerate(entries):
                backup = backups / str(index) if target.exists() else None
                if backup is not None:
                    shutil.copy2(target, backup)
                _atomic_copy(source, target)
                applied.append((target, backup))
            probe = subprocess.run([str(canonical_bin / 'kyrozen.exe'), '--help'], capture_output=True,
                                   text=True, timeout=60, check=False)
            if probe.returncode:
                raise RuntimeError('The activated recovery CLI failed its startup probe.')
        except BaseException:
            # Rollback errors propagate; callers must report uncertain activation.
            for target, backup in reversed(applied):
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    _atomic_copy(backup, target)
            raise


def _write_state(path: Path, state: dict) -> None:
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps(state), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def schedule_windows_activation(stage: WindowsPackageStage, canonical_bin: Path,
                                staged_tui: Path, tui_target: Path, state_file: Path) -> None:
    """Called while holding update_lock; durable pending state precedes dispatch."""
    # Move the TUI out of the caller's TemporaryDirectory before returning.
    permanent_tui = stage.bin_dir.parent / 'openkyrozen-tui.next'
    shutil.copy2(staged_tui, permanent_tui)
    state = json.loads(state_file.read_text(encoding='utf-8')) if state_file.exists() else {}
    state.update(status='pending', generation=stage.token, python=str(stage.python),
                 windows_activation={'pid': os.getpid(), 'bin_dir': str(stage.bin_dir),
                     'canonical_bin': str(canonical_bin), 'staged_tui': str(permanent_tui),
                     'tui_target': str(tui_target)})
    _write_state(state_file, state)
    subprocess.Popen([str(stage.python), '-I', '-m', 'openkyrozen.updates.windows',
                      str(state_file), stage.token], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=(getattr(subprocess, 'DETACHED_PROCESS', 0) |
                                    getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)), close_fds=True)


def _wait_for_process_exit(pid: int) -> None:
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        if ctypes.get_last_error() == 87:  # already exited
            return
        raise OSError(ctypes.get_last_error(), 'Cannot wait for updater process')
    try:
        if kernel.WaitForSingleObject(handle, 0xFFFFFFFF) != 0:
            raise OSError('Waiting for updater process failed')
    finally:
        kernel.CloseHandle(handle)


def finish_windows_activation(state_file: Path, token: str) -> None:
    initial = json.loads(state_file.read_text(encoding='utf-8'))
    if initial.get('generation') != token or initial.get('status') != 'pending':
        return
    _wait_for_process_exit(initial['windows_activation']['pid'])
    deadline = time.monotonic() + 120
    while True:
        try:
            with update_lock(state_file.parent):
                state = json.loads(state_file.read_text(encoding='utf-8'))
                if state.get('generation') != token or state.get('status') != 'pending':
                    return
                activation = state['windows_activation']
                try:
                    activate_windows_package(*(Path(activation[name]) for name in
                        ('bin_dir', 'canonical_bin', 'staged_tui', 'tui_target')))
                except PermissionError:
                    if time.monotonic() < deadline:
                        raise
                    state.update(status='failed', error='Windows entrypoints remain locked; rerun update after exiting OpenKyrozen.')
                except Exception as exc:
                    state.update(status='failed', error=str(exc)[:400])
                else:
                    state.update(status='success')
                _write_state(state_file, state)
                return
        except (BlockingIOError, PermissionError):
            if time.monotonic() >= deadline:
                # Keep pending evidence if another updater owns the lock.
                return
            time.sleep(0.25)


if __name__ == '__main__':
    import sys
    finish_windows_activation(Path(sys.argv[1]), sys.argv[2])
