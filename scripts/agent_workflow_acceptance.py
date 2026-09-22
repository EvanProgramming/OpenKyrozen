#!/usr/bin/env python3
"""Deterministic plan-to-deployment acceptance test for the installed runtime."""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

if os.environ.get("KYROZEN_ACCEPTANCE_INSTALLED") != "1":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from capability_tokens import issue_capability_token
from event_store import EventStore
from interaction import InteractionController
from task_engine import TaskManager


class _LearningStub:
    def feedback_signal(self, _text):
        return None

    def route_profile(self, _text, _profile=None):
        return "coder"

    def begin_run(self, profile, _task, provider_model=None):
        return {"run_id": "agent-acceptance", "profile": profile, "provider_model": provider_model}

    def artifact_context(self, _run):
        return "", []


class _RuntimeStub:
    def turn_start(self, **_kwargs):
        return None

    def turn_end(self, **_kwargs):
        return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main_acceptance() -> None:
    port = _free_port()
    page = "<!doctype html><title>OpenKyrozen acceptance</title><h1>deployed</h1>"
    deployment_test = f'''import http.server
import threading
import unittest
import urllib.request


class DeploymentTest(unittest.TestCase):
    def test_page_is_served(self):
        handler = type("QuietHandler", (http.server.SimpleHTTPRequestHandler,), {{
            "log_message": lambda *args: None,
        }})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", {port}), handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with urllib.request.urlopen("http://127.0.0.1:{port}/index.html", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"deployed", response.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
'''
    plan = {
        "mode": "plan",
        "plan_name": "Deploy local acceptance page",
        "overview": "Create a page and verify its local HTTP deployment.",
        "do_not_execute_until_approved": True,
        "steps": [
            {"step_id": "page", "title": "Create index.html", "details": "Write index.html.",
             "acceptance_criteria": ["index.html exists"]},
            {"step_id": "test", "title": "Create test_deploy.py", "details": "Write test_deploy.py.",
             "acceptance_criteria": ["test_deploy.py exists"]},
            {"step_id": "verify", "title": "Verify local deployment",
             "details": "Run python -m unittest test_deploy.py -v.",
             "acceptance_criteria": ["unittest exits successfully"]},
        ],
    }
    responses = [
        json.dumps(plan),
        "Action: " + json.dumps({"action": "write_file", "args": f"index.html|{page}"}),
        "TaskDone: 0\nAction: " + json.dumps(
            {"action": "write_file", "args": f"test_deploy.py|{deployment_test}"}
        ),
        "TaskDone: 1\nAction: " + json.dumps(
            {"action": "run_cmd", "args": f"{sys.executable} -m unittest test_deploy.py -v"}
        ),
        "TaskDone: 2\nDeployment verified.",
    ]

    original = (
        main.tasks, main._interaction_controller, main.learning_engine,
        main._get_workspace_root(), main._execution_capability_token,
    )
    with tempfile.TemporaryDirectory(prefix="openkyrozen-agent-acceptance-") as directory:
        root = Path(directory)
        store = EventStore(root / "state.sqlite3")
        main.tasks = TaskManager(store, workspace_id="acceptance", session_id="release")
        main._interaction_controller = InteractionController(
            store, workspace_id="acceptance", session_id="release",
        )
        main._interaction_controller.set_mode("plan")
        main.learning_engine = _LearningStub()
        main._set_workspace_root(root)
        main._execution_capability_token = issue_capability_token(
            "agent-acceptance", frozenset({"read", "write", "shell"}),
        )
        events = []
        stream_token = main._stream_event_callback.set(events.append)
        approval_token = main._approval_callback.set(lambda _action, _args: True)
        try:
            with patch.object(main, "_plugin_runtime_for_surface", return_value=_RuntimeStub()), \
                    patch.object(main, "_touch_detached_learning_heartbeat"), \
                    patch.object(main, "dispatch_learning_cycle"), \
                    patch.object(main, "_summarize_old_turns"), \
                    patch.object(main, "_build_messages", return_value=[]), \
                    patch.object(main, "_build_memory_context", return_value=""), \
                    patch.object(main, "_classify_complexity", return_value="simple"), \
                    patch.object(main, "_call_llm_with_spinner", side_effect=responses), \
                    patch.object(main, "_get_llm_response", return_value=""), \
                    patch.object(main, "_finish_learning_run", side_effect=lambda run, receipts, task,
                                 result, records, tokens, started: result):
                proposal = main._chat_turn("Create and deploy a local acceptance page")
                assert "Deploy local acceptance page (v1)" in proposal, proposal
                reply = main._chat_turn("accept plan")

            assert "Deployment verified" in reply, reply
            assert [task["status"] for task in main.tasks.tasks] == ["succeeded"] * 3
            receipts = store.list_events(
                "execution.receipt", workspace_id="acceptance", session_id="release",
            )
            assert [event["payload"]["action"] for event in reversed(receipts)] == [
                "write_file", "write_file", "run_cmd",
            ]
            state = main._interaction_controller.state()
            assert state["executing_plan"] is None
            assert state["preference_mode"] == "plan"
            assert state["effective_mode"] == "plan"
            assert any(
                event.get("event") == "interaction"
                and event.get("interaction", {}).get("effective_mode") == "agent"
                for event in events
            )
            assert (root / "index.html").read_text(encoding="utf-8") == page
            assert (root / "test_deploy.py").is_file()
            with socket.socket() as sock:
                assert sock.connect_ex(("127.0.0.1", port)) != 0
        finally:
            main._approval_callback.reset(approval_token)
            main._stream_event_callback.reset(stream_token)
            (main.tasks, main._interaction_controller, main.learning_engine,
             previous_root, main._execution_capability_token) = original
            main._set_workspace_root(previous_root)


if __name__ == "__main__":
    main_acceptance()
    print("Agent workflow acceptance passed.")
