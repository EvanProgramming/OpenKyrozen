"""Canonical foreground turn engine with a bounded structured-tool loop."""
from __future__ import annotations

from openkyrozen.agent.turn import TurnContext
from openkyrozen.providers.models import FinishReason, ToolReply

_COMPACT_CORE_TOOLS = frozenset({
    "list_dir", "find_files", "search_files", "read_file", "write_file", "edit_file", "apply_patch",
    "run_cmd", "discover_tools",
})


class AgentEngine:
    """Own the foreground turn lifecycle and select native or legacy transport."""

    def __init__(self, runtime):
        self.runtime = runtime

    def run(self, turn: TurnContext) -> str:
        runtime = self.runtime
        runtime._prepare_turn(turn)
        provider = runtime.execution_context.provider or runtime.llm_provider
        get_capabilities = getattr(provider, "get_capabilities", None)
        try:
            capabilities = get_capabilities(runtime.DEEPSEEK_MODEL) if callable(get_capabilities) else None
        except Exception:
            capabilities = None
        if (capabilities and capabilities.native_tools
                and turn.interaction_mode in {"agent", "ask"}):
            self._run_native(turn)
            return self._finish_turn(turn, runtime._complete_turn(turn))
        reply = runtime._initial_turn_response(turn)
        if reply is not None:
            return self._finish_turn(turn, reply)
        runtime._execute_action_rounds(turn)
        return self._finish_turn(turn, runtime._complete_turn(turn))

    def _finish_turn(self, turn: TurnContext, result: str) -> str:
        if getattr(self.runtime.execution_context, "child_run_id", None):
            return result
        schedule = getattr(self.runtime, "_schedule_post_turn_learning", None)
        if callable(schedule):
            try:
                schedule(turn.user_input)
            except Exception as exc:
                record = getattr(self.runtime, "_record_learning_event", None)
                if callable(record):
                    record("learning.dispatch_failed", {
                        "trigger": "post-turn", "reason": type(exc).__name__,
                    })
        return result

    def _native_tool_names(self) -> set[str]:
        runtime = self.runtime
        permitted = runtime._permitted_tool_names()
        if runtime._prompt_profile() != "compact":
            return permitted
        if "discover_tools" not in permitted:
            return permitted
        discovered = getattr(runtime.execution_context, "discovered_tools", frozenset())
        return permitted & (_COMPACT_CORE_TOOLS | set(discovered))

    def _run_native(self, turn: TurnContext) -> None:
        runtime = self.runtime
        from openkyrozen.agent.conversation import model_response_to_dict
        messages = runtime._build_messages(
            turn.user_input, turn.learned_context, turn.memory_context,
            memory_packet=turn.memory_packet, native_tools=True,
        )
        turn.tool_calls = []
        turn.tool_records = []
        turn.tool_results_text = ""
        successful_operations: set[str] = set()
        operation_scope = str(turn.learning_run.get("run_id"))
        final_text = ""
        response = None
        history_tool_names: set[str] = set()
        observed_plan_id = None

        max_steps = getattr(runtime.execution_context, "max_steps", None)
        if max_steps is None:
            max_steps = runtime.MAX_STEPS_PER_TURN
        for step in range(max_steps + 1):
            tool_names = self._native_tool_names()
            plan = getattr(runtime.tasks, "working_plan", None) if turn.planning_required else None
            if plan is not None and plan.status == "active":
                observed_plan_id = plan.id
            completed_plan = (observed_plan_id is not None and plan is not None
                              and plan.id == observed_plan_id and plan.status == "completed")
            needs_plan = (turn.planning_required and not getattr(runtime.execution_context, "child_run_id", None)
                          and (plan is None or plan.status != "active"))
            if needs_plan:
                if completed_plan:
                    tool_names = runtime._permitted_tool_names() & {"update_plan"}
                elif "update_plan" not in runtime._permitted_tool_names():
                    turn.protocol_error_message = "A Working Plan is required, but planning is not permitted."
                    break
                else:
                    tool_names = {"update_plan"}
            token = runtime._record_foreground_model.set(False)
            try:
                try:
                    response = runtime._get_model_response(
                        messages, tool_names=tool_names, history_tool_names=history_tool_names, stream=False,
                    )
                except Exception as exc:
                    runtime._turn_failed.set(True)
                    from openkyrozen.providers.errors import ProviderError
                    detail = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
                    turn.protocol_error_message = f"Native model request failed: {detail}; no further tools were run."
                    if runtime._conversation_turn_id.get() is not None:
                        runtime._record_conversation_entry("model.failure", {
                            "error_type": type(exc).__name__,
                        }, request_id=runtime._last_model_request_id.get())
                    break
            finally:
                runtime._record_foreground_model.reset(token)
            turn.turn_prompt_total += runtime._last_prompt_tokens
            turn.turn_completion_total += runtime._last_completion_tokens
            turn.response_text = response.text
            runtime._last_response_is_native.set(True)

            unknown_tools = [call.name for call in response.tool_calls if call.name not in tool_names]
            if unknown_tools:
                turn.protocol_error_message = ("A Working Plan is required before other tools can run."
                                               if needs_plan else
                                               "The provider returned an unknown tool call; no tool was run.")
                if runtime._conversation_turn_id.get() is not None:
                    runtime._record_conversation_entry("model.failure", {
                        "error_type": "invalid_native_tool_call",
                    }, request_id=runtime._last_model_request_id.get())
                break
            if response.tool_calls and step >= max_steps:
                turn.protocol_error_message = "The tool-call step limit was reached; remaining work was not run."
                if runtime._conversation_turn_id.get() is not None:
                    runtime._record_conversation_entry("model.failure", {
                        "error_type": "tool_step_limit",
                    }, request_id=runtime._last_model_request_id.get())
                break
            if runtime._conversation_turn_id.get() is not None:
                runtime._record_conversation_entry("model.response", {
                    "response": model_response_to_dict(response),
                }, request_id=runtime._last_model_request_id.get())

            if not response.tool_calls:
                final_text = response.text.strip()
                if needs_plan and not completed_plan:
                    final_text = ""
                    if step < max_steps:
                        messages.extend((response if response.continuation else {
                            "role": "assistant", "content": response.text,
                        }, {"role": "user", "content":
                            "Create a Working Plan with the update_plan tool before continuing."}))
                        continue
                    turn.protocol_error_message = "A Working Plan was not established; no further tools were run."
                    break
                if (not final_text or response.finish_reason == FinishReason.LENGTH) and step < 2:
                    messages.extend((response if response.continuation else {
                        "role": "assistant", "content": response.text,
                    }, {"role": "user", "content": (
                        "Your previous response was empty. Continue with a structured tool call if work remains, "
                        "or provide a concise final answer."
                    )}))
                    continue
                if not final_text.startswith("[LLM Error]"):
                    break
                turn.protocol_error_message = final_text
                break
            messages.append(response)
            history_tool_names.update(call.name for call in response.tool_calls)
            calls = [{"action": call.name, "args": call.arguments, "call_id": call.id,
                      "call_id_source": "native",
                      "task_id": getattr(getattr(runtime, "current_session", None), "_delegation_task_id", None)}
                     for call in response.tool_calls]
            turn.tool_calls.extend(calls)

            def record_native_call(call, receipt):
                record = runtime._record_turn_receipt(receipt, allow_legacy_task_matching=False)
                turn.tool_records.append(record)
                prompt_result = runtime._tool_result_for_prompt(receipt)
                runtime._render_tool_result(receipt.action, prompt_result)
                from openkyrozen.tools.models import ToolResult
                from openkyrozen.agent.conversation import tool_result_to_dict
                outcome = receipt.tool_result or ToolResult(data=receipt.result_data, text=receipt.result)
                reply_data = tool_result_to_dict(outcome)
                messages.append(ToolReply(call["call_id"], call["action"], reply_data, not outcome.success))
                turn.tool_results_text += f"- `{call['action']}` returned:\n{runtime._safe_fstring(prompt_result)}\n"
                runtime._update_tasks_panel()

            runtime._execute_tool_batch(
                calls, operation_scope=operation_scope, successful_operations=successful_operations,
                call_id_source="native", on_receipt=record_native_call,
            )

        turn.final_answer = final_text or runtime._deterministic_tool_summary(turn.tool_records)
