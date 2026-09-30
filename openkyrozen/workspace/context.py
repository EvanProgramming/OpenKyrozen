"""Launch-time workspace resolution for the global and project CLI modes."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping


LaunchMode = Literal["global", "project"]


def _secure_directory(path: Path) -> Path:
    """Create a private state directory where the platform supports mode bits."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        # Windows ACLs are managed by the user profile; do not make a portable
        # launcher fail merely because chmod is not available.
        pass
    return path


def _canonical_directory(path: Path, *, cwd: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return candidate.resolve()


def source_scope_id(path: str | os.PathLike[str]) -> str:
    """Return a stable, non-path-bearing identifier for an indexed source root."""
    canonical = os.path.normcase(str(Path(path).expanduser().resolve()))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"source-{digest[:32]}"


@dataclass(frozen=True)
class LaunchContext:
    """Resolved roots used by one CLI or web process."""

    mode: LaunchMode
    active_root: Path
    state_root: Path
    source_scope_id: str
    requested_project: Path | None = None

    @property
    def runtime_state_root(self) -> Path:
        return self.state_root / "v2"

    @property
    def is_global(self) -> bool:
        return self.mode == "global"

    def describe(self) -> str:
        if self.is_global:
            return (
                f"Global mode: tools operate in `{self.active_root}`. "
                "The caller's directory is not used. "
                "Use `kyrozen --project .` for direct project operation."
            )
        return (
            f"Project mode: tools operate directly in `{self.active_root}`. "
            "Changes are made to the origin project files."
        )


def resolve_launch_context(
    *,
    project_path: str | os.PathLike[str] | None = None,
    global_mode: bool = False,
    cwd: str | os.PathLike[str] | None = None,
    home: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> LaunchContext:
    """Resolve and prepare the active root without changing process cwd."""
    env = os.environ if environ is None else environ
    if project_path is not None and global_mode:
        raise ValueError("--project and --global cannot be used together")

    caller_root = Path(cwd or os.getcwd()).expanduser().resolve()
    home_root = Path(home or Path.home()).expanduser().resolve()
    state_root = _secure_directory(home_root / ".kyrozen")
    _secure_directory(state_root / "v2")

    if project_path is not None:
        active_root = _canonical_directory(Path(project_path), cwd=caller_root)
        mode: LaunchMode = "project"
        requested_project = active_root
        if not active_root.is_dir():
            raise ValueError(f"project directory does not exist: {active_root}")
    elif global_mode:
        active_root = _secure_directory(state_root / "workspace")
        mode = "global"
        requested_project = None
    else:
        override = str(env.get("KYROZEN_WORKSPACE_ROOT", "")).strip()
        override_mode = str(env.get("KYROZEN_LAUNCH_MODE", "")).strip().lower()
        if override and override_mode == "global":
            active_root = _secure_directory(_canonical_directory(Path(override), cwd=caller_root))
            mode = "global"
            requested_project = None
        elif override:
            active_root = _canonical_directory(Path(override), cwd=caller_root)
            if not active_root.is_dir():
                raise ValueError(f"configured workspace directory does not exist: {active_root}")
            mode = "project"
            requested_project = active_root
        else:
            active_root = _secure_directory(state_root / "workspace")
            mode = "global"
            requested_project = None

    return LaunchContext(
        mode=mode,
        active_root=active_root,
        state_root=state_root,
        source_scope_id=source_scope_id(active_root),
        requested_project=requested_project,
    )
