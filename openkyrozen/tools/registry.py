from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from openkyrozen.tools.models import CommandResult
from openkyrozen.security.command_policy import _BLOCKED_RE

def set_workspace_root(self, path: str | os.PathLike[str]) -> None:
    """Set the root directory allowed by file and directory tools."""
    self._WORKSPACE_ROOT = Path(path).expanduser().resolve()


def set_project_graph(self, graph: Any) -> None:
    self._PROJECT_GRAPH = graph


def set_github_cli(self, client: Any) -> None:
    self._GITHUB_CLI = client


def _resolve_workspace_path(self, raw_path: str) -> Path:
    expanded = os.path.expanduser(raw_path)
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = self._WORKSPACE_ROOT / candidate
    path = candidate.resolve()
    try:
        common = Path(os.path.commonpath([str(self._WORKSPACE_ROOT), str(path)]))
    except ValueError as exc:
        raise ValueError("path is outside the active workspace") from exc
    if common != self._WORKSPACE_ROOT:
        raise ValueError(f"path is outside the active workspace: {path}")
    return path
