"""Canonical foreground turn engine with a bounded structured-tool loop."""
from __future__ import annotations

import json
import re

from openkyrozen.agent.turn import TurnContext
from openkyrozen.providers.models import FinishReason, ProviderContractError, TextDelta, ToolCallDelta, ToolReply

_COMPACT_CORE_TOOLS = frozenset({
    "list_dir", "find_files", "search_files", "read_file", "write_file", "edit_file", "apply_patch",
    "run_cmd", "discover_tools", "read_tool_artifact", "update_plan",
    "propose_plan", "request_question", "spawn_agents", "wait_subagents",
})
_NATIVE_CONTROLS = frozenset({"propose_plan", "request_question"})

_DELEGATION_REQUEST = re.compile(
    r"(?:^|[.!?]\s+|\n\s*(?:[-*]\s+)?)\s*(?:please\s+)?"
    r"(?P<action>delegate\s+|use\s+(?:(?:an?|one)\s+)?(?:parallel\s+reviews?|sub[- ]?agents?)\b)",
    re.IGNORECASE,
)

_SINGLE_DELEGATION_REQUEST = re.compile(
    r"^(?:delegate\s+(?:a|an|one|the)\s+(?:[\w-]+\s+){0,4}(?:review|investigation)\b"
    r"|use\s+(?:a|one)\s+sub[- ]?agent\b)",
    re.IGNORECASE,
)
_DELEGATION_SENTENCE_END = re.compile(r"[!?\n]|\.(?:\s|$)")
_PARALLEL_DELEGATION_REQUEST = re.compile(
    r"\b(?:parallel|disjoint|multiple|separate\s+assignments|and|or|[2-9]\d*|two|three)\b", re.IGNORECASE,
)


_FOLLOWUP_WRITE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})
_INSPECTION_TOOLS = frozenset({"list_dir", "find_files", "search_files", "read_file", "read_tool_artifact"})


def _delegation_directives(user_input: str):
    prose = re.split(r"(?m)^[ \t]*(?:[`]{3,}|~{3,})[^\n]*", user_input)[::2]
    for part in prose:
        for match in _DELEGATION_REQUEST.finditer(part):
            end = _DELEGATION_SENTENCE_END.search(part, match.end())
            yield part[match.start("action"):end.start() if end else len(part)]


def _requests_delegation(user_input: str) -> bool:
    return any(_delegation_directives(user_input))


def _requests_one_delegation(user_input: str) -> bool:
    directives = _delegation_directives(user_input)
    directive = next(directives, "")
    return (_SINGLE_DELEGATION_REQUEST.match(directive) is not None
            and _PARALLEL_DELEGATION_REQUEST.search(directive) is None and next(directives, None) is None)


def _followup_workspace_tools(runtime):
    permitted = runtime._permitted_tool_names()
    allowed = runtime._effective_execution_capabilities()
    registry = runtime.tool_registry
    plan_tool = registry.get_spec("update_plan")
    if ("update_plan" not in permitted or plan_tool is None
            or plan_tool.source != "builtin" or plan_tool.capability not in allowed):
        return []
    return sorted(name for name in (_FOLLOWUP_WRITE_TOOLS | {"run_cmd"}) & permitted
                  if (spec := registry.get_spec(name)) is not None
                  and spec.source == "builtin" and spec.capability in allowed)


def _can_start_command_followup(runtime, plan):
    if plan is None or plan is not runtime.tasks.working_plan or plan.status != "completed":
        return False
    interaction = runtime._interaction_controller.state()
    tasks = [task for task in runtime.tasks.tasks if task["id"] in plan.task_ids]
    return (not any(interaction.get(key) for key in ("executing_plan", "pending_plan", "pending_question"))
            and {task["id"] for task in tasks} == set(plan.task_ids)
            and not any((task.get("checkpoint") or {}).get("strategic_plan") for task in tasks)
            and "run_cmd" in _followup_workspace_tools(runtime))


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
                and turn.interaction_mode in {"agent", "ask", "plan"}):
            turn.native_tools = True
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
        if "discover_tools" not in permitted:
            return permitted
        discovered = getattr(runtime.execution_context, "discovered_tools", frozenset())
        return permitted & (_COMPACT_CORE_TOOLS | set(discovered))

    def _store_tool_output(self, content):
        runtime = self.runtime
        memory = runtime.memory_bank
        tasks = getattr(runtime, "tasks", None)
        return memory.store.artifacts.get_or_create(
            content, user_id=getattr(tasks, "user_id", None) or memory.user_id,
            workspace_id=getattr(tasks, "workspace_id", None) or memory.workspace_id,
            session_id=getattr(tasks, "session_id", None) or memory.session_id,
        )

    def _archive_replies(self, messages, indexes, *, archive_small=True):
        import json
        from openkyrozen.agent.executor import _TOOL_ARTIFACT_THRESHOLD
        for index in indexes:
            reply = messages[index]
            if reply.name == "read_tool_artifact":
                continue
            output = reply.output
            data = output.get("data") if isinstance(output, dict) else None
            metadata = output.get("metadata") if isinstance(output, dict) else None
            if (isinstance(data, dict) and isinstance(metadata, dict)
                    and isinstance(data.get("artifact_id"), str) and data["artifact_id"]
                    and metadata.get("artifact_id") == data["artifact_id"]):
                continue
            content = json.dumps(reply.output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if not archive_small and len(content) <= _TOOL_ARTIFACT_THRESHOLD:
                continue
            artifact_id = self._store_tool_output(content)
            summary = {"artifact_id": artifact_id, "total_chars": len(content)}
            archived_metadata = {"artifact_id": artifact_id}
            if isinstance(metadata, dict) and isinstance(metadata.get("receipt_id"), str):
                archived_metadata["receipt_id"] = metadata["receipt_id"]
            messages[index] = ToolReply(reply.call_id, reply.name, {
                "data": summary, "text": "Earlier tool result archived; use read_tool_artifact to retrieve it.",
                "error": ({"code": "archived_result", "message": "Tool call failed; see artifact.",
                           "retriable": False, "metadata": {"artifact_id": artifact_id}}
                          if reply.is_error else None),
                "metadata": archived_metadata,
            }, reply.is_error)

    def _working_plan_continuation(self, turn, *, needs_plan=False, followup_tool_groups=()):
        """Offer state once, without scheduling work or granting tool authority."""
        from openkyrozen.agent.completion import _missing_artifact_source_path
        from openkyrozen.agent.delegation import TERMINAL
        from openkyrozen.agent.executor import _strategic_plan_denial
        from openkyrozen.tasks.engine import _unknown_recovery_effect
        from openkyrozen.tools.processes import SessionProcessManager

        runtime = self.runtime
        try:
            context = runtime.execution_context
            if (turn.interaction_mode != "agent" or not turn.planning_required or context.child_run_id
                    or turn.protocol_error_message or turn.pending_interaction_reply is not None
                    or (context.cancelled is not None and context.cancelled.is_set())):
                return None
            interaction = runtime._interaction_controller.state()
            if (interaction.get("pending_question") or interaction.get("pending_plan")
                    or runtime._approval_request.get() is not None):
                return None
            processes = runtime.current_session.process_manager
            if type(processes) is not SessionProcessManager or processes._audit_store is None:
                return None
            with processes._lock:
                if processes._closed or any(h.process.poll() is None for h in processes._handles.values()):
                    return None
            if processes._recovery_context() or any(r["potentially_active"] for r in processes._audit_records()):
                return None
            coordinator = runtime.subagent_manager.coordinator
            if coordinator and any(r["status"] not in TERMINAL or r["version"] !=
                    getattr(turn, "agent_versions", {}).get(r["run_id"]) for r in coordinator.list()):
                return None
            for record in turn.tool_records:
                verification = record.get("verification") or {}
                no_effect_plan_input = (record.get("action") == "update_plan"
                    and record.get("failure") == "invalid_arguments"
                    and verification.get("method") == "plan_input_validation"
                    and (verification.get("observed") or {}).get("committed") is False)
                if ((record.get("success") is not True and not no_effect_plan_input)
                        or verification.get("status") == "failed"):
                    if not needs_plan or not _missing_artifact_source_path(record):
                        return None
            manager = runtime.tasks
            plan = manager.working_plan
            scope = {"user_id": manager.user_id, "workspace_id": manager.workspace_id,
                     "session_id": manager.session_id or ""}
            permitted = runtime._permitted_tool_names()
            can_update_plan = "update_plan" in permitted
            if needs_plan:
                if not can_update_plan or manager.store.get_working_plan(**scope) is not None:
                    return None
                return {"plan_id": None, "revision": 0, "status": "required",
                        "eligible_tasks": [], "completion_evidence": "a plan and verified receipts are required",
                        "decisions": ["create a Working Plan with update_plan and runtime-owned IDs",
                                      "explicitly start a task before its work; never fabricate completion"]}
            if followup_tool_groups:
                followup_tools = _followup_workspace_tools(runtime)
                actions = {record.get("action") for record in turn.tool_records}
                if (plan is None or plan.status != "completed" or interaction.get("executing_plan")
                        or any(group.isdisjoint(followup_tools) for group in followup_tool_groups)
                        or not actions & _INSPECTION_TOOLS or actions - _INSPECTION_TOOLS - {"update_plan"}
                        or manager.store.get_working_plan(**scope) is not None):
                    return None
                tasks = manager.store.list_working_plan_tasks(
                    plan.id, **scope, current_completed_revision=plan.revision)
                if (not tasks or tuple(task["id"] for task in tasks) != plan.task_ids
                        or any((task.get("checkpoint") or {}).get("strategic_plan")
                               or _unknown_recovery_effect(task) or task["status"] != "succeeded"
                               or not manager._task_has_evidence(task) for task in tasks)):
                    return None
                return {"plan_id": plan.id, "revision": plan.revision, "status": "completed",
                        "followup_tools": followup_tools,
                        "decisions": ["Use update_plan at revision zero with no plan_id to add and start "
                                      "a fresh task for the remaining user-requested work.",
                                      "The listed tools can become available for a selected task. "
                                      "Workspace, capability, and approval checks still apply."]}
            if plan is None or plan.status != "active":
                return None
            saved = manager.store.get_working_plan_by_id(plan.id, **scope)
            tasks = manager.store.list_working_plan_tasks(plan.id, **scope)
            if (not saved or saved["status"] != "active" or saved["revision"] != plan.revision
                    or tuple(t["id"] for t in tasks) != plan.task_ids):
                return None
            cached = {t["id"]: t for t in manager.tasks}
            eligible = []
            for task in tasks:
                if (task["status"] in {"blocked", "failed"} or _unknown_recovery_effect(task)
                        or any(task.get(k) != cached.get(task["id"], {}).get(k)
                               for k in ("status", "checkpoint", "acceptance", "dependencies"))):
                    return None
                if manager.active_working_task(task["id"]) is None:
                    continue
                checkpoint = task.get("checkpoint") or {}
                contracts = ([(checkpoint["action"], checkpoint.get("args"))] if checkpoint.get("action")
                             else [(c["action"], c.get("args")) for c in task.get("acceptance", [])
                                   if isinstance(c, dict) and c.get("action")])
                if not can_update_plan and (not interaction.get("executing_plan") or not contracts):
                    return None
                for action, arguments in contracts:
                    spec = runtime.tool_registry.specs.get(action)
                    if action not in permitted or (interaction.get("executing_plan") and _strategic_plan_denial(
                            interaction["executing_plan"], action, arguments, spec, runtime._get_workspace_root())):
                        return None
                eligible.append({"id": task["id"], "status": task["status"]})
            if eligible:
                decisions = ([
                    "continue permitted work with explicit task selection",
                    "request clarification or record a genuine blocker",
                    "use existing plan rules to cancel/replace; require new receipts, never rewrite executed criteria",
                ] if can_update_plan else [
                    "use only the approved contract of a currently selected task",
                    "report task selection as unavailable under the current bounds if no task is selected",
                ])
                return {"plan_id": plan.id, "revision": plan.revision, "status": "active",
                        "eligible_tasks": eligible[:12], "eligible_task_count": len(eligible),
                        "plan_state": "Use the current pinned Working Plan state; read its artifact_id if present.",
                        "completion_evidence": "required; prose and suggestions are not evidence",
                        "decisions": decisions}
        except Exception:
            # Unavailable authority or lifecycle state is a handoff, not permission to retry.
            return None
        return None

    def _run_native(self, turn: TurnContext) -> None:
        runtime = self.runtime
        runtime._last_response_is_native.set(True)
        from openkyrozen.agent.conversation import model_response_to_dict
        from openkyrozen.agent.prompts import _refresh_working_plan_context
        from openkyrozen.providers.errors import ProviderError, ProviderErrorKind
        provider = runtime.execution_context.provider or runtime.llm_provider
        capabilities = provider.get_capabilities(runtime.DEEPSEEK_MODEL)
        stream = capabilities.text_streaming or capabilities.streaming_tool_calls
        messages = runtime._build_messages(
            turn.user_input, turn.learned_context, turn.memory_context,
            memory_packet=turn.memory_packet, native_tools=True,
        )
        turn.tool_calls = []
        turn.tool_records = []
        turn.tool_results_text = ""
        try:
            self._archive_replies(messages, [index for index, item in enumerate(messages) if isinstance(item, ToolReply)])
        except Exception:
            turn.protocol_error_message = "Could not retain earlier tool results; no further tools were run."
            return
        successful_operations: set[str] = set()
        operation_scope = str(turn.learning_run.get("run_id"))
        final_text = ""
        response = None
        history_tool_names: set[str] = {item.name for item in messages if isinstance(item, ToolReply)}
        observed_plan_id = None
        previous_reply_indexes: list[int] = []
        runtime.execution_context.native_control_input = turn.user_input
        runtime.execution_context.native_plan_inspected = False
        control_failures = 0
        pending_plan_validation = None
        plan_continuation_used = False
        final_answer_requested = False
        wire_recovery_used = False
        delegation_requested = turn.planning_required and _requests_delegation(turn.user_input)
        scoped_readonly = getattr(runtime.execution_context, "scoped_readonly_assignment", False)
        delegation_attempts = 0
        retry_delegation = False

        max_steps = getattr(runtime.execution_context, "max_steps", None)
        if max_steps is None:
            max_steps = runtime.MAX_STEPS_PER_TURN
        for step in range(max_steps + 1):
            cancelled = getattr(runtime.execution_context, "cancelled", None)
            if cancelled is not None and cancelled.is_set():
                raise ProviderError(ProviderErrorKind.CANCELLED, terminal=True)
            tool_names = self._native_tool_names()
            optional_plan = scoped_readonly and runtime.tasks.working_plan is None
            planning_required = turn.planning_required and bool(tool_names) and not optional_plan
            if turn.interaction_mode == "plan":
                tool_names = {name for name in tool_names
                              if name in _NATIVE_CONTROLS or (
                                  runtime.tool_registry.specs.get(name).capability in {"read", "network"}
                                  and runtime.tool_registry.specs.get(name).side_effects in {"read_only", "read", "none", "remote"}
                                  and runtime.tool_registry.specs.get(name).risk == "normal")}
            if not runtime.execution_context.native_plan_inspected:
                tool_names.discard("propose_plan")
            plan = getattr(runtime.tasks, "working_plan", None) if planning_required else None
            if plan is not None and plan.status == "active":
                observed_plan_id = plan.id
            completed_plan = (observed_plan_id is not None and plan is not None
                              and plan.id == observed_plan_id and plan.status == "completed")
            cancelled_plan = (observed_plan_id is not None and plan is not None
                              and plan.id == observed_plan_id and plan.status == "cancelled")
            needs_plan = planning_required and not cancelled_plan and (plan is None or plan.status != "active")
            native_tool_phase = ("plan_completed" if completed_plan else "plan_required" if needs_plan
                                 else "plan_active" if planning_required and not cancelled_plan else None)
            if (native_tool_phase is None and turn.interaction_mode == "agent"
                    and getattr(getattr(runtime, "tasks", None), "execution_task_id", None)):
                native_tool_phase = "plan_active"
            if optional_plan and "update_plan" in tool_names:
                native_tool_phase = "plan_optional"
            if final_answer_requested:
                tool_names = set()
                native_tool_phase = None
            elif cancelled_plan:
                tool_names = set()
            elif needs_plan:
                completed_tools = {"update_plan", "read_tool_artifact"}
                if completed_plan:
                    if "spawn_agents" in history_tool_names:
                        completed_tools.add("wait_subagents")
                    accepted = (runtime._interaction_controller.state().get("executing_plan")
                                if "propose_plan" in tool_names else None)
                    if accepted and accepted.get("working_plan_id") == plan.id:
                        completed_tools.add("propose_plan")
                    tool_names = runtime._permitted_tool_names() & completed_tools
                elif "update_plan" not in runtime._permitted_tool_names():
                    turn.protocol_error_message = "A Working Plan is required, but planning is not permitted."
                    break
                else:
                    tool_names = runtime._permitted_tool_names() & completed_tools
            elif planning_required:
                tool_names |= runtime._permitted_tool_names() & {"update_plan"}
                if "request_question" in runtime._permitted_tool_names():
                    tool_names.add("request_question")
            delegation_first = (delegation_requested and (delegation_attempts == 0 or retry_delegation)
                                and step < max_steps
                                and native_tool_phase == "plan_active" and "spawn_agents" in tool_names
                                and runtime.tasks.execution_task_id is not None)
            if delegation_first:
                interaction = runtime._interaction_controller.state()
                delegation_first = not any(interaction.get(key) for key in
                                           ("executing_plan", "pending_plan", "pending_question"))
            if delegation_first:
                delegation_attempts += 1
                retry_delegation = False
                tool_names = tool_names & {"spawn_agents", "request_question"}

            previous_replies = [messages[index] for index in previous_reply_indexes]
            _refresh_working_plan_context(runtime, messages, native=True)
            provider = runtime.execution_context.provider or runtime.llm_provider
            protocol_for = getattr(provider, "native_tool_protocol_for", None)
            command_protocol = (native_tool_phase == "plan_completed" and callable(protocol_for)
                                and protocol_for(messages) == "openai-responses")
            emitted_native_content = False
            emitted_native_text = False

            def observe_native_content(event):
                nonlocal emitted_native_content, emitted_native_text
                if isinstance(event, (TextDelta, ToolCallDelta)):
                    emitted_native_content = True
                if isinstance(event, TextDelta) and event.text:
                    emitted_native_text = True

            def request_response():
                return runtime._get_model_response(
                    messages, tool_names=tool_names, history_tool_names=history_tool_names, stream=stream,
                    _native_tool_phase=native_tool_phase,
                    on_event=observe_native_content if stream else None,
                )

            token = runtime._record_foreground_model.set(False)
            failure_state = getattr(runtime, "_turn_failed", None)
            failed_before_request = failure_state.get() if failure_state is not None else False
            try:
                try:
                    try:
                        response = request_response()
                    except ProviderContractError as exc:
                        if (not exc.malformed_tool_json or wire_recovery_used
                                or not tool_names or step >= max_steps
                                or cancelled is not None and cancelled.is_set()):
                            raise
                        wire_recovery_used = True
                        runtime._publish_runtime_event("protocol.recovery", {"reason": "malformed_tool_json"})
                        # The rejected response never entered conversation history or the executor.
                        notice = {"role": "system", "content": (
                            "The previous generation contained malformed tool arguments. No tool from it ran. "
                            "Continue from the last valid result. If a tool is needed, issue a fresh native "
                            "call with a valid JSON object matching its current schema."
                            + (" The progress text has already been shown. Continue without repeating it."
                               if emitted_native_text else "")
                        )}
                        messages.append(notice)
                        runtime.execution_context.native_wire_regeneration = True
                        try:
                            response = request_response()
                        finally:
                            runtime.execution_context.native_wire_regeneration = False
                            messages[:] = [message for message in messages if message is not notice]
                        runtime._turn_failed.set(failed_before_request)
                    except ProviderError as exc:
                        if (exc.kind != ProviderErrorKind.CONTEXT_OVERFLOW or exc.terminal
                                or emitted_native_content
                                or not runtime._recover_context_after_overflow(messages, runtime.DEEPSEEK_MODEL)):
                            raise
                        response = request_response()
                except Exception as exc:
                    if isinstance(exc, ProviderError) and exc.kind == ProviderErrorKind.CANCELLED:
                        raise
                    runtime._turn_failed.set(True)
                    detail = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
                    turn.protocol_error_message = f"Native model request failed: {detail}; no further tools were run."
                    if runtime._conversation_turn_id.get() is not None:
                        runtime._record_conversation_entry("model.failure", {
                            "error_type": type(exc).__name__,
                        }, request_id=runtime._last_model_request_id.get())
                    break
            finally:
                runtime._record_foreground_model.reset(token)
            previous_reply_indexes = [index for index, item in enumerate(messages)
                                      if any(item is reply for reply in previous_replies)]
            turn.turn_prompt_total += runtime._last_prompt_tokens
            turn.turn_completion_total += runtime._last_completion_tokens
            turn.response_text = response.text
            runtime._last_response_is_native.set(True)

            if response.finish_reason == FinishReason.CANCELLED:
                raise ProviderError(ProviderErrorKind.CANCELLED, terminal=True)
            if response.finish_reason in {FinishReason.ERROR, FinishReason.BLOCKED, FinishReason.UNKNOWN}:
                final_text = response.text.strip()
                blocked = response.finish_reason == FinishReason.BLOCKED
                turn.runtime_status = "blocked" if blocked else "incomplete"
                turn.protocol_error_message = ("The provider blocked this response; no further tools were run."
                                               if blocked else
                                               "The provider returned an unsuccessful response; no further tools were run.")
                if not blocked:
                    runtime._turn_failed.set(True)
                break
            if response.tool_calls and response.finish_reason == FinishReason.LENGTH:
                turn.protocol_error_message = "The provider truncated its tool request; no tools from that response were run."
                final_text = response.text.strip()
                break

            unknown_tools = [call.name for call in response.tool_calls if call.name not in tool_names]
            if unknown_tools:
                if final_answer_requested:
                    turn.protocol_error_message = "The provider called a tool during the final-answer request; no tool was run."
                elif completed_plan:
                    turn.protocol_error_message = "The Working Plan is complete. A tool outside the completed plan's available tools was requested and was not run."
                else:
                    turn.protocol_error_message = ("A Working Plan is required before other tools can run."
                                                   if needs_plan else
                                                   "The provider returned an unknown tool call; no tool was run.")
                if runtime._conversation_turn_id.get() is not None:
                    runtime._record_conversation_entry("model.failure", {
                        "error_type": "invalid_native_tool_call",
                    }, request_id=runtime._last_model_request_id.get())
                break
            mixed_plan_batch = (any(call.name == "update_plan" for call in response.tool_calls)
                                and any(call.name != "update_plan" for call in response.tool_calls))
            if mixed_plan_batch and any(call.name in _NATIVE_CONTROLS for call in response.tool_calls):
                turn.protocol_error_message = "Run Working Plan edits in a separate response before using their task selection."
                break
            if any(call.name in _NATIVE_CONTROLS for call in response.tool_calls) and len(response.tool_calls) != 1:
                turn.protocol_error_message = "Request an interactive control alone in its own response; no tools were run."
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
                if delegation_first and response.finish_reason == FinishReason.FINAL:
                    messages.extend((response if response.continuation else {
                        "role": "assistant", "content": response.text,
                    }, {"role": "system", "content": (
                        "Continue with the permitted workspace tools. Inspect the scope if needed, "
                        "and consider the user's delegation request before completing the work."
                    )}))
                    continue
                final_text = response.text.strip()
                if turn.interaction_mode == "plan":
                    final_text = ""
                    control_failures += 1
                    if control_failures < 3 and step < max_steps:
                        messages.extend((response, {"role": "user", "content": (
                            "Use read-only inspection first, then propose_plan for the structured proposal; "
                            "use request_question if clarification is needed. Do not execute proposed changes."
                        )}))
                        continue
                    turn.protocol_error_message = "A valid Strategic Plan proposal was not prepared; no proposed changes were executed."
                    break
                if (response.finish_reason == FinishReason.FINAL and not final_text.startswith("[LLM Error]")
                        and not plan_continuation_used and not pending_plan_validation and step < max_steps):
                    followup_tool_groups = []
                    if native_tool_phase == "plan_completed" and step + 2 < max_steps:
                        answer = final_text.casefold().replace("’", "'").replace("`", "")
                        opening = " ".join(answer.split("\n\n", 1)[0].split())
                        refusals = ("i can't complete", "i cannot complete", "i'm unable to complete")
                        first_person = opening.startswith(refusals) or (
                            opening.startswith("i ") and any(f"but {phrase}" in opening for phrase in refusals))
                        lists_current_tools = (
                            ("only update_plan" in opening
                             or ("only read_tool_artifact" in opening and "update_plan" in opening))
                            and ("exposed" in opening or "available" in opening)
                        )
                        if first_person and lists_current_tools:
                            if any(phrase in answer for phrase in ("no write tool", "no edit tool")):
                                followup_tool_groups.append(_FOLLOWUP_WRITE_TOOLS)
                            if any(phrase in answer for phrase in (
                                    "no shell", "no command runner", "no command-execution tool")):
                                followup_tool_groups.append({"run_cmd"})
                    state = self._working_plan_continuation(
                        turn, needs_plan=needs_plan and not completed_plan, followup_tool_groups=followup_tool_groups)
                    if state is not None:
                        plan_continuation_used = True
                        if runtime._conversation_turn_id.get() is not None:
                            runtime._record_conversation_entry("plan.continuation", {
                                "plan_id": state["plan_id"], "revision": state["revision"],
                                "reason": {"required": "plan_required", "completed": "followup_tool_gate"}.get(
                                    state["status"], "required_work_remains"),
                            }, request_id=runtime._last_model_request_id.get())
                        messages.extend((response if response.continuation else {
                            "role": "assistant", "content": response.text,
                        }, {"role": "user", "content": json.dumps({
                            "working_plan_continuation": state}, ensure_ascii=False, sort_keys=True)}))
                        continue
                if needs_plan and not completed_plan:
                    final_text = ""
                    turn.protocol_error_message = "A Working Plan was not established; no further tools were run."
                    break
                if (completed_plan and not final_text and response.finish_reason == FinishReason.FINAL
                        and not final_answer_requested and step < max_steps):
                    final_answer_requested = True
                    if response.continuation:
                        messages.append(response)
                    messages.append({"role": "user", "content": (
                        "The Working Plan is complete. Use the existing results to answer the original request "
                        "in its requested format. Return only the final answer without calling tools."
                    )})
                    continue
                if (not final_text or response.finish_reason == FinishReason.LENGTH) and step < 2 and not turn.planning_required:
                    messages.extend((response if response.continuation else {
                        "role": "assistant", "content": response.text,
                    }, {"role": "user", "content": (
                        "Your previous response was empty. Continue with a structured tool call if work remains, "
                        "or provide a concise final answer."
                    )}))
                    continue
                if not final_text or response.finish_reason == FinishReason.LENGTH:
                    turn.protocol_error_message = ("The provider response reached its length limit; the answer is incomplete."
                                                   if response.finish_reason == FinishReason.LENGTH else
                                                   "The provider returned no answer after bounded continuation.")
                    break
                if not final_text.startswith("[LLM Error]"):
                    break
                turn.protocol_error_message = final_text
                break
            messages.append(response)
            current_reply_indexes: list[int] = []
            history_tool_names.update(call.name for call in response.tool_calls)
            if mixed_plan_batch:
                from openkyrozen.agent.conversation import tool_result_to_dict
                from openkyrozen.tools.models import tool_failure
                pending_plan_validation = "Run Working Plan edits in a separate response before using their task selection."
                output = tool_result_to_dict(tool_failure("invalid_arguments", pending_plan_validation, retriable=True))
                # Reject the whole batch before task selection or execution receipts can bind stale state.
                for call in response.tool_calls:
                    messages.append(ToolReply(call.id, call.name, output, True))
                    current_reply_indexes.append(len(messages) - 1)
                    if runtime._conversation_turn_id.get() is not None:
                        runtime._record_conversation_entry("tool.result", {
                            "call_id": call.id, "name": call.name, "arguments": call.arguments,
                            "output": output, "is_error": True,
                        }, call_id=call.id, request_id=runtime._last_model_request_id.get())
                try:
                    self._archive_replies(messages, previous_reply_indexes, archive_small=False)
                except Exception:
                    turn.protocol_error_message = "Could not retain earlier tool results; no further tools were run."
                    break
                previous_reply_indexes = current_reply_indexes
                continue
            pending_plan_validation = None
            selected_task_id = getattr(runtime.tasks, "execution_task_id", None)
            calls = [{"action": call.name, "args": call.arguments, "call_id": call.id,
                      "call_id_source": "native",
                      "task_id": (selected_task_id or getattr(getattr(runtime, "current_session", None),
                                                              "_delegation_task_id", None))
                      if call.name not in _NATIVE_CONTROLS | {"update_plan"} else None,
                      "task_plan_id": getattr(getattr(runtime.tasks, "working_plan", None), "id", None)
                      if selected_task_id and call.name not in _NATIVE_CONTROLS | {"update_plan"} else None}
                     for call in response.tool_calls]
            turn.tool_calls.extend(calls)
            if (native_tool_phase in {"plan_active", "plan_completed"}
                    and len(calls) == 1 and calls[0]["action"] == "update_plan"):
                calls = [{**calls[0], "native_plan_start": plan}]

            def record_native_call(call, receipt):
                record = runtime._record_turn_receipt(receipt, allow_legacy_task_matching=False)
                turn.tool_records.append(record)
                prompt_result = runtime._tool_result_for_prompt(receipt)
                runtime._render_tool_result(receipt.action, prompt_result)
                from openkyrozen.tools.models import ToolResult
                from openkyrozen.agent.conversation import tool_result_to_dict
                outcome = receipt.tool_result or ToolResult(data=receipt.result_data, text=receipt.result)
                reply_data = tool_result_to_dict(outcome)
                if (receipt.action == "wait_subagents" and receipt.success
                        and getattr(receipt, "authorized", False) and getattr(receipt, "tool_source", None) == "builtin"):
                    data = reply_data["data"]
                    for run in data.get("agents", ()) if isinstance(data, dict) else ():
                        if (run.get("status") == "succeeded" and isinstance(run.get("run_id"), str)
                                and type(run.get("version")) is int and run.get("report")):
                            turn.observed_delegation_versions[run["run_id"]] = run["version"]
                if getattr(runtime.execution_context, "child_run_id", None) and receipt.success:
                    reply_data["metadata"]["receipt_id"] = receipt.receipt_id
                if selected_task_id and (record.get("success") or record.get("expected_exit_verified")):
                    current_plan = runtime.tasks.working_plan
                    if (current_plan and current_plan.id == record.get("plan_id")
                            and selected_task_id in current_plan.task_ids):
                        selected = next((task for task in runtime.tasks.tasks if task["id"] == selected_task_id), None)
                        progress = {
                            "plan_id": current_plan.id, "revision": current_plan.revision,
                            "task_id": selected_task_id,
                        }
                        if selected and selected["status"] == "succeeded" and record.get("task_id") == selected_task_id:
                            reply_data["working_plan_progress"] = {**progress, "task_status": "succeeded"}
                            if record.get("expected_exit_verified"):
                                reply_data["next_request"] = (
                                    "The command produced the expected nonzero exit status. The task is verified. "
                                    "Use the pinned Working Plan to start the next pending task if work remains."
                                )
                                command_args = {criterion["args"] for criterion in selected["acceptance"]
                                                if isinstance(criterion, dict) and criterion.get("action") == "run_cmd"
                                                and (criterion.get("expect") or {}).get("exit_code")}
                                checks = [task for task in runtime.tasks.tasks
                                          if task["id"] in current_plan.task_ids and task["status"] == "pending"
                                          and any(isinstance(criterion, dict) and criterion.get("action") == "run_cmd"
                                                  and criterion.get("args") in command_args
                                                  and (criterion.get("expect") or {}).get("exit_code", 0) == 0
                                                  for criterion in task["acceptance"])]
                                if len(checks) == 1 and runtime.tasks.active_working_task(checks[0]["id"]):
                                    reply_data["next_working_plan_task"] = {
                                        "plan_id": current_plan.id, "expected_revision": current_plan.revision,
                                        "operations": [{"op": "start", "task": checks[0]["id"]}],
                                    }
                                    reply_data["next_request"] = (
                                        "The reproduction is verified. Use next_working_plan_task to select the existing "
                                        "post-change command check before editing. A read-only check cannot authorize an edit."
                                    )
                        elif (call["action"] == "run_cmd" and (record.get("verification") or {}).get("status") == "verified"
                              and selected and selected["status"] == "running"
                              and not (selected.get("checkpoint") or {}).get("strategic_plan")
                              and record.get("task_id") is None and record.get("unmatched_reason")
                              and record.get("requested_task_id") == selected_task_id
                              and any(isinstance(criterion, dict) and criterion.get("action") == "run_cmd"
                                      for criterion in selected.get("acceptance", ()))):
                            reply_data["working_plan_progress"] = {
                                **progress, "task_status": "running", "verification": "unmatched",
                            }
                            reply_data["next_request"] = (
                                "The tool succeeded but did not verify the selected task. Compare the exact "
                                "arguments with the structured acceptance in the pinned Working Plan before "
                                "taking further action. If the criterion is wrong, cancel and replace the task."
                            )
                if outcome.success and call["action"] == "update_plan":
                    active_task_id = runtime.tasks.execution_task_id
                    if active_task_id and active_task_id != selected_task_id:
                        reply_data["next_request"] = (
                            "The selected Working Plan task has started. Use current_call_tools from the new request "
                            "for the next permitted action. Recheck tool availability after the plan update."
                        )
                messages.append(ToolReply(call["call_id"], call["action"], reply_data, not outcome.success))
                if record.get("success"):
                    if call["action"] in _NATIVE_CONTROLS:
                        if not isinstance(outcome.data, dict) or outcome.data.get("requires_approval", True):
                            turn.pending_interaction_reply = outcome.text
                    else:
                        spec = runtime.tool_registry.specs.get(call["action"])
                        if (spec and spec.capability in {"read", "network"}
                                and spec.side_effects in {"read_only", "read", "none", "remote"}
                                and call["action"] != "discover_tools"):
                            runtime.execution_context.native_plan_inspected = True
                current_reply_indexes.append(len(messages) - 1)
                turn.tool_results_text += f"- `{call['action']}` returned:\n{runtime._safe_fstring(prompt_result)}\n"
                runtime._update_tasks_panel()

            context = runtime.execution_context
            context.native_plan_active = native_tool_phase == "plan_active"
            context.native_plan_completed = completed_plan
            if (command_protocol and len(calls) == 1 and calls[0]["action"] == "update_plan"
                    and set(calls[0]["args"]) == {"description", "command"}):
                manager = runtime.tasks
                event_id = manager.store.current_interaction_event_id(
                    user_id=manager.user_id, workspace_id=manager.workspace_id, session_id=manager.session_id)
                context.native_completed_predecessor = (plan.id, plan.revision, event_id)
            try:
                runtime._execute_tool_batch(
                    calls, operation_scope=operation_scope, successful_operations=successful_operations,
                    call_id_source="native", on_receipt=record_native_call,
                )
            finally:
                context.native_plan_active = False
                context.native_plan_completed = False
                context.native_completed_predecessor = None
            if (delegation_first and delegation_attempts == 1 and len(calls) == 1
                    and calls[0]["action"] == "spawn_agents"):
                record = turn.tool_records[-1]
                error = record.get("tool_error") or {}
                retry_delegation = (record.get("failure") == "invalid_arguments" and
                    (record.get("authorized") is False
                     or (error.get("metadata") or {}).get("delegation_validation_no_effect") is True))
            if turn.pending_interaction_reply:
                final_text = turn.pending_interaction_reply
                break
            if (any(call["action"] in _NATIVE_CONTROLS for call in calls)
                    and not turn.tool_records[-1].get("success")):
                control_failures += 1
                if control_failures >= 3:
                    turn.protocol_error_message = "Interactive control validation failed after bounded retries; no proposed changes were executed."
                    break
            try:
                self._archive_replies(messages, previous_reply_indexes, archive_small=False)
            except Exception:
                turn.protocol_error_message = "Could not retain earlier tool results; no further tools were run."
                break
            previous_reply_indexes = current_reply_indexes

        if pending_plan_validation and not turn.protocol_error_message:
            turn.protocol_error_message = pending_plan_validation
        runtime._last_response_is_native.set(True)
        turn.final_answer = final_text or runtime._deterministic_tool_summary(turn.tool_records)
