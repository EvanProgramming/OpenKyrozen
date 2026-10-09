from __future__ import annotations

import copy
import asyncio
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
    return self._agent.tool_registry.get_spec(name).input_schema


def _mcp_tool_descriptors(self, allowed: set[str]) -> list[dict[str, Any]]:
    return self._agent.tool_registry.mcp_descriptors(names=allowed)


def _mcp_string_arguments(self, tool_name: str, arguments: Any) -> str:
    """Translate compatibility inputs using the catalog's declared string adapter."""
    return self._agent.tool_registry.get_spec(tool_name).legacy_arguments(arguments)


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
                f"Tool requires '{tool_capability(tool_name,service._agent.AVAILABLE_TOOLS)}' capability; "
                "set KYROZEN_MCP_CAPABILITIES or use the full profile to enable it"
            ))
        try:
            string_args = service._mcp_string_arguments(tool_name, tool_args)
        except (TypeError, ValueError) as e:
            service._agent._notify_tool_execute(tool_name, tool_args, f"Error: invalid params: {e}")
            return service._mcp_error(request_id, -32602, f"Invalid params: {e}")
        try:
            def execute():
                runtime = service._agent
                session_id = service._normalise_session_id(params.get("session_id") or "surface:mcp")
                session = runtime.open_session(session_id, user_id=service._SERVER_ACTOR_ID)
                return runtime.execute(session, tool_name, string_args, capabilities=service._server_capabilities("mcp"), operation_scope=f"mcp:{request_id}")
            receipt = await asyncio.to_thread(execute)
            result_text = receipt.result
            is_error = not receipt.success
        except Exception as e:
            result_text = f"Error: {e}"
            is_error = True
        return service._mcp_response(request_id, result={
            "content": [{"type": "text", "text": result_text}],
            "isError": is_error,
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
