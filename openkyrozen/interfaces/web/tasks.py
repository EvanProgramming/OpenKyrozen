from __future__ import annotations

import math
from typing import Any
from openkyrozen.tasks.engine import TaskManager, TaskWorker
from openkyrozen.security.capabilities import issue_capability_token

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
import uvicorn

def _execute_durable_task(self, task: dict[str, Any]) -> dict[str, Any]:
    service = self
    """Execute only an explicitly stored, Web-profile action."""
    checkpoint = task.get("checkpoint", {})
    action = str(checkpoint.get("action", "")).strip()
    args = checkpoint.get("args", "")
    if not action:
        return {"success": False,
                "result": "No executable action stored; create the task with action and string args."}
    action = service._agent.TOOL_ALIASES.get(action, action)
    if not isinstance(args, str):
        return {"success": False, "action": action,
                "result": "Task args must be a plain string."}
    if action not in service._allowed_server_tools("web"):
        return {"success": False, "action": action,
                "result": f"Error: task action '{action}' is not allowed by the Web capability profile."}
    runtime = service._agent
    with service._chat_lock:
        session = runtime.open_session(task.get("session_id"), user_id=task["user_id"])
        receipt = runtime.execute(session, action, args, capabilities=service._server_capabilities("web"), operation_scope=task["id"])
    result, success = receipt.result, receipt.success
    return {
        "success": success, "action": action, "args": args,
        "result": result,
        "acceptance": str(checkpoint.get("acceptance") or "durable task action completed"),
    }


def _task_manager(self, session_id: str | None) -> TaskManager:
    service = self
    """Build a task manager bound to the authenticated deployment scope."""
    return TaskManager(
        service._agent.memory_bank.store,
        workspace_id=service._agent.interaction_workspace_id(),
        session_id=session_id,
        user_id=service._SERVER_ACTOR_ID,
    )


def _task_worker(self, session_id: str | None) -> TaskWorker:
    service = self
    """Build a bounded worker that can only claim one exact session scope."""
    return TaskWorker(service._task_manager(session_id), service._execute_durable_task, max_tasks=1)


def _task_scopes(self, statuses: set[str]) -> set[str | None]:
    service = self
    rows = service._agent.memory_bank.store.list_tasks(
        workspace_id=service._agent.interaction_workspace_id(),
        statuses=statuses,
        limit=10000,
        user_id=service._SERVER_ACTOR_ID,
    )
    return {row.get("session_id") for row in rows}


def _recover_task_scopes(self) -> list[dict[str, Any]]:
    service = self
    """Recover every unfinished task without collapsing session boundaries."""
    scopes = service._task_scopes({"pending", "running", "failed", "blocked"})
    recovered: list[dict[str, Any]] = []
    for session_id in scopes:
        recovered.extend(service._task_manager(session_id).recover())
    return recovered


def _run_task_worker_cycle(self) -> None:
    service = self
    """Run at most one safe action per pending session in this scheduler tick."""
    for session_id in sorted(service._task_scopes({"pending"}), key=lambda item: item or ""):
        service._task_worker(session_id).run_once()


def _run_scheduled_job(self, job: dict[str, Any]) -> None:
    service = self
    payload = job.get("payload", {})
    if payload.get("type") == "learning_cycle":
        if service._agent.learning_runtime()["status"] != "ready":
            return
        service._agent.dispatch_learning_cycle(surface="web", trigger="scheduled", max_features=4)
        return
    if payload.get("type") == "task_worker":
        service._run_task_worker_cycle()
        return
    if payload.get("type") == "chat":
        session_id = service._normalise_session_id(payload.get("session_id"))
        session = service._get_or_create_session(session_id, service._SERVER_ACTOR_ID)
        service._run_session_chat(session, str(payload.get("message", "")))
        return
    raise ValueError(f"Unknown scheduled job type: {payload.get('type', 'default')}")


async def api_v2_tasks(self, session_id: str | None = None):
    service = self
    session = service._normalise_session_id(session_id) if session_id else None
    manager = service._task_manager(session)
    return {"tasks": manager.store.list_tasks(workspace_id=manager.workspace_id, session_id=session,
                                                user_id=service._SERVER_ACTOR_ID)}


async def api_v2_create_task(self, request: Request):
    service = self
    body = await service._json_object(request)
    if not isinstance(body.get("description"), str) or not body["description"].strip():
        raise HTTPException(400, "description is required")
    session = service._normalise_session_id(body.get("session_id"))
    manager = service._task_manager(session)
    checkpoint: dict[str, Any] = {}
    if body.get("action") is not None:
        action = service._agent.TOOL_ALIASES.get(str(body.get("action")).strip(), str(body.get("action")).strip())
        if action not in service._allowed_server_tools("web"):
            raise HTTPException(403, f"Task action '{action}' is not allowed by the Web capability profile")
        if not isinstance(body.get("args", ""), str):
            raise HTTPException(400, "args must be a plain string")
        checkpoint = {"action": action, "args": body.get("args", "")}
        if isinstance(body.get("acceptance"), list) and body["acceptance"]:
            checkpoint["acceptance"] = body["acceptance"][0]
    priority = body.get("priority", 0)
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise HTTPException(400, "priority must be an integer")
    index = manager.add_task(
        str(body["description"]),
        dependencies=body.get("dependencies") if isinstance(body.get("dependencies"), list) else None,
        acceptance=body.get("acceptance") if isinstance(body.get("acceptance"), list) else None,
        priority=priority,
        checkpoint=checkpoint,
    )
    if not any(job.get("payload", {}).get("type") == "task_worker" for job in service._scheduler.list_jobs()):
        service._scheduler.schedule_every("durable-task-worker", 1.0, payload={"type": "task_worker"},
                                  job_id="job_durable_task_worker", delay_seconds=0)
    return {"task": manager.tasks[index], "queued": True}


async def api_v2_resume_task(self, task_id: str):
    service = self
    candidates = service._agent.memory_bank.store.list_tasks(
        workspace_id=service._agent.interaction_workspace_id(),
        statuses={"pending", "running", "failed", "blocked"},
        limit=10000,
        user_id=service._SERVER_ACTOR_ID,
    )
    stored = next((item for item in candidates if item["id"] == str(task_id)), None)
    if stored is None:
        raise HTTPException(404, "Failed or blocked task not found in this scope")
    manager = service._task_manager(stored.get("session_id"))
    manager.recover()
    task = manager.resume(task_id)
    if task is None:
        raise HTTPException(404, "Failed or blocked task not found in this scope")
    if not any(job.get("payload", {}).get("type") == "task_worker" for job in service._scheduler.list_jobs()):
        service._scheduler.schedule_every("durable-task-worker", 1.0, payload={"type": "task_worker"},
                                  job_id="job_durable_task_worker", delay_seconds=0)
    return {"task": task, "queued": True}


async def api_v2_schedules(self):
    service = self
    return {"schedules": service._scheduler.list_jobs()}


async def api_v2_create_schedule(self, request: Request):
    service = self
    body = await service._json_object(request)
    if not isinstance(body.get("name"), str) or not body["name"].strip():
        raise HTTPException(400, "name is required")
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    if not payload.get("type"):
        raise HTTPException(400, "payload.type is required")
    if "interval_seconds" in body:
        interval = body["interval_seconds"]
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval):
            raise HTTPException(400, "interval_seconds must be a finite number")
        try:
            job_id = service._scheduler.schedule_every(
                body["name"], interval, payload=payload,
                job_id=str(body["id"]) if body.get("id") else None,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise HTTPException(400, "invalid interval_seconds") from exc
    elif body.get("run_at"):
        if not isinstance(body["run_at"], str):
            raise HTTPException(400, "run_at must be an ISO-8601 timestamp")
        try:
            job_id = service._scheduler.schedule_once(
                body["name"], body["run_at"], payload=payload,
                job_id=str(body["id"]) if body.get("id") else None,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise HTTPException(400, "run_at must be an ISO-8601 timestamp") from exc
    else:
        raise HTTPException(400, "interval_seconds or run_at is required")
    return {"schedule": next(item for item in service._scheduler.list_jobs() if item["id"] == job_id)}


async def api_v2_disable_schedule(self, job_id: str):
    service = self
    if not service._agent.memory_bank.store.set_schedule_enabled(job_id, False, workspace_id=service._agent.memory_bank.workspace_id,
                                                         user_id=service._SERVER_ACTOR_ID):
        raise HTTPException(404, "Schedule not found")
    return {"status": "disabled", "job_id": job_id}
