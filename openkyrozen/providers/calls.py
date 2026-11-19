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
        ordinary_plan_edit = False
        if "update_plan" in current_names and registry.get_spec("update_plan").source == "builtin":
            plan_tool = next(item for item in tools if item["name"] == "update_plan")
            schema = plan_tool["parameters"]
            plan = getattr(getattr(self, "tasks", None), "working_plan", None)
            accepted = (self._interaction_controller.state().get("executing_plan")
                        if plan and plan.status == "active" else None)
            ordinary_plan_edit = not accepted or accepted.get("working_plan_id") != plan.id
            if ordinary_plan_edit:
                for shape in (schema, *schema.get("anyOf", ())):
                    operations = shape.get("properties", {}).get("operations", {})
                    for operation in operations.get("items", {}).get("anyOf", ()):
                        operation["properties"].pop("anchor", None)
            if _native_tool_phase == "plan_required":
                operations = schema["properties"]["operations"]
                creation_ops = [item for item in operations["items"]["anyOf"]
                                if item["properties"]["op"]["enum"][0] in {"add", "start"}]
                plan_tool["parameters"] = {
                    "type": "object", "additionalProperties": False,
                    "required": ["expected_revision", "operations"],
                    "properties": {
                        "expected_revision": {"type": "integer", "minimum": 0, "maximum": 0},
                        "operations": {**operations, "minItems": 1, "items": {"anyOf": creation_ops}},
                    },
                }
                plan_tool["description"] = (
                    "Create the required Working Plan at revision zero. Use add operations with a unique ref "
                    "and a task with a description and acceptance criteria. In the same call, use start "
                    "with the first task's ref as its task value. Include typed verification when the "
                    "exact tool arguments and expected result are known."
                )
            elif _native_tool_phase in {"plan_active", "plan_completed"}:
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
                        "For a pending task with only prose acceptance and no execution contract, use update "
                        "to set typed acceptance before start in the same call. For a typed check of an expected failure, "
                        "bind a run_cmd check that exits zero only when the expected failure is observed. "
                        if ordinary_plan_edit else "Preserve approved criteria and request renewed approval for material changes. "
                    )
                    plan_tool["description"] = (
                        "Revise the current Working Plan using its pinned ID and revision. "
                        f"{criterion_guidance}Use exact tool arguments for typed verification. "
                        "Combine compatible operations in execution order."
                    )
                else:
                    operations = properties["operations"]
                    confirm = next(item for item in operations["items"]["anyOf"]
                                   if item["properties"]["op"]["enum"] == ["suggest_complete"])
                    properties["operations"] = {**operations, "items": confirm}
                    plan_tool["description"] = (
                        "Confirm previously verified tasks in the completed Working Plan. "
                        "Use the current pinned plan ID and revision and suggest_complete with the verified task IDs."
                    )
                plan_tool["parameters"] = {
                    "type": "object", "additionalProperties": False, "required": required, "properties": properties,
                }
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
            "plan_active": "The Working Plan is active now. Use current_call_tools in this request "
                           "for the selected task's next action. The plan becomes read-only when the last task finishes. ",
            "plan_completed": "Completed-plan exposure is confirmation-only. The final tool reply may already have "
                              "verified the last task. Answer the original request using the verified results. "
                              "If explicitly asked to confirm the plan, use update_plan with the current IDs and revision. ",
        }.get(_native_tool_phase, "")
        if _native_tool_phase == "plan_active" and ordinary_plan_edit:
            phase_guidance += "Add discovered work within scope before the final verification. "
        if _native_tool_phase == "plan_active" and "discover_tools" in current_names:
            phase_guidance += "Use discover_tools if a permitted tool is missing from the compact list. "
        can_propose_revision = _native_tool_phase == "plan_completed" and "propose_plan" in current_names
        if can_propose_revision:
            phase_guidance = ("The Working Plan is complete. Use update_plan only to confirm completed work, "
                              "or propose_plan to stage a material revision for renewed approval. "
                              "Otherwise, answer the original request using the verified results. ")
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
            if not (isinstance(exc, ProviderContractError) and exc.malformed_tool_json and ledger.attempt_recorded):
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
