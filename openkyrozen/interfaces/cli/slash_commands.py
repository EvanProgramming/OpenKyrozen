from __future__ import annotations

import json
import os
import subprocess
from rich.panel import Panel
from openkyrozen.tasks.engine import TaskWorker
from openkyrozen.agent.modes import InteractionError, render_question
import openkyrozen.routing.system_one as fast_mode
from openkyrozen.workspace.history import HistoryError
from openkyrozen.providers import PROVIDER_ENV_VARS, save_provider_config_encrypted

def _handle_cli_command(self, user_input, interaction_before):
    if user_input.lower().startswith("/agents"):
        parts = user_input.split(maxsplit=2)
        coordinator = self.delegation()
        if len(parts) == 3 and parts[1] == "cancel":
            value = coordinator.cancel(parts[2])
        else:
            if len(parts) == 1:
                from rich.markup import escape
                runs = coordinator.snapshot()
                for run in runs:
                    self.console.print(escape(f"{run['name']} · {run['profile']} · {run['status']} · {run['provider_model']}: {run['assignment']['objective']} ({run['run_id']})"))
                if not runs:
                    self.console.print("No sub-agents in this chat.")
                return None
            value = coordinator.detail(parts[1])
        self.console.print(json.dumps(value, ensure_ascii=False, indent=2))
        return None
    if user_input.lower() in ("/quit", "/exit"):
        self._clear_tasks_panel()
        self.console.print(f"[{self._ERROR}]Goodbye.[/{self._ERROR}]")
        raise SystemExit(0)
    if user_input.lower() == "/learn":
        state = self._load_project_files_into_memory(force=True)
        self.console.print(f"[{self._SUCCESS}]Project graph: {state.get('status')} · {state.get('nodes', 0)} nodes · {state.get('edges', 0)} edges.[/{self._SUCCESS}]")
        return None
    if user_input.lower() == "/api_key":
        new_key = self.console.input(f"[bold yellow]Enter new {self._provider_config.provider.title()} API key: [/bold yellow]").strip()
        if new_key:
            self._provider_config.api_key = new_key
            save_provider_config_encrypted(self._provider_config)
            env_var = PROVIDER_ENV_VARS.get(self._provider_config.provider, "")
            if env_var:
                os.environ[env_var] = new_key
            self.llm_provider = self.get_provider(self._provider_config)
            self.console.print(f"[{self._SUCCESS}]API key updated and saved for future sessions.[/{self._SUCCESS}]")
        else:
            self.console.print(f"[{self._ERROR}]No key provided – key unchanged.[/{self._ERROR}]")
        return None
    if user_input.lower() == "/provider":
        self._switch_provider()
        return None
    if user_input.lower() == "/model" or user_input.lower().startswith("/model "):
        parts = user_input.split(maxsplit=1)
        value = parts[1].strip() if len(parts) > 1 else ""
        if not value:
            if self._provider_config.provider == "ollama":
                models = self.discover_ollama_models(self._provider_config.base_url)
                if models:
                    self.console.print("Installed Ollama models: " + ", ".join(item[0] for item in models))
                else:
                    self.console.print("No Ollama models discovered; enter an exact installed tag.")
            value = self.console.input("Main model name, or 'auto' to restore automatic selection: ").strip()
        if value:
            try:
                self.console.print(self.set_main_model(value))
            except ValueError as exc:
                self.console.print(f"[{self._ERROR}]{exc}[/{self._ERROR}]")
        return None
    if user_input.lower() == "/custom-provider" or user_input.lower().startswith("/custom-provider "):
        parts = user_input.split(maxsplit=2)
        action = parts[1].lower() if len(parts) > 1 else "list"
        name = parts[2] if len(parts) > 2 else ""
        try:
            if action == "list":
                profiles = self.custom_provider_profiles()
                self.console.print("\n".join(f"{p['name']} · {p['base_url']} · {p['model_simple']} / {p['model_complex']}" for p in profiles)
                                   or "No custom provider profiles. Use /custom-provider create.")
            elif action == "use" and name:
                self.console.print(self.use_custom_provider(name))
            elif action == "remove" and name:
                if self.console.input(f"Remove custom provider profile {name!r}? [y/N] ").strip().lower() in {"y", "yes"}:
                    self.console.print(self.delete_custom_provider(name))
            elif action in {"create", "edit"}:
                from getpass import getpass
                if action == "edit" and name:
                    from openkyrozen.providers.custom import load_custom_provider_profile
                    try:
                        config = load_custom_provider_profile(name)
                        current = {"name": config.custom_profile, "base_url": config.base_url,
                                   "api_key": config.api_key, "model_simple": config.model_simple,
                                   "model_complex": config.model_complex,
                                   "context_window_tokens": config.context_window_tokens}
                    except ValueError:
                        current = None
                else:
                    current = None
                if action == "edit" and current is None:
                    raise ValueError("Use /custom-provider edit <existing name>.")
                current = current or {}
                def ask(label, key):
                    default = current.get(key, "")
                    shown = "" if default is None else str(default)
                    return self.console.input(f"{label} [{shown}]: ").strip() or shown
                profile = {"name": ask("Profile name", "name"), "base_url": ask("OpenAI-compatible endpoint URL", "base_url"),
                           "model_simple": ask("Simple model ID", "model_simple"), "model_complex": ask("Complex model ID", "model_complex"),
                           "context_window_tokens": ask("Context limit (blank for none)", "context_window_tokens")}
                if current:
                    profile["_original_name"] = str(current["name"])
                key = getpass("API key (optional; blank keeps existing/none): ")
                profile["api_key"] = key or str(current.get("api_key", ""))
                activate = self.console.input("Use for the main agent now? [Y/n] ").strip().lower() not in {"n", "no"}
                self.console.print(self.save_custom_provider(profile, activate=activate))
            else:
                self.console.print("Usage: /custom-provider list | create | edit <name> | use <name> | remove <name>")
        except (ValueError, OSError) as exc:
            self.console.print(f"Custom provider setup failed: {exc}")
        return None
    if user_input.lower() == "/update":
        self.console.print(f"[{self._ACCENT}]Updating the installed OpenKyrozen package...[/{self._ACCENT}]")
        update_result = self._self_update()
        self.console.print(Panel(str(update_result), title="Update", border_style=self._ACCENT))
        return None

    if user_input.lower() == "/self-learning":
        self._show_self_learning_menu()
        return None

    if user_input.lower().startswith("/graph"):
        parts = user_input.split(maxsplit=2)
        action = parts[1].lower() if len(parts) > 1 else "status"
        if action == "status":
            self.console.print(json.dumps(self.project_graph_snapshot(), ensure_ascii=False, indent=2))
        elif action == "refresh":
            state = self._project_graph.refresh(full=len(parts) > 2 and parts[2].strip() == "--full") if self._project_graph else {"status": "missing"}
            self.console.print(json.dumps(state, ensure_ascii=False, indent=2))
        elif action == "open":
            self.console.print("The interactive graph explorer is available in the Bubble Tea UI with `g`.")
        else:
            self.console.print("Usage: /graph status | /graph refresh [--full] | /graph open")
        return None

    if user_input.lower().startswith("/github"):
        parts = user_input.split(maxsplit=2)
        action = parts[1].lower() if len(parts) > 1 else "status"
        if self._github_cli is None:
            self.console.print("GitHub CLI is not configured for this workspace.")
        elif action == "status":
            self.console.print(json.dumps(self._github_cli.status(), ensure_ascii=False, indent=2))
        elif action == "login":
            self.console.print(self._github_cli.login_interactive())
        elif action == "run" and len(parts) > 2:
            if self._confirm_tool_action("github_cli", parts[2]):
                self.console.print(self._github_cli.run(parts[2]))
        else:
            self.console.print("Usage: /github status | /github login | /github run <gh arguments>")
        return None

    if user_input.lower() == "/skills":
        lines = ["Built-in and installed skills:"]
        for item in self.skill_registry.list():
            lines.append(f"- {item['name']} {item['version']} ({item['source']}, {item['status']})")
        self.console.print("\n".join(lines))
        return None

    if user_input.lower().startswith("/ponytail"):
        parts = user_input.split(maxsplit=1)
        if len(parts) == 1:
            self.console.print(f"Ponytail: {self._ponytail_level}")
        else:
            try:
                self.console.print(f"Ponytail: {self.set_ponytail_level(parts[1])}")
            except ValueError as exc:
                self.console.print(f"Usage: /ponytail off|lite|full|ultra ({exc})")
        return None

    lowered = user_input.lower()
    if (lowered == "/fast" or lowered.startswith("/fast ") or
            lowered == "/system-one" or lowered.startswith("/system-one ") or
            lowered == "/system_one" or lowered.startswith("/system_one ")):
        parts = user_input.split(maxsplit=1)
        if len(parts) == 1:
            state = fast_mode.decision_assist_state()
            backend_name = self.interaction_envelope()["system_one_backend"]
            backends = (state.get("calibration", {}).get("backends", {})
                        if isinstance(state.get("calibration"), dict) else {})
            policies = backends.get(backend_name, {}) if isinstance(backends, dict) else {}
            calibrated = any(bool(policy.get("validated"))
                             for backend_policies in policies.values() if isinstance(backend_policies, dict)
                             for policy in backend_policies.values() if isinstance(policy, dict))
            self.console.print(
                f"System One: {backend_name} · "
                f"Jev model: {state.get('jev_model_alias', 'jev-latest')} "
                f"({state.get('jev_model_release_date') or 'release unknown'}; {state.get('jev_health', 'unknown')}) · "
                f"calibration: {'available' if calibrated else 'not calibrated'} · "
                "Jev sends context to TypeSafe; local Kev-0.8B is less accurate"
            )
        else:
            try:
                backend = parts[1].strip().lower()
                key = None
                if backend == "jev" and not fast_mode.jev_key():
                    import getpass
                    key = getpass.getpass("Jev API key (paid TypeSafe calls): ")
                if backend == "kev":
                    self.console.print("Installing and starting local Kev-0.8B; waiting for a live check…")
                state = self.set_system_one_backend(backend, api_key=key)
                self.console.print(f"System One: {state['system_one_backend']}")
            except (InteractionError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
                self.console.print(f"System One setup failed: {exc}")
        return None
    if lowered in {"/decision-assist", "/assist"} or lowered.startswith("/decision-assist ") or lowered.startswith("/assist "):
        parts = user_input.split(maxsplit=1)
        if len(parts) == 1:
            state = self.decision_assist_state()
            assist_policies = (state.get("calibration", {}).get("backends", {}).get(state["backend"], {})
                               if isinstance(state.get("calibration"), dict) else {})
            calibrated = sum(bool(policy.get("validated")) for policy in assist_policies.values()
                             if isinstance(policy, dict))
            self.console.print(
                f"Decision Assist: {state['backend']} · Jev configured: {state['jev_configured']} · "
                f"Kev ready: {state['kev_ready']} · private Kev consent: {state['kev_private_consent']} · "
                f"Jev model: {state.get('jev_model_alias', 'jev-latest')} "
                f"({state.get('jev_model_release_date') or 'release unknown'}; {state.get('jev_health', 'unknown')}) · "
                f"calibrated actions: {calibrated}"
            )
            return None
        try:
            words = parts[1].strip().lower().split()
            backend = words[0] if words else ""
            if backend == "revoke":
                state = self.revoke_decision_assist_consent()
                self.console.print(f"Decision Assist private Kev consent revoked; backend remains {state['backend']}.")
                return None
            key = None
            if backend == "jev" and not fast_mode.jev_key():
                import getpass
                key = getpass.getpass("Jev API key (paid TypeSafe calls): ")
            consent = False
            if backend == "kev":
                consent = len(words) > 1 and words[1] in {"y", "yes", "consent", "allow"}
                if not consent:
                    answer = self.console.input(
                        "Allow local Kev-0.8B to inspect private workspace context? [y/N] "
                    ).strip().lower()
                    consent = answer in {"y", "yes"}
            state = self.set_decision_assist(backend, private_consent=consent, api_key=key)
            self.console.print(
                f"Decision Assist: {state['backend']} · Kev private consent: "
                f"{state['kev_private_consent']} · Jev model: {state.get('jev_model_alias', 'jev-latest')} "
                f"({state.get('jev_model_release_date') or 'release unknown'})"
            )
        except (InteractionError, RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
            self.console.print(f"Decision Assist setup failed: {exc}")
        return None
    if lowered == "/ask":
        self.set_interaction_mode("ask")
        self.console.print("Interaction mode set to ask.")
        return None
    if lowered.startswith("/mode"):
        parts = user_input.split(maxsplit=1)
        if len(parts) == 1:
            state = self.interaction_envelope()
            self.console.print(f"Interaction mode: {state['preference_mode']} (effective: {state['effective_mode']})")
        else:
            try:
                state = self.set_interaction_mode(parts[1])
                self.console.print(f"Interaction mode set to {state['preference_mode']}.")
            except InteractionError as exc:
                self.console.print(f"Usage: /mode auto|ask|plan|agent ({exc})")
        return None
    if lowered.startswith("/plan"):
        parts = lowered.split(maxsplit=1)
        action = parts[1] if len(parts) > 1 else ""
        if not action:
            self.set_interaction_mode("plan")
            self.console.print("Interaction mode set to plan.")
            return None
        if action == "cancel":
            try:
                self.cancel_interaction_plan()
                self.console.print("Pending plan cancelled.")
            except InteractionError as exc:
                self.console.print(str(exc))
            return None
        if action == "accept":
            user_input = "accept plan"
        else:
            self.console.print("Usage: /plan | /plan accept|cancel")
            return None
    if lowered.startswith("/question"):
        parts = lowered.split(maxsplit=1)
        action = parts[1] if len(parts) > 1 else ""
        try:
            if not action:
                self.console.print(Panel(render_question(self._interaction_controller.reopen_question()), title="Question"))
                return None
            if action not in {"skip", "cancel"}:
                self.console.print("Usage: /question | /question skip|cancel")
                return None
            pending = self._interaction_controller.state().get("pending_question")
            if not pending:
                raise InteractionError("no question is pending")
            self._interaction_controller.resolve_question(
                pending["request_id"], {}, action=action,
            )
            if action == "cancel":
                self.console.print("Pending question cancelled.")
                return None
            resolution = "skipped" if action == "skip" else "cancelled"
            user_input = (
                f"Original request:\n{pending.get('original_input', '')}\n\n"
                f"Clarification was {resolution} by the user. Continue only if safe; otherwise explain the blocker."
            )
        except InteractionError as exc:
            self.console.print(str(exc))
            return None

    if user_input.lower().startswith("/tasks"):
        parts = user_input.split(maxsplit=2)
        if len(parts) == 1 or (len(parts) > 1 and parts[1].lower() == "list"):
            self.console.print(self.tasks.format())
        elif len(parts) == 3 and parts[1].lower() == "resume":
            resumed = self.tasks.resume(parts[2].strip())
            if not resumed:
                self.console.print(f"[{self._ERROR}]Failed or blocked task not found.[/{self._ERROR}]")
            else:
                result = TaskWorker(self.tasks, self._execute_durable_task, max_tasks=1).run_once()
                self.console.print(json.dumps(result or {"status": "queued"}, ensure_ascii=False, indent=2, default=str))
        else:
            self.console.print("Usage: /tasks list | /tasks resume <task-id>")
        return None

    if user_input.lower() == "/history" or user_input.lower().startswith("/history "):
        self.console.print(Panel(self.history_text(), title="Conversation history", border_style=self._ACCENT))
        return None

    if user_input.lower().startswith("/rollback"):
        parts = user_input.split(maxsplit=1)
        argument = parts[1].strip() if len(parts) > 1 else ""
        if argument.lower() == "help":
            self.console.print("Use /history to view the numbered conversation tree.")
            self.console.print("Then use /rollback <number> to preview, or /rollback <number> confirm.")
            return None
        if argument.lower() == "cancel":
            self.console.print("Rollback cancelled.")
            return None
        if not argument:
            self.console.print(Panel(self.history_text(), title="Choose a rollback point", border_style=self._ACCENT))
            argument = self.console.input("Select a node number (or cancel): ").strip()
            if not argument or argument.lower() == "cancel":
                self.console.print("Rollback cancelled.")
                return None
        selector_parts = argument.split()
        if len(selector_parts) > 2 or (len(selector_parts) == 2 and selector_parts[1].lower() != "confirm"):
            self.console.print("Usage: /rollback <number> [confirm] | /rollback cancel")
            return None
        selector = selector_parts[0]
        confirmed = len(selector_parts) == 2
        manager = self.history_manager()
        try:
            target = manager.resolve_selector(selector)
        except HistoryError as exc:
            self.console.print(str(exc))
            return None
        display_selector = target.get("selector", selector)
        changes = target.get("file_summary", {}).get("changes", {})
        self.console.print(
            f"Restore [{display_selector}] {target.get('summary') or 'this point'}: "
            f"+{changes.get('added', 0)} added, "
            f"~{changes.get('changed', 0)} changed, -{changes.get('deleted', 0)} deleted. "
            "Durable memory is preserved."
        )
        if not confirmed and self.console.input('Type "rollback" to confirm (or cancel): ').strip().lower() != "rollback":
            self.console.print("Rollback cancelled.")
            return None
        try:
            current = manager.current()
            if current is None:
                raise HistoryError("no history has been recorded for this conversation")
            result = self.restore_history(target["id"], confirm="rollback", expected_head=current["id"])
            self.console.print(f"Restored [{display_selector}]. Recovery point saved: {result['recovery']['id']}")
        except HistoryError as exc:
            self.console.print(f"Rollback failed: {exc}")
        return None

    if user_input.lower().startswith("/agent"):
        parts = user_input.split(maxsplit=1)
        if len(parts) == 1:
            self.console.print(f"Agent profile: {self._agent_profile_mode}")
        elif parts[1].lower() in {"auto", "coder", "researcher"}:
            self._agent_profile_mode = parts[1].lower()
            self._restore_user_preferences()
            self.console.print(f"Agent profile set to {self._agent_profile_mode}.")
        else:
            self.console.print("Usage: /agent auto|coder|researcher")
        return None

    if user_input.lower().startswith("/learning"):
        parts = user_input.split(maxsplit=2)
        subcommand = parts[1].lower() if len(parts) > 1 else "status"
        if subcommand == "status":
            profile_filter = parts[2].strip() if len(parts) > 2 and parts[2].strip() in {"coder", "researcher"} else None
            proposals = self.learning_engine.status(50, profile=profile_filter)
            if not proposals:
                self.console.print("No learning proposals.")
            else:
                lines = ["Learning proposals:"]
                for proposal in proposals:
                    metrics = proposal.get("artifact_metrics", {})
                    lines.append(
                        f"- {proposal['id']} [{proposal['lifecycle_stage']}] profile={proposal.get('profile') or '-'} "
                        f"uses={metrics.get('verified_uses', 0)} ok={metrics.get('successful_uses', 0)} "
                        f"failed={metrics.get('failed_uses', 0)} predecessor={proposal.get('predecessor') or '-'}: "
                        f"{proposal['content'][:100]}"
                    )
                self.console.print("\n".join(lines))
        elif subcommand == "rollback" and len(parts) > 2:
            result = self.learning_engine.rollback(parts[2].strip())
            self.console.print("Learning proposal rolled back." if result else "Proposal not found.")
        elif subcommand == "explain" and len(parts) > 2:
            proposals = [p for p in self.learning_engine.status(1000) if p["id"] == parts[2].strip()]
            self.console.print(json.dumps(proposals[0], ensure_ascii=False, indent=2) if proposals else "Proposal not found.")
        elif subcommand == "evidence" and len(parts) > 2:
            card = self.learning_engine.evidence_card(parts[2].strip())
            self.console.print(json.dumps(card, ensure_ascii=False, indent=2) if card else "Proposal not found.")
        elif subcommand == "replay" and len(parts) > 2:
            self.console.print("Replay accepts paired frozen results through POST /api/v2/learning/<id>/replay; it never runs live commands.")
        elif subcommand == "metrics":
            profile_filter = parts[2].strip() if len(parts) > 2 and parts[2].strip() in {"coder", "researcher"} else None
            self.console.print(json.dumps(self.learning_engine.metrics(profile_filter), ensure_ascii=False, indent=2))
        else:
            self.console.print("Usage: /learning status [coder|researcher] | /learning metrics [profile] | /learning rollback <id> | /learning explain|evidence|replay <id>")
        return None

    if user_input.lower().startswith("/memory "):
        parts = user_input.split(maxsplit=2)
        if len(parts) == 3 and parts[1].lower() == "why":
            claim = self.learning_engine.explain_claim(parts[2].strip())
            self.console.print(json.dumps(claim, ensure_ascii=False, indent=2) if claim else "Memory claim not found.")
        elif len(parts) == 3 and parts[1].lower() == "forget":
            self.console.print("Memory claim forgotten." if self.learning_engine.forget_claim(parts[2].strip()) else "Memory claim not found.")
        else:
            self.console.print("Usage: /memory why|forget <claim-id>")
        return None

    # /forget — show and optionally delete recent learnings
    if user_input.lower().startswith("/forget"):
        parts = user_input.split(maxsplit=1)
        prefix = parts[1] if len(parts) > 1 else ""
        self.console.print(Panel(self._forget_recent(prefix), title="Forget", border_style=self._WARNING))
        if prefix:
            # Auto-delete entries matching prefix (lowest score first)
            recent = self.memory_bank.get_recent(100)
            matched = [r for r in recent if r and prefix.lower() in r.lower() and not r.startswith("FILE:")]
            if matched:
                self.memory_bank.delete_logs(matched[:3])
                self.console.print(f"[{self._SUCCESS}]Deleted {min(3, len(matched))} entries matching '{prefix}'.[/{self._SUCCESS}]")
        return None

    return user_input
