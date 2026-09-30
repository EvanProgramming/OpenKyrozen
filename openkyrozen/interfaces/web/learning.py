from __future__ import annotations

import json
import asyncio

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
import uvicorn

async def api_v2_learning(self, status: str | None = None, profile: str | None = None, limit: int = 50):
    service = self
    if profile is not None:
        profile = service._normalise_profile(profile)
        if profile == "auto":
            profile = None
    proposals = service._agent.learning_engine.status(max(1, min(limit, 200)), profile=profile, status=status)
    return {"proposals": proposals}


async def api_v2_learning_metrics(self, profile: str | None = None):
    service = self
    if profile is not None:
        profile = service._normalise_profile(profile)
        if profile == "auto":
            profile = None
    return service._agent.learning_engine.metrics(profile)


async def api_v2_learning_features(self):
    service = self
    """Expose the authoritative registry and the latest durable run status."""
    return {
        "policy": service._agent.learning_policy(),
        "runtime": service._agent.learning_runtime(),
        "provider_class": service._agent._learning_provider_class(),
        "cost_source": service._agent.learning_cost_source(),
        "features": service._agent.learning_feature_status(),
    }


async def api_v2_learning_provider(self, request: Request):
    service = self
    """Select the user-owned Local or Remote learning runtime."""
    body = await service._json_object(request)
    mode = body.get("mode")
    if not isinstance(mode, str):
        raise HTTPException(400, "mode must be local or remote")
    try:
        service._agent.set_learning_policy(mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "policy": service._agent.learning_policy(),
        "runtime": service._agent.learning_runtime(),
        "provider_class": service._agent._learning_provider_class(),
        "cost_source": service._agent.learning_cost_source(),
    }


async def api_v2_learning_evidence(self, proposal_id: str):
    service = self
    card = service._agent.learning_engine.evidence_card(proposal_id)
    if card is None:
        raise HTTPException(404, "Learning proposal not found")
    return card


async def api_v2_learning_replay(self, proposal_id: str, request: Request):
    service = self
    body = await service._json_object(request)
    if any(key in body and not isinstance(body[key], list) for key in ("candidate", "predecessor")):
        raise HTTPException(400, "candidate and predecessor must be arrays")
    try:
        return service._agent.learning_engine.record_shadow_replay(
            proposal_id, body.get("candidate", []), body.get("predecessor", []),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


async def api_v2_learning_omission(self, proposal_id: str, request: Request):
    service = self
    body = await service._json_object(request)
    if any(key in body and not isinstance(body[key], list) for key in ("with_item", "without_item")):
        raise HTTPException(400, "with_item and without_item must be arrays")
    try:
        return service._agent.learning_engine.record_omission_trial(
            proposal_id, body.get("with_item", []), body.get("without_item", []),
        )
    except (AttributeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


async def api_v2_learning_retire(self, proposal_id: str):
    service = self
    if not service._agent.learning_engine.retire_artifact(proposal_id):
        raise HTTPException(409, "Artifact lacks non-regressing omission evidence")
    return {"status": "retired", "proposal_id": proposal_id}


async def api_v2_learning_restore(self, proposal_id: str):
    service = self
    if not service._agent.learning_engine.restore_retired(proposal_id):
        raise HTTPException(409, "Artifact is not retired")
    return {"status": "canary", "proposal_id": proposal_id}


async def api_v2_learning_export_capsule(self, proposal_id: str):
    service = self
    try:
        capsule = service._agent.learning_engine.export_capsule(proposal_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if capsule is None:
        raise HTTPException(404, "Learning proposal not found")
    return capsule


async def api_v2_learning_import_capsule(self, request: Request):
    service = self
    try:
        return service._agent.learning_engine.import_capsule(await service._json_object(request))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


async def api_v2_learning_constitution(self):
    service = self
    return service._agent.learning_engine.constitution()


async def api_v2_learning_rollback(self, proposal_id: str):
    service = self
    if not service._agent.learning_engine.rollback(proposal_id):
        raise HTTPException(404, "Learning proposal not found")
    return {"status": "rolled_back", "proposal_id": proposal_id}


async def api_v2_skills(self, status: str | None = None):
    service = self
    return {"skills": service._agent.skill_registry.list(status)}


async def api_v2_install_skill(self, request: Request):
    service = self
    body = await service._json_object(request)
    if not isinstance(body.get("path"), str) or not body["path"].strip():
        raise HTTPException(400, "path is required")
    if "activate" in body and not isinstance(body["activate"], bool):
        raise HTTPException(400, "activate must be a boolean")
    try:
        return service._agent.skill_registry.install(body["path"], activate=body.get("activate", False))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, str(exc))


async def api_v2_activate_skill(self, skill_id: str):
    service = self
    if not service._agent.skill_registry.activate(skill_id):
        raise HTTPException(400, "Skill validation failed or skill not found")
    return {"status": "active", "skill_id": skill_id}


async def api_v2_rollback_skill(self, skill_id: str):
    service = self
    if not service._agent.skill_registry.rollback(skill_id):
        raise HTTPException(404, "Skill not found")
    return {"status": "rolled_back", "skill_id": skill_id}


async def api_v2_agents(self):
    service = self
    return {"agents": service._agent.subagent_manager.list_profiles()}


async def api_v2_run_agent(self, request: Request):
    service = self
    body = await service._json_object(request)
    if (not isinstance(body.get("profile"), str) or not body["profile"].strip()
            or not isinstance(body.get("task"), str) or not body["task"].strip()):
        raise HTTPException(400, "profile and task are required")
    try:
        result = await asyncio.to_thread(
            service._agent.subagent_manager.run,
            body["profile"], body["task"],
            workspace_id=service._agent.memory_bank.workspace_id,
        )
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc))
