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

    if turn.protocol_error_message:
        turn.final_answer = (turn.protocol_error_message + "\n\n" + turn.final_answer
                             if delegated else turn.protocol_error_message)

    turn.final_answer = self._clean_final_response(turn.final_answer)

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

    verified_fix = turn.fix_workflow and turn.fix_workflow.get("stage") in {"verified", "explained"}
    failed_tools = [] if verified_fix else [record for record in turn.tool_records if not record.get("success")]
    turn.runtime_status = "incomplete" if failed_tools or turn.protocol_error_message else "completed"
    if failed_tools:
        failures = "\n".join(
            f"- {record.get('action', 'Tool')}: operation failed; its result was not verified."
            for record in failed_tools
        )
        turn.final_answer = f"{turn.final_answer.rstrip()}\n\nTool status: incomplete; failed operations were not verified.\n{failures}"
    plan = getattr(self.tasks, "working_plan", None)
    if (not self.execution_context.child_run_id
            and turn.interaction_mode == "agent" and getattr(turn, "complexity", None) == "complex"
            and plan is not None and plan.status in {"active", "completed", "cancelled"}):
        plan_tasks = [task for task in self.tasks.tasks if task.get("id") in plan.task_ids]
        succeeded = [task for task in plan_tasks if is_complete(task)]
        cancelled = [task for task in plan_tasks if task.get("status") == "cancelled"]
        blocked = [task for task in plan_tasks if task.get("status") == "blocked"]
        runnable = [task for task in plan_tasks if task.get("status") == "pending" and all(
            next((item.get("status") for item in plan_tasks if item.get("id") == dependency), None) == "succeeded"
            for dependency in task.get("dependencies", [])
        )]
        failed = [task for task in plan_tasks if task.get("status") == "failed"]
        if plan.status == "completed":
            turn.runtime_status = "completed"
            status_line = (f"Plan status: completed; {len(succeeded)} task(s) have verified acceptance evidence"
                           + (f" and {len(cancelled)} were deliberately cancelled." if cancelled else "."))
        elif plan.status == "cancelled":
            turn.runtime_status = "cancelled"
            status_line = "Plan status: cancelled; the Working Plan did not complete."
        elif blocked and not runnable and not any(task.get("status") == "running" for task in plan_tasks):
            turn.runtime_status = "blocked"
            status_line = "Plan status: blocked."
            for task in blocked:
                reason = (task.get("checkpoint") or {}).get("plan_block", {}).get("reason")
                status_line += f"\n- {task['description']}: {reason or 'No blocker reason was recorded.'}"
        else:
            turn.runtime_status = "incomplete"
            outstanding = [task for task in plan_tasks
                           if task.get("status") in {"pending", "running", "failed", "blocked"}]
            status_line = "Plan status: incomplete; required work remains."
            for task in outstanding:
                status_line += f"\n- {task['status']}: {task['description']}"
        turn.final_answer = f"{turn.final_answer.rstrip()}\n\n{status_line}"

    if failed_tools or turn.protocol_error_message:
        turn.runtime_status = "incomplete"

    completion_status = getattr(self, "_turn_completion_status", None)
    if callable(getattr(completion_status, "set", None)):
        completion_status.set(turn.runtime_status)

    self._turn_cost_log.append({
        "tokens": turn.turn_prompt_total + turn.turn_completion_total,
        "time": time.time() - turn.turn_start,
        "tool_calls": len(turn.tool_calls),
    })
    return self._finish_learning_run(
        turn.learning_run, turn.learning_receipts, turn.user_input, turn.final_answer, turn.tool_records,
        turn.turn_prompt_total + turn.turn_completion_total, turn.turn_start,
    )
