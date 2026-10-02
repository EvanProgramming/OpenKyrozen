from __future__ import annotations

import re
import shlex
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

from rich.console import Console
from .protocol import (PROTOCOL_VERSION, MAX_LINE_BYTES, MAX_TEXT_CHARS, MAX_ARGS_CHARS, MAX_REQUEST_ID_CHARS, APPROVAL_TIMEOUT_SECONDS, MAX_ATTACHMENTS, MAX_ATTACHMENT_BYTES, _SENSITIVE_KEY_RE, _OUTPUT_LOCK, _redact, _safe_json)

def set_api_key(self, api_key: str, request_id: str | None = None) -> None:
    runtime = self.agent
    if not api_key.strip():
        self.emit("error", request_id, code="empty_api_key", error="No API key entered.")
        return
    config = runtime._provider_config or self._quiet_call(runtime.detect_provider)
    config.api_key = api_key.strip()
    try:
        self._quiet_call(runtime.save_provider_config_encrypted, config)
        configured = bool(self._quiet_call(
            runtime._prompt_and_init_deepseek, interactive=False, config=config,
        ))
        self.emit("ready", request_id, configured=configured,
                  provider=config.provider, model=config.model_simple,
                  workspace=str(runtime._get_workspace_root()))
        if configured and self._onboarding_kind == "new":
            self._prompt_onboarding_learning(request_id)
        elif configured and self._onboarding_kind == "update":
            self._complete_onboarding(request_id)
        else:
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
    runtime = self.agent
    dsml = runtime.DeepSeekDSMLFilter()
    buffer = ""
    prefixes = ("action:", "tasklist:", "taskdone:", "thought:", "plan:", "definetool:",
                "askuser:", "askuser\n", "planproposal:", "planproposal\n", "<",
                "{", "[", "```json")

    def emit_text(text: str) -> None:
        nonlocal buffer
        if not text:
            return
        buffer += text
        candidate = buffer.lstrip().lower()
        if (any(prefix.startswith(candidate) or candidate.startswith(prefix) for prefix in prefixes)
                or runtime._stream_buffer_has_tool_prefix(buffer)):
            return
        self.emit("stream_delta", request_id, text=_redact(runtime._clean_stream_buffer(buffer)))
        buffer = ""

    def callback(event: dict[str, Any]) -> None:
        nonlocal buffer
        kind = event.get("event")
        if kind == "content":
            emit_text(dsml.feed(str(event.get("chunk", ""))))
        elif kind == "model_complete":
            emit_text(dsml.feed("", final=True))
            if buffer:
                cleaned = "" if runtime.normalize_provider_control(buffer, plan_mode=True) else runtime._clean_final_response(buffer)
                if cleaned:
                    self.emit("stream_delta", request_id, text=_redact(cleaned))
                buffer = ""
        elif kind == "tool_receipt":
            self.emit("tool_receipt", request_id, receipt=event.get("tool_receipt", {}))
        elif kind == "tasks":
            self.emit("tasks", request_id, tasks=event.get("tasks", []))
        elif kind == "interaction":
            self.emit("interaction", request_id, interaction=event.get("interaction", {}))
        elif kind == "permission_check":
            self.emit("permission_check", request_id, state=event.get("state", ""),
                      category=event.get("category", ""))
        elif kind == "subagent":
            self.emit("subagent", request_id, agent=event.get("agent", {}),
                      session_id=event.get("session_id"), source_scope_id=event.get("source_scope_id"))

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


def _attach(self, args: str, request_id: str) -> None:
    runtime = self.agent
    try:
        raw_paths = shlex.split(args)
        if not raw_paths:
            raise ValueError("Usage: /attach PATH [PATH ...]")
        if len(self._staged_attachments) + len(raw_paths) > MAX_ATTACHMENTS:
            raise ValueError(f"At most {MAX_ATTACHMENTS} files can be staged per turn.")

        sources: list[tuple[Path, int]] = []
        for raw_path in raw_paths:
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
            if candidate.is_symlink():
                raise ValueError(f"Symlinks are not accepted: {raw_path}")
            try:
                source = candidate.resolve(strict=True)
            except FileNotFoundError as exc:
                raise ValueError(f"File not found: {raw_path}") from exc
            if not source.is_file():
                raise ValueError(f"Not a regular file: {raw_path}")
            size = source.stat().st_size
            if size > MAX_ATTACHMENT_BYTES:
                raise ValueError(
                    f"File exceeds the 25 MB limit: {raw_path} ({size} bytes)"
                )
            sources.append((source, size))

        workspace = Path(runtime._get_workspace_root()).expanduser().resolve()
        attachments_root = workspace / "attachments"
        if attachments_root.exists() and attachments_root.is_symlink():
            raise ValueError("The workspace attachments directory cannot be a symlink.")
        attachments_root.mkdir(parents=True, exist_ok=True)
        if attachments_root.resolve().parent != workspace:
            raise ValueError("The workspace attachments directory is outside the workspace.")

        batch_dir = attachments_root / uuid.uuid4().hex
        batch_dir.mkdir()
        staged: list[dict[str, Any]] = []
        try:
            for source, size in sources:
                destination = batch_dir / source.name
                counter = 2
                while destination.exists():
                    destination = batch_dir / f"{source.stem}-{counter}{source.suffix}"
                    counter += 1
                shutil.copyfile(source, destination)
                staged.append({
                    "path": destination.relative_to(workspace).as_posix(),
                    "bytes": size,
                })
        except Exception:
            shutil.rmtree(batch_dir, ignore_errors=True)
            raise

        self._staged_attachments.extend(staged)
        details = "\n".join(f"- {item['path']} ({item['bytes']} bytes)" for item in staged)
        self.emit(
            "response", request_id,
            text=f"Staged {len(staged)} file{'s' if len(staged) != 1 else ''}:\n"
                 f"{details}\nType your question to use them.",
        )
    except (OSError, ValueError) as exc:
        self.emit("error", request_id, code="attach_failed", error=str(exc))


def _attachment_prompt(self, text: str) -> str:
    if not self._staged_attachments:
        return text
    details = "\n".join(
        f"- {item['path']} ({item['bytes']} bytes)"
        for item in self._staged_attachments
    )
    return (
        "The user attached these files to this request. They are available in the "
        "active workspace; use existing file tools with these relative paths as needed:\n"
        f"{details}\n\nUser request:\n{text}"
    )


def _run_submit(self, text: str, request_id: str) -> None:
    runtime = self.agent
    stream_token = runtime._stream_event_callback.set(self._stream_projection(request_id))
    approval_token = runtime._approval_callback.set(self._approval)
    try:
        inline = runtime.split_inline_command(text)
        if inline:
            text, command = inline
            self._command(command, {}, request_id)
        if runtime.llm_provider is None:
            self.prompt_api_key(request_id=request_id)
            self.status("waiting", "Provider setup required.", request_id)
            return
        sanitized, flagged = self._quiet_call(runtime._sanitize_input, text)
        metadata = runtime.memory_bank.store.list_events(
            "tui.chat_metadata", limit=1, workspace_id=runtime.interaction_workspace_id(),
            session_id=self._active_session_id, user_id=runtime.memory_bank.user_id,
        ) if self._active_session_id and hasattr(runtime.memory_bank, "store") else []
        if metadata and metadata[0]["payload"].get("title") == "New chat":
            self._register_chat(
                self._active_session_id,
                title=re.sub(r"\s+", " ", text.strip())[:72] or "New chat",
            )
        if flagged:
            self.emit("status", request_id, state="warning",
                      message="Prompt injection text was filtered.", busy=True)
        state = runtime.interaction_envelope(text)
        if not state["pending_question"] and not state["pending_plan"] and not runtime.is_plan_acceptance(text):
            runtime.tasks.clear()
        self.status("thinking", "Thinking…", request_id)
        reply = self._quiet_call(
            runtime.chat, runtime.current_session, self._attachment_prompt(sanitized),
            on_event=runtime._stream_event_callback.get(), approve=self._approval,
            clear_tasks=not bool(state["pending_question"] or state["pending_plan"]
                                 or runtime.is_plan_acceptance(sanitized)),
        )
        reply = runtime._clean_final_response(reply)
        if len(reply.strip()) < 1:
            self.emit("error", request_id, code="empty_response", error="The provider returned no answer.")
            return
        self._staged_attachments.clear()
        runtime.short_term_memory.extend([
            {"role": "user", "content": text},
            {"role": "assistant", "content": reply},
        ])
        self._quiet_call(runtime.memory_bank.add_log, f"User: {text}\nAssistant: {reply}")
        thinking, answer = runtime._split_reply(reply)
        if thinking:
            self.emit("thinking", request_id, text=thinking)
        self.emit("response", request_id, text=answer or "(no content)")
        self.emit("tasks", request_id, tasks=[
            {"id": task["id"], "description": task["description"], "status": task["status"]}
            for task in runtime.tasks.tasks
        ])
        self.interaction(request_id)
        self.usage(request_id)
        self.status("ready", "Ready", request_id)
        self.navigation(request_id)
    except runtime.ProviderUnavailableError as exc:
        self.emit("error", request_id, code=runtime.PROVIDER_UNAVAILABLE_CODE,
                  error=_redact(exc))
    except Exception as exc:
        self.emit("error", request_id, code="turn_failed",
                  error=f"{type(exc).__name__}: {_redact(exc)}")
    finally:
        runtime._approval_callback.reset(approval_token)
        runtime._stream_event_callback.reset(stream_token)
        with self._state_lock:
            self._busy = False
            self._workers.discard(threading.current_thread())


def _features(self) -> list[dict[str, Any]]:
    runtime = self.agent
    return [
        {"name": name, "enabled": bool(runtime._SELF_LEARNING_FLAGS.get(name, True)),
         "description": runtime._LEARNING_FEATURE_REGISTRY[name]["description"]}
        for name in runtime._LEARNING_FEATURE_ORDER
    ]
