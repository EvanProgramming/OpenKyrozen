import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from memory import MemoryBank
from task_engine import TaskManager


class _StubLearning:
    def feedback_signal(self, _text):
        return None

    def route_profile(self, _text, _profile=None):
        return "coder"

    def begin_run(self, profile, _task, provider_model=None):
        return {"run_id": "bugfix-run", "profile": profile, "provider_model": provider_model}

    def artifact_context(self, _run):
        return "", []

    def task_signature(self, _profile, _text):
        return "bugfix-task"


class _NoopPluginRuntime:
    def turn_start(self, **_kwargs):
        return None

    def turn_end(self, **_kwargs):
        return None


def _action_response(action, args):
    return "Action: " + json.dumps({"action": action, "args": args})


class BugFixWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.original_memory = main.memory_bank
        self.original_tasks = main.tasks
        self.original_learning = main.learning_engine
        self.original_root = main._get_workspace_root()
        self.original_fix_outcomes = list(main._fix_outcomes)
        self.tempdirs = []

    def tearDown(self):
        main.memory_bank = self.original_memory
        main.tasks = self.original_tasks
        main.learning_engine = self.original_learning
        main._fix_outcomes[:] = self.original_fix_outcomes
        main._set_workspace_root(self.original_root)
        for directory in self.tempdirs:
            directory.cleanup()

    def _run_fake_bug_turn(self, responses):
        directory = tempfile.TemporaryDirectory()
        self.tempdirs.append(directory)
        root = Path(directory.name)
        main.memory_bank = MemoryBank(root / "state.sqlite3", workspace_id="bugfix", session_id="turn")
        main.tasks = TaskManager(main.memory_bank.store, workspace_id="bugfix", session_id="turn")
        main.learning_engine = _StubLearning()
        main._set_workspace_root(root)
        responses = [item.replace("__ROOT__", str(root)) for item in responses]
        with patch.object(main, "_plugin_runtime_for_surface", return_value=_NoopPluginRuntime()), \
             patch.object(main, "_classify_complexity", return_value="simple"), \
             patch.object(main, "_build_messages", return_value=[]), \
             patch.object(main, "_build_memory_context", return_value=""), \
             patch.object(main, "_call_llm_with_spinner", side_effect=responses), \
             patch.object(main, "_get_llm_response", return_value=""), \
             patch.object(main, "_finish_learning_run", side_effect=lambda run, receipts, task,
                          result, records, tokens, started: result):
            reply = main._chat_turn("bug: the command fails", clear_tasks=True)
        return root, reply, main.memory_bank.store

    def test_fake_provider_requires_real_reproduce_fix_and_verify_evidence(self):
        responses = [
            (
                "Hypothesis: the missing file is the cause.\n"
                + _action_response("run_cmd", "python -c \"raise SystemExit(1)\"")
            ),
            _action_response("write_file", "fixed.txt|fixed"),
            _action_response(
                "run_cmd",
                "python -c \"from pathlib import Path; assert Path('__ROOT__/fixed.txt').read_text() == 'fixed'\"",
            ),
            "The bug is fixed and verified successfully.",
        ]
        root, reply, store = self._run_fake_bug_turn(responses)
        self.assertEqual(reply, "The bug is fixed and verified successfully.")
        self.assertEqual((root / "fixed.txt").read_text(encoding="utf-8"), "fixed")
        stages = [
            event["payload"]["stage"]
            for event in store.list_events("bug_fix.stage", workspace_id="bugfix",
                                           session_id="turn", user_id="local")
        ]
        self.assertIn("reported", stages)
        self.assertIn("reproduced", stages)
        self.assertIn("diagnosed", stages)
        self.assertIn("hypothesized", stages)
        self.assertIn("fixed", stages)
        self.assertIn("verified", stages)
        self.assertEqual(stages[0], "explained")
        receipts = store.list_events("bug_fix.receipt", workspace_id="bugfix",
                                     session_id="turn", user_id="local")
        self.assertEqual({item["payload"]["stage"] for item in receipts},
                         {"reproduced", "fixed", "verified"})
        self.assertTrue(all(item["payload"]["task_id"] and item["payload"]["attempt_id"]
                            for item in receipts))

    def test_fake_provider_cannot_claim_success_without_verification(self):
        root, reply, store = self._run_fake_bug_turn([
            "The bug is fixed now.",
            "The bug is fixed now.",
            "The bug is fixed now.",
            "The bug is fixed now.",
        ])
        self.assertFalse(list(root.glob("fixed.txt")))
        self.assertIn("cannot claim this bug is fixed", reply.lower())
        latest = store.list_events("bug_fix.stage", workspace_id="bugfix",
                                   session_id="turn", user_id="local")[0]
        self.assertEqual(latest["payload"]["stage"], "blocked")
        self.assertEqual(store.list_events("bug_fix.receipt", workspace_id="bugfix",
                                           session_id="turn", user_id="local"), [])

    def test_failed_verification_blocks_and_feedback_keeps_attempt_ids(self):
        responses = [
            "Hypothesis: the missing file is the cause.\n"
            + _action_response("run_cmd", "python -c \"raise SystemExit(1)\""),
            _action_response("write_file", "fixed.txt|fixed"),
            _action_response("run_cmd", "python -c \"raise SystemExit(2)\""),
            "I could not verify the change.",
        ]
        _root, reply, store = self._run_fake_bug_turn(responses)
        self.assertIn("cannot claim this bug is fixed", reply.lower())
        latest = store.list_events("bug_fix.stage", workspace_id="bugfix",
                                   session_id="turn", user_id="local")[0]
        self.assertEqual(latest["payload"]["stage"], "blocked")
        state = main._latest_fix_workflow()
        main._track_fix_outcome("still broken", reply)
        feedback = store.list_events("bug_fix.feedback", workspace_id="bugfix",
                                     session_id="turn", user_id="local")[0]["payload"]
        self.assertEqual(feedback["feedback"], "failure")
        self.assertEqual(feedback["workflow_id"], state["workflow_id"])
        self.assertEqual(feedback["task_id"], state["task_id"])
        self.assertEqual(feedback["attempt_id"], state["attempt_id"])


if __name__ == "__main__":
    unittest.main()
