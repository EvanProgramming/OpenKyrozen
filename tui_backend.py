"""JSONL backend for the Bubble Tea terminal client.

The Python agent remains the source of truth.  This process only translates
bounded JSON messages into existing agent calls and progress events.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
import threading
import uuid
from collections.abc import Mapping
from typing import Any

os.environ.setdefault("KYROZEN_EXECUTION_SURFACE", "tui")
os.environ.setdefault("KYROZEN_TUI_CAPABILITIES", "full")

# Keep imports and legacy Rich initialization out of the machine-readable
# channel too; a plugin should never be able to corrupt the JSONL protocol.
_IMPORT_SINK = io.StringIO()
with contextlib.redirect_stdout(_IMPORT_SINK), contextlib.redirect_stderr(_IMPORT_SINK):
    import main as agent
from rich.console import Console


PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 64 * 1024
MAX_TEXT_CHARS = 12_000
MAX_ARGS_CHARS = 4_000
MAX_REQUEST_ID_CHARS = 100
APPROVAL_TIMEOUT_SECONDS = 15 * 60
_SENSITIVE_KEY_RE = re.compile(r"(?i)(api[_-]?key|secret|password|token)")

_OUTPUT_LOCK = threading.Lock()


def _redact(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    text = str(value if value is not None else "").replace("\x00", "").replace("\r", " ")
    text = re.sub(
        r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>", text,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "sk-<redacted>", text)
    return text[:limit]


def _safe_json(value: Any) -> Any:
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if _SENSITIVE_KEY_RE.search(str(key)) else _safe_json(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact(value)


class Backend:
    def __init__(self) -> None:
        self._output = sys.stdout
        self._quiet_stdout = io.StringIO()
        self._quiet_stderr = io.StringIO()
        # Rich and legacy print calls stay out of the JSONL channel.
        self._original_console = agent.console
        self._original_bg_console = agent.bg_console
        agent.console = Console(file=self._quiet_stdout, force_terminal=False, color_system=None)
        agent.bg_console = Console(file=self._quiet_stderr, force_terminal=False, color_system=None)
        self._state_lock = threading.Lock()
        self._busy = False
        self._started = False
        self._stopping = threading.Event()
        self._workers: set[threading.Thread] = set()
        self._pending_approvals: dict[str, tuple[threading.Event, dict[str, bool]]] = {}
        self._approval_lock = threading.Lock()

    def emit(self, event: str, request_id: str | None = None, **payload: Any) -> None:
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

    def interaction(self, request_id: str | None = None) -> None:
        self.emit("interaction", request_id, interaction=agent.interaction_envelope())

    def _quiet_call(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        with contextlib.redirect_stdout(self._quiet_stdout), contextlib.redirect_stderr(self._quiet_stderr):
            return function(*args, **kwargs)

    def start(self, payload: dict[str, Any], request_id: str) -> None:
        if self._started:
            self.emit("ready", request_id, configured=agent.llm_provider is not None,
                      provider=getattr(agent._provider_config, "provider", ""),
                      model=getattr(agent._provider_config, "model_simple", ""),
                      workspace=str(agent._get_workspace_root()))
            return
        self._started = True
        project = payload.get("project")
        global_mode = bool(payload.get("global", not project))
        try:
            self.status("starting", "Preparing the workspace…", request_id)
            context = self._quiet_call(
                agent.configure_launch_context,
                project_path=project if isinstance(project, str) and project.strip() else None,
                global_mode=global_mode,
            )
            self._quiet_call(agent.bind_interaction_scope, "surface:tui")
            config = self._quiet_call(agent.detect_provider)
            self.status("starting", "Connecting provider…", request_id)
            configured = bool(self._quiet_call(
                agent._prompt_and_init_deepseek, interactive=False, config=config,
            ))
            self._quiet_call(agent._plugin_runtime_for_surface().load_once)
            self.status("starting", "Restoring tasks and memory…", request_id)
            task_results = self._quiet_call(agent._run_recovered_tasks)
            self._quiet_call(agent._load_project_files_into_memory)
            if configured and not self._quiet_call(agent._ensure_detached_learning_worker):
                threading.Thread(target=agent._background_learning_loop, daemon=True).start()
            self.emit("tasks", request_id, tasks=[
                {"id": item["id"], "description": item["description"], "status": item["status"]}
                for item in agent.tasks.tasks
            ])
            self.emit(
                "ready", request_id, configured=configured,
                provider=getattr(config, "provider", ""),
                model=getattr(config, "model_simple", ""),
                workspace=str(context.active_root),
                mode="global" if context.is_global else "project",
                recovered=len(task_results or []),
            )
            if not configured and getattr(config, "provider", "") != "ollama":
                self.prompt_api_key(request_id=request_id)
            self.interaction(request_id)
            self.status("ready", "Ready", request_id)
        except Exception as exc:
            self.emit("error", request_id, code="startup_failed",
                      error=f"{type(exc).__name__}: {_redact(exc)}")

    def prompt_api_key(self, request_id: str | None = None) -> None:
        config = agent._provider_config or self._quiet_call(agent.detect_provider)
        provider = getattr(config, "provider", "deepseek")
        self.emit(
            "prompt", request_id, kind="api_key", masked=True, provider=provider,
            env_var=agent.PROVIDER_ENV_VARS.get(provider, ""),
            message=f"Enter the {provider.title()} API key. It is stored encrypted locally.",
        )

    def prompt_provider(self, request_id: str | None = None) -> None:
        current = getattr(agent._provider_config, "provider", "deepseek")
        self.emit(
            "prompt", request_id, kind="provider", current=current,
            providers=[
                {"name": name, "model": models[0], "local": name == "ollama"}
                for name, models in agent.PROVIDER_DEFAULT_MODELS.items()
            ],
        )

    def configure_provider(self, provider: str, api_key: str | None = None,
                           request_id: str | None = None) -> None:
        provider = provider.strip().lower()
        if provider not in agent.PROVIDER_DEFAULT_MODELS:
            self.emit("error", request_id, code="invalid_provider", error="Unknown provider.")
            return
        current = agent._provider_config or self._quiet_call(agent.detect_provider)
        config = agent.ProviderConfig(
            provider=provider,
            api_key=(api_key if api_key is not None else current.api_key),
        )
        if api_key is None and provider != current.provider:
            config.api_key = ""
        try:
            if config.api_key or provider == "ollama":
                self._quiet_call(agent.save_provider_config_encrypted, config)
            configured = bool(self._quiet_call(
                agent._prompt_and_init_deepseek, interactive=False, config=config,
            ))
            self.emit(
                "ready", request_id, configured=configured, provider=provider,
                model=config.model_simple, workspace=str(agent._get_workspace_root()),
            )
            if not configured and provider != "ollama":
                self.prompt_api_key(request_id=request_id)
            else:
                self.status("ready", f"Using {provider.title()}.", request_id)
        except Exception as exc:
            self.emit("error", request_id, code="provider_setup_failed",
                      error=f"{type(exc).__name__}: {_redact(exc)}")

    def set_api_key(self, api_key: str, request_id: str | None = None) -> None:
        if not api_key.strip():
            self.emit("error", request_id, code="empty_api_key", error="No API key entered.")
            return
        config = agent._provider_config or self._quiet_call(agent.detect_provider)
        config.api_key = api_key.strip()
        try:
            self._quiet_call(agent.save_provider_config_encrypted, config)
            configured = bool(self._quiet_call(
                agent._prompt_and_init_deepseek, interactive=False, config=config,
            ))
            self.emit("ready", request_id, configured=configured,
                      provider=config.provider, model=config.model_simple,
                      workspace=str(agent._get_workspace_root()))
            self.status("ready" if configured else "waiting", "Provider configured." if configured else "Provider unavailable.", request_id)
        except Exception as exc:
            self.emit("error", request_id, code="api_key_setup_failed",
                      error=f"{type(exc).__name__}: {_redact(exc)}")

    def _approval(self, action: str, args: str) -> bool:
        approval_id = uuid.uuid4().hex
        waiter = threading.Event()
        decision = {"approved": False}
        with self._approval_lock:
            self._pending_approvals[approval_id] = (waiter, decision)
        self.emit(
            "prompt", kind="approval", request_id=approval_id,
            masked=True, action=action, args=_redact(args, 600),
            message="This action may change local or remote state.",
        )
        waiter.wait(timeout=APPROVAL_TIMEOUT_SECONDS)
        with self._approval_lock:
            self._pending_approvals.pop(approval_id, None)
        return bool(decision["approved"] and not self._stopping.is_set())

    def approval_response(self, payload: dict[str, Any]) -> None:
        approval_id = payload.get("request_id") or payload.get("id")
        if not isinstance(approval_id, str) or len(approval_id) > MAX_REQUEST_ID_CHARS:
            return
        with self._approval_lock:
            pending = self._pending_approvals.get(approval_id)
        if pending is None:
            return
        pending[1]["approved"] = bool(payload.get("approved", False))
        pending[0].set()

    def _stream_projection(self, request_id: str):
        dsml = agent.DeepSeekDSMLFilter()
        buffer = ""
        prefixes = ("action:", "tasklist:", "taskdone:", "thought:", "plan:", "definetool:",
                    "askuser:", "askuser\n", "planproposal:", "planproposal\n", "<")

        def emit_text(text: str) -> None:
            nonlocal buffer
            if not text:
                return
            buffer += text
            candidate = buffer.lstrip().lower()
            if any(prefix.startswith(candidate) or candidate.startswith(prefix) for prefix in prefixes):
                return
            self.emit("stream_delta", request_id, text=_redact(buffer))
            buffer = ""

        def callback(event: dict[str, Any]) -> None:
            nonlocal buffer
            kind = event.get("event")
            if kind == "content":
                emit_text(dsml.feed(str(event.get("chunk", ""))))
            elif kind == "model_complete":
                emit_text(dsml.feed("", final=True))
                if buffer:
                    cleaned = agent._clean_final_response(buffer)
                    if cleaned:
                        self.emit("stream_delta", request_id, text=_redact(cleaned))
                    buffer = ""
            elif kind == "tool_receipt":
                self.emit("tool_receipt", request_id, receipt=event.get("tool_receipt", {}))
            elif kind == "tasks":
                self.emit("tasks", request_id, tasks=event.get("tasks", []))
            elif kind == "interaction":
                self.emit("interaction", request_id, interaction=event.get("interaction", {}))

        return callback

    def submit(self, text: str, request_id: str) -> None:
        with self._state_lock:
            if self._busy:
                self.emit("error", request_id, code="busy", error="A turn is already running.")
                return
            self._busy = True
        worker = threading.Thread(target=self._run_submit, args=(text, request_id), daemon=True)
        self._workers.add(worker)
        worker.start()

    def _run_submit(self, text: str, request_id: str) -> None:
        stream_token = agent._stream_event_callback.set(self._stream_projection(request_id))
        approval_token = agent._approval_callback.set(self._approval)
        try:
            if agent.llm_provider is None:
                self.prompt_api_key(request_id=request_id)
                self.status("waiting", "Provider setup required.", request_id)
                return
            sanitized, flagged = self._quiet_call(agent._sanitize_input, text)
            if flagged:
                self.emit("status", request_id, state="warning",
                          message="Prompt injection text was filtered.", busy=True)
            state = agent.interaction_envelope(text)
            if not state["pending_question"] and not state["pending_plan"] and not agent.is_plan_acceptance(text):
                agent.tasks.clear()
            self.status("thinking", "Thinking…", request_id)
            reply = self._quiet_call(
                agent._chat_turn, sanitized,
                clear_tasks=not bool(state["pending_question"] or state["pending_plan"]
                                     or agent.is_plan_acceptance(sanitized)),
            )
            reply = agent._clean_final_response(reply)
            if len(reply.strip()) < 1:
                self.emit("error", request_id, code="empty_response", error="The provider returned no answer.")
                return
            agent.short_term_memory.extend([
                {"role": "user", "content": text},
                {"role": "assistant", "content": reply},
            ])
            self._quiet_call(agent.memory_bank.add_log, f"User: {text}\nAssistant: {reply}")
            thinking, answer = agent._split_reply(reply)
            if thinking:
                self.emit("thinking", request_id, text=thinking)
            self.emit("response", request_id, text=answer or "(no content)")
            self.emit("tasks", request_id, tasks=[
                {"id": task["id"], "description": task["description"], "status": task["status"]}
                for task in agent.tasks.tasks
            ])
            self.interaction(request_id)
            self.status("ready", "Ready", request_id)
        except agent.ProviderUnavailableError as exc:
            self.emit("error", request_id, code=agent.PROVIDER_UNAVAILABLE_CODE,
                      error=_redact(exc))
        except Exception as exc:
            self.emit("error", request_id, code="turn_failed",
                      error=f"{type(exc).__name__}: {_redact(exc)}")
        finally:
            agent._approval_callback.reset(approval_token)
            agent._stream_event_callback.reset(stream_token)
            with self._state_lock:
                self._busy = False
                self._workers.discard(threading.current_thread())

    def _features(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "enabled": bool(agent._SELF_LEARNING_FLAGS.get(name, True)),
             "description": agent._LEARNING_FEATURE_REGISTRY[name]["description"]}
            for name in agent._LEARNING_FEATURE_ORDER
        ]

    def _command(self, name: str, args: Any, request_id: str) -> None:
        raw = name.strip()
        if raw.startswith("/"):
            parts = raw.split(maxsplit=2)
            command = parts[0].lower()
            arg_text = " ".join(parts[1:])
        else:
            command = raw.lower().replace("-", "_")
            arg_text = args if isinstance(args, str) else ""
        if command in {"/quit", "/exit", "quit", "exit", "shutdown"}:
            self.stop()
        elif command in {"/provider", "provider"}:
            if isinstance(args, Mapping) and args.get("provider"):
                self.configure_provider(str(args["provider"]), args.get("api_key"), request_id)
            elif arg_text.strip() in agent.PROVIDER_DEFAULT_MODELS:
                self.configure_provider(arg_text.strip(), request_id=request_id)
            else:
                self.prompt_provider(request_id)
        elif command in {"/api_key", "api_key"}:
            if isinstance(args, Mapping) and isinstance(args.get("api_key"), str):
                self.set_api_key(args["api_key"], request_id)
            elif arg_text.strip():
                self.set_api_key(arg_text.strip(), request_id)
            else:
                self.prompt_api_key(request_id)
        elif command in {"/learn", "learn"}:
            self.status("learning", "Indexing workspace files…", request_id)
            self._quiet_call(agent._load_project_files_into_memory, force=True)
            self.emit("response", request_id, text="Project files re-learned and stored in memory.")
            self.status("ready", "Ready", request_id)
        elif command in {"/self-learning", "self_learning"}:
            if isinstance(args, Mapping) and args.get("feature") in agent._SELF_LEARNING_FLAGS:
                feature = str(args["feature"])
                enabled = bool(args.get("enabled", not agent._SELF_LEARNING_FLAGS[feature]))
                agent._SELF_LEARNING_FLAGS[feature] = enabled
                self._quiet_call(
                    agent.memory_bank.store.set_learning_feature_flag, feature, enabled,
                    user_id=agent.memory_bank.user_id, workspace_id=agent.memory_bank.workspace_id,
                )
                self.emit("response", request_id, text=f"{feature}: {'enabled' if enabled else 'disabled'}")
            else:
                self.emit("prompt", request_id, kind="self_learning", features=self._features())
        elif command in {"/tasks", "tasks"}:
            self.emit("tasks", request_id, tasks=[
                {"id": task["id"], "description": task["description"], "status": task["status"]}
                for task in agent.tasks.tasks
            ])
            self.emit("response", request_id, text=agent.tasks.format())
        elif command in {"/agent", "agent"}:
            profile = arg_text.strip().lower()
            if profile in {"auto", "coder", "researcher"}:
                agent._agent_profile_mode = profile
                self.emit("response", request_id, text=f"Agent profile set to {profile}.")
            else:
                self.emit("response", request_id, text=f"Agent profile: {agent._agent_profile_mode}")
        elif command in {"/ask", "ask"}:
            agent.set_interaction_mode("ask")
            self.emit("response", request_id, text="Interaction mode set to ask.")
            self.interaction(request_id)
        elif command in {"/mode", "mode"}:
            mode = arg_text.strip().lower()
            if not mode:
                self.emit("prompt", request_id, kind="mode", modes=["auto", "ask", "plan", "agent"],
                          selected=agent.interaction_envelope()["preference_mode"])
            else:
                try:
                    state = agent.set_interaction_mode(mode)
                    self.emit("response", request_id, text=f"Interaction mode set to {state['preference_mode']}.")
                    self.interaction(request_id)
                except agent.InteractionError as exc:
                    self.emit("error", request_id, code="invalid_mode", error=str(exc))
        elif command in {"/plan", "plan"}:
            action = arg_text.strip().lower()
            if not action:
                agent.set_interaction_mode("plan")
                self.emit("response", request_id, text="Interaction mode set to plan.")
                self.interaction(request_id)
            elif action == "accept":
                self.submit("accept plan", request_id)
            elif action == "cancel":
                try:
                    agent.cancel_interaction_plan()
                    self.emit("response", request_id, text="Pending plan cancelled.")
                    self.interaction(request_id)
                except agent.InteractionError as exc:
                    self.emit("error", request_id, code="plan_not_pending", error=str(exc))
            else:
                self.emit("error", request_id, code="invalid_plan_action", error="Usage: /plan | /plan accept|cancel")
        elif command in {"/question", "question"}:
            action = arg_text.strip().lower()
            try:
                if not action:
                    agent._interaction_controller.reopen_question()
                    self.interaction(request_id)
                elif action in {"skip", "cancel"}:
                    pending = agent._interaction_controller.state().get("pending_question")
                    if not pending:
                        raise agent.InteractionError("no question is pending")
                    agent.resolve_interaction_question(pending["request_id"], {}, action=action)
                    if action == "skip":
                        self.submit(
                            f"Original request:\n{pending.get('original_input', '')}\n\n"
                            "Clarification was skipped. Continue only if safe; otherwise explain the blocker.",
                            request_id,
                        )
                    else:
                        self.emit("response", request_id, text="Pending question cancelled.")
                        self.interaction(request_id)
                else:
                    raise agent.InteractionError("Usage: /question | /question skip|cancel")
            except agent.InteractionError as exc:
                self.emit("error", request_id, code="question_not_pending", error=str(exc))
        elif command in {"/update", "update"}:
            self.status("updating", "Updating OpenKyrozen…", request_id)
            result = self._quiet_call(agent._self_update)
            self.emit("response", request_id, text=result)
            self.status("ready", "Ready", request_id)
        elif command in {"/learning", "learning"}:
            self.emit("response", request_id, text=self._learning_text(arg_text))
        elif command in {"/memory", "memory"}:
            self.emit("response", request_id, text=self._memory_text(arg_text))
        elif command in {"/forget", "forget"}:
            self.emit("response", request_id, text=agent._forget_recent(arg_text.strip()))
        else:
            self.emit("error", request_id, code="unknown_command",
                      error=f"Unknown command: {raw[:120]}")

    def _learning_text(self, args: str) -> str:
        parts = args.split(maxsplit=1)
        subcommand = parts[0].lower() if parts else "status"
        argument = parts[1].strip() if len(parts) > 1 else ""
        if subcommand == "status":
            rows = agent.learning_engine.status(50, profile=argument if argument in {"coder", "researcher"} else None)
            return json.dumps(rows, ensure_ascii=False, indent=2, default=str) if rows else "No learning proposals."
        if subcommand == "metrics":
            return json.dumps(agent.learning_engine.metrics(argument or None), ensure_ascii=False, indent=2, default=str)
        if subcommand == "rollback" and argument:
            return "Learning proposal rolled back." if agent.learning_engine.rollback(argument) else "Proposal not found."
        if subcommand in {"explain", "evidence"} and argument:
            value = (agent.learning_engine.evidence_card(argument) if subcommand == "evidence"
                     else next((row for row in agent.learning_engine.status(1000) if row["id"] == argument), None))
            return json.dumps(value, ensure_ascii=False, indent=2, default=str) if value else "Proposal not found."
        return "Usage: /learning status | /learning metrics | /learning rollback <id> | /learning explain|evidence <id>"

    def _memory_text(self, args: str) -> str:
        parts = args.split(maxsplit=1)
        if len(parts) == 2 and parts[0].lower() in {"why", "forget"}:
            claim = parts[1].strip()
            if parts[0].lower() == "why":
                value = agent.learning_engine.explain_claim(claim)
                return json.dumps(value, ensure_ascii=False, indent=2, default=str) if value else "Memory claim not found."
            return "Memory claim forgotten." if agent.learning_engine.forget_claim(claim) else "Memory claim not found."
        return "Usage: /memory why|forget <claim-id>"

    def _question_response(self, payload: dict[str, Any], request_id: str) -> None:
        response = payload.get("question_response", payload)
        if not isinstance(response, Mapping):
            return
        try:
            pending = agent._interaction_controller.state().get("pending_question")
            resolved = agent.resolve_interaction_question(
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
        except agent.InteractionError as exc:
            self.emit("error", request_id, code="question_not_pending", error=str(exc))

    def _plan_action(self, payload: dict[str, Any], request_id: str) -> None:
        action = str(payload.get("action") or "").lower()
        plan_id = str(payload.get("plan_id") or "") or None
        version = payload.get("version") if isinstance(payload.get("version"), int) and not isinstance(payload.get("version"), bool) else None
        if version is None or version < 1:
            self.emit("error", request_id, code="invalid_plan_action", error="plan version must be a positive integer")
            return
        plan = agent._interaction_controller.state().get("pending_plan")
        if action == "accept" and not plan and agent._interaction_controller.accepted_plan(plan_id, version):
            self.emit("response", request_id, text="Plan was already accepted.")
            self.interaction(request_id)
            return
        if not plan or (plan_id and plan_id != plan.get("plan_id")) or (version is not None and version != plan.get("version")):
            self.emit("error", request_id, code="plan_not_pending", error="plan is not pending at the requested version")
            return
        if action == "accept":
            self.submit("accept plan", request_id)
        elif action == "cancel":
            agent.cancel_interaction_plan(plan_id, version)
            self.emit("response", request_id, text="Pending plan cancelled.")
            self.interaction(request_id)
        elif action == "revise" and isinstance(payload.get("text"), str) and payload["text"].strip():
            self.submit(payload["text"], request_id)
        else:
            self.emit("error", request_id, code="invalid_plan_action", error="plan action must be accept, revise, or cancel")

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
        elif command == "shutdown":
            self.stop()

    def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        with self._approval_lock:
            for waiter, decision in self._pending_approvals.values():
                decision["approved"] = False
                waiter.set()
            self._pending_approvals.clear()
        self.emit("exit", message="OpenKyrozen stopped.")
        agent.console = self._original_console
        agent.bg_console = self._original_bg_console

    @staticmethod
    def validate(payload: Any) -> tuple[dict[str, Any] | None, str | None]:
        if not isinstance(payload, dict):
            return None, "JSON object required."
        command = payload.get("command")
        if command not in {"start", "submit", "command", "approval_response", "question_response", "plan_action", "shutdown"}:
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
        try:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            return None, "Message is not JSON serializable."
        if len(encoded) > MAX_LINE_BYTES:
            return None, "Message is too large."
        return payload, None


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
