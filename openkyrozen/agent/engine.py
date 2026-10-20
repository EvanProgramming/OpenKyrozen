"""Canonical foreground turn engine with a bounded structured-tool loop."""
from __future__ import annotations

from openkyrozen.agent.turn import TurnContext
from openkyrozen.providers.models import FinishReason, ToolReply


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
        tool_names = runtime._permitted_tool_names()
        if (capabilities and capabilities.native_tools and tool_names
                and turn.interaction_mode in {"agent", "ask"}):
            self._run_native(turn, tool_names)
            return runtime._complete_turn(turn)
        reply = runtime._initial_turn_response(turn)
        if reply is not None:
            return reply
        runtime._execute_action_rounds(turn)
        return runtime._complete_turn(turn)

    def _run_native(self, turn: TurnContext, tool_names) -> None:
        runtime = self.runtime
        from openkyrozen.agent.conversation import model_response_to_dict
        messages = runtime._build_messages(
            turn.user_input, turn.learned_context, turn.memory_context, native_tools=True,
        )
        turn.tool_calls = []
        turn.tool_records = []
        turn.tool_results_text = ""
        successful_operations: set[str] = set()
        operation_scope = str(turn.learning_run.get("run_id"))
        final_text = ""
        response = None

        for step in range(runtime.MAX_STEPS_PER_TURN + 1):
            token = runtime._record_foreground_model.set(False)
            try:
                try:
                    response = runtime._get_model_response(
                        messages, tool_names=tool_names, stream=False,
                    )
                except Exception as exc:
                    runtime._turn_failed.set(True)
                    turn.protocol_error_message = f"Native model request failed ({type(exc).__name__}); no further tools were run."
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
                turn.protocol_error_message = "The provider returned an unknown tool call; no tool was run."
                if runtime._conversation_turn_id.get() is not None:
                    runtime._record_conversation_entry("model.failure", {
                        "error_type": "invalid_native_tool_call",
                    }, request_id=runtime._last_model_request_id.get())
                break
            if response.tool_calls and step >= runtime.MAX_STEPS_PER_TURN:
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
            turn.tool_calls.extend({"action": call.name, "args": call.arguments, "call_id": call.id}
                                   for call in response.tool_calls)
            for call in response.tool_calls:
                receipt = runtime._execute_turn_action(
                    call.name, call.arguments, operation_scope=operation_scope,
                    successful_operations=successful_operations, call_id=call.id,
                    call_id_source="native",
                )
                record = runtime._record_turn_receipt(receipt, allow_legacy_task_matching=False)
                turn.tool_records.append(record)
                prompt_result = runtime._tool_result_for_prompt(receipt)
                runtime._render_tool_result(receipt.action, prompt_result)
                from openkyrozen.tools.models import ToolResult
                from openkyrozen.agent.conversation import tool_result_to_dict
                outcome = receipt.tool_result or ToolResult(data=receipt.result_data, text=receipt.result)
                reply_data = tool_result_to_dict(outcome)
                messages.append(ToolReply(call.id, call.name, reply_data, not outcome.success))
                turn.tool_results_text += f"- `{call.name}` returned:\n{runtime._safe_fstring(prompt_result)}\n"
                runtime._update_tasks_panel()

        turn.final_answer = final_text or runtime._deterministic_tool_summary(turn.tool_records)
