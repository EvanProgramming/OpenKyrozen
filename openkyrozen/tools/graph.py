from __future__ import annotations
from openkyrozen.tools.models import ToolError, ToolResult, command_tool_result, legacy_text_api, tool_failure, tool_json_result, tool_success

import json

from openkyrozen.security.command_policy import _BLOCKED_RE

@legacy_text_api
def graph_status(self, _args: str = "") -> str:
    """Return the private Graphify index state and a bounded mini graph."""
    if self._PROJECT_GRAPH is None:
        return tool_failure('not_found', "Error: project graph is not configured.", retriable=False)
    data = self._PROJECT_GRAPH.snapshot()
    return tool_json_result(data, text=json.dumps(data, ensure_ascii=False, indent=2))


@legacy_text_api
def graph_query(self, args: str) -> str:
    """Query the private Graphify index. Args: a project question."""
    if self._PROJECT_GRAPH is None:
        return tool_failure('not_found', "Error: project graph is not configured.", retriable=False)
    result = self._PROJECT_GRAPH.query_result(args)
    return tool_success(result.text) if result.success else tool_failure(result.error_code, result.text)


@legacy_text_api
def graph_explain(self, args: str) -> str:
    """Explain one graph node. Args: node label."""
    if self._PROJECT_GRAPH is None:
        return tool_failure('not_found', "Error: project graph is not configured.", retriable=False)
    result = self._PROJECT_GRAPH.explain_result(args)
    return tool_success(result.text) if result.success else tool_failure(result.error_code, result.text)


@legacy_text_api
def graph_path(self, args: str) -> str:
    """Trace the shortest graph path. Args: left|right."""
    if self._PROJECT_GRAPH is None:
        return tool_failure('not_found', "Error: project graph is not configured.", retriable=False)
    parts = str(args).split("|", 1)
    if len(parts) != 2:
        return tool_failure('invalid_arguments', "Error: graph_path requires left|right.", retriable=False)
    result = self._PROJECT_GRAPH.path_result(parts[0], parts[1])
    return tool_success(result.text) if result.success else tool_failure(result.error_code, result.text)


@legacy_text_api
def graph_refresh(self, args: str = "") -> str:
    """Refresh the private local code graph. Pass --full for a clean rebuild."""
    if self._PROJECT_GRAPH is None:
        return tool_failure('not_found', "Error: project graph is not configured.", retriable=False)
    state = self._PROJECT_GRAPH.refresh(full=str(args).strip().lower() in {"full", "--full"})
    return tool_json_result(state, text=json.dumps(state, ensure_ascii=False, indent=2))
