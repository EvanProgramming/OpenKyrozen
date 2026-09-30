from __future__ import annotations

import os
import json
import time
import threading
import uuid
import re
import asyncio
import subprocess
import queue
from typing import Any
from openkyrozen.providers import get_cost_report, get_cost_summary
from openkyrozen.tasks.engine import TaskManager
from openkyrozen.tools import allowed_tool_names, resolve_capabilities, tool_capability
from openkyrozen.agent.compaction import retain_context_digests

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
import uvicorn

def _normalise_session_id(self, raw_session_id: Any) -> str:
    service = self
    """Validate a client session key without allowing unbounded map growth."""
    if raw_session_id is None or raw_session_id == "":
        return f"sess_{uuid.uuid4().hex}"
    session_id = str(raw_session_id).strip()
    if len(session_id) > 128 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", session_id):
        raise HTTPException(400, "Invalid session_id")
    return session_id


def _normalise_profile(self, raw_profile: Any) -> str:
    service = self
    profile = str(raw_profile or "auto").strip().lower()
    if profile not in {"auto", "coder", "researcher"}:
        raise HTTPException(400, "profile must be auto, coder, or researcher")
    return profile


def _normalise_memory_actor(self, value: Any, field: str, default: str) -> str:
    service = self
    actor = str(value or default).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,64}", actor):
        raise HTTPException(400, f"Invalid {field}")
    return actor


def _normalise_memory_context(self, speaker: Any, audience: Any, channel: Any) -> tuple[str | None, str | None, str | None]:
    service = self
    speaker = service._normalise_memory_actor(speaker, "speaker", "local") if speaker else None
    audience = service._normalise_memory_actor(audience, "audience", speaker or "local") if audience or speaker else None
    channel = service._normalise_memory_actor(channel, "channel", "chat") if channel else None
    return speaker, audience, channel


def _initialise_session_defaults(self, session: dict[str, Any]) -> None:
    service = self
    """Apply the server-owned memory authority required by every chat surface."""
    default = service._SERVER_ACTOR_ID
    session["user_id"] = default
    session.setdefault("profile", "auto")
    session.setdefault("speaker", default)
    session.setdefault("audience", default)
    session.setdefault("channel", "chat")
    session["authorized_speakers"] = [default]


def _set_memory_context(self, session: dict[str, Any], body: dict[str, Any]) -> None:
    service = self
    service._initialise_session_defaults(session)
    default = service._SERVER_ACTOR_ID
    speaker = service._normalise_memory_actor(body.get("speaker"), "speaker", session.get("speaker", default))
    audience = service._normalise_memory_actor(body.get("audience"), "audience", session.get("audience", speaker))
    channel = service._normalise_memory_actor(body.get("channel"), "channel", session.get("channel", "chat"))
    session.update({"speaker": speaker, "audience": audience, "channel": channel,
                    "authorized_speakers": [default]})


def _actor_for_request(self, request: Request) -> str:
    service = self
    """Return the stable single-user deployment actor.

    Authentication is enforced by ``require_api_access``.  This function only
    maps an already-authorized request to the one owner supported by this
    server process; no header or JSON field can select another private user.
    """
    return service._SERVER_ACTOR_ID


def _get_or_create_session(self, session_id: str, user_id: str = "anonymous") -> dict:
    service = self
    user_id = service._SERVER_ACTOR_ID
    with service._chat_lock:
        if session_id not in service._sessions:
            persisted = service._agent.memory_bank.store.list_events(
                event_type="session.message", limit=service._MAX_SESSION_MESSAGES,
                workspace_id=service._agent.memory_bank.workspace_id, session_id=session_id,
                user_id=user_id,
            )
            messages = []
            for event in reversed(persisted):
                payload = event.get("payload", {})
                if payload.get("role") in {"user", "assistant"} and payload.get("content"):
                    messages.append({"role": payload["role"], "content": payload["content"]})
            context_events = service._agent.memory_bank.store.list_events(
                event_type="context.status", limit=1,
                workspace_id=service._agent.memory_bank.workspace_id, session_id=session_id,
                user_id=user_id,
            )
            service._sessions[session_id] = {
                "messages": retain_context_digests(messages, service._MAX_SESSION_MESSAGES),
                "user_id": user_id,
                "session_id": session_id,
                "created": time.time(),
                "context": context_events[0]["payload"] if context_events else None,
            }
            # Keep only last 100 sessions
            if len(service._sessions) > 100:
                oldest = min(service._sessions, key=lambda k: service._sessions[k]["created"])
                del service._sessions[oldest]
        session = service._sessions[session_id]
        service._initialise_session_defaults(session)
        current = service._agent.history_manager(session_id).current()
        if current is not None and current.get("kind") != "recovery":
            session["messages"] = retain_context_digests(list(current.get("conversation", [])), service._MAX_SESSION_MESSAGES)
        return session


def _interaction_for_session(self, session: dict[str, Any]):
    service = self
    return service._agent.InteractionController(
        service._agent.memory_bank.store, user_id=session.get("user_id", service._SERVER_ACTOR_ID),
        workspace_id=service._agent.interaction_workspace_id(), session_id=session["session_id"],
    )


def _history_for_session(self, session_id: str):
    service = self
    return service._agent.history_manager(session_id)


def _ensure_history_baseline(self, session: dict[str, Any]):
    service = self
    manager = service._history_for_session(session["session_id"])
    current = manager.current()
    if current is None:
        tasks = service._agent.memory_bank.store.list_tasks(
            workspace_id=manager.workspace_id, session_id=session["session_id"], user_id=service._SERVER_ACTOR_ID,
        )
        current = manager.ensure_root(
            conversation=list(session.get("messages", [])),
            interaction=service._interaction_for_session(session).state(), tasks=tasks,
            legacy=bool(session.get("messages")),
        )
    elif current.get("kind") != "recovery":
        session["messages"] = retain_context_digests(list(current.get("conversation", [])), service._MAX_SESSION_MESSAGES)
    return manager, current


def _public_history_node(self, node: dict[str, Any], current_id: str | None = None) -> dict[str, Any]:
    service = self
    return {
        "id": node["id"], "parent_id": node.get("parent_id"), "kind": node.get("kind"),
        "selector": node.get("selector"),
        "summary": node.get("summary", ""), "created_at": node.get("created_at"),
        "file_summary": node.get("file_summary", {}), "current": node["id"] == current_id,
    }


def _apply_chat_controls(self, session: dict[str, Any], body: dict[str, Any], message: str) -> tuple[str, str | None]:
    service = self
    """Apply typed interaction controls and return (message, immediate reply)."""
    controller = service._interaction_for_session(session)
    if body.get("question_response") is not None and body.get("plan_action") is not None:
        raise HTTPException(400, "question_response and plan_action are mutually exclusive")
    if "decision_assist_backend" in body:
        backend = str(body.get("decision_assist_backend") or "").strip().lower()
        try:
            service._agent.set_decision_assist(
                backend,
                private_consent=bool(body.get("decision_assist_private_consent", False)),
                api_key=body.get("jev_api_key") if isinstance(body.get("jev_api_key"), str) else None,
            )
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(400, str(exc)) from exc
    system_one_key = "system_one_backend" if "system_one_backend" in body else "fast_backend"
    if system_one_key in body:
        backend = str(body[system_one_key] or "").strip().lower()
        if backend not in {"off", "jev", "kev"}:
            raise HTTPException(400, "system_one_backend must be off, jev, or kev")
        try:
            if backend == "jev":
                key = body.get("jev_api_key")
                if key is not None:
                    if not isinstance(key, str):
                        raise ValueError("jev_api_key must be text")
                    service._agent.fast_mode.save_jev_key(key)
                if not service._agent.fast_mode.jev_key():
                    raise ValueError("Jev API key is required")
            elif backend == "kev":
                service._agent.fast_mode.setup_kev()
            controller.set_system_one_backend(backend)
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(400, str(exc)) from exc
    if "mode" in body:
        try:
            controller.set_mode(str(body["mode"]))
        except service._agent.InteractionError as exc:
            raise HTTPException(400, str(exc)) from exc

    question_response = body.get("question_response")
    if question_response is not None:
        if not isinstance(question_response, dict):
            raise HTTPException(400, "question_response must be an object")
        request_id = str(question_response.get("request_id") or "")
        if not request_id or len(request_id) > 100:
            raise HTTPException(400, "question_response.request_id is required")
        action = str(question_response.get("action") or "answer").lower()
        answers = question_response.get("answers", {})
        if len(json.dumps(answers, ensure_ascii=False, default=str)) > service._MAX_MESSAGE_CHARS:
            raise HTTPException(413, "question_response.answers is too large")
        try:
            resolved = controller.resolve_question(
                request_id, answers, action=action,
            )
        except service._agent.InteractionError as exc:
            raise HTTPException(409, str(exc)) from exc
        if action == "cancel":
            return "", "Pending question cancelled."
        question = resolved["question"]
        message = (
            f"Original request:\n{question.get('original_input', '')}\n\n"
            f"Clarification {action}:\n{json.dumps(resolved['answers'], ensure_ascii=False)}"
        )

    plan_action = body.get("plan_action")
    if plan_action is not None:
        if not isinstance(plan_action, dict):
            raise HTTPException(400, "plan_action must be an object")
        action = str(plan_action.get("action") or "").lower()
        plan_id = str(plan_action.get("plan_id") or "") or None
        version_raw = plan_action.get("version")
        version = version_raw if isinstance(version_raw, int) and not isinstance(version_raw, bool) else None
        if not plan_id or version is None or version < 1:
            raise HTTPException(400, "plan_action.plan_id and positive integer version are required")
        plan = controller.state().get("pending_plan")
        if action == "accept" and not plan and controller.accepted_plan(plan_id, version):
            return "", "Plan was already accepted."
        if not plan or (plan_id and plan_id != plan.get("plan_id")) or (version is not None and version != plan.get("version")):
            raise HTTPException(409, "plan is not pending at the requested version")
        if action == "accept":
            message = "accept plan"
        elif action == "cancel":
            controller.cancel_plan(plan_id, version)
            return "", "Pending plan cancelled."
        elif action == "revise":
            if not message:
                raise HTTPException(400, "message is required to revise a plan")
        else:
            raise HTTPException(400, "plan_action.action must be accept, revise, or cancel")

    if not message and (system_one_key in body or "decision_assist_backend" in body):
        if "decision_assist_backend" in body:
            return "", f"Decision Assist: {service._agent.decision_assist_state()['backend']}."
        return "", f"System One: {controller.envelope()['system_one_backend']}."
    if not message and "mode" in body:
        mode = controller.envelope()["preference_mode"]
        return "", f"Interaction mode set to {mode}."
    return message, None


def _run_session_chat(self, session: dict[str, Any], message: str) -> str:
    """Run a session without swapping process-global agent state."""
    with self._chat_lock:
        self._initialise_session_defaults(session)
        runtime = self._agent
        bound = runtime.open_session(session["session_id"], user_id=self._SERVER_ACTOR_ID)
        bound.messages = list(session["messages"])
        bound.tasks.recover()
        bound.last_learning_run = session.get("last_learning_run")
        bound.learning_notices = []
        with runtime.use_session(bound):
            recalls = runtime.memory_bank.store.list_events("memory.recalled", limit=1, workspace_id=runtime.memory_bank.workspace_id, session_id=bound.session_id, user_id=self._SERVER_ACTOR_ID)
            previous_recall_id = recalls[0]["id"] if recalls else None
            reply = runtime.chat(bound, message, clear_tasks=True, profile=session.get("profile", "auto"), memory_context={key: session.get(key) for key in ("speaker", "audience", "channel", "authorized_speakers")}, on_event=runtime._stream_event_callback.get())
            reply = runtime._clean_final_response(reply)
            session["last_learning_run"] = bound.last_learning_run
            bound.messages.extend([{"role": "user", "content": message}, {"role": "assistant", "content": reply}])
            session["messages"] = retain_context_digests(bound.messages, self._MAX_SESSION_MESSAGES)
            session["interaction"] = runtime.interaction_envelope()
            session["updated"] = time.time()
            session["context"] = runtime.context_usage()
            recalls = runtime.memory_bank.store.list_events("memory.recalled", limit=1, workspace_id=runtime.memory_bank.workspace_id, session_id=bound.session_id, user_id=self._SERVER_ACTOR_ID)
            session["last_memory_receipt"] = recalls[0]["payload"] if recalls and recalls[0]["id"] != previous_recall_id else None
            for role, content in (("user", message), ("assistant", reply)):
                runtime.memory_bank.store.append_event("session.message", {"role": role, "content": content}, user_id=self._SERVER_ACTOR_ID, workspace_id=runtime.memory_bank.workspace_id, session_id=bound.session_id)
            if session["context"]:
                runtime.memory_bank.store.append_event("context.status", session["context"], user_id=self._SERVER_ACTOR_ID, workspace_id=runtime.memory_bank.workspace_id, session_id=bound.session_id)
            return reply


def _validate_message(self, message: str) -> str:
    service = self
    if len(message) > service._MAX_MESSAGE_CHARS:
        raise HTTPException(413, f"Message exceeds {service._MAX_MESSAGE_CHARS} characters")
    return message


def _server_capabilities(self, surface: str) -> frozenset[str]:
    service = self
    """Return the configured capability set for Web or MCP requests."""
    env_name = "KYROZEN_MCP_CAPABILITIES" if surface == "mcp" else "KYROZEN_WEB_CAPABILITIES"
    # workspace is intentionally rich: it includes file writes, shell, network
    # and ordinary Git operations. `full` additionally enables reset/dynamic.
    configured = os.environ.get(env_name, "workspace")
    if os.environ.get("KYROZEN_MCP_ALLOW_DANGEROUS", "").lower() in {"1", "true", "yes"}:
        configured = "full"
    return resolve_capabilities(configured, default="workspace")


def _allowed_server_tools(self, surface: str) -> set[str]:
    service = self
    capabilities = ",".join(sorted(service._server_capabilities(surface)))
    return allowed_tool_names(service._agent.AVAILABLE_TOOLS, capabilities)


def _cost_summary(self) -> str:
    service = self
    return get_cost_summary(
        store=service._agent.memory_bank.store, user_id=service._SERVER_ACTOR_ID,
        workspace_id=service._agent.memory_bank.workspace_id,
    )


def _cost_report(self, scope: str = "installation", session_id: str | None = None) -> dict[str, Any]:
    service = self
    return get_cost_report(
        store=service._agent.memory_bank.store, user_id=service._SERVER_ACTOR_ID,
        workspace_id=service._agent.memory_bank.workspace_id, session_id=session_id, scope=scope,
    )


async def _json_object(self, request: Request) -> dict[str, Any]:
    service = self
    """Parse one bounded JSON object at every REST trust boundary."""
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "Invalid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object required")
    return body


async def api_chat(self, request: Request):
    service = self
    """Non-streaming chat endpoint."""
    body = await service._json_object(request)
    session_id = service._normalise_session_id(body.get("session_id"))
    session = service._get_or_create_session(session_id, service._actor_for_request(request))
    session["profile"] = service._normalise_profile(body.get("profile", session.get("profile", "auto")))
    service._set_memory_context(session, body)
    msg = service._validate_message(service._sanitize_api_message(str(body.get("message", "")).strip()))
    msg, immediate_reply = (await asyncio.to_thread(service._apply_chat_controls, session, body, msg)
                            if "fast_backend" in body or "system_one_backend" in body or "decision_assist_backend" in body
                            else service._apply_chat_controls(session, body, msg))
    if not msg and immediate_reply is None:
        raise HTTPException(400, "Empty message")
    service._audit("CHAT", f"user={session['user_id']} msg={msg[:80]}", session["user_id"])

    try:
        reply = immediate_reply if immediate_reply is not None else service._run_session_chat(session, msg)
    except service._agent.ProviderUnavailableError as exc:
        service._audit("ERROR", service._agent.PROVIDER_UNAVAILABLE_CODE, session["user_id"])
        raise HTTPException(
            status_code=503,
            detail={"code": service._agent.PROVIDER_UNAVAILABLE_CODE, "message": str(exc)},
        ) from exc
    except Exception as e:
        service._audit("ERROR", str(e), session["user_id"])
        raise HTTPException(500, str(e))

    session["interaction"] = service._interaction_for_session(session).envelope()
    service._emit_chat_completed(session, reply, streamed=False)
    service._audit("REPLY", f"len={len(reply)}", session["user_id"])
    return {"reply": reply, "session_id": session_id, "profile": session["profile"],
            "decision_assist": service._agent.decision_assist_state(),
            "memory_receipt": session.get("last_memory_receipt"), "cost": service._cost_summary(),
            "interaction": session["interaction"], "context": session.get("context")}


async def api_chat_stream(self, request: Request):
    service = self
    """SSE streaming chat endpoint."""
    body = await service._json_object(request)
    session_id = service._normalise_session_id(body.get("session_id"))
    session = service._get_or_create_session(session_id, service._actor_for_request(request))
    session["profile"] = service._normalise_profile(body.get("profile", session.get("profile", "auto")))
    service._set_memory_context(session, body)
    msg = service._validate_message(service._sanitize_api_message(str(body.get("message", "")).strip()))
    msg, immediate_reply = (await asyncio.to_thread(service._apply_chat_controls, session, body, msg)
                            if "fast_backend" in body or "system_one_backend" in body or "decision_assist_backend" in body
                            else service._apply_chat_controls(session, body, msg))
    if not msg and immediate_reply is None:
        raise HTTPException(400, "Empty message")
    service._audit("CHAT_STREAM", f"user={session['user_id']} msg={msg[:80]}", session["user_id"])

    class StreamProjection:
        """Pass plain deltas through while holding model control prefixes."""

        prefixes = ("Thought:", "Plan:", "TaskList:", "TaskDone:", "Action:", "DefineTool:",
                    "AskUser:", "AskUser\n", "PlanProposal:", "PlanProposal\n",
                    "{", "[", "```json")

        def __init__(self, sink):
            self.sink = sink
            self.buffer = ""
            self.dsml = service._agent.DeepSeekDSMLFilter()

        def _emit_content(self, chunk: str) -> None:
            self.buffer += chunk
            candidate = self.buffer.lstrip()
            lowered = candidate.lower()
            if candidate and (
                any(prefix.lower().startswith(lowered) for prefix in self.prefixes)
                or any(lowered.startswith(prefix.lower()) for prefix in self.prefixes)
                or service._agent._stream_buffer_has_tool_prefix(self.buffer)
            ):
                return
            if self.buffer:
                self.sink({"event": "content", "chunk": service._agent._clean_stream_buffer(self.buffer)})
                self.buffer = ""

        def __call__(self, event: dict[str, Any]) -> None:
            kind = event.get("event")
            if kind == "content":
                self._emit_content(self.dsml.feed(str(event.get("chunk", ""))))
                return
            if kind == "model_complete":
                self._emit_content(self.dsml.feed("", final=True))
                self._flush()
                return
            self.sink(event)

        def _flush(self) -> None:
            if not self.buffer:
                return
            text = self.buffer
            self.buffer = ""
            if service._agent.normalize_provider_control(text, plan_mode=True):
                text = ""
            elif service._agent._collect_tool_calls(text) or any(
                    text.lstrip().lower().startswith(prefix.lower()) for prefix in self.prefixes
            ):
                text = service._agent._clean_final_response(text)
            if text:
                self.sink({"event": "content", "chunk": text})

    events: queue.Queue[dict[str, Any]] = queue.Queue()
    projection = StreamProjection(events.put)

    def run_streaming_turn() -> None:
        callback_token = service._agent._stream_event_callback.set(projection)
        try:
            reply = immediate_reply if immediate_reply is not None else service._run_session_chat(session, msg)
            if str(reply).startswith("[LLM Error]"):
                events.put({"event": "error", "error": str(reply)})
            else:
                events.put({"event": "complete", "reply": reply})
        except service._agent.ProviderUnavailableError as exc:
            events.put({
                "event": "error",
                "code": service._agent.PROVIDER_UNAVAILABLE_CODE,
                "error": str(exc),
            })
        except Exception as exc:
            events.put({"event": "error", "error": str(exc)})
        finally:
            service._agent._stream_event_callback.reset(callback_token)

    async def generate():
        # The synchronous agent runs in a dedicated worker and safely drains if
        # a client disconnects; it never blocks the ASGI event loop.
        threading.Thread(target=run_streaming_turn, daemon=True).start()
        while True:
            event = await asyncio.to_thread(events.get)
            kind = event.get("event")
            if kind == "content":
                yield f"data: {json.dumps({'event': 'content', 'chunk': event.get('chunk', '')}, ensure_ascii=False)}\n\n"
            elif kind == "tool_receipt":
                yield f"data: {json.dumps({'event': 'tool_receipt', 'tool_receipt': event.get('tool_receipt')}, ensure_ascii=False)}\n\n"
            elif kind == "tasks":
                yield f"data: {json.dumps({'event': 'tasks', 'tasks': event.get('tasks', [])}, ensure_ascii=False)}\n\n"
            elif kind == "interaction":
                yield f"data: {json.dumps({'event': 'interaction', 'interaction': event.get('interaction', {})}, ensure_ascii=False)}\n\n"
            elif kind == "fast_decision":
                yield f"data: {json.dumps({'event': 'fast_decision', 'backend': event.get('backend', '')})}\n\n"
            elif kind == "error":
                yield f"data: {json.dumps({'event': 'error', 'code': event.get('code', 'stream_error'), 'error': event.get('error', 'stream failed')}, ensure_ascii=False)}\n\n"
                break
            elif kind == "complete":
                reply = str(event.get("reply", ""))
                session["interaction"] = service._interaction_for_session(session).envelope()
                if immediate_reply is not None:
                    yield f"data: {json.dumps({'event': 'content', 'chunk': reply}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'event': 'interaction', 'interaction': session['interaction']}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'event': 'usage', 'cost': service._cost_summary()})}\n\n"
                yield f"data: {json.dumps({'event': 'context', 'context': session.get('context')}, ensure_ascii=False)}\n\n"
                if session.get("last_memory_receipt"):
                    yield f"data: {json.dumps({'event': 'memory_receipt', 'memory_receipt': session['last_memory_receipt']}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'event': 'completion', 'status': 'completed'})}\n\n"
                yield "data: [DONE]\n\n"
                service._emit_chat_completed(session, reply, streamed=True)
                service._audit("REPLY_STREAM", f"len={len(reply)}", session["user_id"])
                break

    return StreamingResponse(generate(), media_type="text/event-stream")
