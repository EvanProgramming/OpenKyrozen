"""Isolated driver for the real OpenKyrozen CLI runtime (also loads old snapshots)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    sys.path.insert(0, args.source)
    from openkyrozen.app.bootstrap import build_application
    from openkyrozen.providers import ProviderConfig
    from openkyrozen.providers.openai import OpenAICompatProvider
    application = build_application(surface="cli")
    runtime = application.runtime
    try:
        runtime._set_workspace_root(args.workspace)
        runtime.set_interaction_mode("agent")
        config = ProviderConfig(provider="deepseek", api_key="benchmark-local",
                                base_url=args.url, model_simple="deepseek-v4-flash",
                                model_complex="deepseek-v4-flash")
        runtime._provider_config = config
        runtime.llm_provider = OpenAICompatProvider(config)
        runtime.DEEPSEEK_MODEL_SIMPLE = runtime.DEEPSEEK_MODEL_COMPLEX = "deepseek-v4-flash"
        # Only isolate optional services; leave planning, tools, recovery, receipts,
        # completion and CLI rendering on their actual production paths.
        runtime.dispatch_learning_cycle = lambda **kwargs: None
        runtime._plugin_runtime_for_surface = lambda *args: SimpleNamespace(
            turn_start=lambda **kwargs: None, turn_end=lambda **kwargs: None)
        events = []
        cli_chat = runtime.chat
        runtime.chat = lambda session, message, **kwargs: cli_chat(
            session, message, on_event=events.append, **kwargs)
        prompt = Path(args.prompt_file).read_text()
        runtime._run_cli_chat(prompt, runtime._interaction_controller.state())
        answer = next((m["content"] for m in reversed(runtime.short_term_memory)
                       if m["role"] == "assistant"), "")
        result = {"answer": answer, "tool_calls": sum(e.get("event") == "tool_receipt" for e in events),
                  "tasks": [t["status"] for t in runtime.tasks.tasks],
                  "provider_error": "[LLM Error]" in answer}
        Path(args.result).write_text(json.dumps(result))
    finally:
        application.close()


if __name__ == "__main__":
    main()
