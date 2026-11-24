from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from benchmarks.prompt_worker import _accepted_scope_preserved, _working_plan_edits


class PromptWorkerPlanEvidenceTests(unittest.TestCase):
    def test_required_task_lifecycle_is_not_a_working_plan_revision(self):
        manager = Mock(user_id="u", workspace_id="w", session_id="s")

        def update(revision, operations):
            return {"event_type": "working_plan.updated", "task_id": None, "payload": {
                "plan_id": "plan-1", "revision": revision, "operations": [{"op": op} for op in operations],
            }}

        history = [update(1, ["add"]), update(2, ["start"]), update(3, ["suggest_complete"])]
        history.append({"event_type": "working_plan.cancelled", "task_id": "task-1",
                        "payload": {"plan_id": "plan-1"}})
        manager.store.list_events.side_effect = lambda **_scope: list(reversed(history))
        edits, revisions = _working_plan_edits(manager)
        self.assertEqual(len(edits), 3)
        self.assertEqual(revisions, 0)
        history.append(update(4, ["update", "start"]))
        edits, revisions = _working_plan_edits(manager)
        self.assertEqual(edits[-1]["operations"], ["update", "start"])
        self.assertEqual(revisions, 1)

    def test_plan_revision_evidence_follows_commit_order_when_the_clock_moves_back(self):
        from openkyrozen.persistence.store import EventStore
        from openkyrozen.tasks.engine import TaskManager

        with TemporaryDirectory() as directory, closing(EventStore(Path(directory) / "tasks.sqlite3")) as store:
            manager = TaskManager(store, workspace_id="project", session_id="clock-change")
            with patch("openkyrozen.tasks.engine.utc_now", return_value="2026-11-22T20:00:01+00:00"):
                plan = manager.edit_plan(expected_revision=0, operations=[
                    {"op": "add_read_check", "ref": "read", "path": "notes.txt"},
                ])
            with patch("openkyrozen.tasks.engine.utc_now", return_value="2026-11-22T20:00:00+00:00"):
                manager.edit_plan(plan_id=plan["plan_id"], expected_revision=plan["revision"], operations=[
                    {"op": "start", "task": plan["task_ids"][0]},
                ])
            edits, revisions = _working_plan_edits(manager)
            self.assertEqual([edit["revision"] for edit in edits], [1, 2])
            self.assertEqual(revisions, 0)

    def test_missing_fingerprints_cannot_match_legacy_plan_events_or_tasks(self):
        plan = {"plan_id": "plan-1", "version": 1, "fingerprint": None}
        history = [{"event_type": "interaction.plan_completed", "payload": {
            "plan_id": "plan-1", "version": 1, "status": "completed",
        }}]
        self.assertFalse(_accepted_scope_preserved(history, [None], plan, [{"status": "pending"}]))

    def test_completed_accepted_plan_preserves_scope_after_active_state_is_cleared(self):
        history = [{"event_type": "interaction.plan_completed", "payload": {
            "plan_id": "plan-1", "version": 2, "fingerprint": "accepted-plan", "status": "completed",
        }}]

        self.assertTrue(_accepted_scope_preserved(
            history, ["accepted-plan"],
            {"plan_id": "plan-1", "version": 2, "fingerprint": "accepted-plan"}, []))

    def test_cancelled_or_different_plan_does_not_preserve_scope(self):
        for fingerprint, version, status in (("accepted-plan", 2, "cancelled"),
                                             ("other-plan", 2, "completed"),
                                             ("accepted-plan", 1, "completed")):
            with self.subTest(fingerprint=fingerprint, version=version, status=status):
                history = [{"event_type": "interaction.plan_completed", "payload": {
                    "plan_id": "plan-1", "version": version,
                    "fingerprint": fingerprint, "status": status,
                }}]
                self.assertFalse(_accepted_scope_preserved(
                    history, ["accepted-plan"],
                    {"plan_id": "plan-1", "version": 2, "fingerprint": "accepted-plan"}, []))

    def test_scope_requires_an_accepted_plan_and_keeps_active_bindings(self):
        plan = {"plan_id": "plan-1", "version": 2, "fingerprint": "accepted-plan"}
        history = []
        tasks = [{"status": "succeeded", "strategic_binding": {"fingerprint": "accepted-plan"}}]

        self.assertFalse(_accepted_scope_preserved(history, [], plan, tasks))
        self.assertTrue(_accepted_scope_preserved(history, ["accepted-plan"], plan, tasks))
        tasks[0]["status"] = "cancelled"
        self.assertFalse(_accepted_scope_preserved(history, ["accepted-plan"], plan, tasks))


if __name__ == "__main__":
    unittest.main()
