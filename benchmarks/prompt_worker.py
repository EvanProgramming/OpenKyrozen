"""Isolated driver for the real OpenKyrozen CLI runtime (also loads old snapshots)."""
from __future__ import annotations

import argparse
import json
import hashlib
import os
import shlex
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from contextvars import ContextVar
from functools import wraps


def _normalize_tool_receipt(receipt, workspace_root):
    arguments = receipt.get("arguments")
    if not isinstance(arguments, dict):
        arguments = receipt.get("args")
        if isinstance(arguments, str) and len(arguments) <= 65536 and arguments.lstrip().startswith("{"):
            try:
                parsed = json.loads(arguments)
                if isinstance(parsed, dict):
                    arguments = parsed
            except (TypeError, ValueError):
                pass
    canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    path = (arguments.get("path") or arguments.get("file_path")) if isinstance(arguments, dict) else None
    if isinstance(arguments, str) and len(arguments) <= 65536:
        # Frozen V2 filesystem tools take a bare path, or path|content for writes.
        if receipt.get("action") == "read_file":
            path = arguments.strip()
        elif receipt.get("action") == "write_file" and "|" in arguments:
            path = arguments.split("|", 1)[0].strip()
    try:
        target = Path(path)
        target = target if target.is_absolute() else Path(workspace_root) / target
        target_path = target.resolve().relative_to(Path(workspace_root).resolve()).as_posix()
    except (OSError, ValueError, TypeError):
        target_path = None
    reference = None
    if receipt.get("action") == "git_show":
        if isinstance(arguments, dict):
            reference = arguments.get("reference")
        elif isinstance(arguments, str) and len(arguments) <= 65536:
            try:
                reference = next((part for part in shlex.split(arguments) if not part.startswith("-")), None)
            except ValueError:
                pass
    return {"action": receipt.get("action"), "call_id": receipt.get("call_id"),
            "args_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "target_path": target_path, "git_reference": reference,
            "background": arguments.get("background") is True if isinstance(arguments, dict) else None,
            "success": receipt.get("success") is True, "authorized": receipt.get("authorized") is True,
            "error_code": (receipt.get("tool_error") or {}).get("code"),
            "evidence": json.loads(json.dumps(receipt))}


def _context_observation(runtime):
    port = getattr(runtime, "context_usage", None)
    try:
        status = port() if callable(port) else None
    except Exception:
        status = None
    if not isinstance(status, dict):
        return None
    keys = ("model", "window_tokens", "window_source", "input_tokens", "input_source", "reserve_tokens",
            "breakdown", "breakdown_source", "native_input_overhead_tokens", "native_overhead_method",
            "stable_prefix_tokens", "stable_prefix_fingerprint", "compaction",
            "new_context_tokens", "retransmitted_history_tokens", "new_entry_count",
            "retransmitted_entry_count", "duplicate_entry_count")
    result = {key: status.get(key) for key in keys}
    new, retransmitted = result["new_context_tokens"], result["retransmitted_history_tokens"]
    result["retained_history_tokens"] = new + retransmitted if type(new) is int and type(retransmitted) is int else None
    result["component_attribution_source"] = status.get("component_attribution_source")
    return json.loads(json.dumps(result))


class _ProviderObservations:
    """Observe each provider-port invocation after production preflight.

    Components are source-runtime estimates, never provider attribution. Internal
    transport retries may reuse one invocation payload; the relay owns attempts.
    """
    def __init__(self, runtime, provider):
        self.runtime, self.provider = runtime, provider
        self.records, self.turn, self.prompt_sha256 = [], None, None
        self.depth = ContextVar("benchmark_provider_observation_depth", default=0)
        for name in ("chat", "chat_stream", "chat_response", "chat_response_stream"):
            method = getattr(provider, name, None)
            if callable(method):
                setattr(provider, name, self._wrap(method, name.endswith("stream")))

    def _begin(self, messages, model, tools):
        from openkyrozen.agent.compaction import estimate_messages, _text_tokens, DEFAULT_OUTPUT_RESERVE_TOKENS
        runtime = self.runtime
        compacting = getattr(runtime, "_in_context_compaction", None)
        summary = bool(compacting.get()) if compacting is not None else False
        state_var = getattr(runtime, "_active_context_state", None)
        state = state_var.get() if state_var is not None else None
        schema_json = json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        overhead = _text_tokens(schema_json) + 32 * len(tools or ()) + 64 if tools is not None else 0
        estimated, components = estimate_messages(messages)
        components = dict(components)
        components["fixed_instructions"] += overhead
        status = _context_observation(runtime) if not summary else None
        metrics_var = getattr(runtime, "_context_history_metrics", None)
        metrics = metrics_var.get() if not summary and metrics_var is not None else None
        metrics = metrics if isinstance(metrics, dict) else {}
        new_history, repeated_history = metrics.get("new_context_tokens"), metrics.get("retransmitted_history_tokens")
        record = {"turn": self.turn, "prompt_sha256": self.prompt_sha256,
                  "kind": "summary" if summary else "generation", "model": model,
                  "observation_boundary": "provider_port_after_production_preflight",
                  "estimate_method": "source_runtime_stdlib_messages_plus_json_schema_framing",
                  "window_tokens": getattr(getattr(self.provider, 'config', None), "context_window_tokens", None),
                  "reserve_tokens": getattr(state, "reserve_tokens", DEFAULT_OUTPUT_RESERVE_TOKENS),
                  "estimated_input_tokens": estimated + overhead,
                  "component_estimates": components, "component_attribution_source": "estimated",
                  "native_input_overhead_tokens": overhead,
                  "schema_sha256": hashlib.sha256(schema_json.encode()).hexdigest() if tools is not None else None,
                  "message_count": len(messages),
                  "message_projection_sha256": None,
                  "retained_history_tokens": new_history + repeated_history if type(new_history) is int and type(repeated_history) is int else None,
                  "new_context_tokens": new_history,
                  "retransmitted_history_tokens": repeated_history,
                  "history_attribution_source": "production_history_projection_estimate" if metrics else None,
                  "compaction": status.get("compaction") if status else None,
                  "provider_input_tokens": None, "provider_output_tokens": None,
                  "provider_usage_source": None, "outcome": "pending"}
        # Fingerprint the same source-owned estimate projection without retaining content.
        from openkyrozen.agent.compaction import message_fingerprint
        record["message_projection_sha256"] = message_fingerprint(messages)
        routing_observer = getattr(runtime, '_benchmark_routing_observer', None)
        if routing_observer is not None:
            routing_observer(record, tools)
        self.records.append(record)
        return record

    @staticmethod
    def _usage(record, usage):
        if not isinstance(usage, dict) or usage.get("_estimated"):
            return
        for target, keys in (("provider_input_tokens", ("prompt_tokens", "input_tokens")),
                             ("provider_output_tokens", ("completion_tokens", "output_tokens"))):
            value = next((usage[key] for key in keys if type(usage.get(key)) is int and usage[key] >= 0), None)
            record[target] = value
        if record["provider_input_tokens"] is not None:
            record["provider_usage_source"] = "provider_reported_total"

    def _wrap(self, original, streaming):
        def begin(args, kwargs):
            messages = args[0] if args else kwargs["messages"]
            model = args[1] if len(args) > 1 else kwargs.get("model")
            return self._begin(messages, model, kwargs.get("tools"))
        if streaming:
            @wraps(original)
            def stream(*args, **kwargs):
                if self.depth.get():
                    yield from original(*args, **kwargs)
                    return
                record = begin(args, kwargs)
                token = self.depth.set(self.depth.get() + 1)
                iterator = None
                try:
                    iterator = original(*args, **kwargs)
                    for event in iterator:
                        usage = getattr(event, "usage", None)
                        response = getattr(event, "response", None)
                        self._usage(record, getattr(response, "usage", None) or usage)
                        if type(event).__name__ == "ResponseError":
                            record["outcome"] = "failed"
                        elif type(event).__name__ == "ResponseComplete":
                            record["outcome"] = "returned"
                        yield event
                    if record["outcome"] == "pending":
                        record["outcome"] = "returned"
                except BaseException:
                    if record["outcome"] == "pending":
                        record["outcome"] = "failed"
                    raise
                finally:
                    try:
                        if callable(getattr(iterator, "close", None)):
                            iterator.close()
                    finally:
                        self.depth.reset(token)
            return stream
        @wraps(original)
        def call(*args, **kwargs):
            if self.depth.get():
                return original(*args, **kwargs)
            record = begin(args, kwargs)
            token = self.depth.set(self.depth.get() + 1)
            try:
                response = original(*args, **kwargs)
                usage = response[1] if isinstance(response, tuple) and len(response) == 2 else getattr(response, "usage", None)
                self._usage(record, usage)
                record["outcome"] = "returned"
                return response
            except BaseException:
                record["outcome"] = "failed"
                raise
            finally:
                self.depth.reset(token)
        return call


def _run_conversation(runtime, session, turns, observations=None):
    records = []
    answer = ""
    for index, prompt in enumerate(turns):
        if observations is not None:
            observations.turn = index
            observations.prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        answer = runtime.chat(session, prompt, clear_tasks=False, profile="auto")
        records.append({"turn": index, "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                        "completed": True, "context": _context_observation(runtime),
                        "messages_compacted": getattr(session, "messages_compacted", None)})
    return answer, records


def _accepted_scope_preserved(history, accepted_fingerprints, accepted_plan, task_records):
    if (not isinstance(accepted_plan, dict)
            or accepted_plan.get("fingerprint") not in accepted_fingerprints):
        return False
    identity = ("plan_id", "version", "fingerprint")
    completed = any(
        isinstance(event, dict) and event.get("event_type") == "interaction.plan_completed"
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("status") == "completed"
        and all(event["payload"].get(key) == accepted_plan.get(key) for key in identity)
        for event in history
    )
    return completed or any(
        task.get("status") != "cancelled"
        and (task.get("strategic_binding") or {}).get("fingerprint") == accepted_plan.get("fingerprint")
        for task in task_records if isinstance(task, dict)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--protocol", choices=("chat", "responses"), default="chat")
    parser.add_argument("--scenario", default="")
    parser.add_argument("--process-marker", default="")
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--plan-file")
    parser.add_argument("--conversation-file")
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    conversation = json.loads(Path(args.conversation_file).read_text()) if args.conversation_file else {}
    followups = conversation.get("followups", [])
    if (not isinstance(followups, list) or any(not isinstance(turn, str) or not turn for turn in followups)
            or len(followups) > 20):
        raise ValueError("benchmark followups must be a bounded array of nonempty strings")
    window = conversation.get("context_window_tokens")
    if window is not None and (type(window) is not int or not 1 <= window <= 10000000):
        raise ValueError("benchmark context window must be a positive declared integer")
    from_output_reserve = conversation.get("output_reserve_tokens", 4096)
    if from_output_reserve != 4096:
        raise ValueError("both benchmark arms require the frozen 4096-token output reserve")
    workspace_root = Path(args.workspace).resolve()
    sys.path.insert(0, args.source)
    sys.path.insert(1, str(Path(__file__).resolve().parent))
    actor_client = None
    if os.environ.get('KYROZEN_BENCHMARK_ACTOR_SOCKET') is not None:
        from v3_process_owner import connect_actor_client
        from v3_shell_launcher import install_worker_adapter
        from v3_actor_worker import install_mutation_adapter, install_read_adapter
        actor_client = connect_actor_client()
        install_worker_adapter(client=actor_client)
        install_mutation_adapter(actor_client)
        install_read_adapter(actor_client)
    if os.environ.get("KYROZEN_BENCHMARK_BROKER_FD") is not None:
        # V2 also has a benchmarks package; load this adapter by its owning file
        # without allowing the current source tree to shadow the frozen snapshot.
        import runpy
        runpy.run_path(str(Path(__file__).with_name("v3_shell_launcher.py")))["install_worker_adapter"]()
    from v3_actor_worker import _require_selected_source
    _require_selected_source(args.source)
    from openkyrozen.app.bootstrap import build_application
    from openkyrozen.providers import ProviderConfig
    from openkyrozen.providers.openai import OpenAICompatProvider, OpenAIResponsesProvider
    from openkyrozen.agent.compaction import DEFAULT_OUTPUT_RESERVE_TOKENS
    if DEFAULT_OUTPUT_RESERVE_TOKENS != from_output_reserve:
        raise ValueError("source runtime output reserve differs from the frozen cohort")
    application = build_application(surface="cli")
    runtime = application.runtime
    routing = None
    try:
        if os.environ.get('KYROZEN_BENCHMARK_ROUTING_SUPPLEMENT') is not None:
            if actor_client is None:
                raise PermissionError('routing supplement requires its parent actor broker')
            from v3_routing_supplement import install
            variant = actor_client.rpc('routing_policy')['variant']
            routing = install(runtime, variant)
            runtime._benchmark_routing_variant = variant
        if actor_client is not None:
            from v3_actor_worker import install_delegated_adapter
            install_delegated_adapter(runtime, actor_client, source=args.source)
        runtime._set_workspace_root(args.workspace)
        runtime.set_interaction_mode("ask" if followups else "agent")
        provider_name = "openai" if args.protocol == "responses" else "deepseek"
        config = ProviderConfig(provider=provider_name,
                                api_key=os.environ.get("KYROZEN_BENCHMARK_API_KEY", "benchmark-local"),
                                base_url=args.url, model_simple=args.model,
                                model_complex=args.model, context_window_tokens=window)
        runtime._provider_config = config
        provider_type = OpenAIResponsesProvider if args.protocol == "responses" else OpenAICompatProvider
        runtime.llm_provider = provider_type(config)
        observations = _ProviderObservations(runtime, runtime.llm_provider)
        runtime._default_turn.provider = runtime.llm_provider
        runtime.DEEPSEEK_MODEL_SIMPLE = runtime.DEEPSEEK_MODEL_COMPLEX = args.model
        # Keep production planning, tools, recovery, receipts and completion active.
        runtime.dispatch_learning_cycle = lambda **kwargs: None
        runtime._plugin_runtime_for_surface = lambda *args: SimpleNamespace(
            turn_start=lambda **kwargs: None, turn_end=lambda **kwargs: None)
        events = []
        receipts = []
        cli_chat = runtime.chat
        started = time.monotonic()
        first_delta_ms = None

        def on_event(event):
            nonlocal first_delta_ms
            if hasattr(event, "kind") and hasattr(event, "payload"):
                from openkyrozen.agent.events import runtime_event_as_legacy
                event = runtime_event_as_legacy(event) or {}
            elif hasattr(event, "as_dict"):
                event = event.as_dict()
            if not isinstance(event, dict):
                return
            if first_delta_ms is None and event.get("event") == "content":
                first_delta_ms = round((time.monotonic() - started) * 1000, 2)
            if event.get("event") == "tool_receipt":
                receipt = event.get("tool_receipt", {})
                receipts.append(_normalize_tool_receipt(receipt, workspace_root))
                events.append({"event": "tool_receipt", "action": receipt.get("action"),
                               "success": receipt.get("success") is True})
            elif event.get("event") in {"tasks", "task_update", "working_plan", "subagent"}:
                events.append({"event": event.get("event"),
                               "working_plan": event.get("working_plan"),
                               "tasks": event.get("tasks"),
                               "agent": event.get("agent"),
                               "status": event.get("status"),
                               "description": event.get("description")})
            elif event.get("event") in {"interaction", "protocol_recovery", "protocol.recovery"}:
                events.append({"event": event.get("event"), "interaction": event.get("interaction")})

        runtime.chat = lambda session, message, **kwargs: cli_chat(
            session, message, on_event=on_event, **kwargs)
        prompt = Path(args.prompt_file).read_text()
        initial_plan = json.loads(Path(args.plan_file).read_text()) if args.plan_file else []
        initial_tasks = []
        accepted_fingerprint = None
        accepted_plan_identity = None
        if isinstance(initial_plan, dict):
            proposal = runtime._interaction_controller.propose_plan(initial_plan, original_input=prompt)
            accepted, _ = runtime._interaction_controller.accept_plan(
                runtime.tasks, plan_id=proposal["plan_id"], version=proposal["version"],
            )
            accepted_fingerprint = accepted.get("fingerprint")
            accepted_plan_identity = {key: accepted.get(key) for key in ("plan_id", "version", "fingerprint")}
            initial_tasks = [f"{step['title']}: {step['description']}" for step in accepted["steps"]]
        elif isinstance(initial_plan, list) and all(isinstance(item, str) for item in initial_plan):
            initial_tasks = initial_plan
            if initial_tasks:
                if callable(getattr(runtime.tasks, "edit_plan", None)):
                    runtime.tasks.edit_plan(
                        complexity="complex", expected_revision=0,
                        operations=[{"op": "add", "ref": f"fixture-{index}", "task": {
                            "description": task, "acceptance": ["Complete the independent fixture objective."],
                        }} for index, task in enumerate(initial_tasks, 1)],
                        reason="Initialize the frozen matched evaluation plan.",
                    )
                else:
                    runtime.tasks.add_ordered_plan(initial_tasks, task_id_prefix="fixture")
                prompt = ("Initial working-plan task order:\n" + "\n".join(
                    f"{index}. {task}" for index, task in enumerate(initial_tasks, 1)
                ) + "\nUse update_plan when new evidence requires adding, cancelling, replacing, or reordering work.\n\n"
                          + prompt)
        else:
            raise ValueError("benchmark initial plan must be a plan object or an array of task descriptions")
        snapshot_working_plan = getattr(runtime.tasks, "working_plan_snapshot", None)
        initial_working_plan = snapshot_working_plan() if callable(snapshot_working_plan) else None
        if initial_working_plan:
            events.append({"event": "working_plan", "working_plan": initial_working_plan})
        if runtime.tasks.tasks:
            events.append({"event": "tasks", "tasks": list(runtime.tasks.tasks)})
        if accepted_fingerprint:
            prompt = ("A Strategic Plan has already been accepted. Preserve its approved scope; use the working plan "
                      "for implementation changes, and request renewed approval for material scope changes.\n\n" + prompt)
        answer, conversation_records = _run_conversation(runtime, runtime.current_session, [prompt, *followups], observations)
        manager = getattr(runtime.current_session, "process_manager", None)
        handles = tuple(getattr(manager, "_handles", {}).values())
        cancellations = sum(item["action"] == "stop_process" and item["success"] for item in receipts)
        unique_calls = {(item["action"], item["args_hash"]) for item in receipts}
        working_plan = snapshot_working_plan() if callable(snapshot_working_plan) else None
        plan_snapshots = []
        for event in events:
            snapshot = event.get("working_plan")
            if isinstance(snapshot, dict) and all(
                    (item.get("plan_id"), item.get("revision")) !=
                    (snapshot.get("plan_id"), snapshot.get("revision")) for item in plan_snapshots):
                plan_snapshots.append(snapshot)
        if isinstance(working_plan, dict) and all(
                (item.get("plan_id"), item.get("revision")) !=
                (working_plan.get("plan_id"), working_plan.get("revision")) for item in plan_snapshots):
            plan_snapshots.append(working_plan)
        revisions = [int(item.get("revision", 0)) for item in plan_snapshots]
        working_revisions = max(0, max(revisions) - min(revisions)) if revisions else 0
        task_orders = []
        for event in events:
            items = event.get("tasks")
            if isinstance(items, list):
                order = tuple((item.get("id"), item.get("description")) for item in items if isinstance(item, dict))
                if order and (not task_orders or order != task_orders[-1]):
                    task_orders.append(order)
        task_revisions = max(0, len(task_orders) - 1)
        plan_revisions = max(working_revisions, task_revisions)
        unnecessary_churn = sum(
            left.get("tasks") == right.get("tasks") and left.get("plan_id") == right.get("plan_id")
            for left, right in zip(plan_snapshots, plan_snapshots[1:])
        )
        material_revisions = sum(
            1 for event in events if event.get("event") == "interaction"
            and isinstance(event.get("interaction"), dict)
            and (event["interaction"].get("pending_plan") or {}).get("revision_materiality") == "material"
        )
        interaction_state = runtime._interaction_controller.state()
        history = runtime._interaction_controller._history()
        proposals = [event.get("payload", {}) for event in history
                     if event.get("event_type") == "interaction.plan_proposed"]
        accepted_versions = {(event.get("payload", {}).get("plan_id"),
                              event.get("payload", {}).get("version")) for event in history
                             if event.get("event_type") == "interaction.plan_accepted"}
        accepted_fingerprints = [proposal.get("fingerprint") for proposal in proposals
                                 if (proposal.get("plan_id"), proposal.get("version")) in accepted_versions]
        pending_material = (interaction_state.get("pending_plan") or {}).get("revision_materiality") == "material"
        result = {"answer": answer, "tool_calls": sum(e.get("event") == "tool_receipt" for e in events),
                  "tool_actions": [e.get("action") for e in events if e.get("event") == "tool_receipt"],
                  "tool_receipts": receipts, "events": events,
                  "conversation_records": conversation_records,
                  "request_context_records": observations.records + getattr(runtime, '_benchmark_child_context_records', []),
                  "tool_successes": sum(e.get("event") == "tool_receipt" and e.get("success") is True for e in events),
                  "tasks": [t["status"] for t in runtime.tasks.tasks],
                  "task_records": [{"id": t["id"], "description": t["description"],
                                    "status": t["status"],
                                    "strategic_binding": (t.get("checkpoint") or {}).get("strategic_plan")}
                                   for t in runtime.tasks.tasks],
                  "working_plan": working_plan,
                  "initial_plan_fingerprint": accepted_fingerprint,
                  "accepted_plan_fingerprints": accepted_fingerprints,
                  "material_plan_proposals": sum(item.get("revision_materiality") == "material" for item in proposals),
                  "pending_material_revision": pending_material,
                  "executing_plan_fingerprint": (interaction_state.get("executing_plan") or {}).get("fingerprint"),
                  "plan_revisions": plan_revisions,
                  "cancelled_tasks": sum(t["status"] == "cancelled" for t in runtime.tasks.tasks),
                  "material_revisions": material_revisions,
                  "unnecessary_churn": unnecessary_churn,
                  "duplicate_calls": len(receipts) - len(unique_calls),
                  "protocol_recovery": sum(event["event"] in {"protocol_recovery", "protocol.recovery"}
                                            for event in events),
                  "processes_started": sum(item["action"] == "run_cmd" and item["background"] is True and item["success"]
                                           for item in receipts),
                  "processes_cancelled": cancellations,
                  "processes_alive": sum(handle.process.poll() is None for handle in handles),
                  "process_pids": [handle.process.pid for handle in handles],
                  "process_marker_seen": any(args.process_marker and args.process_marker in handle.stdout
                                              for handle in handles),
                  "first_delta_latency_ms": first_delta_ms,
                  "provider_error": "[LLM Error]" in answer or any(item["outcome"] == "failed" for item in
                      observations.records + getattr(runtime, '_benchmark_child_context_records', []))}
        result["accepted_scope_preserved"] = _accepted_scope_preserved(
            history, accepted_fingerprints, accepted_plan_identity, result["task_records"],
        )
        result["scope_preserved"] = bool(result["accepted_scope_preserved"] and not pending_material)
        if routing is not None:
            result['routing_observations'] = routing.records + getattr(runtime,'_benchmark_child_routing_records',[])
        Path(args.result).write_text(json.dumps(result))
    finally:
        application.close()
        if routing is not None:
            routing.close()


if __name__ == "__main__":
    main()
