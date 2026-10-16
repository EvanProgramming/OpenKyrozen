"""Application agent runtime; all mutable state belongs to an instance."""
from __future__ import annotations

import copy
import asyncio
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from .models import AgentSession, WorkspaceState
from .turn import ExecutionContext
from .ports import EventSink, RuntimeEventSink, Approval


class AgentRuntime:
    """Coordinates feature functions using explicitly injected services."""


    from openkyrozen.agent.preparation import _prepare_turn
    from openkyrozen.agent.initial_response import _initial_turn_response
    from openkyrozen.agent.action_rounds import _execute_action_rounds
    from openkyrozen.agent.completion import _complete_turn
    from openkyrozen.agent.response_recovery import _observe_turn_response, _recover_plan_response

    # agent.protocol
    from openkyrozen.agent.protocol import (
        _is_valid_action,
        _detect_unknown_action,
        _has_unsupported_action_protocol,
        parse_json_from_response,
        _extract_json_objects,
        _collect_unwrapped_tool_calls,
        _collect_tool_calls,
        _marker_line_prefix,
        _action_marker_is_protocol,
        _control_marker_is_protocol,
        _remove_protocol_headings,
        _clean_final_response,
        _stream_buffer_has_tool_prefix,
        _clean_stream_buffer,
        _remove_task_blocks,
        _legacy_plan_value,
        _parse_model_response,
        _observe_model_response,
        _persist_interaction_control,
        _interaction_gate,
        _deterministic_tool_summary,
        _safe_fstring,
        _split_reply,
    )

    # security.permissions
    from openkyrozen.security.permissions import (
        _state_root,
        _approval_log_path,
        _record_tool_approval,
        _confirm_tool_action,
        _jev_permission_check,
        _record_permission_decision,
        _authorize_tool_action,
    )

    # agent.context
    from openkyrozen.agent.context import (
        _context_scope,
        context_usage,
        _store_context_status,
        _record_context_history_metrics,
        _save_learning_context_digest,
        _restore_learning_context_digest,
        _load_reported_context_tokens,
        _cache_reported_context_tokens,
        _is_context_overflow_error,
        _summarize_context_with_chat_model,
        _prepare_context_for_call,
        _recover_context_after_overflow,
    )

    # workspace.binding
    from openkyrozen.workspace.binding import (
        project_graph_snapshot,
        _project_graph_context,
        _get_workspace_root,
        _set_workspace_root,
        _set_launch_context,
        configure_launch_context,
        get_launch_context,
        _is_path_safe,
    )

    # agent.fix_workflow
    from openkyrozen.agent.fix_workflow import (
        _fix_feedback_signal,
        _fix_safe_text,
        _fix_scope_kwargs,
        _latest_fix_workflow,
        _persist_fix_stage,
        _start_fix_workflow,
        _prepare_fix_workflow,
        _fix_evidence_record,
        _fix_is_mutation,
        _fix_reproduction_records,
        _fix_verification_records,
        _fix_has_marker,
        _fix_success_claim,
        _fix_blocked_reply,
        _advance_fix_workflow,
        _track_fix_outcome,
        _get_fix_success_rate,
    )

    # tools.dynamic
    from openkyrozen.tools.dynamic import (
        _record_dynamic_tool_event,
        _reject_dynamic_tool,
        _register_tool,
        _attempt_define_tool,
    )

    # memory.preferences
    from openkyrozen.memory.preferences import (
        _preference_fields,
        _restore_user_preferences,
        _detect_user_preferences,
        _build_preference_context,
    )

    # agent.prompts
    from openkyrozen.agent.prompts import (
        _compose_skills,
        _prompt_profile,
        _permitted_tool_names,
        _discover_tools,
        _build_tools_list,
        _agent_prompt_tools_list,
        _system_prompt,
        _build_messages,
        _refresh_tool_inventory,
    )

    # agent.session
    from openkyrozen.agent.session import (
        _restore_ponytail_level,
        set_ponytail_level,
        _ponytail_context,
        interaction_envelope,
        interaction_workspace_id,
        permission_mode,
        set_permission_mode,
        bind_interaction_scope,
        set_interaction_mode,
        set_system_one_backend,
        set_fast_backend,
        _record_fast_decision,
        _record_decision_assist,
        decision_assist_state,
        set_decision_assist,
        revoke_decision_assist_consent,
        resolve_interaction_question,
        accept_interaction_plan,
        cancel_interaction_plan,
    )

    # plugins.lifecycle
    from openkyrozen.plugins.lifecycle import (
        _record_plugin_event,
        _plugin_runtime_for_surface,
    )

    # agent.subagent_runtime
    from openkyrozen.agent.subagent_runtime import (
        _subagent_action_arguments,
        _subagent_tool_evidence,
        _run_subagent_tool,
        _run_subagent_llm_result,
        _subagent_usage_metrics,
        _run_subagent_llm,
        _subagent_provider_model,
    )

    from openkyrozen.agent.plan_tools import _update_plan
    from openkyrozen.agent.delegation_runtime import (
        _plan_delegation,
        delegation, _invoke_delegated, spawn_agents, send_subagent, list_subagents,
        wait_subagents, cancel_subagent, _delegation_summary,
        _delegation_tool_access,
    )

    # memory.retrieval
    from openkyrozen.memory.retrieval import (
        _check_stored_data,
        _has_cjk,
        _cjk_approximate_words,
        _search_memory,
        _build_memory_context,
    )

    # learning.evolution
    from openkyrozen.learning.evolution import (
        _acceptance_for_tool,
        _research_acceptance,
        _with_learning_notices,
        _finish_learning_run,
        _review_evolution_runs,
    )

    # agent.executor
    from openkyrozen.agent.executor import (
        _notify_tool_execute,
        _operation_action,
        _operation_args,
        _operation_id,
        _is_state_changing_action,
        _make_execution_receipt,
        _run_tool,
        _execute_turn_action,
        _record_turn_receipt,
        _tool_result_for_prompt,
    )

    # tasks.runtime
    from openkyrozen.tasks.runtime import (
        _execute_durable_task,
    )

    # routing.router
    from openkyrozen.routing.router import (
        _select_model,
        _classify_complexity,
    )

    # providers.calls
    from openkyrozen.providers.calls import (
        _provider_timeout_seconds,
        _bounded_provider_call,
        _bounded_provider_stream,
        _get_llm_response,
        _get_model_response,
    )

    # agent.planner
    from openkyrozen.agent.planner import (
        _requires_tool_action,
        _is_tool_error,
        _tasks_from_plan,
        _build_task_progress_hint,
    )

    # security.untrusted_input
    from openkyrozen.security.untrusted_input import (
        _detect_prompt_injection,
        _sanitize_input,
        _is_bug_report, _is_bug_review,
        _is_question,
    )

    # agent.loop
    from openkyrozen.agent.loop import (
        _chat_turn_impl,
        _chat_turn,
    )

    # learning.dispatcher
    from openkyrozen.learning.dispatcher import (
        _learning_timestamp,
        _learning_safe_text,
        _record_learning_event,
        _learning_state_fingerprint,
        _learning_result,
        _run_learning_context_compression,
        _run_learning_technology,
        _run_learning_dynamic_tools,
        _run_learning_preferences,
        _run_learning_memory_scoring,
        _run_learning_graph,
        _run_learning_project_graph,
        _run_learning_skill_composition,
        _run_learning_rollback,
        dispatch_learning_cycle,
        learning_feature_status,
        _store_learning_product,
        _background_learning_loop,
        _ensure_detached_learning_worker,
        _touch_detached_learning_heartbeat,
    )


    def __init__(self, memory, tasks, interaction, workspace):
        self._default_session = AgentSession(memory.session_id or "surface:cli", memory, tasks, interaction, workspace)
        self._default_turn = ExecutionContext()
        self._turn_context = ContextVar("agent_turn", default=None)
        self._session_context = ContextVar("agent_session", default=None)
        self._surface_capability_override = ContextVar("agent_surface_capabilities", default=None)
        self._conversation_turn_id = ContextVar("conversation_turn_id", default=None)
        self._last_model_response = ContextVar("last_model_response", default=None)
        self._record_foreground_model = ContextVar("record_foreground_model", default=False)
        self._stable_prefix_info = ContextVar("stable_prefix_info", default={})
        self._context_history_metrics = ContextVar("context_history_metrics", default={})
        self._context_history_seen = ContextVar("context_history_seen", default=None)
        self._context_history_projection_ids = ContextVar("context_history_projection_ids", default=None)
        self._conversation_expected_head = ContextVar("conversation_expected_head", default=None)
        self._sessions = {}
        self._workspaces = {workspace.root: workspace}

    @property
    def current_session(self):
        return self._session_context.get() or self._default_session

    def open_session(self, session_id, *, user_id=None):
        owner = user_id or self.memory_bank.user_id
        key = (owner, self.interaction_workspace_id(), session_id, self.current_session.workspace.root)
        with self._delegation_lock:
            if key not in self._sessions or self._sessions[key].memory.store is not self.memory_bank.store:
                self._sessions[key] = self._create_session(session_id, owner, self.current_session.workspace, key[1])
            return self._sessions[key]

    def _create_session(self, session_id, owner, workspace, interaction_scope):
        memory = self.memory_bank.scoped(user_id=owner, session_id=session_id, file_scope_id=self.source_scope_id(workspace.root))
        tasks = self.TaskManager(memory.store, workspace_id=interaction_scope, session_id=session_id, user_id=owner)
        interaction = self.InteractionController(memory.store, user_id=owner, workspace_id=interaction_scope, session_id=session_id)
        session = AgentSession(session_id, memory, tasks, interaction, workspace)
        from openkyrozen.agent.conversation import ConversationHistory
        history = ConversationHistory(memory.store, user_id=owner, workspace_id=interaction_scope,
                                     session_id=str(session_id))
        if history.head:
            session.messages = history.display_messages()
            session.history_loaded = True
        session.skills = copy.copy(self.skill_registry)
        session.skills.disabled_builtins = set(self.skill_registry.disabled_builtins)
        session.preferences = {key: False if isinstance(value, bool) else "" for key, value in self._user_preferences.items()}
        if owner == self.memory_bank.user_id:
            session.preferences = dict(self._user_preferences)
            session.knowledge_graph = copy.deepcopy(self._knowledge_graph)
            session.ponytail_level = self._ponytail_level
        session.learning = copy.copy(self.learning_engine)
        if hasattr(session.learning, "memory"):
            session.learning.memory = memory
            session.learning.store = memory.store
            session.learning.registry = session.skills
        session.subagents = copy.copy(self.subagent_manager)
        session.subagents.coordinator = None
        session.subagents.memory = memory
        session.subagents.learning_engine = session.learning
        if hasattr(session.subagents, "profiles"):
            session.subagents.profiles = dict(session.subagents.profiles)
        return session

    @contextmanager
    def use_session(self, session):
        token = self._session_context.set(session)
        try:
            yield session
        finally:
            self._session_context.reset(token)

    def _publish_runtime_event(self, kind, payload, *, model_request_id=None, call_id=None, task_id=None):
        publisher = self._runtime_event_publisher.get()
        if publisher is None:
            return None
        if model_request_id is None:
            model_request_id = self._active_model_request_id.get() or self._last_model_request_id.get()
        try:
            return publisher.publish(
                kind, payload, model_request_id=model_request_id, call_id=call_id, task_id=task_id,
            )
        except Exception:
            return None

    def chat(self, session, message, *, clear_tasks=False, profile=None, memory_context=None,
             on_event: EventSink | None = None, on_runtime_event: RuntimeEventSink | None = None,
             approve: Approval | None = None) -> str:
        with self.use_session(session):
            if not getattr(session, "history_loaded", False):
                history = self.conversation_history
                if history.head:
                    session.messages = history.display_messages()
                session.history_loaded = True
            from .events import RuntimeEventPublisher
            memory = self.memory_bank
            publisher = RuntimeEventPublisher(
                user_id=str(getattr(memory, "user_id", getattr(session, "user_id", "local")) or "local"),
                workspace_id=str(getattr(memory, "workspace_id", "default") or "default"),
                session_id=str(getattr(memory, "session_id", None) or getattr(session, "session_id", None) or "default"),
                on_runtime_event=on_runtime_event,
                on_legacy_event=on_event,
            )
            runtime_events = self._runtime_event_publisher.set(publisher)
            conversation_turn = self._conversation_turn_id.set(f"turn_{uuid.uuid4().hex}")
            conversation_head = self._conversation_expected_head.set(None)
            self._last_model_response.set(None)
            history_seen = self._context_history_seen.set(set())
            history_projection = self._context_history_projection_ids.set(None)
            events = self._stream_event_callback.set(on_event)
            native_response = self._last_response_is_native.set(False)
            turn_failed = self._turn_failed.set(False)
            last_model_request = self._last_model_request_id.set(None)
            approval = self._approval_callback.set(approve)
            task_event_context = getattr(getattr(session, "tasks", None), "_runtime_event_callback", None)
            task_event = task_event_context.set(
                lambda kind, payload, task_id: self._publish_runtime_event(
                    kind, payload if isinstance(payload, dict) else {"data": payload}, task_id=task_id,
                )
            ) if task_event_context is not None else None
            try:
                publisher.publish("turn.started", {})
                try:
                    reply = self._chat_turn(
                        message, clear_tasks=clear_tasks, profile=profile, memory_context=memory_context,
                    )
                except BaseException as exc:
                    from openkyrozen.providers.errors import ProviderError, ProviderErrorKind
                    cancelled = isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) or (
                        isinstance(exc, ProviderError) and exc.kind == ProviderErrorKind.CANCELLED
                    )
                    publisher.publish(
                        "turn.cancelled" if cancelled else "turn.failed",
                        {"error_type": type(exc).__name__},
                    )
                    raise
                if publisher.last_model_failed or self._turn_failed.get():
                    publisher.publish("turn.failed", {"error_type": "provider_error"})
                else:
                    publisher.publish("turn.completed", {"status": "completed"})
                return reply
            finally:
                self._stream_event_callback.reset(events)
                self._last_response_is_native.reset(native_response)
                self._turn_failed.reset(turn_failed)
                self._last_model_request_id.reset(last_model_request)
                self._approval_callback.reset(approval)
                if task_event is not None:
                    task_event_context.reset(task_event)
                self._runtime_event_publisher.reset(runtime_events)
                self._conversation_turn_id.reset(conversation_turn)
                self._conversation_expected_head.reset(conversation_head)
                self._context_history_seen.reset(history_seen)
                self._context_history_projection_ids.reset(history_projection)

    @property
    def conversation_history(self):
        from openkyrozen.agent.conversation import ConversationHistory
        memory = self.memory_bank
        return ConversationHistory(
            memory.store, user_id=memory.user_id,
            workspace_id=self.interaction_workspace_id(),
            session_id=str(self.current_session.session_id),
        )

    def _record_conversation_entry(self, kind, payload, *, call_id=None, task_id=None, request_id=None):
        history = self.conversation_history
        active_turn = self._conversation_turn_id.get()
        parent = self._conversation_expected_head.get() if active_turn else history.head
        entry = history.entry(
            kind, payload, parent_id=parent, turn_id=self._conversation_turn_id.get(),
            request_id=request_id or self._last_model_request_id.get(),
            call_id=call_id, task_id=task_id,
        )
        history.append(entry, expected_head=parent)
        if active_turn:
            self._conversation_expected_head.set(entry.id)
        return entry

    def reconcile_conversation_messages(self, messages):
        """Explicit adapter for callers that still supply a replacement message list."""
        history = self.conversation_history
        if history.head is None:
            history.import_legacy(messages)
        else:
            history.reconcile_legacy(messages)
        self.short_term_memory = history.display_messages()

    @property
    def tool_registry(self):
        from openkyrozen.tools.catalog import builtin_names
        registry = self.current_session.workspace.adapters.tool_registry
        if not registry.runtime_bindings_initialized:
            for owner in ('runtime','orchestration'):
                for name in builtin_names(owner):
                    if name not in registry.specs:
                        registry.register(name,getattr(self,('_' if owner=='runtime' else '')+name))
            registry.runtime_bindings_initialized=True
        return registry

    @property
    def AVAILABLE_TOOLS(self):
        from openkyrozen.tools.manifest import ToolMapping
        from .delegation import TOOLS
        return ToolMapping(self.tool_registry, TOOLS if self.execution_context.child_run_id else ())

    @AVAILABLE_TOOLS.setter
    def AVAILABLE_TOOLS(self, value):
        self.current_session.workspace.adapters.AVAILABLE_TOOLS = value

    @property
    def TOOL_ALIASES(self):
        return self.tool_registry.aliases


    @property
    def _ACTION_MARKER_NAMES(self):
        return tuple(sorted(set(self.AVAILABLE_TOOLS)|set(self.TOOL_ALIASES),key=lambda name:(-len(name),name)))

    @property
    def _ACTION_MARKER_RE(self):
        names = self._ACTION_MARKER_NAMES
        cached = getattr(self,'_action_marker_cache',None)
        if cached is None or cached[0] != names:
            import re
            cached = (names,re.compile(r"(?i)(?<![\w])(?P<name>(?:"+'|'.join(map(re.escape,names))+r"))\s*:"))
            self._action_marker_cache = cached
        return cached[1]

    def run_command(self, args):
        return self.current_session.workspace.adapters.run_command(args)

    def _set_tools_workspace_root(self, root):
        self.current_session.workspace.adapters.set_workspace_root(root)

    def _set_tools_project_graph(self, graph):
        self.current_session.workspace.adapters.set_project_graph(graph)

    def _set_tools_github_cli(self, github):
        self.current_session.workspace.adapters.set_github_cli(github)

    @property
    def memory_bank(self):
        return self.current_session.memory

    @memory_bank.setter
    def memory_bank(self, value):
        self.current_session.memory = value

    @property
    def tasks(self):
        return self.current_session.tasks

    @tasks.setter
    def tasks(self, value):
        self.current_session.tasks = value

    @property
    def _interaction_controller(self):
        return self.current_session.interaction

    @_interaction_controller.setter
    def _interaction_controller(self, value):
        self.current_session.interaction = value

    @property
    def short_term_memory(self):
        return self.current_session.messages

    @short_term_memory.setter
    def short_term_memory(self, value):
        self.current_session.messages = value
        self.current_session.messages_compacted = False

    @property
    def learning_engine(self):
        return self.current_session.learning

    @learning_engine.setter
    def learning_engine(self, value):
        self.current_session.learning = value

    @property
    def subagent_manager(self):
        return self.current_session.subagents

    @subagent_manager.setter
    def subagent_manager(self, value):
        self.current_session.subagents = value

    @property
    def _last_learning_run(self):
        return self.current_session.last_learning_run

    @_last_learning_run.setter
    def _last_learning_run(self, value):
        self.current_session.last_learning_run = value

    @property
    def _learning_notices(self):
        return self.current_session.learning_notices

    @_learning_notices.setter
    def _learning_notices(self, value):
        self.current_session.learning_notices = value

    @property
    def _agent_profile_mode(self):
        return self.current_session.profile

    @_agent_profile_mode.setter
    def _agent_profile_mode(self, value):
        self.current_session.profile = value

    @property
    def _turn_cost_log(self):
        return self.current_session.turn_cost_log

    @_turn_cost_log.setter
    def _turn_cost_log(self, value):
        self.current_session.turn_cost_log = value

    @property
    def _workspace_root(self):
        return self.current_session.workspace.root

    @_workspace_root.setter
    def _workspace_root(self, value):
        self.current_session.workspace.root = value

    @property
    def _launch_context(self):
        return self.current_session.workspace.launch_context

    @_launch_context.setter
    def _launch_context(self, value):
        self.current_session.workspace.launch_context = value

    @property
    def _project_graph(self):
        return self.current_session.workspace.graph

    @_project_graph.setter
    def _project_graph(self, value):
        self.current_session.workspace.graph = value

    @property
    def _github_cli(self):
        return self.current_session.workspace.github

    @_github_cli.setter
    def _github_cli(self, value):
        self.current_session.workspace.github = value

    @property
    def execution_context(self):
        return self._turn_context.get() or self._default_turn

    @property
    def DEEPSEEK_MODEL(self):
        return self.execution_context.model

    @DEEPSEEK_MODEL.setter
    def DEEPSEEK_MODEL(self, value):
        self.execution_context.model = value

    @property
    def _execution_capability_token(self):
        return self.execution_context.capability_token

    @_execution_capability_token.setter
    def _execution_capability_token(self, value):
        self.execution_context.capability_token = value

    @property
    def _last_prompt_tokens(self):
        return self.execution_context.last_prompt_tokens

    @_last_prompt_tokens.setter
    def _last_prompt_tokens(self, value):
        self.execution_context.last_prompt_tokens = value

    @property
    def _last_completion_tokens(self):
        return self.execution_context.last_completion_tokens

    @_last_completion_tokens.setter
    def _last_completion_tokens(self, value):
        self.execution_context.last_completion_tokens = value

    def execute(self, session, action, args, *, capabilities=None, operation_scope="", approve=None,
                _surface_capability_bound=None, _call_id=None):
        """All inbound tool surfaces share authorization, approvals and receipts."""
        from openkyrozen.security.capabilities import issue_capability_token
        from openkyrozen.app.config import AgentConfigError
        with self.use_session(session):
            context_token = self._turn_context.set(copy.copy(self.execution_context))
            approval_token = self._approval_callback.set(approve)
            previous_surface_bound = self._surface_capability_override.get()
            surface_token = self._surface_capability_override.set(
                _surface_capability_bound if _surface_capability_bound is not None else previous_surface_bound
            )
            try:
                current = self._execution_capability_token
                try:
                    granted = self._effective_execution_capabilities(capabilities)
                except AgentConfigError:
                    granted = frozenset()
                self._execution_capability_token = issue_capability_token(
                    current.subject, granted,
                )
                receipt = self._run_tool(
                    action, args, return_receipt=True, operation_scope=operation_scope,
                    _call_id=_call_id or f"synthetic_{uuid.uuid4().hex}", _call_id_source="synthetic",
                )
                self.memory_bank.store.append_event("execution.receipt", receipt.as_dict(),
                    user_id=receipt.user_id, workspace_id=receipt.workspace_id,
                    session_id=receipt.session_id)
                return receipt
            finally:
                self._surface_capability_override.reset(surface_token)
                self._approval_callback.reset(approval_token)
                self._turn_context.reset(context_token)

    def _effective_execution_capabilities(self, requested=None):
        from openkyrozen.app.config import effective_capabilities, load_agent_config
        from openkyrozen.agent.modes import mode_capabilities
        from openkyrozen.security.tool_policy import resolve_capabilities

        config = effective_capabilities(load_agent_config(self._get_workspace_root()))
        default_surface = "full" if self._EXECUTION_SURFACE == "cli" else "workspace"
        if self._EXECUTION_SURFACE == "tui" and self.permission_mode() in {"full_jev", "full"}:
            default_surface = "full"
        surface_bound = self._surface_capability_override.get()
        surface_value = (surface_bound if surface_bound is not None else
                         self._surface_capabilities or default_surface)
        surface = (frozenset(surface_value) if not isinstance(surface_value, str)
                   else resolve_capabilities(surface_value))
        granted = self._execution_capability_token.capabilities & config & surface
        granted = mode_capabilities(granted, self._active_interaction_mode.get())
        if requested is not None:
            requested = requested if isinstance(requested, str) else ",".join(requested)
            granted &= resolve_capabilities(requested, default="readonly") if requested.strip() else frozenset()
        return granted

    def _valid_receipt_task_binding(self, session, task_id):
        if not isinstance(task_id, str) or not task_id.strip():
            return False
        parent = getattr(session, "_delegation_parent", None)
        if parent is not None:
            if getattr(session, "_delegation_task_id", None) != task_id:
                return False
            tasks = parent.tasks
            owner_session = parent
            memory = parent.memory
        else:
            tasks = session.tasks
            owner_session = session
            memory = session.memory
        return (
            tasks.user_id == memory.user_id == owner_session.interaction.user_id
            and tasks.workspace_id == owner_session.interaction.workspace_id
            and tasks.session_id == owner_session.session_id == owner_session.interaction.session_id
            and tasks.active_working_task(task_id) is not None
        )

    def execute_call(self, session, call, *, capabilities=None, operation_scope="", approve=None,
                     task_id: str | None = None,
                     _call_id_source: str = "native",
                     _surface_capability_bound=None):
        """Authorize and execute one structured provider call at the shared boundary."""
        from openkyrozen.providers.models import ToolCall
        from openkyrozen.security.approvals import ApprovalRequest
        from openkyrozen.security.capabilities import issue_capability_token
        from openkyrozen.security.permissions import _call_approval_callback
        from openkyrozen.app.config import AgentConfigError
        from openkyrozen.persistence.models import utc_now
        from openkyrozen.tools.models import render_tool_result, tool_failure

        if not isinstance(call, ToolCall):
            raise TypeError("execute_call requires a ToolCall")
        call_arguments = copy.deepcopy(call.arguments)
        with self.use_session(session):
            context_token = self._turn_context.set(copy.copy(self.execution_context))
            existing_approve = self._approval_callback.get()
            user_approve = approve if approve is not None else existing_approve
            approval_token = self._approval_callback.set(user_approve)
            previous_surface_bound = self._surface_capability_override.get()
            surface_token = self._surface_capability_override.set(
                _surface_capability_bound if _surface_capability_bound is not None else previous_surface_bound
            )
            request_token = None
            spec_token = None
            started_at = utc_now()
            name = call.name
            spec = None
            bound_task_id = task_id
            execution_dispatched = False

            def reject(outcome, *, authorized=False, bound_task=None):
                self._publish_runtime_event(
                    "tool.call", {"tool_name": name,
                                  "argument_keys": sorted(call_arguments)[:64]},
                    call_id=call.id, task_id=bound_task,
                )
                self._notify_tool_execute(name, call_arguments, outcome)
                receipt = self._make_execution_receipt(
                    action=name, args=call_arguments, authorized=authorized, started_at=started_at,
                    success=outcome.success, result=render_tool_result(outcome),
                    operation_scope=operation_scope, failure=outcome.error.code if outcome.error else None,
                    tool_error=outcome.error, result_data=outcome.data, tool_result=outcome,
                    call_id=call.id, call_id_source=_call_id_source, task_id=bound_task, expected_spec=spec,
                )
                self._record_turn_receipt(receipt)
                return receipt

            def mark_dispatched():
                nonlocal execution_dispatched
                execution_dispatched = True

            try:
                name = self.tool_registry.resolve_legacy_name(call.name)
                if task_id is not None and not self._valid_receipt_task_binding(session, task_id):
                    bound_task_id = None
                    return reject(tool_failure("invalid_arguments", "Error: task_id must identify a pending task in the active Working Plan."))
                try:
                    spec = self.tool_registry.get_spec(name)
                except KeyError:
                    receipt = self._run_tool(name, call_arguments, return_receipt=True,
                                             operation_scope=operation_scope, _call_id=call.id,
                                             _call_id_source=_call_id_source, _task_id=bound_task_id,
                                             _on_dispatch=mark_dispatched)
                    self._record_turn_receipt(receipt)
                    return receipt
                validation_error = self.tool_registry._argument_error(spec.input_schema, call_arguments)
                if spec.source == 'legacy':
                    validation_error = 'native calls require a ToolSpec with a declared object executor contract'
                if validation_error:
                    code = 'contract_error' if spec.source == 'legacy' else 'invalid_arguments'
                    outcome = tool_failure(code, f"Error: {validation_error}")
                    return reject(outcome, bound_task=bound_task_id)
                request = ApprovalRequest(
                    call_id=call.id, tool_name=name, arguments=call_arguments,
                    workspace_id=self.memory_bank.workspace_id, session_id=session.session_id,
                    tool_version=spec.version, tool_source=spec.source, capability=spec.capability,
                    risk=spec.risk, side_effects=spec.side_effects,
                )
                request_token = self._approval_request.set(request)
                spec_token = self._approval_tool_spec.set(spec)

                def bound_approval(_action, _details):
                    if not _call_approval_callback(self, user_approve, name, call_arguments, request=request):
                        return False
                    if call.arguments != call_arguments:
                        return False
                    current = self.tool_registry.specs.get(name)
                    if current is not spec or session.session_id != request.session_id:
                        return False
                    if self.memory_bank.workspace_id != request.workspace_id:
                        return False
                    if bound_task_id is not None and not self._valid_receipt_task_binding(session, bound_task_id):
                        return False
                    try:
                        granted = self._effective_execution_capabilities()
                    except AgentConfigError:
                        return False
                    if spec.capability not in granted:
                        return False
                    try:
                        current_request = ApprovalRequest(
                            call_id=call.id, tool_name=name, arguments=call_arguments,
                            workspace_id=self.memory_bank.workspace_id, session_id=session.session_id,
                            tool_version=current.version, tool_source=current.source,
                            capability=current.capability, risk=current.risk,
                            side_effects=current.side_effects,
                        )
                    except (TypeError, ValueError):
                        return False
                    if current_request.fingerprint != request.fingerprint:
                        return False
                    return True

                bound_approval._runtime_approval_wrapper = True

                approval_token2 = self._approval_callback.set(bound_approval if callable(user_approve) else None)
                try:
                    current = self._execution_capability_token
                    try:
                        granted = self._effective_execution_capabilities(capabilities)
                    except AgentConfigError:
                        granted = frozenset()
                    self._execution_capability_token = issue_capability_token(current.subject, granted)
                    receipt = self._run_tool(name, call_arguments, return_receipt=True,
                                             operation_scope=operation_scope,
                                             _typed_arguments=call_arguments, _expected_spec=spec,
                                             _call_id=call.id, _call_id_source=_call_id_source,
                                             _task_id=bound_task_id,
                                             _pre_execution_check=(
                                                 lambda: bound_task_id is None
                                                 or self._valid_receipt_task_binding(session, bound_task_id)
                                             ), _on_dispatch=mark_dispatched)
                    self._record_turn_receipt(receipt)
                    return receipt
                finally:
                    self._approval_callback.reset(approval_token2)
            except KeyboardInterrupt:
                if execution_dispatched:
                    raise
                bound_task_id = bound_task_id if self._valid_receipt_task_binding(session, bound_task_id) else None
                return reject(tool_failure("cancelled", "Tool call was cancelled."), bound_task=bound_task_id)
            except Exception as exc:
                if execution_dispatched:
                    raise
                bound_task_id = bound_task_id if bound_task_id and self._valid_receipt_task_binding(session, bound_task_id) else None
                return reject(tool_failure("execution_error", f"Error: {type(exc).__name__}: {exc}"), bound_task=bound_task_id)
            finally:
                self._surface_capability_override.reset(surface_token)
                if request_token is not None:
                    self._approval_request.reset(request_token)
                if spec_token is not None:
                    self._approval_tool_spec.reset(spec_token)
                self._approval_callback.reset(approval_token)
                self._turn_context.reset(context_token)

    @property
    def skill_registry(self):
        return self.current_session.skills

    @skill_registry.setter
    def skill_registry(self, value):
        self.current_session.skills = value

    @property
    def _user_preferences(self):
        return self.current_session.preferences

    @_user_preferences.setter
    def _user_preferences(self, value):
        self.current_session.preferences = value

    @property
    def _hydrated_preferences(self):
        return self.current_session.hydrated_preferences

    @_hydrated_preferences.setter
    def _hydrated_preferences(self, value):
        self.current_session.hydrated_preferences = value

    @property
    def _preference_scope(self):
        return self.current_session.preference_scope

    @_preference_scope.setter
    def _preference_scope(self, value):
        self.current_session.preference_scope = value

    @property
    def _knowledge_graph(self):
        return self.current_session.knowledge_graph

    @_knowledge_graph.setter
    def _knowledge_graph(self, value):
        self.current_session.knowledge_graph = value

    @property
    def _ponytail_level(self):
        return self.current_session.ponytail_level

    @_ponytail_level.setter
    def _ponytail_level(self, value):
        self.current_session.ponytail_level = value

    def DeepSeekDSMLFilter(self):
        from .types import DeepSeekDSMLFilter
        return DeepSeekDSMLFilter(self._is_valid_action,self._ACTION_MARKER_NAMES)
