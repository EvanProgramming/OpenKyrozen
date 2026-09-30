from __future__ import annotations

from openkyrozen.providers import reset_cost_tracker

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
import uvicorn

async def api_cost(self, scope: str = "installation", session_id: str | None = None):
    service = self
    """Get durable installation, workspace, or session usage totals."""
    if scope not in {"installation", "workspace", "session"}:
        raise HTTPException(400, "scope must be installation, workspace, or session")
    session = service._normalise_session_id(session_id) if session_id else None
    if scope == "session" and not session:
        raise HTTPException(400, "session_id is required for session scope")
    return service._cost_report(scope, session)


async def api_cost_reset(self, request: Request):
    service = self
    """Start a new durable workspace/session reporting window without deleting usage."""
    body = await service._json_object(request)
    if body.get("confirm") != "reset-cost":
        raise HTTPException(400, "Set confirm to reset-cost to reset a usage window")
    scope = str(body.get("scope", "workspace"))
    if scope not in {"workspace", "session"}:
        raise HTTPException(400, "scope must be workspace or session")
    session_id = service._normalise_session_id(body.get("session_id")) if body.get("session_id") else None
    if scope == "session" and not session_id:
        raise HTTPException(400, "session_id is required for session scope")
    reset = reset_cost_tracker(
        store=service._agent.memory_bank.store, user_id=service._SERVER_ACTOR_ID,
        workspace_id=service._agent.memory_bank.workspace_id, session_id=session_id, scope=scope,
    )
    service._audit("USAGE_RESET", f"scope={scope}", service._SERVER_ACTOR_ID)
    return {"reset": reset, **service._cost_report(scope, session_id)}


async def api_health(self):
    service = self
    """Health check."""
    provider_ok = service._agent.llm_provider is not None
    return {
        "status": "ok" if provider_ok else "degraded",
        "provider": service._agent._provider_config.provider if service._agent._provider_config else "unknown",
        "memory_count": service._agent.memory_bank.count_logs(),
    }
