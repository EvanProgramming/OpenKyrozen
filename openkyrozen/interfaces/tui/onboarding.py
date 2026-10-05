from __future__ import annotations

import threading
from openkyrozen.providers.registry import PROVIDER_AUTO_SELECTION
from typing import Any

from rich.console import Console
from .protocol import (PROTOCOL_VERSION, MAX_LINE_BYTES, MAX_TEXT_CHARS, MAX_ARGS_CHARS, MAX_REQUEST_ID_CHARS, APPROVAL_TIMEOUT_SECONDS, MAX_ATTACHMENTS, MAX_ATTACHMENT_BYTES, _SENSITIVE_KEY_RE, _OUTPUT_LOCK, _redact, _safe_json)

def start(self, payload: dict[str, Any], request_id: str) -> None:
    runtime = self.agent
    if self._started:
        self.emit("ready", request_id, configured=runtime.llm_provider is not None,
                  provider=getattr(runtime._provider_config, "provider", ""),
                  model=getattr(runtime._provider_config, "model_simple", ""),
                  main_model=getattr(runtime._provider_config, "model_main", ""),
                  workspace=str(runtime._get_workspace_root()))
        return
    self._started = True
    self._onboarding_kind = str(payload.get("onboarding") or "").strip().lower()
    if self._onboarding_kind not in {"new", "update"}:
        self._onboarding_kind = ""
    onboarding_previous_version = str(payload.get("onboarding_previous_version") or "").strip()
    project = payload.get("project")
    global_mode = bool(payload.get("global", not project))
    try:
        self.status("starting", "Preparing the workspace…", request_id)
        context = self._quiet_call(
            runtime.configure_launch_context,
            project_path=project if isinstance(project, str) and project.strip() else None,
            global_mode=global_mode,
        )
        self._active_session_id = self._new_chat_id()
        self._quiet_call(runtime.bind_interaction_scope, self._active_session_id)
        self._register_chat(self._active_session_id, title=None)
        config = self._quiet_call(runtime.detect_provider)
        self.status("starting", "Connecting provider…", request_id)
        configured = bool(self._quiet_call(
            runtime._prompt_and_init_deepseek, interactive=False, config=config,
        ))
        self._quiet_call(runtime._plugin_runtime_for_surface().load_once)
        self.status("starting", "Restoring tasks and memory…", request_id)
        task_results = self._quiet_call(runtime._run_recovered_tasks)
        graph = runtime._project_graph
        if graph is not None:
            previous_graph = graph.snapshot()
            started = graph.refresh_async(callback=lambda _state: self.graph_state())
            self.emit("graph_state", request_id, graph=previous_graph | {
                "status": "indexing" if started else previous_graph.get("status", "missing"),
            })
        else:
            self.graph_state(request_id)
        if configured and not self._quiet_call(runtime._ensure_detached_learning_worker):
            threading.Thread(target=runtime._background_learning_loop, daemon=True).start()
        self.emit("tasks", request_id, tasks=[
            {"id": item["id"], "description": item["description"], "status": item["status"]}
            for item in runtime.tasks.tasks
        ])
        self.emit(
            "ready", request_id, configured=configured,
            provider=getattr(config, "provider", ""),
            model=getattr(config, "model_simple", ""),
            main_model=getattr(config, "model_main", ""),
            workspace=str(context.active_root),
            mode="global" if context.is_global else "project",
            recovered=len(task_results or []),
            session_id=self._active_session_id,
        )
        self.navigation(request_id)
        self.usage(request_id)
        if self._onboarding_kind:
            self.emit(
                "prompt", request_id, kind="onboarding", onboarding=self._onboarding_kind,
                previous_version=onboarding_previous_version,
                version=getattr(runtime, "RELEASE_VERSION", ""),
            )
        elif (not configured and getattr(config, "provider", "") == "ollama"
              and not (config.model_main not in {"", "auto"} or config.model_simple or config.model_complex)):
            self.prompt_model(request_id)
        elif not configured and not self._quiet_call(runtime.provider_is_configured, config):
            self.prompt_api_key(request_id=request_id)
        self.interaction(request_id)
        self.status("ready", "Ready", request_id)
        threading.Thread(target=self._check_for_update, daemon=True).start()
    except Exception as exc:
        self.emit("error", request_id, code="startup_failed",
                  error=f"{type(exc).__name__}: {_redact(exc)}")


def _check_for_update(self) -> None:
    latest = self.agent._available_update()
    if latest and not self._stopping.is_set():
        self.emit("update_available", version=latest)


def prompt_api_key(self, request_id: str | None = None) -> None:
    runtime = self.agent
    config = runtime._provider_config or self._quiet_call(runtime.detect_provider)
    provider = getattr(config, "provider", "deepseek")
    self.emit(
        "prompt", request_id, kind="api_key", masked=True, provider=provider,
        env_var=runtime.PROVIDER_ENV_VARS.get(provider, ""),
        message=f"Enter the {runtime.PROVIDER_DISPLAY_NAMES.get(provider, provider)} API key. It is stored encrypted locally.",
        onboarding=bool(self._onboarding_kind),
    )


def prompt_provider(self, request_id: str | None = None) -> None:
    runtime = self.agent
    current = getattr(runtime._provider_config, "provider", "deepseek")
    self.emit(
        "prompt", request_id, kind="provider", current=current,
        providers=[
            {"name": name, "display_name": runtime.PROVIDER_DISPLAY_NAMES.get(name, name),
             "model": models[0], "local": name == "ollama",
             "auto_selection": name in PROVIDER_AUTO_SELECTION}
            for name, models in runtime.PROVIDER_DEFAULT_MODELS.items()
        ],
        onboarding=bool(self._onboarding_kind),
    )


def prompt_model(self, request_id: str | None = None) -> None:
    runtime = self.agent
    config = runtime._provider_config or self._quiet_call(runtime.detect_provider)
    choices = []
    if config.provider == "ollama":
        choices = [item[0] for item in runtime.discover_ollama_models(config.base_url)]
    self.emit(
        "prompt", request_id, kind="model", provider=config.provider,
        current=config.model_main or "auto", choices=choices,
        onboarding=bool(self._onboarding_kind),
        message=("Installed Ollama models (suggestions only): " + ", ".join(choices)
                 if choices else "Enter the exact model name. For Ollama, choose an installed tag."),
    )


def configure_main_model(self, model: str | None = None, request_id: str | None = None) -> None:
    runtime = self.agent
    if model is None or not model.strip():
        self.prompt_model(request_id)
        return
    try:
        message = self._quiet_call(runtime.set_main_model, model)
        config = runtime._provider_config
        configured = runtime.llm_provider is not None
        self.emit(
            "ready", request_id, configured=configured, provider=config.provider,
            model=config.model_simple, main_model=config.model_main,
            workspace=str(runtime._get_workspace_root()),
        )
        self.emit("response", request_id, text=message)
        if configured and self._onboarding_kind == "new":
            self._prompt_onboarding_learning(request_id)
        elif configured and self._onboarding_kind == "update":
            self._complete_onboarding(request_id)
        elif not configured and config.provider == "ollama":
            self.prompt_model(request_id)
        elif not configured and not self._quiet_call(runtime.provider_is_configured, config):
            self.prompt_api_key(request_id)
        else:
            self.status("ready" if configured else "waiting", message, request_id)
    except (ValueError, OSError, RuntimeError) as exc:
        self.emit("error", request_id, code="model_setup_failed", error=_redact(exc))


def _prompt_onboarding_learning(self, request_id: str | None = None) -> None:
    runtime = self.agent
    self.emit(
        "prompt", request_id, kind="self_learning", features=self._features(),
        runtime=runtime.learning_runtime(), cost_source=runtime.learning_cost_source(),
        onboarding=True,
    )


def _complete_onboarding(self, request_id: str | None = None) -> None:
    kind = self._onboarding_kind
    self._onboarding_kind = ""
    self.emit("onboarding_complete", request_id, kind=kind)
    self.interaction(request_id)
    self.status("ready", "Ready", request_id)


def _continue_onboarding(self, request_id: str | None = None) -> None:
    runtime = self.agent
    if self._onboarding_kind == "new":
        self.prompt_provider(request_id)
        return
    if self._onboarding_kind == "update":
        config = runtime._provider_config or self._quiet_call(runtime.detect_provider)
        if config.provider == "ollama" and not (config.model_main not in {"", "auto"} or config.model_simple or config.model_complex):
            self.prompt_model(request_id)
            return
        if runtime.llm_provider is None and not self._quiet_call(
                runtime.provider_is_configured, runtime._provider_config or self._quiet_call(runtime.detect_provider)):
            self.prompt_api_key(request_id=request_id)
        else:
            self._complete_onboarding(request_id)


def configure_provider(self, provider: str, api_key: str | None = None,
                       request_id: str | None = None) -> None:
    runtime = self.agent
    provider = provider.strip().lower()
    if provider not in runtime.PROVIDER_DEFAULT_MODELS:
        self.emit("error", request_id, code="invalid_provider", error="Unknown provider.")
        return
    current = runtime._provider_config or self._quiet_call(runtime.detect_provider)
    same_provider = provider == current.provider
    config = runtime.ProviderConfig(
        provider=provider,
        api_key=(api_key if api_key is not None else current.api_key),
        model_simple=current.model_simple if same_provider else "",
        model_complex=current.model_complex if same_provider else "",
        model_main=current.model_main if same_provider else "",
    )
    if api_key is None and provider != current.provider:
        config.api_key = ""
    if provider == "ollama" and not (config.model_main not in {"", "auto"} or config.model_simple or config.model_complex):
        runtime._provider_config = config
        self.prompt_model(request_id)
        return
    try:
        if config.api_key or self._quiet_call(runtime.provider_is_configured, config):
            self._quiet_call(runtime.save_provider_config_encrypted, config)
        configured = bool(self._quiet_call(
            runtime._prompt_and_init_deepseek, interactive=False, config=config,
        ))
        self.emit(
            "ready", request_id, configured=configured, provider=provider,
            model=config.model_simple, main_model=config.model_main,
            workspace=str(runtime._get_workspace_root()),
        )
        if not configured and not self._quiet_call(runtime.provider_is_configured, config):
            self.prompt_api_key(request_id=request_id)
        elif self._onboarding_kind == "new":
            self._prompt_onboarding_learning(request_id)
        elif self._onboarding_kind == "update":
            self._complete_onboarding(request_id)
        else:
            self.status("ready", f"Using {runtime.PROVIDER_DISPLAY_NAMES.get(provider, provider)}.", request_id)
    except Exception as exc:
        self.emit("error", request_id, code="provider_setup_failed",
                  error=f"{type(exc).__name__}: {_redact(exc)}")
