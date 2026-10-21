"""Runtime integration for chat-owned, independently scoped delegation."""
from __future__ import annotations
from openkyrozen.tools.models import ToolError, ToolResult, command_tool_result, legacy_text_api, tool_failure, tool_json_result, tool_success

import copy
import json
import os
import time
import uuid
from pathlib import Path
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import replace

from openkyrozen.agent.delegation import Delegation, TERMINAL, TOOLS, WorkspaceAccess
from openkyrozen.agent.models import WorkspaceState
from openkyrozen.agent.turn import ExecutionContext
from openkyrozen.agent.compaction import ContextState
from openkyrozen.app.config import load_agent_config, effective_capabilities
from openkyrozen.providers.config import ProviderConfig, _ambient_provider_available
from openkyrozen.providers.registry import PROVIDER_REGISTRY
from openkyrozen.providers.custom import load_custom_provider_profile
from openkyrozen.security.capabilities import issue_capability_token
from openkyrozen.security.tool_policy import allowed_tool_names, resolve_capabilities


def _plan_delegation(self, turn):
    """Make one explicit main-model coordination decision after discovery."""
    from openkyrozen.agent.delegation import GUIDANCE
    messages = [{"role": "system", "content":
        "You coordinate the user's work. Decide its distribution using the actual discovered targets. "
        "Use separate investigations when independent specialist reasoning adds coverage; a small source file can still be a distinct security or correctness boundary. "
        "Group closely related helpers into one investigation; do not assign an agent to every file. "
        "Exclude the assistant's own generated state/skills unless the request concerns that runtime. "
        "Do not assign extra reviewers here: independent cross-examination is already automatic. "
        "A single lookup, greeting, or tightly coupled operation stays direct. Do not assume delegation requires a user request for agents.\n"
        + GUIDANCE + "\nReturn only a JSON object, without tool calls, XML, HTML, commentary, or Markdown. "
        'Shape: {"decision":"direct|delegate","reason":"why this distribution fits","assignments":[]}. '
        "For delegate, assignments must use complete objective, context, scope, dependencies, acceptance, deliverables, reason, and profile fields. "
        "Include the user's constraints and any discovered requirements in each context. Read requirements before assessing launch readiness. "
        "Limit findings to the assigned objectives; avoid speculative defects in unrelated helpers. For direct, assignments is empty. "
        "If targets are not yet known, choose direct so the main agent can inspect further. "
        "Do not create duplicate assignments or delegate direct parent plan steps. Available profiles: "
        + ", ".join(self.subagent_manager.profiles)},
        {"role": "user", "content": json.dumps({"request": turn.user_input,
            "workspace": str(self._get_workspace_root()), "mode": turn.interaction_mode,
            "capabilities": sorted(self._execution_capability_token.capabilities),
            "existing_agents": self._delegation_summary(),
            "discovered_evidence": turn.tool_results_text}, ensure_ascii=False)}]
    for attempt in range(3):
        callback_token = self._stream_event_callback.set(lambda _: None) if callable(self._stream_event_callback.get()) else None
        try:
            text = self._call_llm_with_spinner(messages, private=True).strip()
        finally:
            if callback_token is not None:
                self._stream_event_callback.reset(callback_token)
        turn.turn_prompt_total += self._last_prompt_tokens
        turn.turn_completion_total += self._last_completion_tokens
        try:
            decision = json.loads(text.removeprefix("```json").removeprefix("```").removesuffix("```").strip())
            if (decision.get("decision") not in {"direct", "delegate"} or not isinstance(decision.get("reason"), str)
                    or not decision["reason"].strip() or not isinstance(decision.get("assignments"), list)
                    or bool(decision["assignments"]) != (decision["decision"] == "delegate")):
                raise ValueError("Invalid distribution decision")
            self.memory_bank.store.append_event("agent.delegation_decided", {"decision": decision["decision"],
                "reason": self._fix_safe_text(decision["reason"], 2000), "assignment_count": len(decision["assignments"]),
                "run_id": turn.learning_run["run_id"]},
                user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
                session_id=self.memory_bank.session_id)
            if decision["decision"] == "delegate":
                return "Action: " + json.dumps({"action": "spawn_agents", "args": json.dumps({"assignments": decision["assignments"]})})
            return None
        except (ValueError, TypeError, AttributeError):
            messages.extend([{"role": "assistant", "content": text}, {"role": "user", "content":
                "Invalid coordination JSON; no action was executed. Return the exact decision/reason/assignments object."}])
    self.memory_bank.store.append_event("agent.delegation_decision_failed", {"attempts": 3, "run_id": turn.learning_run["run_id"]},
        user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
        session_id=self.memory_bank.session_id)
    return None


def delegation(self):
    manager = self.subagent_manager
    if manager.coordinator is None:
        config = load_agent_config(self._get_workspace_root())["subagents"]
        with self._delegation_lock:
            if manager.coordinator is None:
                key = str(self._get_workspace_root().resolve())
                access = self._workspace_access.setdefault(key, WorkspaceAccess())
                def capture_context():
                    context = copy_context()
                    context.run(self._turn_context.set, copy.copy(self.execution_context))
                    return context
                def reconcile(run):
                    from openkyrozen.agent.types import ExecutionReceipt
                    with self._delegation_lock:
                        task_id = run.get("assignment", {}).get("task_id")
                        review = (run.get("reviews") or [{}])[-1]
                        acceptance = None
                        if task_id and self.tasks.active_working_task(task_id):
                            if review.get("verdict") == "verified" and review.get("evidence"):
                                acceptance = "Independent review verified: " + str(review.get("summary", "assignment"))[:300]
                        receipts = []
                        for record in run.get("result", {}).get("tool_records", []):
                            if not record.get("success") or not record.get("started_at"):
                                continue
                            if acceptance and record.get("task_id") == task_id:
                                record = {**record, "acceptance": acceptance}
                            receipts.append(self._record_turn_receipt(ExecutionReceipt.from_dict(record)))
                        return receipts
                manager.coordinator = Delegation(manager.memory, self._invoke_delegated,
                    profiles=manager.profiles, root=key, concurrency=config["concurrency"],
                    emit=self._emit_stream_event, access=access, capture_context=capture_context,
                    redact=lambda text: self._fix_safe_text(text, 200000, preserve_lines=True), on_verified=reconcile)
    return manager.coordinator


def _json_args(args):
    value = json.loads(args or "{}")
    if not isinstance(value, dict):
        raise ValueError("Delegation arguments must encode a JSON object")
    return value


def _typed_json_args(args):
    if isinstance(args, dict):
        return args, None
    try:
        return _json_args(args), None
    except (TypeError, ValueError) as exc:
        return None, tool_failure("invalid_arguments", f"Error: invalid delegation arguments: {exc}")


@legacy_text_api
def spawn_agents(self, args):
    """Automatically start parallel specialist assignments; args is a JSON assignment batch."""
    if self.execution_context.child_run_id:
        raise ValueError("Only the main agent can delegate")
    value, error = _typed_json_args(args)
    if error:
        return error
    assignments = value.get("assignments")
    if isinstance(assignments, list):
        for assignment in assignments:
            task_id = assignment.get("task_id") if isinstance(assignment, dict) else None
            if task_id is not None and not self._valid_receipt_task_binding(self.current_session, task_id):
                return tool_failure("invalid_arguments", "Error: assignment task_id must identify a pending task in the active Working Plan.")
    data = {"agents": self.delegation().spawn(assignments, budget=self.execution_context.delegation_budget)}
    return tool_json_result(data, text=json.dumps(data, ensure_ascii=False))


@legacy_text_api
def send_subagent(self, args):
    """Reuse a finished sub-agent; args JSON contains run_id and a complete assignment."""
    value, error = _typed_json_args(args)
    if error:
        return error
    assignment = value.get("assignment")
    task_id = assignment.get("task_id") if isinstance(assignment, dict) else None
    if task_id is not None and not self._valid_receipt_task_binding(self.current_session, task_id):
        return tool_failure("invalid_arguments", "Error: assignment task_id must identify a pending task in the active Working Plan.")
    data = self.delegation().send(value.get("run_id"), assignment,
                                  budget=self.execution_context.delegation_budget)
    return tool_json_result(data, text=json.dumps(data, ensure_ascii=False))


@legacy_text_api
def list_subagents(self, args):
    """List agents or inspect one run_id from this chat/project; args is JSON."""
    value, error = _typed_json_args(args)
    if error:
        return error
    coordinator = self.delegation()
    data = coordinator.detail(value["run_id"]) if value.get("run_id") else {
        "agents": [coordinator.summary(run, results=True) for run in coordinator.list()]}
    return tool_json_result(data, text=json.dumps(data, ensure_ascii=False))


@legacy_text_api
def wait_subagents(self, args):
    """Wait up to 60 seconds for delegated work and its mandatory reviews; args is JSON."""
    value, error = _typed_json_args(args)
    if error:
        return error
    coordinator = self.delegation()
    data = {"agents": [coordinator.summary(run, results=True) for run in coordinator.wait(value.get("run_ids"), value.get("timeout", 30))]}
    return tool_json_result(data, text=json.dumps(data, ensure_ascii=False))


@legacy_text_api
def cancel_subagent(self, args):
    """Cancel a scoped sub-agent and prevent subsequent tool execution; args JSON contains run_id."""
    value, error = _typed_json_args(args)
    if error:
        return error
    data = self.delegation().cancel(value.get("run_id"))
    return tool_json_result(data, text=json.dumps(data, ensure_ascii=False))


def _invoke_delegated(self, run, *, review, feedback, coordinator, report_repair=False):
    parent = self.current_session
    brief = run["assignment"]
    role = "reviewer" if review else run["profile"]
    settings = load_agent_config(coordinator.root)["subagents"]["roles"].get(role, {})
    main_config = copy.deepcopy(self._provider_config)
    if main_config is None:
        raise self.ProviderUnavailableError("Main provider is not configured")
    provider_name = (settings.get("provider", main_config.provider) if review else
                     brief.get("provider", settings.get("provider", main_config.provider)))
    if provider_name not in PROVIDER_REGISTRY:
        raise ValueError("Unknown sub-agent provider")
    spec = PROVIDER_REGISTRY[provider_name]
    model = (settings.get("model") if review else brief.get("model", settings.get("model")))
    if provider_name == "custom":
        profile_name = (settings.get("custom_profile") if review else
                        brief.get("custom_profile", settings.get("custom_profile")))
        if profile_name:
            config = load_custom_provider_profile(str(profile_name))
        elif main_config.provider == "custom":
            config = main_config
        else:
            raise ValueError("Set custom_profile for a custom sub-agent provider")
        model = model or config.model_complex
    elif provider_name == main_config.provider:
        config = main_config
        model = model or self.DEEPSEEK_MODEL
    else:
        # Explicitly override generic KYROZEN_* defaults: they belong to the main provider.
        key = os.environ.get(spec.api_key_env, "") if spec.api_key_env else ""
        if not key and not _ambient_provider_available(provider_name):
            raise self.ProviderUnavailableError(f"Missing credentials for {provider_name}; configure {spec.api_key_env or 'ambient authentication'}")
        config = ProviderConfig(provider=provider_name, api_key=key,
            model_simple=spec.model_simple, model_complex=spec.model_complex,
            context_window_tokens=spec.context_window_tokens)
        config.base_url = spec.base_url
        model = model or spec.model_complex or spec.model_simple
    if not model:
        raise ValueError("Sub-agent provider requires a model/deployment")
    config.model_simple = config.model_complex = model
    with coordinator.lock:
        if review:
            run["review_agent"].update(status="reviewing", provider_model=f"{provider_name}:{model}")
        else:
            run["provider_model"] = f"{provider_name}:{model}"
        coordinator._publish(run)
    provider = self.get_provider(config)  # No implicit cross-provider fallback.
    child_id = run.get("budget_id", run["run_id"]) + ("_review_" + uuid.uuid4().hex if review else "")
    adapters = self.workspace_factory(root=coordinator.root)
    workspace = WorkspaceState(str(coordinator.root), adapters, parent.workspace.launch_context,
                               parent.workspace.graph, parent.workspace.github)
    child = self._create_session(child_id, parent.memory.user_id, workspace, parent.tasks.workspace_id)
    adapters.set_project_graph(parent.workspace.graph)
    adapters.set_github_cli(parent.workspace.github)
    profile = self.subagent_manager.profiles[role]
    capabilities = self._execution_capability_token.capabilities & resolve_capabilities(profile.capabilities)
    capabilities &= effective_capabilities(load_agent_config(coordinator.root))
    if review or self._active_interaction_mode.get() in {"ask", "plan"}:
        capabilities &= resolve_capabilities("readonly")
    tools = (allowed_tool_names(adapters.AVAILABLE_TOOLS, ",".join(capabilities)) - set(TOOLS)) if not report_repair else set()
    child._delegation_parent = parent
    child._delegation_task_id = brief.get("task_id") if not review else None
    profile = replace(profile, metadata={**profile.metadata, "structured": True, "review": review,
        "display_name": run["reviewer"] if review else run["name"], "assignment": brief,
        "artifacts": copy.deepcopy(run.get("report", {}).get("artifacts", [])) if review else []})
    allowed_artifacts = {(coordinator.root / path).resolve() for path in run["assignment"]["scope"]}
    for record in run.get("result", {}).get("tool_records", []):
        if record.get("success") and record.get("action") == "read_file":
            args = record.get("arguments") or record.get("args") or {}
            if isinstance(args, str):
                try: args = json.loads(args)
                except json.JSONDecodeError: args = {"path": args.split("|", 1)[0]}
            path = args.get("path") if isinstance(args, dict) else None
            if path:
                allowed_artifacts.add((coordinator.root / path).resolve())
        if record.get("success") and record.get("action") in {"write_file", "edit_file", "apply_patch"}:
            args = record.get("args") or {}
            if isinstance(args, str):
                try: args = json.loads(args)
                except json.JSONDecodeError: args = {"path": args.split("|", 1)[0]}
            path = args.get("path") if isinstance(args, dict) else None
            if path:
                allowed_artifacts.add((coordinator.root / path).resolve())
    child._delegation_review_artifacts = [
        item if isinstance(item, str) else item.get("path", "")
        for item in profile.metadata["artifacts"]
        if isinstance(item, str) or isinstance(item, dict) and item.get("path")
    ] if review else []
    child._delegation_review_artifacts = [path for path in child._delegation_review_artifacts
        if any((coordinator.root / path).resolve() == allowed or
               (coordinator.root / path).resolve().is_relative_to(allowed)
               for allowed in allowed_artifacts)]
    task = ("Independently verify this assignment and submitted result. Read actual source evidence and every declared artifact with tools. "
            "Independently read every declared artifact, including references outside the assignment's scope. "
            "Find incorrect claims, missing acceptance checks, and regressions. Do not trust an author's success claim. "
            "Check each asserted affected input against the source and language/library semantics; a plausible example is not a reproduced check. "
            "If an exact behavior cannot be established, report the uncertainty rather than verify it.\n"
            + json.dumps({"assignment": brief, "submitted": run.get("report"),
                          "receipts": [{key: item for key, item in receipt.items() if key != "result"}
                              for receipt in run.get("result", {}).get("tool_records", [])]}, ensure_ascii=False)) if review else json.dumps(brief, ensure_ascii=False)
    if report_repair:
        task = ("Convert the previous response into the required JSON report. Do not call tools or add claims.\n"
                + json.dumps({"assignment": brief,
                              "previous_response": feedback.get("invalid_response", "") if isinstance(feedback, dict) else ""},
                             ensure_ascii=False))
    if feedback:
        task += ("\nReturn a corrected JSON report using only the supplied response.\n" if report_repair else
                 "\nCorrect these review findings and return an updated report:\n") + json.dumps(feedback, ensure_ascii=False)
    task = (
        f"You are the {role} sub-agent. {profile.system_prompt}\n\n"
        + task + "\n\n"
        + ("Return only a JSON review with verdict (verified|changes_requested|unverifiable), summary, findings, and evidence. "
           "A verified verdict requires independent tool inspection and evidence bound to actual receipts."
           if review else
           "Return only a JSON report with status (completed|partial|blocked|failed), summary, findings, evidence, "
           "artifacts, checks, uncertainties, and suggested_followups. Completed requires checked acceptance criteria; "
           "cite actual tool receipt IDs. Do not claim unsupported results.")
    )
    child.interaction.set_mode("ask" if review or report_repair else "agent")
    context = ExecutionContext(model=model, provider=provider, provider_config=config,
        capability_token=issue_capability_token("subagent:" + child_id, capabilities),
        child_run_id=run["run_id"], coordinator=coordinator,
        delegation_budget=coordinator.budgets.get(run["run_id"]),
        delegation_budget_run_id=run.get("budget_id", run["run_id"]),
        delegation_tools=frozenset(tools), max_steps=profile.max_steps)
    turn_token = self._turn_context.set(context)
    state_token = self._active_context_state.set(ContextState(model, config.context_window_tokens))
    usage_token = self._active_usage_run_id.set(child_id)
    parent_publisher = self._runtime_event_publisher.get()
    event_token = None
    if parent_publisher is not None:
        child_publisher = parent_publisher.fork(session_id=child.memory.session_id)
        event_token = self._runtime_event_publisher.set(child_publisher)
    conversation_token = self._conversation_turn_id.set(f"turn_{child_id}_{uuid.uuid4().hex}")
    record_token = self._record_foreground_model.set(True)
    started = time.monotonic()
    try:
        with self.use_session(child):
            before_receipts = {
                event["payload"].get("receipt_id")
                for event in child.memory.store.list_events(
                    "execution.receipt", limit=None, user_id=child.memory.user_id,
                    workspace_id=child.tasks.workspace_id, session_id=child.memory.session_id,
                )
            }
            answer = self.chat(child, task, profile=role, approve=self._approval_callback.get())
            answer = answer.split("\n\nTool status:", 1)[0].split("\n\nPlan status:", 1)[0]
            receipt_events = child.memory.store.list_events(
                "execution.receipt", limit=None, user_id=child.memory.user_id,
                workspace_id=child.tasks.workspace_id, session_id=child.memory.session_id,
            )
            tool_records = [event["payload"] for event in receipt_events
                            if event["payload"].get("receipt_id") not in before_receipts]
            for receipt in tool_records:
                coordinator.record_receipt(run["run_id"], child_id, receipt)
            if answer.startswith("Native model request failed:"):
                raise RuntimeError(answer)
            result = {
                "result": answer,
                "session_id": child_id,
                "tool_records": tool_records,
                "evidence": [{"receipt_id": item["receipt_id"], "action": item["action"],
                              "success": True} for item in tool_records if item.get("success")],
                "metrics": self._subagent_usage_metrics(child_id, started),
            }
            return result
    finally:
        self._record_foreground_model.reset(record_token)
        self._conversation_turn_id.reset(conversation_token)
        with coordinator.lock:
            run.setdefault("usage", {})[child_id] = self._subagent_usage_metrics(child_id, started)
        self._active_usage_run_id.reset(usage_token)
        self._active_context_state.reset(state_token)
        self._turn_context.reset(turn_token)
        if event_token is not None:
            self._runtime_event_publisher.reset(event_token)


def _delegation_summary(self):
    coordinator = self.subagent_manager.coordinator
    if coordinator is None:
        return ""
    runs = coordinator.list()
    return json.dumps([{"name": run["name"], "status": run["status"],
        "report": run.get("report"), "reviews": [{key: item for key, item in review.items() if key != "tool_receipts"} for review in run["reviews"]],
        "error": run.get("error")} for run in runs], ensure_ascii=False)


@contextmanager
def _delegation_tool_access(self, action, args):
    context = self.execution_context
    coordinator = context.coordinator
    access = coordinator.access if coordinator else self._workspace_access.setdefault(
        str(self._get_workspace_root().resolve()), WorkspaceAccess(),
    )
    path = None
    path_fields = {
        "write_file": ("path", ""), "edit_file": ("path", ""), "apply_patch": ("path", ""),
        "read_file": ("path", ""), "search_files": ("path", "."),
        "list_dir": ("path", "."), "list_tree": ("path", "."), "find_files": ("directory", "."),
    }
    if action in path_fields:
        field, default = path_fields[action]
        parts = str(args).split("|", 1) if not isinstance(args, dict) else []
        raw = args.get(field, default) if isinstance(args, dict) else (
            parts[1].strip() if action == "find_files" and len(parts) > 1 else parts[0].strip()
        )
        if isinstance(args, str) and (action in {"edit_file", "apply_patch", "read_file", "search_files",
                                                 "list_dir", "list_tree", "find_files"}) and raw.startswith("{"):
            try:
                request = json.loads(str(args))
                if isinstance(request, dict):
                    raw = request.get(field, default)
                else:
                    raw = default
            except json.JSONDecodeError:
                raw = default
        path = Path(self.current_session.workspace.adapters._resolve_workspace_path(raw or default)).resolve()
    if coordinator:
        coordinator.check_cancelled(context.child_run_id)
        from openkyrozen.security.tool_policy import tool_policy
        tools = getattr(self, "AVAILABLE_TOOLS", None)
        policy = tool_policy(action, tools)
        spec = getattr(getattr(self, "tool_registry", None), "specs", {}).get(action)
        capability = spec.capability if spec is not None else (
            policy.get("capability", "unknown") if isinstance(policy, dict)
            else getattr(policy, "capability", "unknown")
        )
        effects = spec.side_effects if spec is not None else (
            policy.get("side_effects", "unknown") if isinstance(policy, dict)
            else getattr(policy, "side_effects", "unknown")
        )
        read_only_network = capability == "network" and action in {
            "search_web", "read_webpage", "analyze_remote_repo", "github_status", "github_read",
        }
        if action not in {"write_file", "edit_file", "apply_patch"} and not read_only_network and (
            capability in {"write", "shell", "git", "browser", "destructive"}
            or effects not in {"none", "read", "read_only"}
        ):
            raise PermissionError(f"Sub-agent capability '{capability}' is not confined to assigned files")
        run = coordinator.runs[context.child_run_id]
        brief = run["assignment"]
        writing = action in {"write_file", "edit_file", "apply_patch"}
        reading = action in {"read_file", "search_files", "list_dir", "list_tree", "find_files"}
        if writing or reading:
            scopes = [Path(coordinator.root / item) for item in brief["scope"]]
            if reading:
                scopes.extend(Path(coordinator.root / item)
                              for item in getattr(self.current_session, "_delegation_review_artifacts", []))
            within_scope = any(path == scope or (scope.is_dir() and path.is_relative_to(scope)) for scope in scopes)
            if not within_scope:
                raise ValueError("Sub-agent read is outside its assigned scope" if reading else
                                 "Sub-agent write is outside its assigned files")
        workspace_wide_reads = {"git_status", "git_diff", "git_log", "git_show",
                                 "graph_query", "graph_explain", "graph_path", "graph_status"}
        if action in workspace_wide_reads and "." not in brief["scope"]:
            raise ValueError("Sub-agent workspace-wide read is outside its assigned scope")
        parent = getattr(self.current_session, "_delegation_parent", None)
        parent_state = parent.interaction.state() if parent else {}
        parent_plan = parent_state.get("executing_plan") if parent_state else None
        if parent and parent_plan:
            from openkyrozen.agent.executor import (
                _awaiting_revision_mutation, _legacy_object_arguments, _strategic_plan_denial,
            )
            arguments = args if isinstance(args, dict) else None
            spec = getattr(getattr(self, "tool_registry", None), "specs", {}).get(action)
            if arguments is None and isinstance(args, str):
                if spec is not None:
                    normalized = _legacy_object_arguments(args, spec)
                    arguments = normalized if normalized else None
            match_args = (json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                          if arguments is not None else str(args))
            parent_runtime = parent if callable(getattr(parent, "_is_state_changing_action", None)) else self
            if _awaiting_revision_mutation(parent_state, action, arguments if arguments is not None else args,
                                           spec, parent_runtime):
                raise PermissionError("Parent Strategic Plan revision is awaiting approval")
            denial = _strategic_plan_denial(
                parent_plan, action, arguments if arguments is not None else args,
                spec, parent.workspace.root,
            )
            if denial:
                raise PermissionError(f"Sub-agent operation exceeds the accepted Strategic Plan: {denial}")
            if self._is_state_changing_action(action, args):
                allowed, reason = parent.tasks.mutation_matches_current_task(action, match_args)
                if not allowed:
                    raise ValueError("Sub-agent action does not match accepted plan: " + reason)
    guarded = action in path_fields or self._is_state_changing_action(action, args) or action in {"git_diff", "git_show"}
    if access and guarded:
        cancelled = (lambda: coordinator.cancelled[context.child_run_id].is_set()) if coordinator else (lambda: False)
        with access.acquire(str(path) if path is not None else None, cancelled, context.child_run_id,
                            reading=action in path_fields and action not in {"write_file", "edit_file", "apply_patch"},
                            recursive=action in {"search_files", "find_files", "list_tree"}):
            yield
    else:
        yield
