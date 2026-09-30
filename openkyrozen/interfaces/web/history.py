from __future__ import annotations

import time
from openkyrozen.agent.compaction import retain_context_digests

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
import uvicorn

async def api_v2_events(self, event_type: str | None = None, session_id: str | None = None, limit: int = 100):
    service = self
    session = service._normalise_session_id(session_id) if session_id else None
    return {"events": service._agent.memory_bank.store.list_events(
        event_type=event_type, limit=max(1, min(limit, 500)),
        workspace_id=service._agent.interaction_workspace_id(), session_id=session, user_id=service._SERVER_ACTOR_ID,
    )}


async def api_v2_session_history(self, session_id: str):
    service = self
    session_id = service._normalise_session_id(session_id)
    with service._chat_lock:
        session = service._get_or_create_session(session_id, service._SERVER_ACTOR_ID)
        manager, current = service._ensure_history_baseline(session)
        nodes = manager.display_nodes()
        return {
            "session_id": session_id, "current_node_id": current["id"],
            "nodes": [service._public_history_node(node, current["id"]) for node in nodes],
        }


async def api_v2_session_history_rollback(self, session_id: str, node_id: str, request: Request):
    service = self
    if "write" not in service._server_capabilities("web"):
        raise HTTPException(403, "History rollback requires workspace write capability")
    body = await service._json_object(request)
    if body.get("confirm") != "rollback":
        raise HTTPException(400, 'confirm must be "rollback"')
    expected_head = body.get("expected_head_id")
    if expected_head is not None and (not isinstance(expected_head, str) or len(expected_head) > 100):
        raise HTTPException(400, "expected_head_id must be a string")
    session_id = service._normalise_session_id(session_id)
    with service._chat_lock:
        session = service._get_or_create_session(session_id, service._SERVER_ACTOR_ID)
        manager, current = service._ensure_history_baseline(session)
        try:
            target, recovery = manager.rollback(
                node_id, confirm="rollback", expected_head=expected_head,
                current_conversation=list(session.get("messages", [])),
                current_interaction=service._interaction_for_session(session).state(),
                current_tasks=service._agent.memory_bank.store.list_tasks(
                    workspace_id=manager.workspace_id, session_id=session_id, user_id=service._SERVER_ACTOR_ID,
                ),
            )
        except service._agent.HistoryError as exc:
            message = str(exc)
            status = 409 if "changed" in message else 404 if "not found" in message else 400
            raise HTTPException(status, message) from exc
        session["messages"] = retain_context_digests(list(target.get("conversation", [])), service._MAX_SESSION_MESSAGES)
        session["interaction"] = target.get("interaction", {})
        session["updated"] = time.time()
        service._audit("HISTORY_ROLLBACK", f"session={session_id} target={target['id']} recovery={recovery['id']}", service._SERVER_ACTOR_ID)
        return {
            "status": "rolled_back", "session_id": session_id, "node_id": target["id"],
            "recovery_node_id": recovery["id"], "preserved_memory": True,
        }


async def api_v2_sessions(self, limit: int = 100):
    service = self
    return {"sessions": service._agent.memory_bank.store.list_sessions(
        workspace_id=service._agent.memory_bank.workspace_id, limit=max(1, min(limit, 500)),
        user_id=service._SERVER_ACTOR_ID,
    )}


async def api_v2_session(self, session_id: str):
    service = self
    session_id = service._normalise_session_id(session_id)
    with service._chat_lock:
        session = service._get_or_create_session(session_id, service._SERVER_ACTOR_ID)
    return {"session_id": session_id, "messages": session["messages"], "updated": session.get("updated"),
            "interaction": service._interaction_for_session(session).envelope(), "context": session.get("context")}
