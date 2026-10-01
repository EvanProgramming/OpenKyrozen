from __future__ import annotations

import time
import json
from openkyrozen.tasks.engine import is_complete

def _complete_turn(self, turn):
    from openkyrozen.agent.delegation import TERMINAL
    coordinator = self.subagent_manager.coordinator
    delegated = []
    if coordinator:
        delegated = [run for run in coordinator.list()
                     if run["status"] not in TERMINAL or run["version"] != getattr(turn, "agent_versions", {}).get(run["run_id"])]
        ids = [run["run_id"] for run in delegated]
        while any(run["status"] not in TERMINAL for run in delegated):
            delegated = coordinator.wait(ids, 30)
        if delegated:
            for run in delegated:
                if run["status"] == "succeeded":
                    turn.tool_records.extend(run.get("parent_receipts", []))
            turn.tool_results_text += "\nDelegated results and independent verification:\n" + self._delegation_summary()
            if all(run["status"] == "succeeded" for run in delegated):
                # Retain accepted evidence if the synthesis provider fails or
                # emits an interaction/tool control instead of the report.
                sections = []
                for run in delegated:
                    submitted = run["report"]
                    lines = [f"{run['name']}: {submitted['summary']}"]
                    for field in ("findings", "evidence", "uncertainties"):
                        lines.extend(f"- {field}: " + (json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else str(item))
                            for item in submitted[field])
                    sections.append("\n".join(lines))
                turn.final_answer = "Verified delegated findings:\n\n" + "\n\n".join(sections)
                final = self._call_llm_with_spinner([{"role": "system", "content":
                    "Synthesize the verified delegated results for the user. Include evidence and limitations. "
                    "Return a plain-language findings report only, no Actions, AskUser, PlanProposal, or execution plan. "
                    "The user requested findings; proposed code fixes do not require a plan acceptance. "
                    "Do not introduce new behavioral claims, affected-input examples or code absent from the verified reports. "
                    "Retain file references, affected inputs, fixes and uncertainties. Do not claim other incomplete tasks succeeded."},
                    {"role": "user", "content": turn.user_input + "\n" + json.dumps([
                        {"name": run["name"], "assignment": run["assignment"], "report": run["report"],
                         "reviews": [{key: item for key, item in review.items() if key in {"verdict", "summary", "findings", "evidence"}}
                                     for review in run["reviews"]]} for run in delegated], ensure_ascii=False)}])
                turn.turn_prompt_total += self._last_prompt_tokens
                turn.turn_completion_total += self._last_completion_tokens
                parsed_final = self._parse_model_response(final)
                if (parsed_final["clean"] and not parsed_final["tool_calls"] and not parsed_final["question"]
                        and not parsed_final["plan_proposal"] and not parsed_final["protocol_error"]
                        and not parsed_final["unsupported_action_protocol"] and not final.startswith("[LLM Error]")):
                    turn.final_answer = parsed_final["clean"]
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
        turn.final_answer = (turn.protocol_error_message + "\n\n" + turn.final_answer
                             if delegated else turn.protocol_error_message)

    elapsed = time.time() - turn.turn_start
    self._turn_cost_log.append({
        "tokens": turn.turn_prompt_total + turn.turn_completion_total,
        "time": elapsed,
        "tool_calls": len(turn.tool_calls)
    })
    turn.final_answer = self._clean_final_response(turn.final_answer)

    # If tasks were completed, generate a summary so the user knows what happened
    total_tools_executed = len(turn.tool_records)
    if total_tools_executed >= 2 and not delegated and not durable_tasks_incomplete and turn.interaction_mode == "agent":
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

    unresolved = [run for run in delegated if run["status"] != "succeeded"]
    if unresolved:
        lines = ["Delegated work is not fully verified:"]
        for run in delegated:
            lines.append(f"- {run['name']}: {run['status']} — " + str(run.get("error") or
                (run["reviews"][-1]["summary"] if run["reviews"] else run.get("report", {}).get("summary", "No accepted result"))))
            submitted = run.get("report", {})
            for field in ("findings", "uncertainties"):
                lines.extend(f"  {field}: {item}" for item in submitted.get(field, []))
            if run["reviews"]:
                lines.extend(f"  Review finding: {item}" for item in run["reviews"][-1]["findings"])
        turn.final_answer = "\n".join(lines)

    return self._finish_learning_run(
        turn.learning_run, turn.learning_receipts, turn.user_input, turn.final_answer, turn.tool_records,
        turn.turn_prompt_total + turn.turn_completion_total, turn.turn_start,
    )
