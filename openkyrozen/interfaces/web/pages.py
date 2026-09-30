from __future__ import annotations



from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
import uvicorn

async def chat_page(self):
    service = self
    return service.CHAT_HTML


async def fast_diagnostics(self, request: Request, session_id: str):
    service = self
    session = service._get_or_create_session(service._normalise_session_id(session_id), service._actor_for_request(request))
    controller = service._interaction_for_session(session)
    store = service._agent.memory_bank.store
    events = store.list_events(
        "decision.fast", limit=100, user_id=controller.user_id,
        workspace_id=controller.workspace_id, session_id=controller.session_id,
    )
    usage = store.usage_totals(user_id=controller.user_id, workspace_id=service._agent.memory_bank.workspace_id,
                               session_id=controller.session_id)
    assist_events = store.list_events(
        "decision.assist", limit=100, user_id=controller.user_id,
        workspace_id=controller.workspace_id, session_id=controller.session_id,
    )
    state = controller.state()
    return {"system_one_backend": state["system_one_backend"], "fast_backend": state["fast_backend"],
            "decision_assist": service._agent.decision_assist_state(),
            "decisions": [event["payload"] for event in events],
            "assist_decisions": [event["payload"] for event in assist_events],
            "llm_usage": usage}


async def decision_assist_status(self):
    service = self
    return service._agent.decision_assist_state()


async def pwa_manifest(self):
    service = self
    return {
        "name": "OpenKyrozen",
        "short_name": "Kyrozen",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#050608",
        "theme_color": "#00f0ff",
    }
