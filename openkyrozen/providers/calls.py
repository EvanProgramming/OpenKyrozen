from __future__ import annotations

import os
import math
import sys
import uuid
import hashlib
import json
from typing import Any
from openkyrozen.providers.usage import usage_scope
from openkyrozen.providers.base import get_model_response, get_model_events
from openkyrozen.providers.models import (
    ModelResponse, ProviderContractError, ToolReply, ResponseComplete, ResponseError, TextDelta, ToolCallDelta, UsageUpdate,
)
from openkyrozen.providers.context import context_accounting_messages
from openkyrozen.providers.retry import provider_request_scope, bounded_call, bounded_stream
from openkyrozen.agent.types import ContextOverflowError, ProviderUnavailableError


def _delegation_budget(self):
    budget = self.execution_context.delegation_budget
    run_id = self.execution_context.delegation_budget_run_id
    if budget is not None and run_id and not budget.can_continue(run_id, cost_picos=0):
        raise RuntimeError("Delegated model budget is exhausted or usage is unknown")
    return budget, run_id


def _provider_timeout_seconds(self) -> float:
    default = 180.0 if self.execution_context.child_run_id else 90.0
    try:
        value = float(os.environ.get("KYROZEN_PROVIDER_TIMEOUT_SECONDS", str(default)))
        return max(1.0, min(value, 600.0)) if math.isfinite(value) else default
    except ValueError:
        return default


def _child_cancellation(self):
    context = self.execution_context
    cancelled = getattr(context, "cancelled", None)
    child = None
    if context.child_run_id and context.coordinator:
        child = context.coordinator.cancelled.get(context.child_run_id)
    if cancelled is not None and child is not None:
        from openkyrozen.agent.turn import CancellationEvents
        return CancellationEvents(cancelled, child)
    if child is not None:
        return child
    return cancelled


def _bounded_provider_call(self, callback: Any) -> Any:
    with provider_request_scope(self._provider_timeout_seconds(), _child_cancellation(self)) as request:
        return bounded_call(callback, request)


def _bounded_provider_events(self, provider, messages, model, tools, on_event=None):
    from openkyrozen.providers.retry import bounded_stream
    with provider_request_scope(self._provider_timeout_seconds(), _child_cancellation(self)) as request:
        response = None
        events = bounded_stream(lambda: get_model_events(provider, messages, model, tools=tools), request)
        try:
            for event in events:
                if on_event is not None:
                    on_event(event)
                if isinstance(event, ResponseError):
                    raise event.error
                if isinstance(event, ResponseComplete):
                    response = event.response
            if response is None:
                raise RuntimeError("Provider stream ended without a complete response")
            return response
        except Exception as exc:
            from openkyrozen.providers.errors import normalize_provider_error
            if isinstance(exc, (ProviderUnavailableError, ContextOverflowError)):
                raise
            raise normalize_provider_error(exc, getattr(provider, "name", None), model)


def _bounded_provider_stream(self, provider: Any, messages: list[dict[str, str]], model: str,
                             on_chunk: Any = None) -> str:
    with provider_request_scope(self._provider_timeout_seconds(), _child_cancellation(self)) as request:
        chunks = []
        try:
            for chunk in bounded_stream(lambda: provider.chat_stream(messages, model), request):
                chunks.append(str(chunk))
                if on_chunk:
                    on_chunk(chunk)
                else:
                    sys.stdout.write(str(chunk))
                    sys.stdout.flush()
        except Exception as exc:
            from openkyrozen.providers.errors import normalize_provider_error
            raise normalize_provider_error(exc, getattr(provider, "name", None), model)
        return "".join(chunks).strip()


def _restrict_plan_verification(schema, available):
    """Keep typed criterion alternatives whose evidence tool can run."""
    properties = schema.get("properties", {})
    if "verification" in properties:
        action = properties["verification"]["properties"]["action"]
        action["enum"] = [name for name in action["enum"] if name in available]
        return bool(action["enum"])
    if "anyOf" in schema:
        schema["anyOf"] = [option for option in schema["anyOf"]
                           if _restrict_plan_verification(option, available)]
        if not schema["anyOf"]:
            return False
    if "items" in schema and not _restrict_plan_verification(schema["items"], available):
        return False
    return all(_restrict_plan_verification(value, available) for value in properties.values())


def _get_model_response(self, messages: list[dict], model: str | None = None, *, tool_names=None,
                        history_tool_names=(), stream: bool = False, on_event=None,
                        _native_tool_phase: str | None = None) -> ModelResponse:
    """Receive the full response under existing deadline, usage and context scopes."""
    provider = self.execution_context.provider or self.llm_provider
    tools = None
    if tool_names is not None:
        if isinstance(tool_names, str) or not isinstance(tool_names, (list, tuple, set, frozenset)):
            raise TypeError("tool_names must be a collection of canonical tool names")
        registry = self.tool_registry
        allowed = self._effective_execution_capabilities()
        if isinstance(history_tool_names, str) or not isinstance(history_tool_names, (list, tuple, set, frozenset)):
            raise TypeError("history_tool_names must be a collection of current-turn tool names")
        history_names = set(history_tool_names)
        if any(not isinstance(name, str) or not name for name in history_names):
            raise TypeError("history_tool_names must contain nonempty tool names")
        names = set(history_names)
        requested_names = set()
        for requested in tool_names:
            canonical = registry.resolve_legacy_name(requested)
            if canonical in registry.specs:
                names.add(canonical)
                requested_names.add(canonical)
        tools = [item for item in registry.provider_schemas(names=names)
                 if registry.get_spec(item["name"]).capability in allowed or item["name"] in history_names]
        current_names = {item["name"] for item in tools if item["name"] in requested_names
                         and registry.get_spec(item["name"]).capability in allowed}
        from openkyrozen.agent.engine import _requests_one_delegation, _requests_one_test_review
        single_assignment = _requests_one_delegation(self.execution_context.native_control_input)
        test_review = single_assignment and _requests_one_test_review(self.execution_context.native_control_input)
        if "spawn_agents" in current_names and registry.get_spec("spawn_agents").source == "builtin":
            declaration = next(item for item in tools if item["name"] == "spawn_agents")
            schema = declaration["parameters"]
            custom_brief = schema["anyOf"][0]
            assignments = custom_brief["properties"]["assignments"]
            if test_review:
                schema["anyOf"].reverse()
                schema["properties"]["assignments"] = assignments
                declaration["description"] = (
                    'For the requested independent test review, use test_review with scope and focus. '
                    'Use ["."] if the tests are not yet located. OpenKyrozen prepares the read-only brief. '
                    'The main turn runs tests and records the result. '
                    'Use assignments only when a custom specialist brief is necessary.'
                )
            else:
                declaration["parameters"] = custom_brief
            if single_assignment:
                assignments["maxItems"] = 1
                declaration["description"] += " Submit one focused assignment. Omit task_id for independent review findings."
                task_ids = [task["id"] for task in self.tasks.tasks
                            if self._valid_receipt_task_binding(self.current_session, task["id"])]
                fields = assignments["items"]["properties"]
                if task_ids:
                    fields["task_id"]["enum"] = task_ids
                else:
                    fields.pop("task_id", None)
        ordinary_plan_edit = False
        ordinary_followup = False
        command_followup = False
        if "update_plan" in current_names and registry.get_spec("update_plan").source == "builtin":
            plan_tool = next(item for item in tools if item["name"] == "update_plan")
            schema = plan_tool["parameters"]
            plan = getattr(getattr(self, "tasks", None), "working_plan", None)
            interaction = self._interaction_controller.state()
            accepted = interaction.get("executing_plan")
            ordinary_plan_edit = not accepted or accepted.get("working_plan_id") != getattr(plan, "id", None)
            permitted = self._permitted_tool_names()
            verification_tools = {name for name in {"read_file", "run_cmd", "git_show"} & permitted
                                  if registry.specs[name].capability in allowed}
            # Shell execution cannot be confined to a delegated file assignment.
            if self.execution_context.child_run_id:
                verification_tools.discard("run_cmd")
            _restrict_plan_verification(schema, verification_tools)
            ordinary_work = not accepted and not interaction.get("pending_plan")
            can_add_command = ordinary_work and "run_cmd" in verification_tools
            can_add_read = ordinary_plan_edit and "read_file" in verification_tools
            ordinary_followup = _native_tool_phase == "plan_completed" and ordinary_work
            from openkyrozen.tasks.engine import (
                can_bind_command_check, can_replace_complete_read_check, pending_reproduction_task,
            )
            command_tasks = [task for task in self.tasks.tasks if task["id"] in plan.task_ids] if (
                _native_tool_phase == "plan_active" and plan is not None
                and can_add_command and "run_cmd" in current_names) else ()
            can_replace_command = any(task["status"] == "running"
                and (task.get("evidence") or [{}])[-1].get("failed_command_exit_code") is not None
                for task in command_tasks)
            can_replace_read = any(can_replace_complete_read_check(task) for task in command_tasks)
            can_bind_command = any(can_bind_command_check(task) for task in command_tasks)
            unavailable = set()
            if not (can_replace_command or can_replace_read):
                unavailable.add("replace_command_check")
            if not can_bind_command:
                unavailable.add("bind_command_check")
            add_command_guidance = (
                "Use add_command_check with ref, description and command for each new command-based check, "
                "including newly discovered verification. "
                "Run a failing reproduction command by itself first and keep its own exit status. "
                "Set expected_exit_code to the expected nonzero status (for example 1). A match verifies the task. "
                "Use a separate zero-exit check for the post-fix test. "
                if can_add_command else ""
            )
            if not can_add_command:
                unavailable.add("add_command_check")
            if not can_add_read:
                unavailable.add("add_read_check")
            for shape in (schema, *schema.get("anyOf", ())):
                operations = shape.get("properties", {}).get("operations")
                if operations is None:
                    continue
                variants = [item for item in operations["items"]["anyOf"]
                            if item["properties"]["op"]["enum"][0] not in unavailable]
                if ordinary_plan_edit:
                    for operation in variants:
                        operation["properties"].pop("anchor", None)
                operations["items"]["anyOf"] = variants
            if _native_tool_phase in {"plan_required", "plan_optional"}:
                operations = schema["properties"]["operations"]
                creation_ops = [item for item in operations["items"]["anyOf"]
                                if item["properties"]["op"]["enum"][0] in {
                                    "add", "add_read_check", "add_command_check", "start",
                                }]
                plan_tool["parameters"] = {
                    "type": "object", "additionalProperties": False,
                    "required": ["expected_revision", "operations"],
                    "properties": {
                        "expected_revision": {"type": "integer", "minimum": 0, "maximum": 0},
                        "operations": {**operations, "minItems": 1, "items": {"anyOf": creation_ops}},
                    },
                }
                plan_tool["description"] = (
                    ("For multiple dependent inspection steps, optionally create a Working Plan at revision zero. "
                     if _native_tool_phase == "plan_optional" else "Create the required Working Plan at revision zero. ")
                    + ("For inspection of unknown file contents, use add_read_check with ref and path. "
                       if can_add_read else "")
                    + f"{add_command_guidance}Use add for other work. "
                    "In the same call, start the first task by its ref."
                )
                if test_review and can_add_read:
                    plan_tool["description"] += (
                        " For an independent test review, start with one complete read of a relevant file. "
                        "Use native source inspection to locate tests and bind the actual verification command later."
                    )
            elif _native_tool_phase in {"plan_active", "plan_completed"}:
                alternatives = None
                required = ["plan_id", "expected_revision", "operations"]
                properties = {name: schema["properties"][name] for name in required}
                properties["plan_id"] = {
                    **properties["plan_id"], "description": "ID from the current pinned Working Plan.",
                }
                properties["operations"] = {**properties["operations"], "minItems": 1}
                if _native_tool_phase == "plan_active":
                    properties["reason"] = {"type": "string", "minLength": 1,
                                            "description": "Optional rationale for the plan edit."}
                    criterion_guidance = (
                        f"{add_command_guidance}Use update to set other typed criteria before start in the same call. "
                        if ordinary_plan_edit else "Preserve approved criteria and request renewed approval for material changes. "
                    )
                    if can_bind_command:
                        criterion_guidance += ("Use bind_command_check with the listed task ID and exact command for a "
                                               "pending or just-selected prose-only task. For a planned failure, include its "
                                               "expected_exit_code when binding. Start the task in the same call if pending. ")
                    if can_replace_read:
                        criterion_guidance += (
                            "For a source edit after selecting a complete-read check, use one replace_command_check "
                            "with task, ref and the exact test command. It starts the new check and preserves dependencies. "
                        )
                    if can_replace_command:
                        criterion_guidance += (
                            "If the selected verification command needs correction, use replace_command_check "
                            "with task, ref and the corrected command. It starts a new task and preserves the "
                            "failed receipt. Update is for unstarted work. "
                        )
                    plan_tool["description"] = (
                        "Revise the current Working Plan using its pinned ID and revision. "
                        f"{criterion_guidance}Use exact tool arguments for typed verification. "
                        "Combine compatible operations in execution order."
                    )
                    protocol_for = getattr(provider, "native_tool_protocol_for", None)
                    reproduction = (pending_reproduction_task(self.tasks) if command_tasks
                                    and callable(protocol_for) and protocol_for(messages) == "openai-responses"
                                    else None)
                    if reproduction:
                        bind = next(item for item in properties["operations"]["items"]["anyOf"]
                                    if item["properties"]["op"]["enum"] == ["bind_command_check"])
                        fields = bind["properties"]
                        alternatives = [{
                            "type": "object", "additionalProperties": False,
                            "required": ["command", "expected_exit_code"],
                            "properties": {
                                "command": {**fields["command"], "description": "Run the reproducer alone, without a shell wrapper."},
                                "expected_exit_code": fields["expected_exit_code"],
                            },
                        }]
                        plan_tool["description"] = (
                            "The first runnable task is the reproduction. Use only command and expected_exit_code "
                            "to bind and start its exact failing command in one call. "
                            "The runtime binds the current task and revision. Run that exact command in the next call. "
                            + plan_tool["description"]
                        )
                else:
                    operations = properties["operations"]
                    variants = operations["items"]["anyOf"]
                    confirm = next(item for item in variants
                                   if item["properties"]["op"]["enum"] == ["suggest_complete"])
                    properties["operations"] = {**operations, "items": confirm}
                    plan_tool["description"] = (
                        "Confirm previously verified tasks in the completed Working Plan. "
                        "Use the current pinned plan ID and revision and suggest_complete with the verified task IDs."
                    )
                    if ordinary_followup:
                        creation_ops = [
                            item for item in variants
                            if item["properties"]["op"]["enum"][0] in {
                                "add", "add_read_check", "add_command_check", "start",
                            }
                        ]
                        from openkyrozen.agent.engine import _can_start_command_followup

                        protocol_for = getattr(provider, "native_tool_protocol_for", None)
                        command_followup = (callable(protocol_for) and protocol_for(messages) == "openai-responses"
                                            and _can_start_command_followup(self, plan))
                        if command_followup:
                            alternatives = [
                                {"type": "object", "additionalProperties": False,
                                 "required": ["description", "command"], "properties": {
                                     "description": {"type": "string", "minLength": 1},
                                     "command": {"type": "string", "minLength": 1},
                                 }},
                                {"type": "object", "additionalProperties": False,
                                 "required": ["expected_revision", "operations"], "properties": {
                                     "expected_revision": {"type": "integer", "enum": [0]},
                                     "operations": {**operations, "items": {"anyOf": creation_ops}},
                                 }},
                            ]
                            plan_tool["description"] = (
                                "For remaining work with a known test command, use only description and the exact "
                                "foreground command to create and start one task. Do the file edit in the next "
                                "permitted tool call, then run this command for verification. Keep file contents "
                                "and edit arguments in those later tool calls. "
                                + plan_tool["description"]
                            )
                        else:
                            required.remove("plan_id")
                            properties["operations"]["items"] = {"anyOf": [*creation_ops, confirm]}
                        plan_tool["description"] += (
                            " For a follow-up without the two-field command form, omit plan_id, set expected_revision "
                            "to 0, add tasks for the remaining work, and start the first task by its ref."
                            if command_followup else
                            " If work remains, omit plan_id, set expected_revision to 0, add tasks, and start the "
                            "first task by its ref."
                        )
                        if can_add_command and not command_followup:
                            plan_tool["description"] += " Use add_command_check for command verification."
                projected = {"type": "object", "additionalProperties": False,
                             "required": required, "properties": properties}
                plan_tool["parameters"] = ({"type": "object", "anyOf": [*alternatives, projected]}
                                           if alternatives else projected)
        history_only = {item["name"] for item in tools} - current_names
        marker = "## Current native tool availability\n"
        previous = list(messages)
        messages[:] = [item for item in messages if not (
            isinstance(item, dict) and item.get("role") == "system"
            and str(item.get("content", "")).startswith(marker)
        )]
        phase_guidance = {
            "plan_required": "Planning-required exposure is temporary. Call update_plan to create and start a task "
                             "before using workspace tools such as read_file or Git history. Check current_call_tools "
                             "again once active. Use read_tool_artifact only with a "
                             "runtime-provided artifact_id. It does not open workspace files. ",
            "plan_optional": "Your read-only assignment already defines the objective and acceptance. "
                             "Use the available scoped inspection tools now and cite their receipt IDs in your report. "
                             "Use update_plan only if separate dependent checks need tracking. ",
            "plan_active": "The Working Plan is active now. Use current_call_tools in this request "
                           "for the selected task's next action. The plan becomes read-only when the last task finishes. ",
            "plan_completed": "The final tool reply may already have verified the last task. "
                              "If the original request is fulfilled, answer using the verified results. "
                              "If explicitly asked to confirm the plan, use update_plan with the current IDs and revision. ",
        }.get(_native_tool_phase, "")
        if ordinary_followup:
            if command_followup:
                phase_guidance += ("For remaining work with a known test command, use update_plan with only "
                                   "description and command. Make edits in later tool calls. ")
            else:
                phase_guidance += ("If the completed tasks covered only part of the original request, use update_plan "
                                   "at revision 0 with no plan_id to create and start a fresh plan for the remaining work. ")
            from openkyrozen.agent.engine import _followup_workspace_tools
            followup_tools = _followup_workspace_tools(self)
            if followup_tools:
                phase_guidance += ("These tools can become available after starting a fresh plan: "
                                   + ", ".join(followup_tools) + ". Existing permission and approval checks apply. ")
        if _native_tool_phase == "plan_active" and ordinary_plan_edit:
            phase_guidance += "Add discovered work within scope before the final verification. "
        if _native_tool_phase == "plan_active" and "discover_tools" in current_names:
            phase_guidance += "Use discover_tools if a permitted tool is missing from the compact list. "
        can_propose_revision = _native_tool_phase == "plan_completed" and "propose_plan" in current_names
        if can_propose_revision:
            phase_guidance = ("The Working Plan is complete. Use update_plan only to confirm completed work, "
                              "or propose_plan to stage a material revision for renewed approval. "
                              "Otherwise, answer the original request using the verified results. ")
        if "spawn_agents" not in history_names:
            if "spawn_agents" in current_names:
                phase_guidance += (
                    "Call spawn_agents for user-requested independent work before doing it yourself. "
                    "Use the Working Plan for your own tasks and collect the delegated results before answering. "
                )
                if current_names <= {"spawn_agents", "request_question"}:
                    phase_guidance += (
                        "This request is for the independent assignments. If you need workspace inspection "
                        "to determine their scopes, state that briefly and continue on the next request. "
                    )
                    if single_assignment:
                        phase_guidance += ("Delegate the one requested review. Keep its scope to the evidence needed "
                                           "for that review and handle other inspection in the main turn. ")
            elif ((_native_tool_phase == "plan_required" or ordinary_followup)
                    and "spawn_agents" in self._permitted_tool_names()
                    and registry.get_spec("spawn_agents").capability in allowed):
                if _native_tool_phase == "plan_required":
                    phase_guidance += (
                        "Delegation becomes available after you start a Working Plan task. "
                        "If the user asks for delegation, keep the direct plan short and use spawn_agents "
                        "for the independent assignments. Use scoped reading tools for discovery during work. "
                    )
                    if single_assignment:
                        phase_guidance += ("Keep the first plan to a single direct task. Add the final test check "
                                           "after identifying the test command. Delegate the requested review only. ")
                else:
                    phase_guidance += (
                        "If user-requested delegation remains, start a fresh Working Plan task "
                        "to make spawn_agents available again. "
                    )
        after_replies = (
            ((_native_tool_phase == "plan_active" and self.tasks.execution_task_id is not None)
             or _native_tool_phase == "plan_completed")
            and messages
            and isinstance(messages[-1], ToolReply)
        )
        position = len(messages) if after_replies else min(1, len(messages))
        messages.insert(position, {"role": "system", "content": marker + json.dumps({
            "current_call_tools": sorted(current_names), "history_only_declarations": sorted(history_only),
        }, sort_keys=True) + "\nThis current list supersedes earlier request lists. " + phase_guidance
            + "Current schemas define argument contracts. History-only declarations do not grant execution permission; "
              "they retain earlier tool-call correlation. Runtime workspace, capability, approval and plan checks still apply."})
        from openkyrozen.agent.context import _update_context_history_projection
        _update_context_history_projection(self, messages, previous)
    self._last_prompt_tokens = self._last_completion_tokens = 0
    self._last_response_is_native.set(False)
    self._last_model_response.set(None)
    self._turn_failed.set(False)
    selected_model = model or self.DEEPSEEK_MODEL
    state = self._active_context_state.get()
    if state is not None and not self._in_context_compaction.get():
        from openkyrozen.providers.models import provider_identity
        state.request_provider_id = provider_identity(provider)
        state.request_model = selected_model
        if tool_names is None:
            state.native_input_overhead_tokens = 0
            state.native_schema_fingerprint = ""
    if tool_names is not None and not self._in_context_compaction.get():
        if state is not None:
            from openkyrozen.agent.compaction import _text_tokens
            project = getattr(provider, "_tool_wire_projector", None)
            wire_tools = project(tools) if project is not None else tools
            schema_json = json.dumps(wire_tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            state.native_input_overhead_tokens = _text_tokens(schema_json) + 32 * len(tools or ()) + 64
            state.native_schema_fingerprint = hashlib.sha256(schema_json.encode("utf-8")).hexdigest()
        self._prepare_context_for_call(messages, selected_model)
    if self._active_context_state.get() is not None and not self._in_context_compaction.get():
        self._record_context_history_metrics(messages)
    request_id = f"model_{uuid.uuid4().hex}"
    request_token = self._active_model_request_id.set(request_id)
    self._last_model_request_id.set(request_id)
    self._publish_runtime_event("model.requested", {"model": selected_model}, model_request_id=request_id)

    def usage_snapshot(fallback=None):
        try:
            totals = self.memory_bank.store.usage_totals(
                user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
                session_id=self.memory_bank.session_id, run_id=self._active_usage_run_id.get(),
            )
            return {key: int(totals.get(key, 0) or 0) for key in (
                "attempts", "authoritative_attempts", "estimated_attempts", "prompt_tokens",
                "cache_hit_tokens", "cache_miss_tokens", "completion_tokens", "reasoning_tokens", "cost_picos",
            )}
        except Exception:
            usage = fallback if isinstance(fallback, dict) else {}
            return {key: int(usage.get(key, 0) or 0) for key in ("prompt_tokens", "completion_tokens")}

    def observe(event):
        if isinstance(event, TextDelta):
            self._publish_runtime_event("model.text_delta", {"text": event.text}, model_request_id=request_id)
        elif isinstance(event, ToolCallDelta):
            self._publish_runtime_event("model.tool_call_delta", {
                "index": event.index, "id_delta": event.id_delta,
                "name_delta": event.name_delta,
                "arguments_delta": "<redacted>" if event.arguments_delta else "",
                "arguments_length": len(event.arguments_delta),
            }, model_request_id=request_id)
        elif isinstance(event, UsageUpdate):
            self._publish_runtime_event("usage.updated", usage_snapshot(event.usage), model_request_id=request_id)
        elif isinstance(event, ResponseComplete):
            response = event.response
            self._publish_runtime_event("usage.updated", usage_snapshot(response.usage), model_request_id=request_id)
            self._publish_runtime_event("model.completed", {
                "finish_reason": response.finish_reason.value,
                "provider": response.metadata.get("provider"),
                "model": response.metadata.get("model", selected_model),
            }, model_request_id=request_id)
        if callable(on_event):
            try:
                on_event(event)
            except Exception:
                pass

    delegation_budget, delegation_budget_run_id = _delegation_budget(self)
    with usage_scope(store=self.memory_bank.store, user_id=self.memory_bank.user_id,
                     workspace_id=self.memory_bank.workspace_id, session_id=self.memory_bank.session_id,
                     run_id=self._active_usage_run_id.get(), surface=self._EXECUTION_SURFACE,
                     delegation_budget=delegation_budget,
                     delegation_budget_run_id=delegation_budget_run_id) as ledger:
        try:
            if provider is None:
                raise ProviderUnavailableError(self.PROVIDER_UNAVAILABLE_MESSAGE)
            if stream:
                response = _bounded_provider_events(self, provider, messages, selected_model, tools, observe)
            else:
                response = self._bounded_provider_call(
                    lambda: get_model_response(provider, messages, selected_model, tools=tools)
                )
                self._publish_runtime_event("usage.updated", usage_snapshot(response.usage), model_request_id=request_id)
                self._publish_runtime_event("model.completed", {
                    "finish_reason": response.finish_reason.value,
                    "provider": response.metadata.get("provider"),
                    "model": response.metadata.get("model", selected_model),
                }, model_request_id=request_id)
            self._last_response_is_native.set(bool(tools) and not bool(response.metadata.get("legacy")))
            self._last_model_response.set(response)
            if self._record_foreground_model.get() and self._conversation_turn_id.get():
                from openkyrozen.agent.conversation import model_response_to_dict
                self._record_conversation_entry("model.response", {
                    "response": model_response_to_dict(response),
                }, request_id=request_id)
        except Exception as exc:
            if not (isinstance(exc, ProviderContractError) and ledger.attempt_recorded):
                from openkyrozen.providers.usage import _track_cost
                _track_cost(getattr(provider, "name", "unknown"), None, model=selected_model,
                            completion_state="failed")
            self._turn_failed.set(True)
            self._publish_runtime_event("model.failed", {
                "error_type": type(exc).__name__,
                "error_kind": getattr(getattr(exc, "kind", None), "value", None),
            }, model_request_id=request_id)
            raise
        finally:
            self._active_model_request_id.reset(request_token)
    usage = response.usage or {}
    self._last_prompt_tokens = usage.get("prompt_tokens", 0) or 0
    self._last_completion_tokens = usage.get("completion_tokens", 0) or 0
    if not self.execution_context.child_run_id:
        self._total_prompt_tokens += self._last_prompt_tokens
        self._total_completion_tokens += self._last_completion_tokens
    if response.text.startswith("[Ollama Error]") and self._is_context_overflow_error(RuntimeError(response.text)):
        from openkyrozen.providers.errors import normalize_provider_error
        raise normalize_provider_error(RuntimeError(response.text), getattr(provider, "name", None), model or self.DEEPSEEK_MODEL)
    state = self._active_context_state.get()
    if state is not None and not self._in_context_compaction.get():
        context_messages = context_accounting_messages(messages)
        if usage and not usage.get("_estimated"):
            state.note_provider_usage(context_messages, int(self._last_prompt_tokens))
            self._cache_reported_context_tokens(context_messages, int(self._last_prompt_tokens))
        else:
            state.update(context_messages)
        self._store_context_status(state)
    return response


def _get_llm_response(self, messages: list[dict[str, str]], model: str | None = None, stream: bool = False,
                      on_chunk: Any = None, on_stream_end: Any = None) -> str:
    from openkyrozen.providers.base import LLMProvider
    provider = self.execution_context.provider or self.llm_provider
    if provider is None:
        self._last_response_is_native.set(False)
        self._turn_failed.set(True)
        request_id = f"model_{uuid.uuid4().hex}"
        request_token = self._active_model_request_id.set(request_id)
        self._last_model_request_id.set(request_id)
        try:
            self._publish_runtime_event("model.requested", {"model": model or self.DEEPSEEK_MODEL},
                                        model_request_id=request_id)
            self._publish_runtime_event("model.failed", {"error_type": "ProviderUnavailableError"},
                                        model_request_id=request_id)
        finally:
            self._active_model_request_id.reset(request_token)
        raise ProviderUnavailableError(self.PROVIDER_UNAVAILABLE_MESSAGE)
    publisher = self._runtime_event_publisher.get()
    self._last_prompt_tokens = self._last_completion_tokens = 0
    stream_contract = getattr(type(provider), "chat_response_stream", None)
    if stream and callable(stream_contract) and stream_contract is not LLMProvider.chat_response_stream:
        def forward(event):
            if (isinstance(event, TextDelta) and callable(on_chunk)
                    and (publisher is None or not publisher.observes_events)):
                on_chunk(event.text)
            if (isinstance(event, ResponseComplete) and callable(on_stream_end)
                    and (publisher is None or not publisher.observes_events)):
                on_stream_end()

        try:
            result = self._get_model_response(messages, model, stream=True, on_event=forward).as_legacy_tuple()[0]
            self._turn_failed.set(False)
            return result
        except TimeoutError as exc:
            self._turn_failed.set(True)
            return f"[LLM Error] {exc}"
        except Exception as exc:
            self._turn_failed.set(True)
            if (self._active_context_state.get() is not None and not self._in_context_compaction.get()
                    and self._is_context_overflow_error(exc)):
                raise ContextOverflowError(str(exc)) from exc
            return f"[LLM Error] {exc}"
    if stream and hasattr(provider, "chat_stream"):
        self._last_response_is_native.set(False)
        self._turn_failed.set(False)
        request_id = f"model_{uuid.uuid4().hex}"
        request_token = self._active_model_request_id.set(request_id)
        self._last_model_request_id.set(request_id)
        self._publish_runtime_event("model.requested", {"model": model or self.DEEPSEEK_MODEL},
                                    model_request_id=request_id)
        before = self.memory_bank.store.usage_totals(
            user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
            session_id=self.memory_bank.session_id, run_id=self._active_usage_run_id.get(),
        )
        try:
            delegation_budget, delegation_budget_run_id = _delegation_budget(self)
            with usage_scope(
                    store=self.memory_bank.store, user_id=self.memory_bank.user_id,
                    workspace_id=self.memory_bank.workspace_id, session_id=self.memory_bank.session_id,
                    run_id=self._active_usage_run_id.get(), surface=self._EXECUTION_SURFACE,
                    delegation_budget=delegation_budget,
                    delegation_budget_run_id=delegation_budget_run_id):
                text = self._bounded_provider_stream(provider, messages, model or self.DEEPSEEK_MODEL, on_chunk)
            if text.startswith(("[Ollama Error]", "[LLM Error]")) and self._is_context_overflow_error(RuntimeError(text)):
                raise ContextOverflowError(text)
            after = self.memory_bank.store.usage_totals(
                user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
                session_id=self.memory_bank.session_id, run_id=self._active_usage_run_id.get(),
            )
            self._last_prompt_tokens = max(0, int(after["prompt_tokens"]) - int(before["prompt_tokens"]))
            self._last_completion_tokens = max(0, int(after["completion_tokens"]) - int(before["completion_tokens"]))
            if not self.execution_context.child_run_id:
                self._total_prompt_tokens += self._last_prompt_tokens
                self._total_completion_tokens += self._last_completion_tokens
            snapshot = {key: int(after.get(key, 0) or 0) for key in (
                "attempts", "authoritative_attempts", "estimated_attempts", "prompt_tokens",
                "cache_hit_tokens", "cache_miss_tokens", "completion_tokens", "reasoning_tokens", "cost_picos",
            )}
            self._publish_runtime_event("usage.updated", snapshot, model_request_id=request_id)
            self._publish_runtime_event("model.completed", {
                "finish_reason": "unknown", "provider": getattr(provider, "name", None),
                "model": model or self.DEEPSEEK_MODEL, "legacy_stream": True,
            }, model_request_id=request_id)
            self._turn_failed.set(False)
            if callable(on_stream_end) and (publisher is None or not publisher.observes_events):
                on_stream_end()
        except Exception as exc:
            self._turn_failed.set(True)
            self._publish_runtime_event("model.failed", {
                "error_type": type(exc).__name__,
                "error_kind": getattr(getattr(exc, "kind", None), "value", None),
            }, model_request_id=request_id)
            if (self._active_context_state.get() is not None and not self._in_context_compaction.get()
                    and self._is_context_overflow_error(exc)):
                raise ContextOverflowError(str(exc)) from exc
            return f"[LLM Error] {exc}"
        finally:
            self._active_model_request_id.reset(request_token)
        state = self._active_context_state.get()
        if state is not None and not self._in_context_compaction.get():
            state.update(messages)
            self._store_context_status(state)
        return text
    try:
        result = self._get_model_response(messages, model).as_legacy_tuple()[0]
        self._turn_failed.set(False)
        return result
    except TimeoutError as exc:
        return f"[LLM Error] {exc}"
    except Exception as exc:
        self._turn_failed.set(True)
        if (self._active_context_state.get() is not None and not self._in_context_compaction.get()
                and self._is_context_overflow_error(exc)):
            raise ContextOverflowError(str(exc)) from exc
        return f"[LLM Error] {exc}"
