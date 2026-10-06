"""Runtime integration for chat-owned, independently scoped delegation."""
from __future__ import annotations

import copy
import json
import os
import time
import uuid
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
                        return [self._record_turn_receipt(ExecutionReceipt(**{
                            key: record[key] for key in ExecutionReceipt.__dataclass_fields__ if key in record}))
                            for record in run.get("result", {}).get("tool_records", [])
                            if record.get("success") and record.get("started_at")]
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


def spawn_agents(self, args):
    """Automatically start parallel specialist assignments; args is a JSON assignment batch."""
    if self.execution_context.child_run_id:
        raise ValueError("Only the main agent can delegate")
    return json.dumps({"agents": self.delegation().spawn(_json_args(args).get("assignments"))}, ensure_ascii=False)


def send_subagent(self, args):
    """Reuse a finished sub-agent; args JSON contains run_id and a complete assignment."""
    value = _json_args(args)
    return json.dumps(self.delegation().send(value.get("run_id"), value.get("assignment")), ensure_ascii=False)


def list_subagents(self, args):
    """List agents or inspect one run_id from this chat/project; args is JSON."""
    value = _json_args(args)
    coordinator = self.delegation()
    return json.dumps(coordinator.detail(value["run_id"]) if value.get("run_id") else
                      {"agents": [coordinator.summary(run, results=True) for run in coordinator.list()]}, ensure_ascii=False)


def wait_subagents(self, args):
    """Wait up to 60 seconds for delegated work and its mandatory reviews; args is JSON."""
    value = _json_args(args)
    coordinator = self.delegation()
    return json.dumps({"agents": [coordinator.summary(run, results=True) for run in coordinator.wait(value.get("run_ids"), value.get("timeout", 30))]}, ensure_ascii=False)


def cancel_subagent(self, args):
    """Cancel a scoped sub-agent and prevent subsequent tool execution; args JSON contains run_id."""
    return json.dumps(self.delegation().cancel(_json_args(args).get("run_id")), ensure_ascii=False)


def _invoke_delegated(self, run, *, review, feedback, coordinator):
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
    if provider_name == main_config.provider:
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
    child_id = run["run_id"] + ("_review_" + uuid.uuid4().hex if review else "")
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
    tools = allowed_tool_names(adapters.AVAILABLE_TOOLS, ",".join(capabilities)) - set(TOOLS)
    child._delegation_parent = parent
    profile = replace(profile, metadata={**profile.metadata, "structured": True, "review": review,
        "display_name": run["reviewer"] if review else run["name"], "assignment": brief,
        "artifacts": copy.deepcopy(run.get("report", {}).get("artifacts", [])) if review else []})
    task = ("Independently verify this assignment and submitted result. Read actual evidence with tools. "
            "Independently read every declared artifact, including references outside the assignment's scope. "
            "Find incorrect claims, missing acceptance checks, and regressions. Do not trust an author's success claim. "
            "Check each asserted affected input against the source and language/library semantics; a plausible example is not a reproduced check. "
            "If an exact behavior cannot be established, report the uncertainty rather than verify it.\n"
            + json.dumps({"assignment": brief, "submitted": run.get("report"),
                          "receipts": [{key: item for key, item in receipt.items() if key != "result"}
                              for receipt in run.get("result", {}).get("tool_records", [])]}, ensure_ascii=False)) if review else json.dumps(brief, ensure_ascii=False)
    if feedback:
        task += "\nCorrect these review findings and return an updated report:\n" + json.dumps(feedback, ensure_ascii=False)
    if not review:
        child.messages = copy.deepcopy(coordinator.messages.get(run["run_id"], []))
    context = ExecutionContext(model=model, provider=provider, provider_config=config,
        capability_token=issue_capability_token("subagent:" + child_id, capabilities),
        child_run_id=run["run_id"], coordinator=coordinator)
    turn_token = self._turn_context.set(context)
    state_token = self._active_context_state.set(ContextState(model, config.context_window_tokens))
    usage_token = self._active_usage_run_id.set(child_id)
    started = time.monotonic()
    try:
        with self.use_session(child):
            result = self._run_subagent_llm_result(profile, task, [], tools)
            result["session_id"] = child_id
            result["metrics"] = self._subagent_usage_metrics(child_id, result.pop("started"))
            return result
    finally:
        with coordinator.lock:
            run.setdefault("usage", {})[child_id] = self._subagent_usage_metrics(child_id, started)
        self._active_usage_run_id.reset(usage_token)
        self._active_context_state.reset(state_token)
        self._turn_context.reset(turn_token)


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
    if action in {"write_file", "edit_file", "read_file", "search_files"}:
        raw = str(args).split("|", 1)[0].strip()
        if action in {"edit_file", "read_file", "search_files"} and raw.startswith("{"):
            try:
                request = json.loads(str(args))
                if isinstance(request, dict):
                    default = "." if action == "search_files" else ""
                    raw = request.get("path", default)
                else:
                    raw = "." if action == "search_files" else ""
            except json.JSONDecodeError:
                raw = "." if action == "search_files" else ""
        if action == "search_files" and not raw:
            raw = "."
        path = str(self.current_session.workspace.adapters._resolve_workspace_path(raw))
    if coordinator:
        coordinator.check_cancelled(context.child_run_id)
        if action in {"write_file", "edit_file"}:
            brief = coordinator.runs[context.child_run_id]["assignment"]
            owned = {str((coordinator.root / p).resolve()) for p in brief["scope"]}
            if path not in owned:
                raise ValueError("Sub-agent write is outside its assigned files")
        parent = getattr(self.current_session, "_delegation_parent", None)
        if parent and parent.interaction.state().get("executing_plan") and self._is_state_changing_action(action, str(args)):
            allowed, reason = parent.tasks.mutation_matches_current_task(action, str(args))
            if not allowed:
                raise ValueError("Sub-agent action does not match accepted plan: " + reason)
    guarded = action in {"read_file", "write_file", "edit_file", "search_files"} or self._is_state_changing_action(action, str(args)) or action in {"git_diff", "git_show"}
    if access and guarded:
        cancelled = (lambda: coordinator.cancelled[context.child_run_id].is_set()) if coordinator else (lambda: False)
        with access.acquire(path, cancelled, context.child_run_id,
                            reading=action in {"read_file", "search_files"}):
            yield
    else:
        yield
