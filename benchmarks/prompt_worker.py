"""Isolated driver for the real OpenKyrozen CLI runtime (also loads old snapshots)."""
from __future__ import annotations

import argparse
import json
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
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
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
        config = ProviderConfig(provider=provider_name, api_key="benchmark-local",
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
        cli_chat = runtime.chat
        started = time.monotonic()
        first_delta_ms = None

        def on_event(event):
            nonlocal first_delta_ms
            if first_delta_ms is None and event.get("event") == "content":
                first_delta_ms = round((time.monotonic() - started) * 1000, 2)
            if event.get("event") == "tool_receipt":
                receipt = event.get("tool_receipt", {})
                events.append({"event": "tool_receipt", "action": receipt.get("action"),
                               "success": receipt.get("success"), "call_id": receipt.get("call_id")})
            elif event.get("event") in {"tasks", "task_update", "working_plan", "subagent"}:
                events.append({"event": event.get("event")})

        runtime.chat = lambda session, message, **kwargs: cli_chat(
            session, message, on_event=on_event, **kwargs)
        prompt = Path(args.prompt_file).read_text()
        runtime._run_cli_chat(prompt, runtime._interaction_controller.state())
        answer = next((m["content"] for m in reversed(runtime.short_term_memory)
                       if m["role"] == "assistant"), "")
        result = {"answer": answer, "tool_calls": sum(e.get("event") == "tool_receipt" for e in events),
                  "tool_actions": [e.get("action") for e in events if e.get("event") == "tool_receipt"],
                  "tool_successes": sum(e.get("event") == "tool_receipt" and e.get("success") is True for e in events),
                  "tasks": [t["status"] for t in runtime.tasks.tasks],
                  "plan_revisions": sum(e.get("event") == "working_plan" for e in events),
                  "cancelled_tasks": sum(t["status"] == "cancelled" for t in runtime.tasks.tasks),
                  "first_delta_latency_ms": first_delta_ms,
                  "provider_error": "[LLM Error]" in answer}
        Path(args.result).write_text(json.dumps(result))
    finally:
        application.close()


if __name__ == "__main__":
    main()
