from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from rich.console import Console
from .protocol import (PROTOCOL_VERSION, MAX_LINE_BYTES, MAX_TEXT_CHARS, MAX_ARGS_CHARS, MAX_REQUEST_ID_CHARS, APPROVAL_TIMEOUT_SECONDS, MAX_ATTACHMENTS, MAX_ATTACHMENT_BYTES, _SENSITIVE_KEY_RE, _OUTPUT_LOCK, _redact, _safe_json)

def _memory_text(self, args: str) -> str:
    runtime = self.agent
    parts = args.split(maxsplit=1)
    if len(parts) == 2 and parts[0].lower() in {"why", "forget"}:
        claim = parts[1].strip()
        if parts[0].lower() == "why":
            value = runtime.learning_engine.explain_claim(claim)
            return json.dumps(value, ensure_ascii=False, indent=2, default=str) if value else "Memory claim not found."
        return "Memory claim forgotten." if runtime.learning_engine.forget_claim(claim) else "Memory claim not found."
    return "Usage: /memory why|forget <claim-id>"


def _question_response(self, payload: dict[str, Any], request_id: str) -> None:
    runtime = self.agent
    response = payload.get("question_response", payload)
    if not isinstance(response, Mapping):
        return
    try:
        pending = runtime._interaction_controller.state().get("pending_question")
        resolved = runtime.resolve_interaction_question(
            str(response.get("request_id") or ""), response.get("answers", {}),
            action=str(response.get("action") or "answer"),
        )
        action = str(response.get("action") or "answer")
        if action == "cancel":
            self.emit("response", request_id, text="Pending question cancelled.")
            self.interaction(request_id)
            return
        question = resolved["question"] if pending else {}
        text = (f"Original request:\n{question.get('original_input', '')}\n\n"
                f"Clarification {action}:\n{json.dumps(resolved['answers'], ensure_ascii=False)}")
        self.submit(text, request_id)
    except runtime.InteractionError as exc:
        self.emit("error", request_id, code="question_not_pending", error=str(exc))


def _plan_action(self, payload: dict[str, Any], request_id: str) -> None:
    runtime = self.agent
    action = str(payload.get("action") or "").lower()
    plan_id = str(payload.get("plan_id") or "") or None
    version = payload.get("version") if isinstance(payload.get("version"), int) and not isinstance(payload.get("version"), bool) else None
    if version is None or version < 1:
        self.emit("error", request_id, code="invalid_plan_action", error="plan version must be a positive integer")
        return
    plan = runtime._interaction_controller.state().get("pending_plan")
    if action == "accept" and not plan and runtime._interaction_controller.accepted_plan(plan_id, version):
        self.emit("response", request_id, text="Plan was already accepted.")
        self.interaction(request_id)
        return
    if not plan or (plan_id and plan_id != plan.get("plan_id")) or (version is not None and version != plan.get("version")):
        self.emit("error", request_id, code="plan_not_pending", error="plan is not pending at the requested version")
        return
    if action == "accept":
        self.submit("accept plan", request_id)
    elif action == "cancel":
        runtime.cancel_interaction_plan(plan_id, version)
        self.emit("response", request_id, text="Pending plan cancelled.")
        self.interaction(request_id)
    elif action == "revise" and isinstance(payload.get("text"), str) and payload["text"].strip():
        self.submit(payload["text"], request_id)
    else:
        self.emit("error", request_id, code="invalid_plan_action", error="plan action must be accept, revise, or cancel")


def _graph_request(self, payload: dict[str, Any], request_id: str) -> None:
    runtime = self.agent
    graph = runtime._project_graph
    if graph is None:
        self.emit("error", request_id, code="graph_unavailable", error="Project graph is not configured.")
        return
    action = str(payload.get("action") or "snapshot")
    if action == "snapshot":
        community = payload.get("community")
        self.emit("graph_state", request_id, graph=graph.explore(
            community=community if isinstance(community, int) and not isinstance(community, bool) else None,
            limit=30,
        ))
    elif action == "search":
        self.emit("graph_state", request_id, graph=graph.explore(query=str(payload.get("query") or "")[:200], limit=30))
    elif action == "neighbors":
        self.emit("graph_state", request_id, graph=graph.explore(node_id=str(payload.get("node_id") or "")[:200], limit=30))
    elif action == "path":
        detail = graph.path(str(payload.get("left") or "")[:200], str(payload.get("right") or "")[:200])
        self.emit("graph_state", request_id, graph=graph.explore(limit=30) | {"detail": detail})
    elif action == "refresh":
        full = bool(payload.get("full", False))
        previous = graph.explore(limit=30)
        started = graph.refresh_async(full=full, callback=lambda _state: self.emit(
            "graph_state", request_id, graph=graph.explore(limit=30),
        ))
        self.emit("graph_state", request_id, graph=previous | {
            "status": "indexing" if started else previous.get("status", "indexing")
        })
