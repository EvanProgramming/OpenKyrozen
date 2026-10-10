from __future__ import annotations

from typing import Any



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
    from openkyrozen.tools.catalog import builtin_names
    available = tools if tools is not None else builtin_names()
    granted = resolve_capabilities(capabilities)
    return {name for name in available if tool_capability(name,tools) in granted}


def tool_capability(name: str, tools=None) -> str:
    """Read active registry policy; unknown compatibility tools remain dynamic."""
    registry = getattr(tools, 'registry', None)
    if registry is not None and name in registry.specs:
        return registry.get_spec(name).capability
    from openkyrozen.tools.catalog import builtin_metadata
    metadata = builtin_metadata(name)
    return metadata['capability'] if metadata else 'dynamic'


def tool_policy(name, tools=None):
    registry = getattr(tools, 'registry', None)
    if registry is not None and name in registry.specs:
        return registry.get_spec(name)
    from openkyrozen.tools.catalog import builtin_metadata
    return builtin_metadata(name)


def tool_risk(name, tools=None):
    policy = tool_policy(name,tools)
    return policy.risk if hasattr(policy,'risk') else policy['risk'] if policy else 'normal'


def tool_confirmation_required(name, surface, tools=None):
    if name == 'define_tool':
        return True
    policy = tool_policy(name,tools)
    surfaces = policy.confirmation_surfaces if hasattr(policy,'confirmation_surfaces') else policy.get('confirmation_surfaces',()) if policy else ()
    return tool_risk(name,tools) == 'high' and surface in surfaces
