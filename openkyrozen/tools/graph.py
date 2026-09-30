from __future__ import annotations

import json

from openkyrozen.tools.models import CommandResult
from openkyrozen.security.command_policy import _BLOCKED_RE

def graph_status(self, _args: str = "") -> str:
    """Return the private Graphify index state and a bounded mini graph."""
    if self._PROJECT_GRAPH is None:
        return "Error: project graph is not configured."
    return json.dumps(self._PROJECT_GRAPH.snapshot(), ensure_ascii=False, indent=2)


def graph_query(self, args: str) -> str:
    """Query the private Graphify index. Args: a project question."""
    return self._PROJECT_GRAPH.query(args) if self._PROJECT_GRAPH is not None else "Error: project graph is not configured."


def graph_explain(self, args: str) -> str:
    """Explain one graph node. Args: node label."""
    return self._PROJECT_GRAPH.explain(args) if self._PROJECT_GRAPH is not None else "Error: project graph is not configured."


def graph_path(self, args: str) -> str:
    """Trace the shortest graph path. Args: left|right."""
    if self._PROJECT_GRAPH is None:
        return "Error: project graph is not configured."
    parts = str(args).split("|", 1)
    if len(parts) != 2:
        return "Error: graph_path requires left|right."
    return self._PROJECT_GRAPH.path(parts[0], parts[1])


def graph_refresh(self, args: str = "") -> str:
    """Refresh the private local code graph. Pass --full for a clean rebuild."""
    if self._PROJECT_GRAPH is None:
        return "Error: project graph is not configured."
    state = self._PROJECT_GRAPH.refresh(full=str(args).strip().lower() in {"full", "--full"})
    return json.dumps(state, ensure_ascii=False, indent=2)
