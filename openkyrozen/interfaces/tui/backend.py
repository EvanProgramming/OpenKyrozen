from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import threading
import uuid
from collections.abc import Mapping
from typing import Any
from rich.console import Console
from .protocol import (PROTOCOL_VERSION, MAX_LINE_BYTES, MAX_TEXT_CHARS, MAX_ARGS_CHARS, MAX_REQUEST_ID_CHARS, APPROVAL_TIMEOUT_SECONDS, MAX_ATTACHMENTS, MAX_ATTACHMENT_BYTES, _SENSITIVE_KEY_RE, _OUTPUT_LOCK, _redact, _safe_json)

class Backend:
    def __init__(self, application=None) -> None:
        from openkyrozen.app.bootstrap import build_application
        self._owns_application = application is None
        self.application = application if application is not None else build_application(surface="tui")
        self.agent = self.application.runtime
        runtime = self.agent
        self._output = sys.stdout
        self._quiet_stdout = io.StringIO()
        self._quiet_stderr = io.StringIO()
        # Rich and legacy print calls stay out of the JSONL channel.
        self._original_console = runtime.console
        self._original_bg_console = runtime.bg_console
        runtime.console = Console(file=self._quiet_stdout, force_terminal=False, color_system=None)
        runtime.bg_console = Console(file=self._quiet_stderr, force_terminal=False, color_system=None)
        self._state_lock = threading.Lock()
        self._busy = False
        self._started = False
        self._stopping = threading.Event()
        self._workers: set[threading.Thread] = set()
        self._pending_approvals: dict[str, tuple[threading.Event, dict[str, bool]]] = {}
        self._approval_lock = threading.Lock()
        self._staged_attachments: list[dict[str, Any]] = []
        self._onboarding_kind = ""
        self._active_session_id = ""


    def emit(self, event: str, request_id: str | None = None, **payload: Any) -> None:
        if event == "agents":
            agents, detail = payload.pop("agents", []), payload.pop("detail", None)
            self.emit("agents_reset", request_id, **payload)
            for agent in agents:
                self.emit("subagent", request_id, agent=agent, **payload)
            if detail:
                text = json.dumps(_safe_json(detail), ensure_ascii=False, indent=2)
                for offset in range(0, len(text), 8000):
                    self.emit("agent_detail", request_id, run_id=detail["run_id"],
                              reset=offset == 0, text=text[offset:offset+8000], **payload)
            return
        message: dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "event": event,
            "request_id": request_id,
        }
        message.update(_safe_json(payload))
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            line = json.dumps({
                "v": PROTOCOL_VERSION,
                "event": "error",
                "request_id": request_id,
                "code": "event_too_large",
                "error": "Backend event exceeded the size limit.",
            }, separators=(",", ":"))
        with _OUTPUT_LOCK:
            self._output.write(line + "\n")
            self._output.flush()


    def status(self, state: str, message: str = "", request_id: str | None = None) -> None:
        self.emit("status", request_id, state=state, message=message, busy=self._busy)


    def usage(self, request_id: str | None = None) -> None:
        runtime = self.agent
        """Project the durable workspace usage summary without exposing content."""
        try:
            totals = runtime.memory_bank.store.usage_totals(
                user_id=runtime.memory_bank.user_id,
                workspace_id=runtime.memory_bank.workspace_id,
            )
            self.emit("usage", request_id, scope="workspace", attempts=totals["attempts"],
                      authoritative_attempts=totals["authoritative_attempts"],
                      estimated_attempts=totals["estimated_attempts"],
                      unknown_attempts=totals["unknown_attempts"],
                      prompt_tokens=totals["prompt_tokens"],
                      completion_tokens=totals["completion_tokens"],
                      reasoning_tokens=totals["reasoning_tokens"],
                      cost_picos=totals["cost_picos"])
        except Exception:
            # Usage is presentation-only; an unavailable ledger must not break chat.
            return


    def interaction(self, request_id: str | None = None) -> None:
        runtime = self.agent
        envelope = runtime.interaction_envelope()
        envelope["decision_assist"] = runtime.decision_assist_state()
        self.emit("interaction", request_id, interaction=envelope)


    def graph_state(self, request_id: str | None = None, **extra: Any) -> None:
        runtime = self.agent
        state = runtime.project_graph_snapshot()
        self.emit("graph_state", request_id, graph={**state, **extra})


    def _quiet_call(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        with contextlib.redirect_stdout(self._quiet_stdout), contextlib.redirect_stderr(self._quiet_stderr):
            return function(*args, **kwargs)


    def dispatch(self, payload: dict[str, Any]) -> None:
        command = payload.get("command")
        request_id = payload.get("request_id") or uuid.uuid4().hex
        if not isinstance(request_id, str) or len(request_id) > MAX_REQUEST_ID_CHARS:
            request_id = uuid.uuid4().hex
        if command == "start":
            self.start(payload, request_id)
        elif command == "submit":
            text = payload.get("text", payload.get("message", ""))
            if isinstance(text, str):
                self.submit(text, request_id)
        elif command == "command":
            name = payload.get("name", payload.get("value", ""))
            if isinstance(name, str):
                self._command(name, payload.get("args", ""), request_id)
        elif command == "approval_response":
            self.approval_response(payload)
        elif command == "question_response":
            self._question_response(payload, request_id)
        elif command == "plan_action":
            self._plan_action(payload, request_id)
        elif command == "graph_request":
            self._graph_request(payload, request_id)
        elif command == "navigate":
            action = payload.get("action")
            if action == "switch" and isinstance(payload.get("scope_id"), str) and isinstance(payload.get("session_id"), str):
                self._switch_chat(payload["scope_id"], payload["session_id"], request_id)
            elif action in {"delete_chat", "delete_project"} and isinstance(payload.get("scope_id"), str):
                self._delete_navigation_item(action, payload, request_id)
            elif action == "new":
                self._command("new", "", request_id)
            elif action == "project" and isinstance(payload.get("path"), str):
                self._command("project", payload["path"], request_id)
            else:
                self.emit("error", request_id, code="invalid_navigation", error="Invalid navigation action.")
        elif command == "shutdown":
            self.stop()


    def stop(self) -> None:
        runtime = self.agent
        if self._stopping.is_set():
            return
        self._stopping.set()
        coordinator = getattr(runtime.subagent_manager, "coordinator", None)
        if coordinator:
            coordinator.close()
        with self._approval_lock:
            for waiter, decision in self._pending_approvals.values():
                decision["approved"] = False
                waiter.set()
            self._pending_approvals.clear()
        self.emit("exit", message="OpenKyrozen stopped.")
        runtime.console = self._original_console
        runtime.bg_console = self._original_bg_console
        if self._owns_application:
            self.application.close()


    @staticmethod
    def validate(payload: Any) -> tuple[dict[str, Any] | None, str | None]:
        if not isinstance(payload, dict):
            return None, "JSON object required."
        command = payload.get("command")
        if command not in {"start", "submit", "command", "approval_response", "question_response", "plan_action", "graph_request", "navigate", "shutdown"}:
            return None, "Unknown backend command."
        request_id = payload.get("request_id")
        if request_id is not None and (not isinstance(request_id, str) or len(request_id) > MAX_REQUEST_ID_CHARS):
            return None, "Invalid request_id."
        if command in {"submit", "command"}:
            key = "text" if command == "submit" else "name"
            if not isinstance(payload.get(key), str) or len(payload[key]) > (MAX_TEXT_CHARS if command == "submit" else MAX_ARGS_CHARS):
                return None, f"{key} is missing or too long."
        if command == "approval_response":
            if not isinstance(payload.get("request_id"), str):
                return None, "request_id is required."
            if not isinstance(payload.get("approved"), bool):
                return None, "approved must be boolean."
        if command == "question_response" and not isinstance(payload.get("question_response", payload), Mapping):
            return None, "question_response must be an object."
        if command == "plan_action":
            if payload.get("action") not in {"accept", "revise", "cancel"}:
                return None, "plan action must be accept, revise, or cancel."
            if (not isinstance(payload.get("plan_id"), str)
                    or not isinstance(payload.get("version"), int)
                    or isinstance(payload.get("version"), bool)):
                return None, "plan_id and integer version are required."
        if command == "graph_request":
            if payload.get("action") not in {"snapshot", "search", "neighbors", "path", "refresh"}:
                return None, "invalid graph action."
        try:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            return None, "Message is not JSON serializable."
        if len(encoded) > MAX_LINE_BYTES:
            return None, "Message is too large."
        return payload, None

    from .navigation import (_new_chat_id, _register_chat, _chat_title, _scope_chats, _navigation_groups, navigation, _bind_chat, _switch_chat, _emit_bound_state, _delete_navigation_item, _delete_navigation_item_locked, _trash_project, _trash_vectors, _history_path, _remove_tree)

    from .onboarding import (start, _check_for_update, prompt_api_key, prompt_provider, prompt_model,
                             configure_main_model, _prompt_onboarding_learning, _complete_onboarding,
                             _continue_onboarding, configure_provider, custom_provider_command,
                             custom_provider_input_prompt, custom_provider_input)

    from .turns import (set_api_key, _approval, approval_response, _stream_projection, submit, _attach, _attachment_prompt, _run_submit, _features)

    from .commands import (_command, _learning_text)

    from .controls import (_memory_text, _question_response, _plan_action, _graph_request)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kyrozen-backend", description="OpenKyrozen Bubble Tea JSONL backend")
    parser.add_argument("--project")
    parser.add_argument("--global", dest="global_mode", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    backend = Backend()
    if args.project or args.global_mode:
        backend.dispatch({
            "command": "start", "request_id": uuid.uuid4().hex,
            "project": args.project, "global": args.global_mode,
        })
    for raw_line in sys.stdin.buffer:
        if backend._stopping.is_set():
            break
        if len(raw_line) > MAX_LINE_BYTES:
            backend.emit("error", code="message_too_large", error="Backend message is too large.")
            continue
        try:
            payload = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            backend.emit("error", code="invalid_json", error="Malformed JSONL message.")
            continue
        payload, error = backend.validate(payload)
        if error:
            backend.emit("error", code="invalid_message", error=error)
            continue
        backend.dispatch(payload)
        if backend._stopping.is_set():
            break
    backend.stop()


if __name__ == "__main__":
    main()
