#!/usr/bin/env python3
"""Run automatic delegation and evidence review in an isolated project.

--live reads the saved provider configuration without changing it. No secrets or
transcripts are printed. The default provider is deterministic; tools are real.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

if os.environ.get("KYROZEN_ACCEPTANCE_INSTALLED") != "1":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openkyrozen.app.bootstrap import build_application, build_memory
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.providers.factory import get_provider
from openkyrozen.security.capabilities import issue_capability_token
from openkyrozen.tools import ToolAdapters


def assignment(path):
    return {"profile": "researcher", "objective": "Inspect " + path,
        "context": "Check the function against its documented invariant; cite the actual file",
        "scope": [path], "dependencies": [], "acceptance": ["Inspect implementation and independently check invariant"],
        "deliverables": ["Evidence-backed structured findings"], "reason": "Independent subsystem audit"}


class Fixture:
    def chat(self, messages, model=None):
        specialist = messages[0]["content"].startswith("You are the specialised")
        if specialist:
            review = "Independently verify" in messages[1]["content"]
            if len(messages) == 2:
                brief = json.loads(messages[1]["content"].split("\n", 1)[-1]) if review else json.loads(messages[1]["content"])
                path = (brief["assignment"] if review else brief)["scope"][0]
                return "Action: " + json.dumps({"action": "read_file", "args": path}), None
            evidence = ["Actual read_file receipt and invariant inspection"]
            if review:
                return json.dumps({"verdict": "verified", "summary": "Implementation independently inspected",
                    "findings": [], "evidence": evidence}), None
            return json.dumps({"status": "completed", "summary": "Function preserves its invariant",
                "findings": ["Implementation matches specification"], "evidence": evidence,
                "artifacts": [], "checks": ["Inspected actual implementation"], "uncertainties": [], "suggested_followups": []}), None
        if messages[0]["content"].startswith("Synthesize"):
            return "Both implementations were inspected and independently verified.", None
        if any("spawn_agents(" in message["content"] for message in messages):
            return "Inspection is ready for synthesis.", None
        return 'Action: ' + json.dumps({"action": "spawn_agents", "args": json.dumps({"assignments": [assignment("alpha.py"), assignment("beta.py")]})}), None


def acceptance(*, live=False):
    with tempfile.TemporaryDirectory(prefix="openkyrozen-subagent-acceptance-") as directory:
        root = Path(directory)
        (root / "alpha.py").write_text("# Sum all values, including negative values.\ndef total(values):\n    return sum(values)\n")
        (root / "beta.py").write_text("# Count unique values, preserving case distinctions.\ndef count(values):\n    return len(set(values))\n")
        config = ProviderConfig(provider="deepseek", api_key="fixture-only", model_simple="fixture", model_complex="fixture")
        if live:
            from openkyrozen.security.credentials import decrypt_api_key
            saved = json.loads((Path.home() / ".kyrozen_config.json").read_text())
            config = ProviderConfig(provider=saved["provider"], api_key=decrypt_api_key(saved.get("api_key", "")),
                model_simple=saved.get("model_simple", ""), model_complex=saved.get("model_complex", ""))
            if not config.api_key:
                raise RuntimeError("BLOCKED: saved provider credential unavailable")
        with patch.dict(os.environ, {"KYROZEN_WORKSPACE_ROOT": str(root), "KYROZEN_SKILLS_DIR": str(root / "skills"),
                "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_PROVIDER_TIMEOUT_SECONDS": "60"}):
            app = build_application(memory=build_memory(root / "state.sqlite3"), tools=ToolAdapters(root))
            runtime = app.runtime
            runtime._provider_config = config
            runtime.DEEPSEEK_MODEL = config.model_complex or config.model_simple
            runtime.llm_provider = get_provider(config) if live else Fixture()
            runtime.get_provider = get_provider if live else lambda _: Fixture()
            runtime.console = __import__("rich.console", fromlist=["Console"]).Console(file=io.StringIO())
            runtime._execution_capability_token = issue_capability_token("acceptance", frozenset({"read"}))
            runtime._surface_capabilities = "read"
            runtime.dispatch_learning_cycle = lambda **_: {}
            runtime._touch_detached_learning_heartbeat = lambda: None
            runtime.set_interaction_mode("agent")
            events = []
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    reply = runtime.chat(runtime.current_session,
                        "Audit the two independent implementations alpha.py and beta.py against their documented invariants. "
                        "Use separate parallel specialist investigations and cross-examine both submitted results with actual source reads before synthesis. "
                        "Decide how to divide the investigation using the delegation guidance. Do not change files or run commands.",
                        on_event=events.append)
                coordinator = runtime.subagent_manager.coordinator
                assert coordinator is not None, "FAIL: main agent did not delegate: " + runtime._fix_safe_text(reply, 1000)
                runs = coordinator.wait(timeout=60)
                assert len(runs) >= 2, "FAIL: independent investigations were not delegated"
                assert all(run["status"] == "succeeded" for run in runs), json.dumps([
                    coordinator.summary(run, results=True) for run in runs], ensure_ascii=False)
                assert all(run["reviews"] and run["reviews"][-1]["tool_receipts"] for run in runs)
                assert all(run["result"]["tool_records"] for run in runs)
                observed_sources = set()
                for run in runs:
                    for records in (run["result"]["tool_records"], run["reviews"][-1]["tool_receipts"]):
                        sources = [receipt for receipt in records if receipt.get("success") and receipt.get("action") == "read_file"
                            and Path(receipt["args"]).name in {"alpha.py", "beta.py"}]
                        assert sources, "FAIL: author or reviewer did not inspect a fixture source"
                        for receipt in sources:
                            path = root / receipt["args"]
                            assert receipt["result"] == path.read_text(), "FAIL: inspection receipt changed physical source lines"
                            observed_sources.add(path.name)
                assert observed_sources == {"alpha.py", "beta.py"}
                assert all(task["status"] in {"succeeded", "done"} for task in runtime.tasks.tasks), (
                    "FAIL: parent tasks remain unverified: " + json.dumps([
                        {"description": task["description"], "status": task["status"], "checkpoint": task.get("checkpoint")}
                        for task in runtime.tasks.tasks]))
                assert len({run["name"] for run in runs}) == len(runs)
                assert any(event.get("event") == "subagent" for event in events)
                if live:
                    assert all(run["result"]["metrics"]["attempts"] > 0 for run in runs)
                print(json.dumps({"status": "PASS", "mode": "live" if live else "fixture",
                    "provider": config.provider, "agents": [{"name": run["name"], "status": run["status"],
                        "provider_model": run["provider_model"], "reviews": len(run["reviews"]),
                        "summary": run["report"]["summary"], "review_summary": run["reviews"][-1]["summary"],
                        "usage": run["result"]["metrics"]} for run in runs],
                    "source_fidelity": "PASS",
                    "final_report_present": bool(reply)}, indent=2))
            finally:
                app.close()
    print("Sub-agent workflow acceptance passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    acceptance(live=parser.parse_args().live)
