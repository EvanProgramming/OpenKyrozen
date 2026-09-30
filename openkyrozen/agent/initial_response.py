from __future__ import annotations

import time
from openkyrozen.app.config import load_agent_config
from openkyrozen.security.tool_policy import tool_capability

def _initial_turn_response(self, turn):
    MAX_RETRIES = 3
    turn.auto_clarified = False
    messages = self._build_messages(turn.user_input, turn.learned_context, turn.memory_context)
    turn.response_text = self._call_llm_with_spinner(messages).strip()
    turn.turn_prompt_total += self._last_prompt_tokens
    turn.turn_completion_total += self._last_completion_tokens

    if not turn.response_text or not turn.response_text.strip():
        messages.append({
            "role": "user",
            "content": (
                "System: You returned nothing. Output exactly one listed read-only inspection Action now."
                if turn.interaction_mode == "plan" else
                "System: You returned nothing. Please output your Thought and JSON Action now."
            ),
        })
        turn.response_text = self._call_llm_with_spinner(messages).strip()
        turn.turn_prompt_total += self._last_prompt_tokens
        turn.turn_completion_total += self._last_completion_tokens
    if turn.response_text.startswith("[LLM Error]"):
        return self._finish_learning_run(
            turn.learning_run, turn.learning_receipts, turn.user_input, turn.response_text, [],
            turn.turn_prompt_total + turn.turn_completion_total, turn.turn_start,
        )

    # Parse and observe each model response once.  Keep the raw response for
    # model context; use the parsed clean field for anything user-facing.
    turn.response_text, response_meta = self._observe_turn_response(turn, turn.response_text, messages)
    turn.response_text, response_meta, plan_recovery_exhausted = self._recover_plan_response(turn,
        turn.response_text, response_meta, messages,
        inspection_complete=turn.inspection_complete,
    )
    if plan_recovery_exhausted:
        return self._finish_learning_run(
            turn.learning_run, turn.learning_receipts, turn.user_input,
            "Plan mode could not produce a valid read-only inspection or PlanProposal after bounded recovery; no changes were made.",
            [], turn.turn_prompt_total + turn.turn_completion_total, turn.turn_start,
        )
    turn.tool_calls = response_meta["tool_calls"]
    if response_meta["define_tool_registered"]:
        agent_config = load_agent_config(self._get_workspace_root())
        messages.append({
            "role": "system",
            "content": "Refreshed tool inventory after DefineTool registration:\n"
                       + self._agent_prompt_tools_list(agent_config),
        })

    interaction_reply = self._interaction_gate(response_meta, turn.user_input)
    if interaction_reply is not None:
        return self._finish_learning_run(
            turn.learning_run, turn.learning_receipts, turn.user_input, interaction_reply, [],
            turn.turn_prompt_total + turn.turn_completion_total, turn.turn_start,
        )

    # ---- Unknown action detection (all complexity levels) ----
    _unknown_retries = 0
    while _unknown_retries < self.MAX_UNKNOWN_TOOL_RETRIES:
        unknown_action = response_meta["unknown_action"]
        if not unknown_action:
            break
        _unknown_retries += 1
        self._notify_tool_execute(
            unknown_action, "", f"Error: unknown tool '{unknown_action}'",
        )
        allowed_actions = sorted(
            name for name in self.AVAILABLE_TOOLS
            if tool_capability(name) in self._execution_capability_token.capabilities
        )
        msg = (
            f"System: Action '{unknown_action}' is not recognized. "
            "You must use one of the following actions: "
            + ", ".join(allowed_actions) + ".\n"
            "Do not invent new action names. Pick from the list and output an Action block."
        )
        messages.append({"role": "user", "content": msg})
        turn.response_text = self._call_llm_with_spinner(messages).strip()
        turn.turn_prompt_total += self._last_prompt_tokens
        turn.turn_completion_total += self._last_completion_tokens
        turn.response_text, response_meta = self._observe_turn_response(turn, turn.response_text, messages)
        turn.tool_calls = response_meta["tool_calls"]
        interaction_reply = self._interaction_gate(response_meta, turn.user_input)
        if interaction_reply is not None:
            return self._finish_learning_run(
                turn.learning_run, turn.learning_receipts, turn.user_input, interaction_reply, [],
                turn.turn_prompt_total + turn.turn_completion_total, turn.turn_start,
            )

    # ---- Plan enforcement: MEDIUM and COMPLEX only ----
    _llm_has_plan = response_meta["has_plan"]
    if turn.interaction_mode == "agent" and turn.complexity in ("medium", "complex"):
        plan_attempts = 0
        while not _llm_has_plan and turn.tool_calls and plan_attempts < 2:
            plan_attempts += 1
            plan_hint = (
                "System: This is a {0} task. Output a Plan block first:\n"
                "Plan:\n1. <first step – what and why>\n2. <second step>\n...".format(turn.complexity)
            )
            messages.append({"role": "user", "content": plan_hint})
            turn.response_text = self._call_llm_with_spinner(messages).strip()
            turn.turn_prompt_total += self._last_prompt_tokens
            turn.turn_completion_total += self._last_completion_tokens
            turn.response_text, response_meta = self._observe_turn_response(turn, turn.response_text, messages)
            turn.tool_calls = response_meta["tool_calls"]
            _llm_has_plan = response_meta["has_plan"]
            interaction_reply = self._interaction_gate(response_meta, turn.user_input)
            if interaction_reply is not None:
                return self._finish_learning_run(
                    turn.learning_run, turn.learning_receipts, turn.user_input, interaction_reply, [],
                    turn.turn_prompt_total + turn.turn_completion_total, turn.turn_start,
                )
        if not _llm_has_plan and turn.tool_calls and plan_attempts >= 2:
            # Auto‑generate a minimal plan from tool calls
            plan_lines = ["Plan:"]
            for i, tc in enumerate(turn.tool_calls):
                plan_lines.append(f"{i+1}. Execute {tc.get('action','?')}")
            # Don't inject — just let it proceed without plan this time

    # ---- TaskList enforcement: COMPLEX only ----
    _llm_has_tasklist = response_meta["has_tasklist"]
    if turn.interaction_mode == "agent" and turn.complexity == "complex" and turn.tool_calls:
        if not _llm_has_tasklist and _llm_has_plan:
            self._tasks_from_plan(turn.response_text)
            if self.tasks.tasks:
                _llm_has_tasklist = True
                self._update_tasks_panel()
        if not _llm_has_tasklist:
            # Auto‑generate TaskList from plan or tool calls
            # Never clear durable tasks here: a repeated plan is an update to
            # the current turn, not permission to erase recovered progress.
            if _llm_has_plan and not self.tasks.tasks:
                self._tasks_from_plan(turn.response_text)
            if not self.tasks.tasks:
                for tc in turn.tool_calls:
                    safe_args = str(tc.get('args') or '')[:50]
                    self.tasks.add_task(f"Execute {tc.get('action','?')}: {safe_args}")
            if self.tasks.tasks:
                self._update_tasks_panel()

    if not turn.tool_calls:
        if (turn.interaction_mode == "agent" and (self._requires_tool_action(turn.user_input) or _llm_has_plan or _llm_has_tasklist
                or response_meta["define_tool_registered"]
                or response_meta["unsupported_action_protocol"])):
            action_retries = 0
            while not turn.tool_calls and action_retries < 3:
                action_retries += 1
                if response_meta["unsupported_action_protocol"]:
                    reminder = (
                        f"System: {self._UNSUPPORTED_ACTION_PROTOCOL_MESSAGE} "
                        "Output only one complete Action block now."
                    )
                elif action_retries == 1:
                    reminder = (
                        "System: You output a protocol block but no Action block. "
                        "You **must** now output a JSON Action block to perform the work. "
                        "If a new tool was registered, use its exact name from the refreshed tool inventory. "
                        "Do not repeat the Plan or DefineTool block — output only the next Action."
                    )
                elif action_retries == 2:
                    reminder = (
                        "System: STILL no Action block. Output ONLY this now:\n\n"
                        "Action:\n```json\n{\"action\": \"write_file\", \"args\": \"...\"}\n```\n\n"
                        "Pick the first step from your Plan and execute it. No Plan, no TaskList."
                    )
                else:
                    reminder = (
                        "System: FINAL attempt. You have a Plan. Execute step 1. "
                        "Output a single Action block. Nothing else.\n"
                        "Action:\n```json\n"
                    )
                messages.append({"role": "user", "content": reminder})
                turn.response_text = self._call_llm_with_spinner(messages).strip()
                turn.turn_prompt_total += self._last_prompt_tokens
                turn.turn_completion_total += self._last_completion_tokens
                if not turn.response_text:
                    continue
                turn.response_text, response_meta = self._observe_turn_response(turn, turn.response_text, messages)
                turn.tool_calls = response_meta["tool_calls"]
                interaction_reply = self._interaction_gate(response_meta, turn.user_input)
                if interaction_reply is not None:
                    return self._finish_learning_run(
                        turn.learning_run, turn.learning_receipts, turn.user_input, interaction_reply, [],
                        turn.turn_prompt_total + turn.turn_completion_total, turn.turn_start,
                    )
                if response_meta["define_tool_registered"]:
                    agent_config = load_agent_config(self._get_workspace_root())
                    messages.append({
                        "role": "system",
                        "content": "Refreshed tool inventory after DefineTool registration:\n"
                                   + self._agent_prompt_tools_list(agent_config),
                    })
                if turn.tool_calls:
                    _llm_has_plan = response_meta["has_plan"]
                    _llm_has_tasklist = response_meta["has_tasklist"]
        if not turn.tool_calls:
            elapsed = time.time() - turn.turn_start
            self._turn_cost_log.append({
                "tokens": turn.turn_prompt_total + turn.turn_completion_total,
                "time": elapsed,
                "tool_calls": 0
            })
            proposed = response_meta["clean"] or "I could not produce a user-facing response."
            if response_meta["unsupported_action_protocol"]:
                proposed = self._UNSUPPORTED_ACTION_PROTOCOL_MESSAGE
            turn.fix_workflow, proposed = self._advance_fix_workflow(
                turn.fix_workflow, turn.user_input, proposed, [], turn.response_text,
            )
            return self._finish_learning_run(
                turn.learning_run, turn.learning_receipts, turn.user_input, proposed, [],
                turn.turn_prompt_total + turn.turn_completion_total, turn.turn_start,
            )
