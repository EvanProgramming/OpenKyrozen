from __future__ import annotations

import re
from typing import Any
from openkyrozen.app.config import AgentConfigError, effective_capabilities, load_agent_config
from openkyrozen.security.dynamic_tools import SAFE_BUILTINS, validate_tool_source


def _record_dynamic_tool_event(self, event_type: str, name: str, *, reason: str = "",
                               description: str = "") -> None:
    """Record a bounded dynamic-tool decision without persisting source code."""
    payload = {"name": str(name)[:80] or "<unknown>"}
    if reason:
        payload["reason"] = str(reason)[:300]
    if description:
        payload["description"] = str(description)[:300]
    try:
        self.memory_bank.store.append_event(
            event_type, payload, user_id=self.memory_bank.user_id,
            workspace_id=self.memory_bank.workspace_id, session_id=self.memory_bank.session_id,
        )
    except Exception:
        # Audit logging must never turn a safe rejection into a chat failure.
        pass


def _reject_dynamic_tool(self, name: str, reason: str) -> bool:
    self._record_dynamic_tool_event("tool.rejected", name, reason=reason)
    return False


def _register_tool(self, name: str, code: str, description: str = "") -> bool:
    """Register a new callable tool dynamically. Returns True on success."""
    if not self.ALLOW_DYNAMIC_TOOLS:
        return self._reject_dynamic_tool(name, "dynamic tools are disabled by policy")
    if not name or not code:
        return self._reject_dynamic_tool(name, "tool name and source are required")
    try:
        if "dynamic" not in effective_capabilities(load_agent_config(self._get_workspace_root())):
            return self._reject_dynamic_tool(name, "agent configuration does not allow dynamic tools")
    except AgentConfigError as exc:
        return self._reject_dynamic_tool(name, f"invalid agent configuration: {type(exc).__name__}")
    if not self._execution_capability_token.allows("dynamic"):
        return self._reject_dynamic_tool(name, "dynamic capability is not granted or has expired")
    # Validate: name must be a valid identifier
    if not re.match(r"^[a-zA-Z_]\w*$", name):
        return self._reject_dynamic_tool(name, "tool name must be a Python identifier")

    # Don't overwrite built-in or already registered tools.
    if name in self.AVAILABLE_TOOLS:
        return self._reject_dynamic_tool(name, "tool name is already registered")

    valid, reason = validate_tool_source(code, name)
    if not valid:
        return self._reject_dynamic_tool(name, reason)

    # Compile the tool function in a restricted global namespace.
    try:
        local_ns: dict[str, Any] = {}
        exec(code, {"__builtins__": SAFE_BUILTINS}, local_ns)
        fn = local_ns.get(name)
        if fn is None or not callable(fn):
            return self._reject_dynamic_tool(name, "source did not create the requested callable")
    except Exception as exc:
        return self._reject_dynamic_tool(name, f"tool compilation failed: {type(exc).__name__}")

    # Apply description
    if description and not getattr(fn, "__doc__", None):
        fn.__doc__ = description

    if not self._confirm_tool_action("define_tool", name):
        return self._reject_dynamic_tool(name, "dynamic-tool approval was denied")

    # Register
    self.AVAILABLE_TOOLS[name] = fn
    # Rebuild tools list for system prompt
    self.TOOLS_LIST = self._build_tools_list()

    # Log the decision and inventory change, never the generated source.
    self._record_dynamic_tool_event("tool.registered", name, description=description)
    self.memory_bank.add_log(f"TOOL_CREATED: {name} — {description[:300]}")
    return True


def _attempt_define_tool(self, text: str) -> bool:
    r"""Parse a DefineTool block from LLM output and register the tool.
    Format:
    DefineTool:
    ```python
    def tool_name(args: str) -> str:
        '''Description'''
        ...
    ```
    """
    if not re.search(r"DefineTool\s*:", text, re.IGNORECASE):
        return False
    pattern = r"DefineTool:\s*```(?:python)?\s*([\s\S]*?)\s*```"
    match = re.search(pattern, text)
    if not match:
        return self._reject_dynamic_tool("<unknown>", "malformed DefineTool block")

    code = match.group(1).strip()
    if not code:
        return self._reject_dynamic_tool("<unknown>", "DefineTool source is empty")

    # Extract function name and description
    name_match = re.search(r"def\s+(\w+)\s*\(", code)
    if not name_match:
        return self._reject_dynamic_tool("<unknown>", "DefineTool source has no function definition")
    name = name_match.group(1)

    desc_match = re.search(r'"""([^"]*)"""', code) or re.search(r"'''([^']*)'''", code)
    description = desc_match.group(1).strip() if desc_match else ""

    return self._register_tool(name, code, description)
