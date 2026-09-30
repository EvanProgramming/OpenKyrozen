from __future__ import annotations

from openkyrozen.memory.service import MemoryBank

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
import uvicorn

async def api_memory(self, q: str = "", limit: int = 10):
    service = self
    """Search stored memories."""
    q = q[:service._MAX_MEMORY_QUERY_CHARS]
    limit = max(1, min(limit, service._MAX_MEMORY_RESULTS))
    if q:
        results = service._agent.memory_bank.recall(q, n_results=limit)
    else:
        results = service._agent.memory_bank.get_recent(limit)
    return {"results": results, "total": service._agent.memory_bank.count_logs()}


async def api_v2_memory(self, request: Request, q: str = "", limit: int = 10, session_id: str | None = None,
                        speaker: str | None = None, audience: str | None = None,
                        channel: str | None = None):
    service = self
    """Structured memory endpoint with scope and provenance metadata."""
    limit = max(1, min(limit, service._MAX_MEMORY_RESULTS))
    speaker, audience, channel = service._normalise_memory_context(speaker, audience, channel)
    authorized_speakers = {service._actor_for_request(request)}
    if q:
        memory = service._agent.memory_bank.scoped( user_id=service._actor_for_request(request),
                            workspace_id=service._agent.memory_bank.workspace_id,
                            session_id=service._normalise_session_id(session_id) if session_id else None)
        results = memory.recall_records(q, n_results=limit, speaker=speaker, audience=audience, channel=channel,
                                        authorized_speakers=authorized_speakers)
    else:
        memory = service._agent.memory_bank.scoped( user_id=service._actor_for_request(request),
                            workspace_id=service._agent.memory_bank.workspace_id,
                            session_id=service._normalise_session_id(session_id) if session_id else None)
        rows = memory.store.list_memories(status="active", limit=limit * 4,
                                          workspace_id=memory.workspace_id, session_id=memory.session_id,
                                          user_id=service._actor_for_request(request))
        results = memory.filter_records(rows, speaker=speaker, audience=audience, channel=channel,
                                        authorized_speakers=authorized_speakers)[:limit]
    return {"results": results, "total": memory.count_logs(), "scope": {
        "workspace_id": memory.workspace_id, "session_id": memory.session_id,
    }}


async def api_v2_memory_claims(self, request: Request, speaker: str | None = None, audience: str | None = None,
                               channel: str | None = None):
    service = self
    speaker, audience, channel = service._normalise_memory_context(speaker, audience, channel)
    rows = service._agent.memory_bank.store.list_memories(status=None, limit=10000,
                                                  workspace_id=service._agent.memory_bank.workspace_id,
                                                  user_id=service._actor_for_request(request))
    claims = [row for row in rows if row.get("metadata", {}).get("claim")]
    return {"claims": service._agent.memory_bank.filter_records(
        claims, speaker=speaker, audience=audience, channel=channel,
        authorized_speakers={service._actor_for_request(request)},
    )}


async def api_v2_create_memory_claim(self, request: Request):
    service = self
    body = await service._json_object(request)
    actor = service._actor_for_request(request)
    if body.get("claim_type") == "private_fact" and body.get("speaker") not in {None, actor}:
        raise HTTPException(403, "Private claims must belong to the authenticated speaker")
    if body.get("claim_type") == "private_fact":
        body["speaker"] = actor
    try:
        return service._agent.learning_engine.remember_claim(
            key=body.get("key", ""), value=body.get("value", ""), kind=body.get("kind", "fact"),
            authority=body.get("authority", "owner"), scope=body.get("scope", "global"),
            scope_value=body.get("scope_value", ""), evidence_id=body.get("evidence_id"),
            claim_type=body.get("claim_type", "general"), speaker=body.get("speaker"),
            audiences=body.get("audiences") if isinstance(body.get("audiences"), list) else [],
            channel=body.get("channel"), visibility=body.get("visibility", "public"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


async def api_v2_memory_claim(self, request: Request, claim_id: str, speaker: str | None = None,
                              audience: str | None = None, channel: str | None = None):
    service = self
    speaker, audience, channel = service._normalise_memory_context(speaker, audience, channel)
    actor = service._actor_for_request(request)
    claim = service._agent.learning_engine.explain_claim(
        claim_id, user_id=actor, workspace_id=service._agent.memory_bank.workspace_id,
    )
    visible = service._agent.memory_bank.filter_records(
        [claim] if claim else [], speaker=speaker, audience=audience, channel=channel,
        authorized_speakers={actor},
    )
    if not visible:
        raise HTTPException(404, "Memory claim not found")
    return visible[0]


async def api_v2_memory_forget_claim(self, request: Request, claim_id: str):
    service = self
    actor = service._actor_for_request(request)
    if not service._agent.learning_engine.forget_claim(
            claim_id, user_id=actor, workspace_id=service._agent.memory_bank.workspace_id):
        raise HTTPException(404, "Memory claim not found")
    return {"status": "forgotten", "memory_id": claim_id}
