from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rich.console import Console
from .protocol import (PROTOCOL_VERSION, MAX_LINE_BYTES, MAX_TEXT_CHARS, MAX_ARGS_CHARS, MAX_REQUEST_ID_CHARS, APPROVAL_TIMEOUT_SECONDS, MAX_ATTACHMENTS, MAX_ATTACHMENT_BYTES, _SENSITIVE_KEY_RE, _OUTPUT_LOCK, _redact, _safe_json)

def _command(self, name: str, args: Any, request_id: str) -> None:
    runtime = self.agent
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
        elif arg_text.strip() in runtime.PROVIDER_DEFAULT_MODELS:
            self.configure_provider(arg_text.strip(), request_id=request_id)
        else:
            self.prompt_provider(request_id)
    elif command in {"onboarding_continue", "/onboarding_continue"}:
        self._continue_onboarding(request_id)
    elif command in {"/api_key", "api_key"}:
        if isinstance(args, Mapping) and isinstance(args.get("api_key"), str):
            self.set_api_key(args["api_key"], request_id)
        elif arg_text.strip():
            self.set_api_key(arg_text.strip(), request_id)
        else:
            self.prompt_api_key(request_id)
    elif command in {"/attach", "attach"}:
        self._attach(arg_text, request_id)
    elif command in {"/new", "new"}:
        with self._state_lock:
            if self._busy:
                self.emit("error", request_id, code="busy", error="Cannot create a chat while a turn is running.")
                return
            context = runtime.get_launch_context()
            self._bind_chat(
                self._new_chat_id(),
                project_path=str(context.active_root) if context and not context.is_global else None,
                global_mode=not context or context.is_global, create=True,
            )
        self._emit_bound_state(request_id)
    elif command in {"/project", "project"}:
        path = arg_text.strip()
        if not path:
            self.emit("prompt", request_id, kind="project")
            return
        with self._state_lock:
            if self._busy:
                self.emit("error", request_id, code="busy", error="Cannot open a project while a turn is running.")
                return
            try:
                project_path = Path(path).expanduser()
                project_path.mkdir(parents=True, exist_ok=True)
                self._bind_chat(
                    self._new_chat_id(), project_path=str(project_path.resolve()), create=True,
                )
            except (OSError, ValueError) as exc:
                self.emit("error", request_id, code="project_open_failed", error=str(exc))
                return
        self._emit_bound_state(request_id)
    elif command in {"/sessions", "sessions"}:
        self.navigation(request_id)
        self.emit("prompt", request_id, kind="sessions")
    elif command in {"/session", "session"}:
        parts = arg_text.split()
        if not parts:
            self.emit("error", request_id, code="invalid_session", error="Usage: /session <id>")
            return
        current_scope = runtime.interaction_workspace_id()
        self._switch_chat(current_scope, parts[0], request_id)
    elif command in {"/learn", "learn"}:
        self.status("learning", "Refreshing the private project graph…", request_id)
        self._quiet_call(runtime._load_project_files_into_memory, force=True)
        self.graph_state(request_id)
        self.emit("response", request_id, text="Private project graph refreshed.")
        self.status("ready", "Ready", request_id)
    elif command in {"/self-learning", "self_learning"}:
        if isinstance(args, Mapping) and (args.get("mode") or args.get("policy")):
            try:
                mode = runtime.set_learning_policy(str(args.get("mode") or args["policy"]))
                self.emit("response", request_id, text=f"learning mode: {mode}")
                if args.get("onboarding") and self._onboarding_kind == "new":
                    self._complete_onboarding(request_id)
                else:
                    self.emit("prompt", request_id, kind="self_learning", features=self._features(),
                              runtime=runtime.learning_runtime(), cost_source=runtime.learning_cost_source())
            except ValueError as exc:
                self.emit("error", request_id, text=str(exc))
        elif isinstance(args, Mapping) and args.get("feature") in runtime._SELF_LEARNING_FLAGS:
            feature = str(args["feature"])
            enabled = bool(args.get("enabled", not runtime._SELF_LEARNING_FLAGS[feature]))
            runtime._SELF_LEARNING_FLAGS[feature] = enabled
            self._quiet_call(
                runtime.memory_bank.store.set_learning_feature_flag, feature, enabled,
                user_id=runtime.memory_bank.user_id, workspace_id=runtime.memory_bank.workspace_id,
            )
            self.emit("response", request_id, text=f"{feature}: {'enabled' if enabled else 'disabled'}")
        else:
            self.emit("prompt", request_id, kind="self_learning", features=self._features(),
                      runtime=runtime.learning_runtime(), cost_source=runtime.learning_cost_source())
    elif command in {"/tasks", "tasks"}:
        self.emit("tasks", request_id, tasks=[
            {"id": task["id"], "description": task["description"], "status": task["status"]}
            for task in runtime.tasks.tasks
        ])
        self.emit("response", request_id, text=runtime.tasks.format())
    elif command in {"/agents", "agents"}:
        coordinator = runtime.delegation()
        try:
            if isinstance(args, Mapping) and args.get("cancel"):
                coordinator.cancel(str(args["cancel"]))
            detail = coordinator.detail(str(args["run_id"])) if isinstance(args, Mapping) and args.get("run_id") else None
            self.emit("agents", request_id, agents=coordinator.snapshot(), detail=detail,
                      session_id=runtime.current_session.session_id, source_scope_id=runtime.memory_bank.file_scope_id)
            if not isinstance(args, Mapping) or not (args.get("run_id") or args.get("cancel")):
                self.emit("prompt", request_id, kind="agents")
        except ValueError as exc:
            self.emit("error", request_id, code="agent_not_found", error=str(exc))
    elif command in {"/agent", "agent"}:
        profile = arg_text.strip().lower()
        if profile in {"auto", "coder", "researcher"}:
            runtime._agent_profile_mode = profile
            self.emit("response", request_id, text=f"Agent profile set to {profile}.")
        else:
            self.emit("response", request_id, text=f"Agent profile: {runtime._agent_profile_mode}")
    elif command in {"/ask", "ask"}:
        runtime.set_interaction_mode("ask")
        self.emit("response", request_id, text="Interaction mode set to ask.")
        self.interaction(request_id)
    elif command in {"/fast", "fast", "/system-one", "system-one", "/system_one", "system_one"}:
        backend = arg_text.strip().lower()
        if isinstance(args, Mapping):
            backend = str(args.get("backend") or backend).strip().lower()
        if not backend:
            current = runtime.interaction_envelope().get("system_one_backend", runtime.interaction_envelope()["fast_backend"])
            status = runtime.decision_assist_state()
            backends = (status.get("calibration") or {}).get("backends", {})
            policies = backends.get(current, {}) if isinstance(backends, dict) else {}
            calibrated = any(bool(policy.get("validated"))
                             for backend_policies in policies.values() if isinstance(backend_policies, dict)
                             for policy in backend_policies.values() if isinstance(policy, dict))
            self.emit("response", request_id, text=(
                f"System One: {current}. Jev model: {status.get('jev_model_alias', 'jev-latest')} "
                f"({status.get('jev_model_release_date') or 'release unknown'}; {status.get('jev_health', 'unknown')}); calibration: "
                f"{'available' if calibrated else 'not calibrated'}. Use /system-one off|jev|kev "
                "(legacy /fast alias). Jev sends context to TypeSafe; local Kev-0.8B is less accurate."
            ))
        elif backend == "jev" and not (runtime.fast_mode.jev_key() or (isinstance(args, Mapping) and args.get("api_key"))):
            self.emit("prompt", request_id, kind="fast_key", message="Enter Jev API key (paid TypeSafe calls; stored encrypted locally)")
        else:
            try:
                if backend == "kev":
                    self.status("starting", "Installing local Kev-0.8B and checking the model…", request_id)
                state = runtime.set_fast_backend(backend, api_key=args.get("api_key") if isinstance(args, Mapping) else None)
                self.emit("response", request_id, text=f"System One: {state['system_one_backend']}.")
                self.interaction(request_id)
                self.status("ready", "Ready", request_id)
            except (runtime.InteractionError, RuntimeError, OSError, ValueError) as exc:
                self.emit("error", request_id, code="fast_setup_failed", alias_code="system_one_setup_failed", error=str(exc))
    elif command in {"/decision-assist", "decision-assist", "/assist", "assist"}:
        backend = arg_text.strip().lower()
        if isinstance(args, Mapping):
            backend = str(args.get("backend") or backend).strip().lower()
        if not backend:
            state = runtime.decision_assist_state()
            assist_policies = (state.get("calibration", {}).get("backends", {}).get(state["backend"], {})
                               if isinstance(state.get("calibration"), dict) else {})
            calibrated = sum(bool(policy.get("validated")) for policy in assist_policies.values()
                             if isinstance(policy, dict))
            self.emit("response", request_id, text=(
                f"Decision Assist: {state['backend']} (Kev private consent: "
                f"{'yes' if state['kev_private_consent'] else 'no'}; "
                f"Jev: {'ready' if state['jev_configured'] else 'not configured'}; "
                f"Kev: {'ready' if state['kev_ready'] else 'not ready'}; "
                f"model: {state.get('jev_model_alias', 'jev-latest')} "
                f"({state.get('jev_model_release_date') or 'release unknown'}; {state.get('jev_health', 'unknown')}); "
                f"calibrated actions: {calibrated}). "
                "Use /decision-assist off|jev|kev yes, or revoke."
            ))
        elif backend == "jev" and not (runtime.fast_mode.jev_key() or (isinstance(args, Mapping) and args.get("api_key"))):
            self.emit("prompt", request_id, kind="decision_assist_key", message="Enter Jev API key (paid TypeSafe calls; stored encrypted locally)")
        else:
            try:
                consent = bool(args.get("private_consent", False)) if isinstance(args, Mapping) else False
                words = backend.split()
                backend = words[0]
                if backend == "revoke":
                    state = runtime.revoke_decision_assist_consent()
                    self.emit("response", request_id, text=(
                        f"Decision Assist private Kev consent revoked; backend remains {state['backend']}."
                    ))
                    self.interaction(request_id)
                    return
                if len(words) > 1 and words[1] in {"yes", "y", "consent", "allow"}:
                    consent = True
                state = runtime.set_decision_assist(
                    backend, private_consent=consent,
                    api_key=args.get("api_key") if isinstance(args, Mapping) else None,
                )
                self.emit("response", request_id, text=(
                    f"Decision Assist: {state['backend']}. Private Kev consent: "
                    f"{'yes' if state['kev_private_consent'] else 'no'}."
                ))
                self.interaction(request_id)
            except (runtime.InteractionError, RuntimeError, OSError, ValueError) as exc:
                self.emit("error", request_id, code="decision_assist_setup_failed", error=str(exc))
    elif command in {"/permissions", "permissions"}:
        mode = arg_text.strip().lower()
        if isinstance(args, Mapping):
            mode = str(args.get("mode") or mode).strip().lower()
        if not mode:
            state = runtime.interaction_envelope()
            self.emit("response", request_id, text=f"Permissions: {state['permission_mode']}.")
        elif self._busy:
            self.emit("error", request_id, code="busy", error="Cannot change permissions while a turn is running.")
        elif mode in {"full_jev", "jev"} and not (runtime.fast_mode.jev_key() or (isinstance(args, Mapping) and args.get("api_key"))):
            self.emit("prompt", request_id, kind="permission_jev_key", message="Enter Jev API key for protected full access (stored encrypted locally).")
        else:
            try:
                state = runtime.set_permission_mode(
                    mode, jev_api_key=args.get("api_key") if isinstance(args, Mapping) else None,
                )
                self.emit("response", request_id, text=f"Permissions set to {state['permission_mode']}.")
                self.interaction(request_id)
            except (runtime.InteractionError, RuntimeError, OSError, ValueError) as exc:
                self.emit("error", request_id, code="invalid_permissions", error=str(exc))
    elif command in {"/mode", "mode"}:
        mode = arg_text.strip().lower()
        if not mode:
            self.emit("prompt", request_id, kind="mode", modes=["auto", "ask", "plan", "agent"],
                      selected=runtime.interaction_envelope()["preference_mode"])
        else:
            try:
                state = runtime.set_interaction_mode(mode)
                self.emit("response", request_id, text=f"Interaction mode set to {state['preference_mode']}.")
                self.interaction(request_id)
            except runtime.InteractionError as exc:
                self.emit("error", request_id, code="invalid_mode", error=str(exc))
    elif command in {"/plan", "plan"}:
        action = arg_text.strip().lower()
        if not action:
            runtime.set_interaction_mode("plan")
            self.emit("response", request_id, text="Interaction mode set to plan.")
            self.interaction(request_id)
        elif action == "accept":
            self.submit("accept plan", request_id)
        elif action == "cancel":
            try:
                runtime.cancel_interaction_plan()
                self.emit("response", request_id, text="Pending plan cancelled.")
                self.interaction(request_id)
            except runtime.InteractionError as exc:
                self.emit("error", request_id, code="plan_not_pending", error=str(exc))
        else:
            self.emit("error", request_id, code="invalid_plan_action", error="Usage: /plan | /plan accept|cancel")
    elif command in {"/question", "question"}:
        action = arg_text.strip().lower()
        try:
            if not action:
                runtime._interaction_controller.reopen_question()
                self.interaction(request_id)
            elif action in {"skip", "cancel"}:
                pending = runtime._interaction_controller.state().get("pending_question")
                if not pending:
                    raise runtime.InteractionError("no question is pending")
                runtime.resolve_interaction_question(pending["request_id"], {}, action=action)
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
                raise runtime.InteractionError("Usage: /question | /question skip|cancel")
        except runtime.InteractionError as exc:
            self.emit("error", request_id, code="question_not_pending", error=str(exc))
    elif command in {"/update", "update"}:
        self.status("updating", "Updating OpenKyrozen…", request_id)
        result = self._quiet_call(runtime._self_update)
        if result.startswith("Updated OpenKyrozen from "):
            self.emit("restart", request_id, text=result)
        else:
            self.emit("response", request_id, text=result)
            self.status("ready", "Ready", request_id)
    elif command in {"/history", "history"}:
        self.emit("response", request_id, text=runtime.history_text())
    elif command in {"/rollback", "rollback"}:
        parts = arg_text.strip().split()
        if not parts or parts[0].lower() == "help":
            text = runtime.history_text()
            if not parts:
                text += "\n\nChoose a node with /rollback <number>, then confirm with /rollback <number> confirm."
            else:
                text += "\n\nUse /rollback <number> to preview, /rollback <number> confirm to restore, or /rollback cancel."
            self.emit("response", request_id, text=text)
        elif parts[0].lower() == "cancel":
            self.emit("response", request_id, text="Rollback cancelled.")
        elif len(parts) > 2 or (len(parts) == 2 and parts[1].lower() != "confirm"):
            self.emit("error", request_id, code="invalid_rollback_command",
                      error="Usage: /rollback <number> [confirm] | /rollback cancel")
        else:
            selector = parts[0]
            confirmed = len(parts) == 2
            try:
                manager = runtime.history_manager()
                current = manager.current()
                if current is None:
                    raise runtime.HistoryError("no history has been recorded for this conversation")
                target = manager.resolve_selector(selector)
                changes = target.get("file_summary", {}).get("changes", {})
                display_selector = target.get("selector", selector)
                preview = (
                    f"Restore [{display_selector}] {target.get('summary') or 'this point'}: "
                    f"+{changes.get('added', 0)} added, ~{changes.get('changed', 0)} changed, "
                    f"-{changes.get('deleted', 0)} deleted. Durable memory is preserved."
                )
                if not confirmed:
                    self.emit(
                        "response", request_id,
                        text=preview + f"\nType /rollback {selector} confirm to continue, or /rollback cancel.",
                    )
                    return
                self._quiet_call(
                    runtime.restore_history, target["id"], confirm="rollback", expected_head=current["id"],
                )
                self.navigation(request_id)
                self.interaction(request_id)
                self.graph_state(request_id)
                self.usage(request_id)
                self.status("ready", f"Restored [{display_selector}]. Recovery point saved.", request_id)
            except runtime.HistoryError as exc:
                self.emit("error", request_id, code="rollback_failed", error=str(exc))
    elif command in {"/graph", "graph"}:
        parts = arg_text.strip().split(maxsplit=1)
        action = parts[0].lower() if parts else "open"
        if action in {"open", "status"}:
            self.graph_state(request_id)
            if action == "open":
                self.emit("prompt", request_id, kind="graph")
        elif action == "refresh":
            full = len(parts) > 1 and parts[1].strip() == "--full"
            self.status("learning", "Refreshing project graph…", request_id)
            if runtime._project_graph is None:
                self.emit("error", request_id, code="graph_unavailable", error="Project graph is not configured.")
            else:
                state = self._quiet_call(runtime._project_graph.refresh, full=full)
                self.emit("graph_state", request_id, graph=state | {"mini": runtime._project_graph.snapshot().get("mini", {})})
                self.status("ready", "Ready", request_id)
        else:
            self.emit("error", request_id, code="invalid_graph_command", error="Usage: /graph [open|status|refresh [--full]]")
    elif command in {"/github", "github"}:
        parts = arg_text.strip().split(maxsplit=1)
        action = parts[0].lower() if parts else "status"
        client = runtime._github_cli
        if client is None:
            self.emit("error", request_id, code="github_unavailable", error="GitHub CLI is not configured.")
        elif action == "status":
            self.emit("github_state", request_id, github=client.status())
        elif action == "login":
            if not client.binary():
                installed = self._quiet_call(client.install_managed)
                if not installed.get("success"):
                    self.emit("error", request_id, code="github_install_failed", error=installed.get("message", "GitHub CLI installation failed."))
                    return
            self.emit("prompt", request_id, kind="github_auth", binary=client.binary(), hostname=client.hostname())
        elif action == "run" and len(parts) > 1:
            receipt = self._quiet_call(
                runtime.execute, runtime.current_session, "github_cli", parts[1],
                operation_scope="tui-command", approve=self._approval,
            )
            self.emit("response", request_id, text=receipt.result)
        else:
            self.emit("error", request_id, code="invalid_github_command", error="Usage: /github status | /github login | /github run <gh arguments>")
    elif command in {"/skills", "skills"}:
        rows = runtime.skill_registry.list()
        self.emit("response", request_id, text="\n".join(
            ["Built-in and installed skills:"] + [f"- {item['name']} {item['version']} ({item['source']}, {item['status']})" for item in rows]
        ))
    elif command in {"/ponytail", "ponytail"}:
        level = arg_text.strip().lower()
        if not level:
            self.emit("response", request_id, text=f"Ponytail: {runtime._ponytail_level}")
        else:
            try:
                self.emit("response", request_id, text=f"Ponytail: {runtime.set_ponytail_level(level)}")
            except ValueError as exc:
                self.emit("error", request_id, code="invalid_ponytail_level", error=str(exc))
    elif command in {"/learning", "learning"}:
        self.emit("response", request_id, text=self._learning_text(arg_text))
    elif command in {"/memory", "memory"}:
        self.emit("response", request_id, text=self._memory_text(arg_text))
    elif command in {"/forget", "forget"}:
        self.emit("response", request_id, text=runtime._forget_recent(arg_text.strip()))
    else:
        self.emit("error", request_id, code="unknown_command",
                  error=f"Unknown command: {raw[:120]}")


def _learning_text(self, args: str) -> str:
    runtime = self.agent
    parts = args.split(maxsplit=1)
    subcommand = parts[0].lower() if parts else "status"
    argument = parts[1].strip() if len(parts) > 1 else ""
    if subcommand == "status":
        rows = runtime.learning_engine.status(50, profile=argument if argument in {"coder", "researcher"} else None)
        return json.dumps(rows, ensure_ascii=False, indent=2, default=str) if rows else "No learning proposals."
    if subcommand == "metrics":
        return json.dumps(runtime.learning_engine.metrics(argument or None), ensure_ascii=False, indent=2, default=str)
    if subcommand == "rollback" and argument:
        return "Learning proposal rolled back." if runtime.learning_engine.rollback(argument) else "Proposal not found."
    if subcommand in {"explain", "evidence"} and argument:
        value = (runtime.learning_engine.evidence_card(argument) if subcommand == "evidence"
                 else next((row for row in runtime.learning_engine.status(1000) if row["id"] == argument), None))
        return json.dumps(value, ensure_ascii=False, indent=2, default=str) if value else "Proposal not found."
    return "Usage: /learning status | /learning metrics | /learning rollback <id> | /learning explain|evidence <id>"
