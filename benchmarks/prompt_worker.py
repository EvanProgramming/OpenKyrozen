"""Isolated driver for the real OpenKyrozen CLI runtime (also loads old snapshots)."""
from __future__ import annotations

import argparse
import json
import hashlib
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace


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
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    workspace_root = Path(args.workspace).resolve()
    sys.path.insert(0, args.source)
    from openkyrozen.app.bootstrap import build_application
    from openkyrozen.providers import ProviderConfig
    from openkyrozen.providers.openai import OpenAICompatProvider, OpenAIResponsesProvider
    application = build_application(surface="cli")
    runtime = application.runtime
    try:
        runtime._set_workspace_root(args.workspace)
        runtime.set_interaction_mode("agent")
        provider_name = "openai" if args.protocol == "responses" else "deepseek"
        config = ProviderConfig(provider=provider_name,
                                api_key=os.environ.get("KYROZEN_BENCHMARK_API_KEY", "benchmark-local"),
                                base_url=args.url, model_simple=args.model,
                                model_complex=args.model)
        runtime._provider_config = config
        provider_type = OpenAIResponsesProvider if args.protocol == "responses" else OpenAICompatProvider
        runtime.llm_provider = provider_type(config)
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
                args = receipt.get("args") or {}
                args_hash = hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                path = args.get("path") or args.get("file_path") if isinstance(args, dict) else None
                try:
                    target_path = Path(str(path)).resolve().relative_to(workspace_root).as_posix()
                except (OSError, ValueError):
                    target_path = None
                receipts.append({"action": receipt.get("action"), "args_hash": args_hash,
                                 "target_path": target_path,
                                 "background": isinstance(args, dict) and args.get("background") is True,
                                 "success": receipt.get("success") is True,
                                 "authorized": receipt.get("authorized") is True,
                                 "error_code": (receipt.get("tool_error") or {}).get("code")})
                events.append({"event": "tool_receipt", "action": receipt.get("action"),
                               "success": receipt.get("success") is True})
            elif event.get("event") in {"tasks", "task_update", "working_plan", "subagent"}:
                events.append({"event": event.get("event"),
                               "working_plan": event.get("working_plan"),
                               "tasks": event.get("tasks")})
            elif event.get("event") in {"interaction", "protocol_recovery", "protocol.recovery"}:
                events.append({"event": event.get("event"), "interaction": event.get("interaction")})

        runtime.chat = lambda session, message, **kwargs: cli_chat(
            session, message, on_event=on_event, **kwargs)
        prompt = Path(args.prompt_file).read_text()
        initial_plan = json.loads(Path(args.plan_file).read_text()) if args.plan_file else []
        initial_tasks = []
        accepted_fingerprint = None
        if isinstance(initial_plan, dict):
            proposal = runtime._interaction_controller.propose_plan(initial_plan, original_input=prompt)
            accepted, _ = runtime._interaction_controller.accept_plan(
                runtime.tasks, plan_id=proposal["plan_id"], version=proposal["version"],
            )
            accepted_fingerprint = accepted.get("fingerprint")
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
        if accepted_fingerprint:
            prompt = ("A Strategic Plan has already been accepted. Preserve its approved scope; use the working plan "
                      "for implementation changes, and request renewed approval for material scope changes.\n\n" + prompt)
        answer = runtime.chat(runtime.current_session, prompt, clear_tasks=False, profile="auto")
        manager = runtime.current_session.process_manager
        handles = tuple(manager._handles.values())
        cancellations = sum(item["action"] == "stop_process" and item["success"] for item in receipts)
        unique_calls = {(item["action"], item["args_hash"]) for item in receipts}
        working_plan = runtime.tasks.working_plan_snapshot()
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
                  "tool_receipts": receipts,
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
                  "processes_started": sum(item["action"] == "run_cmd" and item["background"] and item["success"]
                                           for item in receipts),
                  "processes_cancelled": cancellations,
                  "processes_alive": sum(handle.process.poll() is None for handle in handles),
                  "process_pids": [handle.process.pid for handle in handles],
                  "process_marker_seen": any(args.process_marker and args.process_marker in handle.stdout
                                              for handle in handles),
                  "first_delta_latency_ms": first_delta_ms,
                  "provider_error": "[LLM Error]" in answer}
        bound_scope_active = bool(accepted_fingerprint and any(
            task["status"] != "cancelled"
            and (task.get("strategic_binding") or {}).get("fingerprint") == accepted_fingerprint
            for task in result["task_records"]
        ))
        result["accepted_scope_preserved"] = bool(
            accepted_fingerprint and accepted_fingerprint in accepted_fingerprints
            and result["executing_plan_fingerprint"] == accepted_fingerprint and bound_scope_active
        )
        result["scope_preserved"] = bool(result["accepted_scope_preserved"] and not pending_material)
        Path(args.result).write_text(json.dumps(result))
    finally:
        application.close()


if __name__ == "__main__":
    main()
