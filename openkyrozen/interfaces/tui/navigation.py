from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rich.console import Console
from .protocol import (PROTOCOL_VERSION, MAX_LINE_BYTES, MAX_TEXT_CHARS, MAX_ARGS_CHARS, MAX_REQUEST_ID_CHARS, APPROVAL_TIMEOUT_SECONDS, MAX_ATTACHMENTS, MAX_ATTACHMENT_BYTES, _SENSITIVE_KEY_RE, _OUTPUT_LOCK, _redact, _safe_json)

@staticmethod
def _new_chat_id() -> str:
    return f"chat-{uuid.uuid4().hex}"


def _register_chat(self, session_id: str, *, title: str = "New chat") -> None:
    runtime = self.agent
    context = runtime.get_launch_context()
    if context is None:
        return
    store = runtime.memory_bank.store
    if not context.is_global:
        store.append_event(
            "tui.project_opened",
            {"path": str(context.active_root), "name": context.active_root.name,
             "source_scope_id": context.source_scope_id},
            user_id=runtime.memory_bank.user_id,
            workspace_id=runtime.memory_bank.workspace_id,
        )
    store.append_event(
        "tui.chat_metadata",
        {"title": title, "scope": context.mode, "path": str(context.active_root)},
        user_id=runtime.memory_bank.user_id,
        workspace_id=runtime.interaction_workspace_id(context),
        session_id=session_id,
    )


def _chat_title(self, workspace_id: str, session: dict[str, Any]) -> str:
    runtime = self.agent
    metadata = runtime.memory_bank.store.list_events(
        "tui.chat_metadata", limit=1, workspace_id=workspace_id,
        session_id=str(session["session_id"]), user_id=runtime.memory_bank.user_id,
    )
    if metadata:
        title = str(metadata[0]["payload"].get("title") or "").strip()
        if title and title != "New chat":
            return title
    first = str(session.get("user_message") or "").strip()
    if not first:
        for item in session.get("conversation") or []:
            if isinstance(item, Mapping) and item.get("role") == "user":
                first = str(item.get("content") or "").strip()
                if first:
                    break
    return re.sub(r"\s+", " ", first)[:72] or "New chat"


def _scope_chats(self, workspace_id: str) -> list[dict[str, Any]]:
    runtime = self.agent
    store = runtime.memory_bank.store
    by_id = {
        str(item["session_id"]): item
        for item in store.list_history_sessions(
            user_id=runtime.memory_bank.user_id, workspace_id=workspace_id, limit=200,
        )
    }
    for item in store.list_sessions(
            user_id=runtime.memory_bank.user_id, workspace_id=workspace_id, limit=200):
        by_id.setdefault(str(item["session_id"]), item)
    chats = []
    for session_id, item in by_id.items():
        if session_id.startswith("chat-") or session_id == "surface:tui":
            chats.append({
                "session_id": session_id,
                "title": self._chat_title(workspace_id, item),
                "updated_at": str(item.get("updated_at") or ""),
            })
    return sorted(chats, key=lambda item: item["updated_at"], reverse=True)


def _navigation_groups(self) -> list[dict[str, Any]]:
    runtime = self.agent
    store = runtime.memory_bank.store
    groups = [{
        "scope": "global", "scope_id": runtime.memory_bank.workspace_id,
        "name": "No Project", "path": "",
        "chats": self._scope_chats(runtime.memory_bank.workspace_id),
    }]
    projects: dict[str, dict[str, str]] = {}
    for event in store.list_events(
            "tui.project_opened", limit=1000, workspace_id=runtime.memory_bank.workspace_id,
            user_id=runtime.memory_bank.user_id):
        payload = event["payload"]
        scope_id, path = str(payload.get("source_scope_id") or ""), str(payload.get("path") or "")
        if scope_id and path and scope_id not in projects:
            projects[scope_id] = {"path": path, "name": str(payload.get("name") or Path(path).name)}
    for scope_id, project in projects.items():
        groups.append({
            "scope": "project", "scope_id": scope_id, "name": project["name"],
            "path": project["path"], "chats": self._scope_chats(scope_id),
        })
    return groups


def navigation(self, request_id: str | None = None) -> None:
    runtime = self.agent
    context = runtime.get_launch_context()
    messages = []
    for item in runtime.short_term_memory:
        if not isinstance(item, Mapping) or item.get("role") not in {"user", "assistant"}:
            continue
        text = item.get("content")
        if isinstance(text, str) and text.strip():
            messages.append({"role": item["role"], "text": text})
    self.emit(
        "navigation", request_id,
        active_session_id=self._active_session_id,
        active_scope_id=runtime.interaction_workspace_id(context),
        scope=context.mode if context else "global",
        workspace=str(runtime._get_workspace_root()),
        groups=self._navigation_groups(), messages=messages,
    )


def _bind_chat(self, session_id: str, *, project_path: str | None = None,
               global_mode: bool = False, create: bool = False) -> None:
    runtime = self.agent
    context = self._quiet_call(
        runtime.configure_launch_context, project_path=project_path, global_mode=global_mode,
    )
    self._quiet_call(runtime.bind_interaction_scope, session_id)
    self._quiet_call(runtime._plugin_runtime_for_surface().load_once)
    self._active_session_id = session_id
    self._staged_attachments.clear()
    if create:
        self._register_chat(session_id)
    graph = runtime._project_graph
    if graph is not None:
        graph.refresh_async(callback=lambda _state: self.graph_state())
    return context


def _switch_chat(self, scope_id: str, session_id: str, request_id: str) -> None:
    with self._state_lock:
        if self._busy:
            self.emit("error", request_id, code="busy", error="Cannot switch chats while a turn is running.")
            return
        target = next((group for group in self._navigation_groups()
                       if group["scope_id"] == scope_id), None)
        if target is None or not any(chat["session_id"] == session_id for chat in target["chats"]):
            self.emit("error", request_id, code="unknown_session", error="Unknown chat for this project.")
            return
        try:
            self._bind_chat(
                session_id, project_path=target["path"] or None,
                global_mode=target["scope"] == "global",
            )
        except (OSError, ValueError) as exc:
            self.emit("error", request_id, code="session_switch_failed", error=str(exc))
            return
    self._emit_bound_state(request_id)


def _emit_bound_state(self, request_id: str) -> None:
    runtime = self.agent
    context = runtime.get_launch_context()
    self.navigation(request_id)
    self.emit("tasks", request_id, tasks=[
        {"id": item["id"], "description": item["description"], "status": item["status"]}
        for item in runtime.tasks.tasks
    ])
    self.interaction(request_id)
    self.graph_state(request_id)
    self.emit("ready", request_id, configured=runtime.llm_provider is not None,
              provider=getattr(runtime._provider_config, "provider", ""),
              model=getattr(runtime._provider_config, "model_simple", ""),
              workspace=str(context.active_root), mode=context.mode,
              session_id=self._active_session_id)
    self.usage(request_id)
