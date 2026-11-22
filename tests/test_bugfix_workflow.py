import json
from contextlib import ExitStack, closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openkyrozen.app.bootstrap import build_application

_application = build_application(surface="cli")

main = _application.runtime
from openkyrozen.app.bootstrap import build_memory as MemoryBank
from openkyrozen.tasks.engine import TaskManager
from openkyrozen.agent.modes import InteractionController


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
        self.original_interaction = main._interaction_controller
        self.original_root = main._get_workspace_root()
        self.original_fix_outcomes = list(main._fix_outcomes)
        self.tempdirs = []
        self._sqlite_owners = ExitStack()
        self.addCleanup(self._sqlite_owners.close)

    def tearDown(self):
        if main.memory_bank is not self.original_memory:
            main.memory_bank.close()
        main.memory_bank = self.original_memory
        main.tasks = self.original_tasks
        main.learning_engine = self.original_learning
        main._interaction_controller = self.original_interaction
        main._fix_outcomes[:] = self.original_fix_outcomes
        main._set_workspace_root(self.original_root)
        self._sqlite_owners.close()
        for directory in self.tempdirs:
            directory.cleanup()

    def _run_fake_bug_turn(self, responses):
        directory = tempfile.TemporaryDirectory()
        self.tempdirs.append(directory)
        root = Path(directory.name)
        with patch.dict("os.environ", {"KYROZEN_DISABLE_VECTOR_INDEX": "1"}):
            main.memory_bank = self._sqlite_owners.enter_context(closing(
                MemoryBank(root / "state.sqlite3", workspace_id="bugfix", session_id="turn")))
        main.tasks = TaskManager(main.memory_bank.store, workspace_id="bugfix", session_id="turn")
        main._interaction_controller = InteractionController(
            main.memory_bank.store, workspace_id="bugfix", session_id="turn",
        )
        main._interaction_controller.set_mode("agent")
        main.learning_engine = _StubLearning()
        main._set_workspace_root(root)
        responses = [item.replace("__ROOT__", str(root)) for item in responses]
        with patch.object(main, "_plugin_runtime_for_surface", return_value=_NoopPluginRuntime()), \
             patch.object(main, "_classify_complexity", return_value="simple"), \
             patch.object(main, "_build_messages", return_value=[]), \
             patch.object(main, "_build_memory_context", return_value=""), \
             patch.object(main, "_call_llm_with_spinner", side_effect=responses), \
             patch.object(main, "_get_llm_response", return_value=""), \
             patch.object(main, "_schedule_post_turn_learning"), \
             patch.object(main, "_finish_learning_run", side_effect=lambda run, receipts, task,
                          result, records, tokens, started: result):
            reply = main._chat_turn("bug: the command fails", clear_tasks=True)
        return root, reply, main.memory_bank.store

    def test_bugfix_fixture_does_not_schedule_unrelated_background_learning(self):
        with patch.object(main, "_schedule_post_turn_learning") as schedule:
            self._run_fake_bug_turn(["The bug is fixed now."] * 4)
        schedule.assert_not_called()

    def test_structured_source_edits_separate_reproduction_from_verification(self):
        failed = {"action": "run_cmd", "args": "python -m unittest", "result": "FAILED", "success": False}
        passed = {**failed, "result": "OK", "success": True}
        for tool in ("edit_file", "apply_patch"):
            with self.subTest(tool=tool):
                rejected = {"action": tool, "authorized": False, "success": False}
                accepted = {"action": tool, "authorized": True, "success": True}
                self.assertEqual(main._fix_verification_records([failed, rejected, passed]), [])
                self.assertEqual(main._fix_reproduction_records([failed, accepted, passed]), [failed])
                self.assertEqual(main._fix_verification_records([failed, accepted, passed]), [passed])

    def test_native_fix_recognizes_a_verified_rerun_without_hypothesis_prose(self):
        self._run_fake_bug_turn(["The bug is fixed now."] * 4)
        native = {"call_id_source": "native", "authorized": True}
        failed = {**native, "receipt_id": "reproduce", "operation_id": "original-command",
                  "action": "run_cmd", "result": "FAILED", "success": False, "failure": "command_failed",
                  "verification": {"method": "command_exit_status", "status": "failed"}}
        edit = {**native, "receipt_id": "edit", "action": "edit_file", "success": True,
                "verification": {"file_content_changed": True}}
        passed = {**failed, "receipt_id": "rerun", "result": "OK", "success": True, "failure": None,
                  "verification": {"method": "command_exit_status", "status": "verified"}}
        for label, records, expected in (
            ("verified", [failed, edit, passed], "explained"),
            ("unrelated check", [failed, edit, {**passed, "operation_id": "different-command"}], "blocked"),
            ("failed retry", [failed, edit, passed, {**failed, "receipt_id": "retry"}], "blocked"),
            ("stale check", [failed, edit, passed, {**edit, "receipt_id": "later-edit"}], "blocked"),
            ("later checkout", [failed, edit, passed, {"action": "git_checkout", "success": True,
                                                       "side_effects": "mutation"}], "blocked"),
            ("unproven edit", [failed, {**edit, "verification": {}}, passed], "blocked"),
        ):
            with self.subTest(label=label):
                request = "Fix the bug"
                answer = "The bug is fixed. The original tests passed."
                state, reply = main._advance_fix_workflow(main._start_fix_workflow(request), request, answer, records)
                self.assertEqual(state["stage"], expected)
                if expected == "explained":
                    self.assertEqual(reply, answer)
                    self.assertEqual(state["resolved_reproduction_receipts"], ["reproduce"])
                    self.assertEqual([item["receipt_id"] for item in state["evidence"]], ["reproduce", "edit", "rerun"])

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
