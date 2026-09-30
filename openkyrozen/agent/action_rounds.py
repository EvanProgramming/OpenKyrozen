from __future__ import annotations

import uuid
from typing import Any
from openkyrozen.tasks.engine import canonical_status, is_complete, is_terminal
from openkyrozen.security.tool_policy import tool_capability

def _execute_action_rounds(self, turn):
    results: list[str] = []
    turn.tool_records: list[dict[str, Any]] = []
    successful_operations: set[str] = set()
    operation_scope = str(turn.learning_run.get("run_id") or uuid.uuid4().hex)
    for tc in turn.tool_calls:
        receipt = self._execute_turn_action(
            tc.get("action", ""), tc.get("args", ""), operation_scope=operation_scope,
            successful_operations=successful_operations,
        )
        turn.tool_records.append(self._record_turn_receipt(receipt))
        prompt_result = self._tool_result_for_prompt(receipt)
        self._render_tool_result(receipt.action, prompt_result)
        results.append(f"- `{receipt.action}({receipt.args!r})` returned:\n{self._safe_fstring(prompt_result)}")
        if self.tasks.tasks:
            self._update_tasks_panel()
    all_tool_result_lines = list(results)
    turn.tool_results_text = "\n".join(all_tool_result_lines)

    # Unified multi-step loop - always runs, can handle early errors
    round_limit = self.MAX_STEPS_PER_TURN
    round_count = 0
    incomplete_prompt_attempts = 0
    turn.final_answer: str | None = None
    turn.pending_interaction_reply: str | None = None
    turn.protocol_error_message: str | None = None
    current_reply = turn.response_text
    has_errors = any(not record["success"] for record in turn.tool_records)
    consecutive_search_failures = 0  # track failed search_web calls to prevent loops
    total_search_calls = sum(record["action"] == "search_web" for record in turn.tool_records)
    unsupported_action_retries = 0
    # check for missing arguments errors
    _args_missing_errors = [
        "requires a command",
        "requires args in format path|content",
    ]
    _has_args_missing = any(
        any(pat in r for pat in _args_missing_errors) for r in results
    )
    # Remember the first tool-call block for the context
    initial_response = turn.response_text

    while round_count < round_limit:
        # Build the summary messages. If the last tool call failed, include guidance.
        error_hint = ""
        if has_errors:
            error_hint = (
                "The tool returned an error. "
            )
            if _has_args_missing:
                error_hint += (
                    "You probably forgot to provide the argument string. "
                    "For `write_file` the argument must be `path|content` (with a pipe separator). "
                    "For `run_cmd` or `execute_terminal_command` the argument must be the command string. "
                    "Do not leave the argument empty.\n"
                )
            else:
                error_hint += "The action name may be wrong. "
            error_hint += (
                "If you used a self-created tool, try using a built-in tool instead. "
                "Use one of the following actions: "
                + ", ".join(sorted(self.AVAILABLE_TOOLS.keys())) + ".\n"
            )

        # Search throttle: prevent infinite search loops WITHOUT stopping the task
        search_throttle = ""
        if consecutive_search_failures >= 3:
            search_throttle = (
                f"\n⚠️  **Search unavailable:** {consecutive_search_failures} consecutive "
                "search_web calls failed (rate‑limited or backend down).\n"
                "**Do NOT abort the task.** Continue by other means:\n"
                "- Use `read_webpage` on URLs you already know.\n"
                "- Work with partial/earlier search results you already have.\n"
                "- Use `run_cmd` with curl to fetch known API endpoints.\n"
                "- Write a script, read a file, or use any other tool.\n"
                "- If truly stuck, explain what you could find and what's missing.\n"
                "But **keep going** with the task — do not give up.\n"
            )
        elif total_search_calls >= 5:
            search_throttle = (
                f"\n⚠️  **Search rate warning:** {total_search_calls} search_web calls "
                "made this turn. At most 1 more search is allowed. Prefer other "
                "tools (read_webpage, run_cmd with curl, file operations) to "
                "continue the task. **Do not stop** — complete the user's request.\n"
            )
        summary_messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "You are Kyrozen, an intelligent AI assistant. "
                    "You have just obtained the following information by running tools. "
                    "Tool calls are internal progress, not the user-facing response. If more steps are needed, output the next Action block. "
                    "Otherwise, output a concise plain-language report of the work and result; never output only TaskDone or an Action block. "
                    "Output a PlanProposal block only in plan mode. "
                    f"The effective interaction mode is {turn.interaction_mode}."
                )
            },
            {"role": "system", "content": self._workspace_info()},
            {
                "role": "system",
                "content": self._build_memory_context(turn.user_input, context=turn.memory_context),
            },
            {
                "role": "system",
                "content": (
                    "Here are the tools you can use. "
                    "Make sure to pick an action name exactly as listed:\n"
                    + self._build_tools_list(self._execution_capability_token.capabilities)
                )
            },
            {"role": "user", "content": turn.user_input},
            {"role": "assistant", "content": current_reply},
            {
                "role": "system",
                "content": (
                    "REMINDER: The user's original request was:\n"
                    f"---\n{turn.user_input[:500]}\n---\n"
                    "Complete ALL parts of this request before stopping."
                )
            },
            {
                "role": "user",
                "content": (
                    self._build_task_progress_hint() +
                    f"The tools returned:\n{turn.tool_results_text}\n\n"
                    + error_hint + search_throttle +
                    "Please continue if there are remaining steps, or respond with the final answer.\n"
                    "If you have completed a task, you **must** output `TaskDone: <index>` "
                    "(replace index with the zero-based index) **before** the next Action block. "
                    "Do not omit the `TaskDone:` line.\n"
                    "Use AskUser only when material ambiguity requires clarification."
                )
            }
        ]
        if turn.interaction_mode in {"ask", "plan"}:
            completion_instruction = (
                "or finish with exactly one PlanProposal"
                if turn.interaction_mode == "plan" else
                "or finish with a direct answer"
            )
            summary_messages = [
                {"role": "system", "content": (
                    f"You are Kyrozen in read-only {turn.interaction_mode.title()} mode. Use only the listed inspection tools. "
                    f"Continue inspecting when evidence is still needed; otherwise {completion_instruction}."
                )},
                {"role": "system", "content": self._workspace_info()},
                {"role": "system", "content": (
                    "Available read-only tools:\n"
                    + self._build_tools_list(self._execution_capability_token.capabilities)
                )},
                {"role": "user", "content": turn.user_input},
                {"role": "assistant", "content": current_reply},
                {"role": "user", "content": (
                    f"Read-only inspection results:\n{turn.tool_results_text}\n\n"
                    "Continue with one listed inspection Action, ask a material clarification, "
                    f"{completion_instruction}. No changes have been authorized."
                )},
            ]
        step_reply = self._call_llm_with_spinner(summary_messages).strip()
        turn.turn_prompt_total += self._last_prompt_tokens
        turn.turn_completion_total += self._last_completion_tokens
        if not step_reply:
            break
        if step_reply.startswith("[LLM Error]"):
            for index, task in enumerate(self.tasks.tasks):
                if canonical_status(task["status"]) in {"pending", "running"}:
                    self.tasks.set_status(index, "blocked")
            turn.final_answer = step_reply
            break

        # Observe this response once.  The same parsed result drives task
        # updates, unknown-action handling, loop control, and final rendering.
        step_reply, step_meta = self._observe_turn_response(turn, step_reply, summary_messages)
        step_reply, step_meta, plan_recovery_exhausted = self._recover_plan_response(turn,
            step_reply, step_meta, summary_messages,
            inspection_complete=bool(turn.tool_records),
        )
        if plan_recovery_exhausted:
            turn.protocol_error_message = (
                "Plan mode could not produce a valid read-only inspection or PlanProposal after bounded recovery; "
                "no changes were made."
            )
            break
        next_tool_calls = step_meta["tool_calls"]
        if step_meta["protocol_error"]:
            turn.protocol_error_message = step_meta["protocol_error"]
            break

        interaction_reply = self._persist_interaction_control(step_meta, turn.user_input)
        if interaction_reply is not None:
            turn.pending_interaction_reply = interaction_reply
            turn.final_answer = interaction_reply
            break

        if step_meta["unsupported_action_protocol"] and not next_tool_calls:
            unsupported_action_retries += 1
            if unsupported_action_retries >= 3:
                turn.protocol_error_message = self._UNSUPPORTED_ACTION_PROTOCOL_MESSAGE
                break
            step_reply = self._call_llm_with_spinner(summary_messages + [{
                "role": "user",
                "content": (
                    f"System: {self._UNSUPPORTED_ACTION_PROTOCOL_MESSAGE} "
                    "Re-emit the next tool call exactly as one Action JSON block."
                ),
            }]).strip()
            turn.turn_prompt_total += self._last_prompt_tokens
            turn.turn_completion_total += self._last_completion_tokens
            if not step_reply:
                continue
            step_reply, step_meta = self._observe_turn_response(turn, step_reply, summary_messages)
            next_tool_calls = step_meta["tool_calls"]
            if step_meta["protocol_error"]:
                turn.protocol_error_message = step_meta["protocol_error"]
                break
            interaction_reply = self._persist_interaction_control(step_meta, turn.user_input)
            if interaction_reply is not None:
                turn.pending_interaction_reply = interaction_reply
                turn.final_answer = interaction_reply
                break
            if not next_tool_calls and step_meta["unsupported_action_protocol"]:
                continue

        # ----- reject unknown action names and force re-prompting -----
        _unknown_tool_retries = 0
        while _unknown_tool_retries < 3:
            unknown_action = step_meta["unknown_action"]
            if not unknown_action:
                break
            _unknown_tool_retries += 1
            msg = (
                f"System: Action '{unknown_action}' is not recognized.\n"
                "You **must** use one of the following action names exactly:\n"
                + ", ".join(sorted(
                    name for name in self.AVAILABLE_TOOLS
                    if tool_capability(name) in self._execution_capability_token.capabilities
                )) + "\n"
                "Do not invent new names. Output an Action block now."
            )
            # re-prompt the LLM
            step_reply = self._call_llm_with_spinner(
                summary_messages + [{"role": "user", "content": msg}]
            ).strip()
            turn.turn_prompt_total += self._last_prompt_tokens
            turn.turn_completion_total += self._last_completion_tokens
            if not step_reply:
                break
            # Re-observe the replacement response exactly once.
            step_reply, step_meta = self._observe_turn_response(turn, step_reply, summary_messages)
            next_tool_calls = step_meta["tool_calls"]
            if step_meta["protocol_error"]:
                turn.protocol_error_message = step_meta["protocol_error"]
                break
            interaction_reply = self._persist_interaction_control(step_meta, turn.user_input)
            if interaction_reply is not None:
                turn.pending_interaction_reply = interaction_reply
                turn.final_answer = interaction_reply
                next_tool_calls = []
                break

        if turn.protocol_error_message or turn.pending_interaction_reply is not None:
            break

        # if after 3 retries the action is still unknown, clear the list to avoid a crash
        if _unknown_tool_retries >= 3:
            next_tool_calls = []

        # if there are no more tool calls, the LLM might be giving a natural reply
        if not next_tool_calls:
            if turn.interaction_mode == "plan":
                turn.protocol_error_message = "Plan mode ended without a valid PlanProposal; no changes were made."
                break

            # Stop once every task is terminal, but only trust model prose when
            # every durable task actually succeeded.
            all_terminal = not self.tasks.tasks or all(is_terminal(t) for t in self.tasks.tasks)
            all_succeeded = not self.tasks.tasks or all(is_complete(t) for t in self.tasks.tasks)
            if all_terminal and not step_meta["define_tool_registered"]:
                turn.final_answer = (
                    step_meta["clean"] if all_succeeded and step_meta["clean"]
                    else self._deterministic_tool_summary(turn.tool_records)
                )
                break

            # Find the next pending task to tell the LLM what to do
            next_pending_desc = ""
            for t in self.tasks.tasks:
                if canonical_status(t["status"]) == "pending":
                    next_pending_desc = t["description"]
                    break

            if self.tasks.tasks and any(
                not is_complete(t) and canonical_status(t["status"]) != "cancelled"
                for t in self.tasks.tasks
            ):
                incomplete_prompt_attempts += 1
                if incomplete_prompt_attempts >= 12:
                    for idx, task in enumerate(self.tasks.tasks):
                        if canonical_status(task["status"]) in {"pending", "running"}:
                            self.tasks.set_status(idx, "blocked")
                    self._render_warning(f"Blocked remaining tasks after {incomplete_prompt_attempts} unsuccessful nudges; no task was marked complete.")
                    break

                # Build a specific nudge mentioning the exact next task
                nudge = (
                    f"System: You have {sum(1 for t in self.tasks.tasks if t['status'] == 'pending')} "
                    "incomplete tasks remaining. "
                )
                if next_pending_desc:
                    nudge += f"The next task is: \"{next_pending_desc}\". "
                nudge += "Output the Action block for this task NOW. Do not explain or summarise — just act."
                summary_messages.append({"role": "user", "content": nudge})
                step_reply = self._call_llm_with_spinner(summary_messages).strip()
                turn.turn_prompt_total += self._last_prompt_tokens
                turn.turn_completion_total += self._last_completion_tokens
                if not step_reply:
                    break
                step_reply, step_meta = self._observe_turn_response(turn, step_reply, summary_messages)
                next_tool_calls = step_meta["tool_calls"]
                if step_meta["protocol_error"]:
                    turn.protocol_error_message = step_meta["protocol_error"]
                    break
                interaction_reply = self._persist_interaction_control(step_meta, turn.user_input)
                if interaction_reply is not None:
                    turn.pending_interaction_reply = interaction_reply
                    turn.final_answer = interaction_reply
                    break
                if not next_tool_calls:
                    # Still no action — don't give up yet, loop will try again
                    # (incomplete_prompt_attempts will eventually trigger the 12-nudge limit)
                    pass
            elif self._is_question(step_reply):
                turn.final_answer = step_meta["clean"] or self._deterministic_tool_summary(turn.tool_records)
                break
            else:
                summary_messages.append({
                    "role": "user",
                    "content": "System: You have not output an Action block. "
                               "Output the next JSON Action block now to continue."
                })
                step_reply = self._call_llm_with_spinner(summary_messages).strip()
                turn.turn_prompt_total += self._last_prompt_tokens
                turn.turn_completion_total += self._last_completion_tokens
                if not step_reply:
                    break
                step_reply, step_meta = self._observe_turn_response(turn, step_reply, summary_messages)
                next_tool_calls = step_meta["tool_calls"]
                if step_meta["protocol_error"]:
                    turn.protocol_error_message = step_meta["protocol_error"]
                    break
                interaction_reply = self._persist_interaction_control(step_meta, turn.user_input)
                if interaction_reply is not None:
                    turn.pending_interaction_reply = interaction_reply
                    turn.final_answer = interaction_reply
                    break
                if not next_tool_calls:
                    turn.final_answer = step_meta["clean"] or self._deterministic_tool_summary(turn.tool_records)
                    break

        # execute all new tool calls
        round_count += 1
        next_results: list[str] = []
        has_errors = False
        for tc2 in next_tool_calls:
            receipt = self._execute_turn_action(
                tc2.get("action", ""), tc2.get("args", ""), operation_scope=operation_scope,
                successful_operations=successful_operations,
            )
            turn.tool_records.append(self._record_turn_receipt(receipt))
            prompt_result = self._tool_result_for_prompt(receipt)
            self._render_tool_result(receipt.action, prompt_result)
            next_results.append(f"- `{receipt.action}({receipt.args!r})` returned:\n{self._safe_fstring(prompt_result)}")
            if self.tasks.tasks:
                self._update_tasks_panel()
            if not receipt.success:
                has_errors = True
            # Track search_web failures to prevent infinite search loops
            if receipt.action == "search_web":
                total_search_calls += 1
                if not receipt.success or "Search temporarily unavailable" in receipt.result:
                    consecutive_search_failures += 1
                else:
                    consecutive_search_failures = 0  # reset on success
        all_tool_result_lines.extend(next_results)
        turn.tool_results_text = "\n".join(all_tool_result_lines)
        _has_args_missing = any(
            any(pat in r for pat in [
                "requires a command",
                "requires args in format path|content",
            ]) for r in next_results
        )
        # Hard limit: too many search_web calls → one last chance to complete
        if total_search_calls >= 7 and consecutive_search_failures >= 3:
            final_msg = (
                "System: Search is unavailable (7+ calls, {0} consecutive failures). "
                "Stop searching now. Give your **best final answer** using whatever "
                "information you already gathered from earlier tool results. "
                "Do NOT output another Action block — just give the final answer "
                "directly. Acknowledge any gaps honestly."
            ).format(consecutive_search_failures)
            step_reply = self._call_llm_with_spinner(
                summary_messages + [{"role": "user", "content": final_msg}]
            ).strip()
            step_reply, search_meta = self._observe_turn_response(turn, step_reply, summary_messages)
            interaction_reply = self._interaction_gate(search_meta, turn.user_input)
            if interaction_reply is not None:
                if search_meta.get("question") is not None or search_meta.get("plan_proposal") is not None:
                    turn.pending_interaction_reply = interaction_reply
                else:
                    turn.protocol_error_message = interaction_reply
                turn.final_answer = interaction_reply
            else:
                turn.final_answer = search_meta["clean"] or self._deterministic_tool_summary(turn.tool_records)
            self._render_warning("Search limit reached — synthesizing final answer.")
            break
        # Check if all tasks reached a durable terminal state; if so, stop.
        # (also stops if no tasks were set — medium complexity just runs tools sequentially)
        if not next_tool_calls and (not self.tasks.tasks or all(is_terminal(t) for t in self.tasks.tasks)):
            # Only the latest response may provide prose.  The initial Action
            # response is never a valid fallback after work has run.
            unsuccessful = any(
                canonical_status(t["status"]) in {"failed", "blocked"}
                for t in self.tasks.tasks
            )
            turn.final_answer = (
                self._deterministic_tool_summary(turn.tool_records)
                if unsuccessful or not step_meta["clean"]
                else step_meta["clean"]
            )
            break
        # Update the LLM's previous output so the next iteration sees the new results (remove task blocks)
        current_reply = self._remove_task_blocks(step_reply)
