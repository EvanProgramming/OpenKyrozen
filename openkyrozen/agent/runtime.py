"""Application agent runtime; all mutable state belongs to an instance."""
from __future__ import annotations

import copy
from collections import ChainMap
from contextlib import contextmanager
from contextvars import ContextVar
from .models import AgentSession, WorkspaceState
from .turn import ExecutionContext
from .ports import EventSink, Approval


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
        _build_tools_list,
        _agent_prompt_tools_list,
        _system_prompt,
        _build_messages,
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
        _get_llm_response,
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
        _background_learning_loop,
        _ensure_detached_learning_worker,
        _touch_detached_learning_heartbeat,
    )


    def __init__(self, memory, tasks, interaction, workspace):
        self._default_session = AgentSession(memory.session_id or "surface:cli", memory, tasks, interaction, workspace)
        self._default_turn = ExecutionContext()
        self._turn_context = ContextVar("agent_turn", default=None)
        self._session_context = ContextVar("agent_session", default=None)
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

    def chat(self, session, message, *, clear_tasks=False, profile=None, memory_context=None, on_event: EventSink | None = None, approve: Approval | None = None) -> str:
        with self.use_session(session):
            events = self._stream_event_callback.set(on_event)
            approval = self._approval_callback.set(approve)
            try:
                return self._chat_turn(message, clear_tasks=clear_tasks, profile=profile, memory_context=memory_context)
            finally:
                self._stream_event_callback.reset(events)
                self._approval_callback.reset(approval)

    @property
    def AVAILABLE_TOOLS(self):
        from .delegation import TOOLS
        orchestration = {} if self.execution_context.child_run_id else {name: getattr(self, name) for name in TOOLS}
        return ChainMap(self.current_session.workspace.adapters.AVAILABLE_TOOLS, orchestration)

    @AVAILABLE_TOOLS.setter
    def AVAILABLE_TOOLS(self, value):
        self.current_session.workspace.adapters.AVAILABLE_TOOLS = value

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

    def execute(self, session, action, args, *, capabilities=None, operation_scope="", approve=None):
        """All inbound tool surfaces share authorization, approvals and receipts."""
        from openkyrozen.security.capabilities import issue_capability_token
        from openkyrozen.security.tool_policy import resolve_capabilities
        with self.use_session(session):
            context_token = self._turn_context.set(copy.copy(self.execution_context))
            approval_token = self._approval_callback.set(approve)
            try:
                if capabilities is not None:
                    self._execution_capability_token = issue_capability_token(
                        f"surface:{self.surface}", resolve_capabilities(capabilities if isinstance(capabilities, str) else ",".join(capabilities), default="readonly"))
                receipt = self._run_tool(action, args, return_receipt=True, operation_scope=operation_scope)
                self.memory_bank.store.append_event("execution.receipt", receipt.as_dict(),
                    user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
                    session_id=self.memory_bank.session_id)
                return receipt
            finally:
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
        return DeepSeekDSMLFilter(self._is_valid_action)
