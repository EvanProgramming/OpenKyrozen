from __future__ import annotations

import json
from typing import Any
from openkyrozen.tasks.engine import is_complete, is_terminal
from openkyrozen.agent.modes import InteractionError, is_plan_acceptance
from openkyrozen.workspace.history_port import TurnToken

from .turn import TurnContext, scoped_turn

def _chat_turn_impl(self, user_input: str, clear_tasks: bool = False, profile: str | None = None,
                    memory_context: dict[str, Any] | None = None,
                    inspection_complete: bool = False) -> str:
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
               memory_context: dict[str, Any] | None = None) -> str:
    """Run one chat turn with failure-isolated plugin lifecycle hooks."""
    previous_capability_token = self._execution_capability_token
    original_user_input = user_input
    history_token: TurnToken | None = None
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
    accepted_plan: dict[str, Any] | None = None
    mode_override = self._interaction_mode_override.get()
    state = self._interaction_controller.state(user_input)
    executing_plan = state.get("executing_plan") if mode_override is None else None
    pending_question = state.get("pending_question") if mode_override is None else None
    pending_plan = state.get("pending_plan") if mode_override is None else None
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
        try:
            accepted_plan, created = self._interaction_controller.accept_plan(self.tasks)
        except InteractionError:
            return "There is no pending plan to accept."
        if not created and not self._interaction_controller.state().get("executing_plan"):
            return "Plan was already accepted."
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
            reply = self._chat_turn_impl(
                user_input, clear_tasks=clear_tasks, profile=profile,
                memory_context=memory_context,
                inspection_complete=bool(pending_question or pending_plan),
            )
            if backend := self._fast_used_backend.get():
                reply = f"Made a decision with {'Jev' if backend == 'jev' else 'Kev'}.\n\n{reply}"
        except Exception as exc:
            if active_plan:
                self._interaction_controller.complete_plan(active_plan, status="failed")
            self._track_fix_outcome(original_user_input, f"Turn failed: {type(exc).__name__}: {exc}")
            runtime.turn_end(reply="", success=False,
                             error=f"{type(exc).__name__}: {exc}", **context)
            raise
        state_after = self._interaction_controller.state()
        if (active_plan and not state_after.get("pending_question") and self.tasks.tasks
                and all(is_terminal(task) for task in self.tasks.tasks)):
            status = "completed" if all(is_complete(task) for task in self.tasks.tasks) else "failed"
            self._interaction_controller.complete_plan(active_plan, status=status)
            self._emit_stream_event({"event": "interaction", "interaction": self.interaction_envelope()})
        self._track_fix_outcome(original_user_input, reply)
        runtime.turn_end(reply=reply, success=True, **context)
        if history_token is not None:
            try:
                self.history_manager().commit_turn(
                    history_token, user_message=original_user_input, assistant_message=reply,
                    conversation=list(self.short_term_memory) + [
                        {"role": "user", "content": original_user_input},
                        {"role": "assistant", "content": self._clean_final_response(reply)},
                    ], interaction=self._interaction_controller.state(), tasks=list(self.tasks.tasks),
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
