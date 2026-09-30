from __future__ import annotations

import time
from openkyrozen.tasks.engine import is_complete

def _complete_turn(self, turn):
    if turn.final_answer is None:
        # Fallback if the loop exited without a final answer
        turn.final_answer = self._deterministic_tool_summary(turn.tool_records)
    elif len(turn.final_answer.strip()) < 10:
        turn.final_answer = self._deterministic_tool_summary(turn.tool_records)

    # Durable task rows are authoritative.  Never let a natural-language
    # response or generated recap claim completion when any task failed,
    # blocked, cancelled, pending, or is still running.
    durable_tasks_incomplete = bool(self.tasks.tasks) and not all(is_complete(t) for t in self.tasks.tasks)
    if durable_tasks_incomplete and turn.pending_interaction_reply is None:
        summary = self._deterministic_tool_summary(turn.tool_records)
        turn.final_answer = (
            f"{turn.protocol_error_message}\n\n{summary}" if turn.protocol_error_message else summary
        )
    elif turn.protocol_error_message:
        turn.final_answer = turn.protocol_error_message

    elapsed = time.time() - turn.turn_start
    self._turn_cost_log.append({
        "tokens": turn.turn_prompt_total + turn.turn_completion_total,
        "time": elapsed,
        "tool_calls": len(turn.tool_calls)
    })
    turn.final_answer = self._clean_final_response(turn.final_answer)

    # If tasks were completed, generate a summary so the user knows what happened
    total_tools_executed = len(turn.tool_records)
    if total_tools_executed >= 2 and not durable_tasks_incomplete and turn.interaction_mode == "agent":
        summary_prompt = (
            "You just completed a multi-step task. Summarise your work below.\n\n"
            "## What was accomplished\n"
            "- (2-4 bullet points: tools used, files created, key results)\n\n"
            "Output only the completed summary in plain text (no Action blocks).\n\n"
            f"Tool results:\n{turn.tool_results_text[:1500]}"
        )
        try:
            summary_raw = self._get_llm_response(
                [{"role": "system", "content": summary_prompt}]
            ).strip()
            turn.turn_prompt_total += self._last_prompt_tokens
            turn.turn_completion_total += self._last_completion_tokens
            summary_meta = self._parse_model_response(summary_raw)
            summary = summary_meta["clean"] if not summary_meta["tool_calls"] else ""
            if summary and len(summary) > 30:
                if len(turn.final_answer.strip()) < 60:
                    turn.final_answer = summary
                else:
                    turn.final_answer = turn.final_answer + "\n\n---\n\n" + summary
        except Exception:
            pass

    turn.fix_workflow, turn.final_answer = self._advance_fix_workflow(
        turn.fix_workflow, turn.user_input, turn.final_answer,
        turn.tool_records, f"{turn.response_text}\n{turn.final_answer}",
    )

    return self._finish_learning_run(
        turn.learning_run, turn.learning_receipts, turn.user_input, turn.final_answer, turn.tool_records,
        turn.turn_prompt_total + turn.turn_completion_total, turn.turn_start,
    )
