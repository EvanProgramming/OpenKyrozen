from __future__ import annotations

import json
from typing import Any
from openkyrozen.tasks.engine import is_complete, is_terminal
from openkyrozen.agent.modes import InteractionError, is_plan_acceptance
from openkyrozen.workspace.history_port import TurnToken

from .turn import TurnContext, scoped_turn

def _chat_turn_impl(self, user_input: str, clear_tasks: bool = False, profile: str | None = None,
                    memory_context: dict[str, Any] | None = None,
                    inspection_complete: bool = False,
                    plan_acceptance: dict[str, Any] | None = None) -> str:
    """Coordinate context, protocol recovery, execution and completion for one turn."""
    turn = TurnContext(user_input, clear_tasks, profile, memory_context, inspection_complete)
    self._prepare_turn(turn)
    reply = self._initial_turn_response(turn)
    if reply is not None:
        return reply
    self._execute_action_rounds(turn)
    return self._complete_turn(turn)


@scoped_turn
def _chat_turn(self, user_input: str, clear_tasks: bool = False, profile: str | None = None,
               memory_context: dict[str, Any] | None = None,
               plan_acceptance: dict[str, Any] | None = None) -> str:
    """Run one chat turn with failure-isolated plugin lifecycle hooks."""
    previous_capability_token = self._execution_capability_token
    original_user_input = user_input
    history_token: TurnToken | None = None
    conversation = self.conversation_history
    if conversation.head is None:
        conversation.import_legacy(list(self.short_term_memory))
    elif not getattr(self.current_session, "messages_compacted", False):
        conversation.reconcile_legacy(list(self.short_term_memory))
    self._conversation_expected_head.set(conversation.head)
    self._interaction_controller.reconcile_plan_completion(self.tasks)
    try:
        history_token = self.history_manager().begin_turn(
            conversation=list(self.short_term_memory), interaction=self._interaction_controller.state(user_input),
            tasks=list(self.tasks.tasks),
        )
    except Exception as exc:
        self.memory_bank.store.append_event(
            "history.record_failed", {"stage": "begin", "error": str(exc)[:500]},
            user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
            session_id=self.memory_bank.session_id,
        )
    self._record_conversation_entry("user.input", {"text": original_user_input})
    accepted_plan: dict[str, Any] | None = None
    mode_override = self._interaction_mode_override.get()
    state = self._interaction_controller.state(user_input)
    executing_plan = state.get("executing_plan") if mode_override is None else None
    pending_question = state.get("pending_question") if mode_override is None else None
    pending_plan = state.get("pending_plan") if mode_override is None else None
    early_reply = None
    if mode_override is None and pending_question:
        resolved = self._interaction_controller.resolve_question(
            pending_question["request_id"], user_input, action="answer",
        )
        user_input = (
            f"Original request:\n{pending_question.get('original_input', '')}\n\n"
            f"Clarification answers:\n{json.dumps(resolved['answers'], ensure_ascii=False)}"
        )
        clear_tasks = False
    elif mode_override is None and is_plan_acceptance(user_input):
        created = None
        try:
            if plan_acceptance is not None:
                plan_id = plan_acceptance.get("plan_id")
                version = plan_acceptance.get("version")
                if (not isinstance(plan_id, str) or not plan_id.strip()
                        or not isinstance(version, int) or isinstance(version, bool) or version < 1):
                    raise InteractionError("plan acceptance identity is invalid")
                current_state = self._interaction_controller.state()
                current_plan = current_state.get("pending_plan") or current_state.get("executing_plan")
                if (not current_plan or current_plan.get("plan_id") != plan_id
                        or current_plan.get("version") != version):
                    raise InteractionError("plan acceptance identity is stale")
            accepted_plan, created = self._interaction_controller.accept_plan(
                self.tasks,
                plan_id=plan_id if plan_acceptance is not None else None,
                version=version if plan_acceptance is not None else None,
            )
        except InteractionError:
            early_reply = (
                "The plan changed before acceptance. Review the current proposal and accept it explicitly."
                if plan_acceptance else "There is no pending plan to accept."
            )
        if created is False and not self._interaction_controller.state().get("executing_plan"):
            early_reply = "Plan was already accepted."
        if early_reply is None:
            user_input = (
                "Execute the accepted plan below in Agent mode. Complete each durable task with evidence.\n\n"
                + json.dumps(accepted_plan, ensure_ascii=False)
            )
            self._emit_stream_event({
                "event": "interaction",
                "interaction": self._interaction_controller.envelope(user_input),
            })
            clear_tasks = False
    elif mode_override is None and pending_plan:
        user_input = (
            f"Original request:\n{pending_plan.get('original_input', '')}\n\n"
            f"Current plan v{pending_plan.get('version')}:\n{json.dumps(pending_plan, ensure_ascii=False)}\n\n"
            f"Revision feedback:\n{user_input}"
        )
        clear_tasks = False

    mode_token = self._active_interaction_mode.set(
        mode_override or self._interaction_controller.state(user_input)["effective_mode"]
    )
    fast_token = self._fast_used_backend.set("")
    active_plan = accepted_plan or executing_plan
    try:
        runtime = self._plugin_runtime_for_surface()
        context = {
            "user_id": self.memory_bank.user_id,
            "workspace_id": self.memory_bank.workspace_id,
            "session_id": self.memory_bank.session_id,
            "profile": profile or self._agent_profile_mode,
        }
        runtime.turn_start(user_input=original_user_input, **context)
        try:
            if early_reply is not None:
                self._turn_failed.set(False)
                reply = early_reply
            else:
                reply = self._chat_turn_impl(
                    user_input, clear_tasks=clear_tasks, profile=profile,
                    memory_context=memory_context,
                    inspection_complete=bool(pending_question or pending_plan),
                    plan_acceptance=plan_acceptance,
                )
            if backend := self._fast_used_backend.get():
                reply = f"Made a decision with {'Jev' if backend == 'jev' else 'Kev'}.\n\n{reply}"
        except Exception as exc:
            self._record_conversation_entry("turn.outcome", {
                "status": "failed", "error_type": type(exc).__name__,
            })
            from openkyrozen.agent.compaction import is_context_digest, retain_context_digests
            display = conversation.display_messages()
            limit = self.SHORT_TERM_CAP * 2
            digest = next((item for item in reversed(self.short_term_memory) if is_context_digest(item)), None)
            projected = ([digest] if digest else []) + display
            self.short_term_memory = retain_context_digests(projected, limit)
            self.current_session.messages_compacted = bool(digest) or len(projected) > limit
            if active_plan:
                self._interaction_controller.complete_plan(active_plan, status="failed")
            self._track_fix_outcome(original_user_input, f"Turn failed: {type(exc).__name__}: {exc}")
            runtime.turn_end(reply="", success=False,
                             error=f"{type(exc).__name__}: {exc}", **context)
            raise
        self._interaction_controller.reconcile_plan_completion(self.tasks)
        state_after = self._interaction_controller.state()
        working_plan = self.tasks.working_plan
        active_task_ids = (
            list(active_plan["working_task_ids"])
            if active_plan and "working_task_ids" in active_plan else
            list(working_plan.task_ids) if active_plan and working_plan
            and working_plan.id == active_plan.get("working_plan_id") else []
        )
        active_tasks = [task for task in self.tasks.tasks if task.get("id") in active_task_ids]
        active_objectives = {step.get("id") for step in active_plan.get("steps", [])} if active_plan else set()
        tasks_remain_plan_bound = all(
            ((task.get("checkpoint") or {}).get("strategic_plan") or {}).get("plan_id") == active_plan.get("plan_id")
            and ((task.get("checkpoint") or {}).get("strategic_plan") or {}).get("objective_id") in active_objectives
            for task in active_tasks
        )
        if (active_plan and active_task_ids and not state_after.get("pending_question") and active_tasks
                and tasks_remain_plan_bound
                and all(is_terminal(task) for task in active_tasks)):
            statuses = {task.get("status") for task in active_tasks}
            status = ("completed" if statuses == {"succeeded"} else
                      "cancelled" if statuses <= {"succeeded", "cancelled"} else "failed")
            self._interaction_controller.complete_plan(
                active_plan, status=status, task_manager=self.tasks,
            )
            self._emit_stream_event({"event": "interaction", "interaction": self.interaction_envelope()})
        self._track_fix_outcome(original_user_input, reply)
        runtime.turn_end(reply=reply, success=True, **context)
        interaction = self._interaction_controller.state()
        self._record_conversation_entry("interaction.reference", {
            "interaction": interaction,
            "plan_id": interaction.get("pending_plan", {}).get("plan_id")
                        if isinstance(interaction.get("pending_plan"), dict) else None,
            "task_ids": [item.get("id") for item in self.tasks.tasks if item.get("id")],
        })
        self._record_conversation_entry("turn.outcome", {
            "status": "failed" if self._turn_failed.get() else "completed",
            "text": self._clean_final_response(reply),
        })
        from openkyrozen.agent.compaction import is_context_digest, retain_context_digests
        digest = next((item for item in reversed(self.short_term_memory) if is_context_digest(item)), None)
        display = conversation.display_messages()
        projected = ([digest] if digest else []) + display
        truncated = len(projected) > self.SHORT_TERM_CAP * 2
        self.short_term_memory = retain_context_digests(
            projected, self.SHORT_TERM_CAP * 2,
        )
        self.current_session.messages_compacted = bool(digest) or truncated
        if history_token is not None:
            try:
                self.history_manager().commit_turn(
                    history_token, user_message=original_user_input, assistant_message=reply,
                    conversation=conversation.display_messages(),
                    interaction=self._interaction_controller.state(), tasks=list(self.tasks.tasks),
                    journal_head=conversation.head,
                )
            except Exception as exc:
                self.memory_bank.store.append_event(
                    "history.record_failed", {"stage": "commit", "error": str(exc)[:500]},
                    user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
                    session_id=self.memory_bank.session_id,
                )
        return reply
    finally:
        self._execution_capability_token = previous_capability_token
        self._active_interaction_mode.reset(mode_token)
        self._fast_used_backend.reset(fast_token)
        self._active_context_state.set(None)
