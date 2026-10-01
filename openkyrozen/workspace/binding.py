from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from openkyrozen.workspace.context import LaunchContext, resolve_launch_context, source_scope_id


def project_graph_snapshot(self) -> dict[str, Any]:
    return self._project_graph.snapshot() if self._project_graph is not None else {"status": "missing", "nodes": 0, "edges": 0, "communities": 0, "mini": {"nodes": [], "edges": []}}


def _project_graph_context(self, query: str) -> str:
    if self._project_graph is None or self.project_graph_snapshot().get("status") not in {"ready", "stale"}:
        return ""
    lowered = str(query).lower()
    indicators = (
        "code", "project", "repository", "repo", "architecture", "implement", "fix", "bug",
        "function", "class", "module", "dependency", "call", "file", "test", "refactor",
    )
    if not any(item in lowered for item in indicators):
        return ""
    result = self._project_graph.query(query, budget=1200)
    if result.startswith("Error:"):
        return ""
    return (
        "<project_graph_context>\n"
        "Source-derived Graphify context. Treat inferred edges as leads and verify consequential claims in source.\n"
        + result[:6000] + "\n</project_graph_context>"
    )


def _get_workspace_root(self) -> Path:
    """Return the configured target project, never the installed package root."""
    return Path(self._workspace_root).expanduser().resolve()


def _set_workspace_root(self, path: str) -> None:
    previous_root = self._get_workspace_root()
    root = str(Path(path).expanduser().resolve())
    if root != str(previous_root):
        from openkyrozen.agent.models import WorkspaceState
        workspace = self._workspaces.get(root)
        if workspace is None:
            adapters = self.workspace_factory()
            adapters.set_workspace_root(root)
            adapters.AVAILABLE_TOOLS["check_stored_data"] = self._check_stored_data
            adapters.AVAILABLE_TOOLS["search_memory"] = self._search_memory
            adapters.AVAILABLE_TOOLS["discover_tools"] = self._discover_tools
            workspace = WorkspaceState(root, adapters)
            self._workspaces[root] = workspace
        if any(session is self.current_session for session in self._sessions.values()):
            if self._session_context.get() is not None:
                raise ValueError("A bound session cannot change its workspace during a turn")
            self._default_session = self._create_session(
                f"surface:{self.surface}", self.memory_bank.user_id, workspace, source_scope_id(root),
            )
        else:
            self.current_session.workspace = workspace
    else:
        self._workspace_root = root
    active_root = self._get_workspace_root()
    if active_root != previous_root:
        self._last_project_scan_time = 0.0
        self._last_project_scan_root = None
    self._set_tools_workspace_root(self._workspace_root)
    current_context = getattr(self, "_launch_context", None)
    if isinstance(current_context, LaunchContext) and current_context.active_root != active_root:
        # Direct callers (notably tests and embedding applications) can still
        # use the legacy setter without leaving a stale mode description.
        self._launch_context = None
    current_memory = getattr(self, "memory_bank", None)
    if current_memory is not None:
        current_memory.file_scope_id = source_scope_id(active_root)


def _set_launch_context(self, context: LaunchContext) -> LaunchContext:
    """Bind one resolved launch context to every shared runtime boundary."""
    self._set_workspace_root(str(context.active_root))
    self._launch_context = context
    self._project_graph = self.ProjectGraph(
        context.active_root, context.runtime_state_root, context.source_scope_id,
    )
    self._github_cli = self.GitHubCLI(context.active_root, context.runtime_state_root)
    self._set_tools_project_graph(self._project_graph)
    self._set_tools_github_cli(self._github_cli)
    try:
        self.skill_registry.seed_builtins()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        self.memory_bank.store.append_event(
            "builtin_skill.seed_failed", {"error": str(exc)[:500]},
            user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
            session_id=self.memory_bank.session_id,
        )
    self._restore_user_preferences()
    self._restore_self_learning_flags()
    self._restore_ponytail_level()
    return context


def configure_launch_context(self,
    *,
    project_path: str | os.PathLike[str] | None = None,
    global_mode: bool = False,
    cwd: str | os.PathLike[str] | None = None,
) -> LaunchContext:
    """Resolve and bind the CLI/web context before agent startup."""
    return self._set_launch_context(resolve_launch_context(
        project_path=project_path,
        global_mode=global_mode,
        cwd=cwd,
    ))


def get_launch_context(self) -> LaunchContext | None:
    """Return the current process context, if startup has bound one."""
    return self._launch_context


def _is_path_safe(self, path: str) -> bool:
    if not self._workspace_root:
        return True
    try:
        root = os.path.realpath(self._workspace_root)
        resolved = os.path.realpath(os.path.expanduser(path))
        return os.path.commonpath([root, resolved]) == root
    except ValueError:
        return False
    except Exception:
        return False
