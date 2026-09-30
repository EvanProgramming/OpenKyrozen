from __future__ import annotations

import copy
from typing import Any
from openkyrozen.tools import tool_capability
from openkyrozen.security.capabilities import issue_capability_token

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
import uvicorn

def _mcp_response(self, request_id: Any, *, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    service = self
    response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = result
    return response


def _mcp_error(self, request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    service = self
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return service._mcp_response(request_id, error=error)


def _mcp_tool_schema(self, name: str, fn: Any) -> dict[str, Any]:
    service = self
    """Return the object schema for a tool's existing string contract."""
    schemas: dict[str, dict[str, Any]] = {
        "read_file": {
            "properties": {"path": {"type": "string"}}, "required": ["path"],
        },
        "write_file": {
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            }, "required": ["path", "content"],
        },
        "list_dir": {"properties": {"path": {"type": "string"}}},
        "list_tree": {"properties": {"path": {"type": "string"}}},
        "find_files": {
            "properties": {
                "pattern": {"type": "string"},
                "directory": {"type": "string"},
            }, "required": ["pattern"],
        },
        "run_cmd": {"properties": {"command": {"type": "string"}}, "required": ["command"]},
        "execute_terminal_command": {
            "properties": {"command": {"type": "string"}}, "required": ["command"],
        },
        "search_web": {"properties": {"query": {"type": "string"}}, "required": ["query"]},
        "read_webpage": {"properties": {"url": {"type": "string"}}, "required": ["url"]},
        "git_clone": {
            "properties": {
                "url": {"type": "string"},
                "destination": {"type": "string"},
            }, "required": ["url"],
        },
        "analyze_remote_repo": {"properties": {"url": {"type": "string"}}, "required": ["url"]},
        "browser_open": {"properties": {"url": {"type": "string"}}, "required": ["url"]},
        "browser_snapshot": {"properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
        "browser_close": {"properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
        "browser_click": {
            "properties": {
                "session_id": {"type": "string"},
                "selector": {"type": "string"},
            }, "required": ["session_id", "selector"],
        },
        "browser_type": {
            "properties": {
                "session_id": {"type": "string"},
                "selector": {"type": "string"},
                "text": {"type": "string"},
            }, "required": ["session_id", "selector", "text"],
        },
    }
    schema = copy.deepcopy(schemas.get(name, {
        "properties": {"args": {"type": "string"}},
    }))
    schema["type"] = "object"
    schema["additionalProperties"] = False
    if name not in schemas:
        schema["description"] = "Plain-string arguments can be supplied as the `args` property."
    return schema


def _mcp_tool_descriptors(self, allowed: set[str]) -> list[dict[str, Any]]:
    service = self
    return [
        {
            "name": name,
            "description": (getattr(fn, "__doc__", "") or "").strip().split("\n")[0],
            "inputSchema": service._mcp_tool_schema(name, fn),
        }
        for name, fn in service._agent.AVAILABLE_TOOLS.items()
        if name in allowed
    ]


def _mcp_string_arguments(self, tool_name: str, arguments: Any) -> str:
    service = self
    """Map MCP object arguments to the tool's documented pipe/string format."""
    if arguments is None:
        return ""
    if isinstance(arguments, str):
        return arguments
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object or plain string")
    if not arguments:
        return ""
    if set(arguments) == {"args"}:
        if not isinstance(arguments["args"], str):
            raise ValueError("arguments.args must be a string")
        return arguments["args"]

    def value(*names: str, required: bool = True) -> str:
        present = next((name for name in names if name in arguments), None)
        if present is None:
            if required:
                raise ValueError(f"missing required argument: {names[0]}")
            return ""
        if not isinstance(arguments[present], str):
            raise ValueError(f"argument '{present}' must be a string")
        return arguments[present]

    if tool_name == "read_file":
        return value("path", "file_path")
    if tool_name == "write_file":
        return f"{value('path', 'file_path')}|{value('content', 'text')}"
    if tool_name in {"list_dir", "list_tree"}:
        return value("path", required=False)
    if tool_name == "find_files":
        pattern = value("pattern")
        directory = value("directory", required=False)
        return f"{pattern}|{directory}" if directory else pattern
    if tool_name in {"run_cmd", "execute_terminal_command"}:
        return value("command", "cmd")
    if tool_name == "search_web":
        return value("query")
    if tool_name in {"read_webpage", "browser_open", "analyze_remote_repo"}:
        return value("url")
    if tool_name == "git_clone":
        url = value("url")
        destination = value("destination", required=False)
        return f"{url}|{destination}" if destination else url
    if tool_name in {"browser_snapshot", "browser_close"}:
        return value("session_id", "sessionId")
    if tool_name == "browser_click":
        return f"{value('session_id', 'sessionId')}|{value('selector')}"
    if tool_name == "browser_type":
        return f"{value('session_id', 'sessionId')}|{value('selector')}|{value('text')}"

    # Every legacy tool has a plain-string contract.  Requiring the explicit
    # `args` wrapper keeps ambiguous object shapes from silently changing the
    # command sent to a tool.
    raise ValueError(f"tool '{tool_name}' accepts object arguments only as {{'args': '<string>'}}")


async def mcp_endpoint(self, request: Request):
    service = self
    """MCP-compatible endpoint for AI tool interoperability."""
    try:
        body = await request.json()
    except Exception:
        return service._mcp_error(None, -32700, "Parse error")
    if not isinstance(body, dict):
        return service._mcp_error(None, -32600, "Invalid Request")
    request_id = body.get("id")
    if "id" in body and isinstance(request_id, (dict, list, bool)):
        request_id = None
    if body.get("jsonrpc") not in (None, "2.0") or not isinstance(body.get("method"), str):
        return service._mcp_error(request_id, -32600, "Invalid Request")
    method = body["method"]
    params = body.get("params", {})
    if not isinstance(params, dict):
        return service._mcp_error(request_id, -32602, "Invalid params: params must be an object")

    if method == "initialize":
        requested_version = params.get("protocolVersion")
        selected_version = (
            requested_version if requested_version in service._MCP_SUPPORTED_PROTOCOL_VERSIONS
            else service._MCP_PROTOCOL_VERSION
        )
        return service._mcp_response(request_id, result={
            "protocolVersion": selected_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": service._MCP_SERVER_INFO,
        })
    if method in {"notifications/initialized", "ping"}:
        return service._mcp_response(request_id, result={})
    if method == "server/discover":
        allowed = service._allowed_server_tools("mcp")
        return service._mcp_response(request_id, result={
            "protocolVersion": service._MCP_PROTOCOL_VERSION,
            "serverInfo": service._MCP_SERVER_INFO,
            "capabilities": {"tools": {"listChanged": False}},
            "tools": service._mcp_tool_descriptors(allowed),
        })
    if method == "tools/list":
        allowed = service._allowed_server_tools("mcp")
        return service._mcp_response(request_id, result={"tools": service._mcp_tool_descriptors(allowed)})
    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", "")
        if not isinstance(tool_name, str) or not tool_name:
            service._agent._notify_tool_execute(str(tool_name), tool_args, "Error: tool name is required")
            return service._mcp_error(request_id, -32602, "Invalid params: tool name is required")
        fn = service._agent.AVAILABLE_TOOLS.get(tool_name)
        if fn is None:
            service._agent._notify_tool_execute(tool_name, tool_args, f"Error: Tool '{tool_name}' not found")
            return service._mcp_error(request_id, -32601, f"Tool '{tool_name}' not found")
        if tool_name not in service._allowed_server_tools("mcp"):
            service._agent._notify_tool_execute(
                tool_name, tool_args, f"Error: Tool '{tool_name}' is not authorized",
            )
            return service._mcp_error(request_id, -32001, (
                f"Tool requires '{tool_capability(tool_name)}' capability; "
                "set KYROZEN_MCP_CAPABILITIES or use the full profile to enable it"
            ))
        try:
            string_args = service._mcp_string_arguments(tool_name, tool_args)
        except (TypeError, ValueError) as e:
            service._agent._notify_tool_execute(tool_name, tool_args, f"Error: invalid params: {e}")
            return service._mcp_error(request_id, -32602, f"Invalid params: {e}")
        try:
            with service._chat_lock:
                runtime = service._agent
                session = runtime.open_session("surface:mcp", user_id=service._SERVER_ACTOR_ID)
                receipt = runtime.execute(session, tool_name, string_args, capabilities=service._server_capabilities("mcp"), operation_scope=f"mcp:{request_id}")
            result_text = receipt.result
        except Exception as e:
            result_text = f"Error: {e}"
        return service._mcp_response(request_id, result={
            "content": [{"type": "text", "text": result_text}],
            "isError": service._agent._is_tool_error(result_text),
        })
    if method == "chat/send":
        try:
            msg = service._validate_message(service._sanitize_api_message(str(params.get("message", "")).strip()))
        except HTTPException as exc:
            return service._mcp_error(request_id, -32602, str(exc.detail))
        if not msg:
            return service._mcp_error(request_id, -32602, "Empty message")
        session_id = service._normalise_session_id(params.get("session_id"))
        session = service._get_or_create_session(session_id, service._SERVER_ACTOR_ID)
        try:
            mode_token = service._agent._interaction_mode_override.set("agent")
            controls_token = service._agent._interaction_controls_enabled.set(False)
            try:
                reply = service._run_session_chat(session, msg)
            finally:
                service._agent._interaction_controls_enabled.reset(controls_token)
                service._agent._interaction_mode_override.reset(mode_token)
        except service._agent.ProviderUnavailableError as exc:
            return service._mcp_error(
                request_id,
                -32002,
                "LLM provider unavailable",
                {"code": service._agent.PROVIDER_UNAVAILABLE_CODE, "message": str(exc)},
            )
        except Exception as exc:
            service._audit("MCP_CHAT_ERROR", type(exc).__name__, service._SERVER_ACTOR_ID)
            return service._mcp_error(request_id, -32603, "Internal error")
        return service._mcp_response(request_id, result={"content": reply, "session_id": session_id})
    return service._mcp_error(request_id, -32601, f"Unknown method: {method}")
