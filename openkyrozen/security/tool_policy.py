from __future__ import annotations

from typing import Any

_TOOL_CAPABILITIES: dict[str, str] = {
    "spawn_agents": "read", "send_subagent": "read", "list_subagents": "read",
    "wait_subagents": "read", "cancel_subagent": "read",
    "check_stored_data": "read",
    "search_memory": "read",
    "read_file": "read",
    "calculate": "read",
    "discover_tools": "read",
    "list_dir": "read",
    "list_tree": "read",
    "find_files": "read",
    "git_status": "read",
    "git_diff": "read",
    "git_log": "read",
    "git_show": "read",
    "write_file": "write",
    "run_cmd": "shell",
    "execute_terminal_command": "shell",
    "search_web": "network",
    "read_webpage": "network",
    "git_clone": "git",
    "git_branch": "git",
    "analyze_remote_repo": "network",
    "browser_open": "browser",
    "browser_snapshot": "browser",
    "browser_click": "browser",
    "browser_type": "browser",
    "browser_close": "browser",
    "git_add": "git",
    "git_commit": "git",
    "git_push": "git",
    "git_pull": "git",
    "git_checkout": "git",
    "git_stash": "git",
    "git_reset": "destructive",
    "git_remote": "git",
    "graph_status": "read",
    "graph_query": "read",
    "graph_explain": "read",
    "graph_path": "read",
    "graph_refresh": "read",
    "github_status": "network",
    "github_read": "network",
    "github_cli": "git",
}


_CAPABILITY_PROFILES: dict[str, frozenset[str]] = {
    # Rich local-workspace access: read/write files, shell, network and Git.
    "workspace": frozenset({"read", "write", "shell", "network", "git", "browser"}),
    # Explicit opt-in for irreversible operations and user-defined tools.
    "full": frozenset({"read", "write", "shell", "network", "git", "browser", "destructive", "dynamic"}),
    "readonly": frozenset({"read", "network"}),
}


def resolve_capabilities(value: str | None, default: str = "readonly") -> frozenset[str]:
    """Resolve a named or comma-separated capability profile."""
    raw = (value or default).strip().lower()
    if raw in _CAPABILITY_PROFILES:
        return _CAPABILITY_PROFILES[raw]
    requested = {part.strip() for part in raw.split(",") if part.strip()}
    return frozenset(requested & {"read", "write", "shell", "network", "git", "browser", "destructive", "dynamic"})


def allowed_tool_names(tools: dict[str, Any] | None = None, capabilities: str | None = None) -> set[str]:
    """Return tools allowed by a capability profile.

    Unknown tools are treated as dynamic tools, so a newly registered tool
    cannot silently bypass a restricted Web/MCP profile.
    """
    available = tools if tools is not None else _TOOL_CAPABILITIES
    granted = resolve_capabilities(capabilities)
    allowed: set[str] = set()
    for name in available:
        capability = _TOOL_CAPABILITIES.get(name)
        if capability is None:
            if "dynamic" in granted:
                allowed.add(name)
            continue
        if capability in granted:
            allowed.add(name)
    return allowed


def tool_capability(name: str) -> str:
    """Return the capability required by a tool, treating unknown tools as dynamic."""
    return _TOOL_CAPABILITIES.get(name, "dynamic")
