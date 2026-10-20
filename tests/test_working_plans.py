from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openkyrozen.persistence.store import EventStore
from openkyrozen.tasks.engine import TaskManager
from openkyrozen.tasks.models import WorkingPlan, requires_working_plan
from openkyrozen.tasks.worker import TaskWorker
from openkyrozen.persistence.database import SQLiteDatabase
from openkyrozen.app.bootstrap import build_application, build_memory
from openkyrozen.security.capabilities import issue_capability_token
from openkyrozen.tools.adapters import ToolAdapters
from openkyrozen.tools.models import ToolResult


class WorkingPlanModelTests(unittest.TestCase):
    def test_ordered_plan_edits_create_and_revise_at_expected_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="atomic-create")

            created = manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "inspect", "task": {
                    "description": "Inspect workspace", "acceptance": ["files identified"],
                }},
                {"op": "add", "ref": "test", "task": {
                    "description": "Run tests", "acceptance": ["tests pass"],
                }},
                {"op": "add_dependency", "task": "test", "depends_on": "inspect"},
            ])

            self.assertEqual(created["revision"], 1)
            self.assertEqual(len(created["tasks"]), 2)
            self.assertNotEqual(created["references"]["inspect"], created["references"]["test"])
            self.assertEqual(created["tasks"][1]["dependencies"], [created["references"]["inspect"]])
            revised = manager.edit_plan(
                plan_id=created["plan_id"], expected_revision=1, complexity="complex",
                operations=[{"op": "reorder", "tasks": [created["references"]["test"],
                                                               created["references"]["inspect"]]}],
            )
            self.assertEqual(revised["revision"], 2)
            self.assertEqual([task["id"] for task in revised["tasks"]], [
                created["references"]["test"], created["references"]["inspect"],
            ])
            recovered = TaskManager(store, workspace_id="project", session_id="atomic-create")
            recovered.recover()
            self.assertEqual(recovered.working_plan.revision, 2)
            self.assertEqual(list(recovered.working_plan.task_ids), [
                created["references"]["test"], created["references"]["inspect"],
            ])

    def test_stale_or_invalid_plan_edit_leaves_database_and_memory_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="stale-edit")
            created = manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "first", "task": {
                    "description": "First", "acceptance": ["done"],
                }},
            ])
            memory_before = (manager.working_plan, [dict(task) for task in manager.tasks])
            with self.assertRaisesRegex(ValueError, "revision"):
                manager.edit_plan(plan_id=created["plan_id"], expected_revision=0,
                                  complexity="complex", operations=[{"op": "add", "ref": "stale", "task": {
                                      "description": "Stale", "acceptance": ["must not persist"],
                                  }}])
            with self.assertRaisesRegex(ValueError, "empty"):
                manager.edit_plan(plan_id=created["plan_id"], expected_revision=1,
                                  complexity="complex", operations=[])
            with self.assertRaisesRegex(ValueError, "dependency"):
                manager.edit_plan(plan_id=created["plan_id"], expected_revision=1, complexity="complex", operations=[
                    {"op": "add_dependency", "task": "first", "depends_on": "missing"},
                ])
            self.assertEqual(manager.working_plan, memory_before[0])
            self.assertEqual(manager.tasks, memory_before[1])
            self.assertEqual(store.get_working_plan(
                user_id="local", workspace_id="project", session_id="stale-edit",
            )["revision"], 1)
            self.assertEqual(len(store.list_working_plan_tasks(
                created["plan_id"], user_id="local", workspace_id="project", session_id="stale-edit",
            )), 1)

    def test_plan_edit_rejects_incomplete_reorder_and_dependency_cycles(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="invalid-edits")
            created = manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "a", "task": {"description": "A", "acceptance": ["A"]}},
                {"op": "add", "ref": "b", "task": {"description": "B", "acceptance": ["B"]}},
                {"op": "add_dependency", "task": "b", "depends_on": "a"},
            ])
            with self.assertRaisesRegex(ValueError, "reorder"):
                manager.edit_plan(plan_id=created["plan_id"], expected_revision=1, complexity="complex", operations=[
                    {"op": "reorder", "tasks": [created["references"]["a"]]},
                ])
            with self.assertRaisesRegex(ValueError, "cycle"):
                manager.edit_plan(plan_id=created["plan_id"], expected_revision=1, complexity="complex", operations=[
                    {"op": "add_dependency", "task": created["references"]["a"],
                     "depends_on": created["references"]["b"]},
                ])
            self.assertEqual(manager.working_plan.revision, 1)

    def test_update_cancel_and_remove_dependency_operations_preserve_task_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="mutations")
            created = manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "first", "task": {"description": "First", "acceptance": ["first"]}},
                {"op": "add", "ref": "second", "task": {"description": "Second", "acceptance": ["second"]}},
                {"op": "add_dependency", "task": "second", "depends_on": "first"},
            ])
            first_id, second_id = created["references"]["first"], created["references"]["second"]
            revised = manager.edit_plan(plan_id=created["plan_id"], expected_revision=1, operations=[
                {"op": "update", "task": first_id, "fields": {"description": "Inspect first"}},
                {"op": "remove_dependency", "task": second_id, "depends_on": first_id},
                {"op": "cancel", "task": second_id},
            ])
            self.assertEqual(revised["revision"], 2)
            self.assertEqual(revised["tasks"][0]["description"], "Inspect first")
            self.assertEqual(revised["tasks"][1]["id"], second_id)
            self.assertEqual(revised["tasks"][1]["dependencies"], [])
            self.assertEqual(revised["tasks"][1]["status"], "cancelled")

    def test_plan_edits_preserve_evidence_and_require_new_task_for_changed_work(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="retain-evidence")
            created = manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "first", "task": {"description": "Inspect", "acceptance": ["files listed"]}},
            ])
            task_id = created["references"]["first"]
            evidence = manager.record_evidence(task_id=task_id, action="list_dir", result="src", success=True)
            with self.assertRaisesRegex(ValueError, "new task"):
                manager.edit_plan(plan_id=created["plan_id"], expected_revision=1, operations=[
                    {"op": "update", "task": task_id, "fields": {"description": "Inspect another directory"}},
                ])
            revised = manager.edit_plan(plan_id=created["plan_id"], expected_revision=1, operations=[
                {"op": "add", "ref": "second", "task": {"description": "Inspect another directory",
                                                               "acceptance": ["second directory listed"]}},
            ])
            self.assertEqual(revised["tasks"][0]["id"], task_id)
            self.assertEqual(revised["tasks"][0]["evidence"], [evidence])
            self.assertEqual(revised["tasks"][0]["status"], "pending")

    def test_executed_task_dependencies_cannot_be_changed_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="executed-dependency")
            created = manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "first", "task": {"description": "First", "acceptance": ["first"]}},
                {"op": "add", "ref": "second", "task": {"description": "Second", "acceptance": ["second"]}},
            ])
            task_id = created["references"]["first"]
            manager.record_evidence(task_id=task_id, action="read_file", result="read", success=True,
                                    acceptance="first")
            with self.assertRaisesRegex(ValueError, "executed task"):
                manager.edit_plan(plan_id=created["plan_id"], expected_revision=1, operations=[
                    {"op": "add_dependency", "task": task_id, "depends_on": created["references"]["second"]},
                ])
            self.assertEqual(manager.working_plan.revision, 1)
            self.assertEqual(manager.tasks[0]["dependencies"], [])
            self.assertTrue(manager.tasks[0]["evidence"])

    def test_duplicate_batch_references_are_rejected_before_plan_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="duplicate-refs")
            with self.assertRaisesRegex(ValueError, "unique"):
                manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                    {"op": "add", "ref": "same", "task": {"description": "A", "acceptance": ["A"]}},
                    {"op": "add", "ref": "same", "task": {"description": "B", "acceptance": ["B"]}},
                ])
            self.assertIsNone(store.get_working_plan(
                user_id="local", workspace_id="project", session_id="duplicate-refs",
            ))

    def test_batch_reference_cannot_shadow_existing_task_id(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="shadow-ref")
            created = manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "first", "task": {"description": "First", "acceptance": ["first"]}},
            ])
            task_id = created["references"]["first"]
            with self.assertRaisesRegex(ValueError, "reference"):
                manager.edit_plan(plan_id=created["plan_id"], expected_revision=1, operations=[
                    {"op": "add", "ref": task_id, "task": {"description": "Replacement", "acceptance": ["new"]}},
                    {"op": "cancel", "task": task_id},
                ])
            self.assertEqual(manager.tasks[0]["status"], "pending")

    def test_edit_preserves_legacy_checkpoint_and_acceptance_shapes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="legacy-checkpoint")
            created = manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "first", "task": {"description": "First", "acceptance": ["first"]}},
            ])
            task = manager.tasks[0]
            task["acceptance"] = [{"action": "read_file", "args": "README.md"}]
            task["checkpoint"] = {"action": "run_cmd", "args": "pytest -q", "ordered_plan": {"id": "legacy"}}
            manager._persist(task)
            revised = manager.edit_plan(plan_id=created["plan_id"], expected_revision=1, operations=[
                {"op": "update", "task": task["id"], "fields": {"priority": 2}},
            ])
            self.assertEqual(revised["tasks"][0]["checkpoint"], task["checkpoint"])
            self.assertEqual(revised["tasks"][0]["acceptance"], task["acceptance"])
            changed_execution = manager.edit_plan(plan_id=created["plan_id"], expected_revision=2, operations=[
                {"op": "update", "task": task["id"], "fields": {
                    "execution": {"action": "run_cmd", "arguments": {"command": "pytest -q"}},
                }},
            ])
            self.assertEqual(changed_execution["tasks"][0]["checkpoint"]["ordered_plan"], {"id": "legacy"})
            self.assertEqual(changed_execution["tasks"][0]["checkpoint"]["args"], '{"command":"pytest -q"}')

    def test_legacy_done_status_does_not_block_plan_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="legacy-status-edit")
            created = manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "first", "task": {"description": "First", "acceptance": ["first"]}},
            ])
            with store.database.connection() as db:
                db.execute("UPDATE tasks SET status='done' WHERE id=?", (created["references"]["first"],))
            revised = manager.edit_plan(plan_id=created["plan_id"], expected_revision=1, operations=[
                {"op": "add", "ref": "second", "task": {"description": "Second", "acceptance": ["second"]}},
            ])
            self.assertEqual(revised["revision"], 2)

    def test_foreground_execution_obeys_dependencies_after_reorder(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="foreground-dependency")
            created = manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "first", "task": {"description": "Inspect source", "acceptance": ["inspect"],
                                                                    "execution": {"action": "read_file", "arguments": {"path": "a.py"}}}},
                {"op": "add", "ref": "second", "task": {"description": "Inspect dependent", "acceptance": ["dependent"],
                                                                     "execution": {"action": "read_file", "arguments": {"path": "b.py"}}}},
                {"op": "add_dependency", "task": "second", "depends_on": "first"},
                {"op": "reorder", "tasks": ["second", "first"]},
            ])
            second_id = created["references"]["second"]
            self.assertIsNone(manager.active_working_task(second_id))
            allowed, _ = manager.mutation_matches_current_task("read_file", '{"path":"b.py"}')
            self.assertFalse(allowed)

    def test_cancelled_replaced_step_does_not_prevent_plan_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = TaskManager(EventStore(Path(directory) / "state.sqlite3"),
                                  workspace_id="project", session_id="cancel-replacement")
            created = manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "old", "task": {"description": "Old step", "acceptance": ["old"]}},
                {"op": "add", "ref": "last", "task": {"description": "Last step", "acceptance": ["last"]}},
            ])
            old_id, last_id = created["references"]["old"], created["references"]["last"]
            revised = manager.edit_plan(plan_id=created["plan_id"], expected_revision=1, operations=[
                {"op": "cancel", "task": old_id},
                {"op": "add", "ref": "replacement", "task": {"description": "Replacement", "acceptance": ["replacement"]}},
            ])
            for task_id, acceptance in ((last_id, "last"), (revised["references"]["replacement"], "replacement")):
                index = next(i for i, item in enumerate(manager.tasks) if item["id"] == task_id)
                self.assertTrue(manager.set_status(index, "succeeded", evidence={"success": True, "acceptance": acceptance}))
            self.assertEqual(manager.working_plan.status, "completed")

    def test_edit_cancelling_every_member_terminalizes_plan_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = TaskManager(EventStore(Path(directory) / "state.sqlite3"),
                                  workspace_id="project", session_id="cancel-all-edit")
            created = manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "one", "task": {"description": "One", "acceptance": ["one"]}},
                {"op": "add", "ref": "two", "task": {"description": "Two", "acceptance": ["two"]}},
            ])
            result = manager.edit_plan(plan_id=created["plan_id"], expected_revision=1, operations=[
                {"op": "cancel", "task": created["references"]["one"]},
                {"op": "cancel", "task": created["references"]["two"]},
            ])
            self.assertEqual(result["status"], "cancelled")
            self.assertIsNone(manager.store.get_working_plan(
                user_id=manager.user_id, workspace_id=manager.workspace_id, session_id=manager.session_id,
            ))

    def test_stale_manager_cannot_terminalize_a_newer_plan_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            stale = TaskManager(store, workspace_id="project", session_id="terminal-race")
            created = stale.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "one", "task": {"description": "One", "acceptance": ["one"]}},
            ])
            fresh = TaskManager(store, workspace_id="project", session_id="terminal-race")
            fresh.recover()
            fresh.edit_plan(plan_id=created["plan_id"], expected_revision=1, operations=[
                {"op": "add", "ref": "two", "task": {"description": "Two", "acceptance": ["two"]}},
            ])
            self.assertTrue(stale.set_status(0, "succeeded", evidence={"success": True, "acceptance": "one"}))
            saved = store.get_working_plan(user_id=stale.user_id, workspace_id=stale.workspace_id,
                                          session_id=stale.session_id)
            members = store.list_working_plan_tasks(saved["id"], user_id=stale.user_id,
                                                    workspace_id=stale.workspace_id, session_id=stale.session_id)
            self.assertEqual(saved["revision"], 2)
            self.assertEqual([task["status"] for task in members], ["succeeded", "pending"])

    def test_clear_does_not_cancel_a_task_claimed_by_another_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            stale = TaskManager(store, workspace_id="project", session_id="clear-claim-race")
            stale.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "one", "task": {"description": "One", "acceptance": ["one"]}},
            ])
            worker = TaskManager(store, workspace_id="project", session_id="clear-claim-race")
            claimed = worker.claim_next()
            stale.clear()
            saved = store.get_working_plan(user_id=stale.user_id, workspace_id=stale.workspace_id,
                                          session_id=stale.session_id)
            self.assertIsNotNone(saved)
            saved_task = next(task for task in store.list_tasks(
                user_id=stale.user_id, workspace_id=stale.workspace_id, session_id=stale.session_id,
            ) if task["id"] == claimed["id"])
            self.assertEqual(saved_task["status"], "running")

    def test_mutation_match_does_not_fall_through_from_blocked_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = TaskManager(EventStore(Path(directory) / "state.sqlite3"),
                                  workspace_id="project", session_id="blocked-plan-scope")
            result = manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "first", "task": {"description": "First", "acceptance": ["first"]}},
                {"op": "add", "ref": "second", "task": {"description": "Second", "acceptance": ["second"]}},
                {"op": "add_dependency", "task": "second", "depends_on": "first"},
            ])
            manager.set_status(0, "failed")
            unrelated = manager.add_task("Unrelated pending task", task_id="outside-plan")
            allowed, _ = manager.mutation_matches_current_task("write_file", "outside-plan|content")
            self.assertFalse(allowed)

    def test_sessionless_working_plan_edit_keeps_event_session_null(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id=None)
            manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "one", "task": {"description": "One", "acceptance": ["one"]}},
            ])
            with store.database.connection() as db:
                session = db.execute("SELECT session_id FROM events WHERE event_type='working_plan.updated'").fetchone()[0]
            self.assertIsNone(session)

    def test_stale_worker_cannot_resurrect_cancelled_task(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            worker_manager = TaskManager(store, workspace_id="project", session_id="cancel-race")
            created = worker_manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "first", "task": {"description": "First", "acceptance": ["first"]}},
            ])
            task_id = created["references"]["first"]
            worker_manager.claim_next()
            editor = TaskManager(store, workspace_id="project", session_id="cancel-race")
            editor.recover()
            editor.edit_plan(plan_id=created["plan_id"], expected_revision=1,
                             operations=[{"op": "cancel", "task": task_id}])
            worker_manager.record_evidence(task_id=task_id, action="read_file", result="late", success=True,
                                            acceptance="first")
            index = next(i for i, item in enumerate(worker_manager.tasks) if item["id"] == task_id)
            self.assertFalse(worker_manager.set_status(index, "succeeded", evidence={"success": True, "acceptance": "first"}))
            saved = store.list_working_plan_tasks(created["plan_id"], user_id="local", workspace_id="project",
                                                   session_id="cancel-race")[0]
            self.assertEqual(saved["status"], "cancelled")

    def test_worker_finishing_after_concurrent_cancel_keeps_cancellation_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="worker-cancel")
            created = manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "first", "task": {"description": "First", "acceptance": ["first"]}},
            ])
            task_id = created["references"]["first"]

            def execute(_task):
                editor = TaskManager(store, workspace_id="project", session_id="worker-cancel")
                editor.recover()
                editor.edit_plan(plan_id=created["plan_id"], expected_revision=1,
                                 operations=[{"op": "cancel", "task": task_id}])
                return {"success": True, "result": "late result", "action": "read_file",
                        "acceptance": "first"}

            result = TaskWorker(manager, execute).run_once()
            self.assertEqual(result["status"], "cancelled")
            saved = store.list_working_plan_tasks(created["plan_id"], user_id="local", workspace_id="project",
                                                   session_id="worker-cancel")[0]
            self.assertEqual(saved["status"], "cancelled")
            self.assertEqual(saved["evidence"], [])

    def test_plan_claim_order_honors_reorder_and_unsatisfied_prerequisites(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="claim-order")
            created = manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "first", "task": {"description": "First", "acceptance": ["first"]}},
                {"op": "add", "ref": "second", "task": {"description": "Second", "acceptance": ["second"]}},
                {"op": "add_dependency", "task": "second", "depends_on": "first"},
            ])
            first_id, second_id = created["references"]["first"], created["references"]["second"]
            self.assertEqual(manager.claim_next()["id"], first_id)
            self.assertTrue(manager.set_status(0, "failed"))
            self.assertIsNone(manager.claim_next())
            self.assertEqual(store.list_working_plan_tasks(
                created["plan_id"], user_id="local", workspace_id="project", session_id="claim-order",
            )[1]["status"], "pending")

    def test_reordered_ready_tasks_claim_in_explicit_plan_order(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="reorder-claim")
            created = manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "a", "task": {"description": "A", "acceptance": ["A"]}},
                {"op": "add", "ref": "b", "task": {"description": "B", "acceptance": ["B"]}},
                {"op": "reorder", "tasks": ["b", "a"]},
            ])
            self.assertEqual(manager.claim_next()["id"], created["references"]["b"])

    def test_legacy_done_dependency_counts_as_satisfied(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="legacy-done")
            created = manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "first", "task": {"description": "First", "acceptance": ["first"]}},
                {"op": "add", "ref": "second", "task": {"description": "Second", "acceptance": ["second"]}},
                {"op": "add_dependency", "task": "second", "depends_on": "first"},
            ])
            first_id, second_id = created["references"]["first"], created["references"]["second"]
            with store.database.connection() as db:
                db.execute("UPDATE tasks SET status='done' WHERE id=?", (first_id,))
            self.assertEqual(manager.claim_next()["id"], second_id)

    def test_edit_rejects_progress_changed_after_candidate_was_read(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="claim-race")
            created = manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                {"op": "add", "ref": "first", "task": {"description": "First", "acceptance": ["first"]}},
            ])
            original_apply = store.apply_working_plan_edit
            def claim_before_commit(*args, **kwargs):
                store.claim_task(user_id="local", workspace_id="project", session_id="claim-race")
                return original_apply(*args, **kwargs)
            store.apply_working_plan_edit = claim_before_commit
            with self.assertRaisesRegex(ValueError, "progress changed"):
                manager.edit_plan(plan_id=created["plan_id"], expected_revision=1, complexity="complex", operations=[
                    {"op": "add", "ref": "second", "task": {"description": "Second", "acceptance": ["second"]}},
                ])
            self.assertEqual(manager.working_plan.revision, 1)
            self.assertEqual(len(manager.working_plan.task_ids), 1)
            self.assertEqual(len(store.list_working_plan_tasks(
                created["plan_id"], user_id="local", workspace_id="project", session_id="claim-race",
            )), 1)

    def test_edit_storage_failure_rolls_back_plan_tasks_and_event(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="edit-failure")
            original_json = store.database._json
            def fail_plan_event(value):
                if isinstance(value, dict) and value.get("revision") == 1 and value.get("plan_id"):
                    raise OSError("injected event failure")
                return original_json(value)
            store.database._json = fail_plan_event
            with self.assertRaisesRegex(OSError, "event failure"):
                manager.edit_plan(complexity="complex", expected_revision=0, operations=[
                    {"op": "add", "ref": "first", "task": {"description": "First", "acceptance": ["first"]}},
                ])
            store.database._json = original_json
            self.assertIsNone(manager.working_plan)
            self.assertIsNone(store.get_working_plan(
                user_id="local", workspace_id="project", session_id="edit-failure",
            ))
            self.assertEqual(store.list_tasks(user_id="local", workspace_id="project", session_id="edit-failure"), [])
            self.assertEqual(store.list_events(user_id="local", workspace_id="project", session_id="edit-failure"), [])

    def test_complexity_policy_only_requires_a_plan_for_complex_work(self):
        self.assertFalse(requires_working_plan("simple"))
        self.assertFalse(requires_working_plan("medium"))
        self.assertTrue(requires_working_plan("complex"))

    def test_task_status_transitions_are_explicit_and_terminal_states_stay_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = TaskManager(EventStore(Path(directory) / "state.sqlite3"),
                                  workspace_id="project", session_id="lifecycle")
            index = manager.add_task("inspect the repository")

            self.assertTrue(manager.set_status(index, "running"))
            self.assertFalse(manager.set_status(index, "succeeded"))
            self.assertTrue(manager.set_status(index, "failed"))
            self.assertTrue(manager.resume(manager.tasks[index]["id"]))
            self.assertTrue(manager.set_status(index, "succeeded", evidence={
                "success": True, "acceptance": "verified workspace inspection",
            }))
            self.assertFalse(manager.set_status(index, "running"))
            self.assertEqual(manager.tasks[index]["status"], "succeeded")

    def test_plan_completes_only_after_every_task_has_acceptance_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = TaskManager(EventStore(Path(directory) / "state.sqlite3"),
                                  workspace_id="project", session_id="complete-plan")
            plan = manager.update_plan(complexity="complex", tasks=[
                {"description": "First step", "acceptance": ["first result exists"]},
                {"description": "Second step", "acceptance": ["second result exists"]},
            ])
            manager.set_status(0, "succeeded", evidence={"success": True, "acceptance": "first result exists"})
            self.assertEqual(manager.working_plan.status, "active")

            manager.set_status(1, "succeeded", evidence={"success": True, "acceptance": "second result exists"})

            self.assertEqual(manager.working_plan.status, "completed")
            self.assertIsNone(manager.store.get_working_plan(
                user_id="local", workspace_id="project", session_id="complete-plan",
            ))
            with manager.store.database.connection() as db:
                self.assertEqual(db.execute(
                    "SELECT status FROM working_plans WHERE id=?", (plan.id,),
                ).fetchone()["status"], "completed")

    def test_recovery_reconciles_a_crash_after_last_task_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="crash-window")
            manager.update_plan(complexity="complex", tasks=[
                {"description": "Run acceptance checks", "acceptance": ["checks pass"]},
            ])
            completed = dict(manager.tasks[0])
            completed.update(
                status="succeeded",
                evidence=[{"success": True, "acceptance": "checks pass"}],
            )
            store.write_task(
                completed, user_id="local", workspace_id="project", session_id="crash-window",
            )

            recovered = TaskManager(store, workspace_id="project", session_id="crash-window")
            recovered.recover()

            self.assertEqual(recovered.working_plan.status, "completed")
            self.assertIsNone(store.get_working_plan(
                user_id="local", workspace_id="project", session_id="crash-window",
            ))

    def test_working_plan_update_uses_stable_runtime_ids_and_recovers_from_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="plan")

            plan = manager.update_plan(complexity="complex", tasks=[
                {"description": "Inspect the workspace", "acceptance": ["identify relevant files"]},
                {"description": "Run the focused checks", "acceptance": ["tests pass"]},
            ])

            self.assertIsInstance(plan, WorkingPlan)
            self.assertEqual(plan.complexity, "complex")
            self.assertEqual(len(plan.task_ids), 2)
            first_task_id = plan.task_ids[0]
            manager.set_status(0, "running")
            manager.update_plan(complexity="complex", tasks=[
                {"id": first_task_id, "description": "Inspect the workspace", "acceptance": ["identify relevant files"]},
                {"id": plan.task_ids[1], "description": "Run the focused checks", "acceptance": ["tests pass"]},
            ])
            self.assertEqual(manager.tasks[0]["status"], "running")
            with self.assertRaisesRegex(ValueError, "executed task"):
                manager.update_plan(complexity="complex", tasks=[
                    {"id": first_task_id, "description": "Inspect the current workspace", "acceptance": ["record relevant files"]},
                    {"id": plan.task_ids[1], "description": "Run focused checks", "acceptance": ["tests pass"]},
                ])
            self.assertTrue(manager.set_status(0, "cancelled"))
            revised = manager.update_plan(complexity="complex", tasks=[
                {"id": first_task_id, "description": "Inspect the workspace", "acceptance": ["identify relevant files"]},
                {"id": plan.task_ids[1], "description": "Run the focused checks", "acceptance": ["tests pass"]},
                {"description": "Inspect the current workspace", "acceptance": ["record relevant files"]},
            ])
            self.assertEqual(revised.id, plan.id)
            self.assertEqual(revised.task_ids[0], first_task_id)
            self.assertEqual(manager.tasks[0]["status"], "cancelled")
            self.assertEqual(manager.tasks[0]["evidence"], [])
            self.assertEqual(manager.tasks[2]["acceptance"], ["record relevant files"])

            recovered = TaskManager(store, workspace_id="project", session_id="plan")
            recovered.recover()
            self.assertEqual(recovered.working_plan, revised)
            self.assertEqual(recovered.tasks[0]["id"], first_task_id)

    def test_update_after_terminal_plan_creates_new_plan_instead_of_reopening_it(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="terminal-update")
            first = manager.update_plan(complexity="complex", tasks=[
                {"description": "first", "acceptance": ["first done"]},
            ])
            manager.set_status(0, "succeeded", evidence={"success": True, "acceptance": "first done"})

            second = manager.update_plan(complexity="complex", tasks=[
                {"description": "second", "acceptance": ["second done"]},
            ])

            self.assertNotEqual(first.id, second.id)
            self.assertEqual(second.status, "active")
            with store.database.connection() as db:
                self.assertEqual(db.execute("SELECT status FROM working_plans WHERE id=?", (first.id,)).fetchone()["status"], "completed")

    def test_clear_cancels_failed_and_blocked_tasks_in_the_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="clear-terminal-tasks")
            manager.update_plan(complexity="complex", tasks=[
                {"description": "failed", "acceptance": ["recover or cancel"]},
                {"description": "blocked", "acceptance": ["recover or cancel"]},
            ])
            manager.set_status(0, "failed")
            manager.set_status(1, "blocked")

            manager.clear()

            self.assertEqual([task["status"] for task in store.list_tasks(
                user_id="local", workspace_id="project", session_id="clear-terminal-tasks",
            )], ["cancelled", "cancelled"])

    def test_clear_does_not_complete_a_partially_succeeded_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="clear-partial-plan")
            plan = manager.update_plan(complexity="complex", tasks=[
                {"description": "first", "acceptance": ["first is verified"]},
                {"description": "second", "acceptance": ["second is verified"]},
            ])
            manager.set_status(0, "succeeded", evidence={
                "success": True, "acceptance": "first is verified",
            })

            manager.clear()

            with store.database.connection() as db:
                status = db.execute(
                    "SELECT status FROM working_plans WHERE id=?", (plan.id,),
                ).fetchone()["status"]
            self.assertEqual(status, "cancelled")

    def test_legacy_done_without_acceptance_evidence_cannot_complete_mixed_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="legacy-done-evidence")
            plan = manager.update_plan(complexity="complex", tasks=[
                {"description": "first", "acceptance": ["first is verified"]},
                {"description": "second", "acceptance": ["second is verified"]},
            ])
            with store.database.connection() as db:
                db.execute("UPDATE tasks SET status='done' WHERE id=?", (plan.task_ids[0],))
            manager.recover()
            self.assertTrue(manager.set_status(1, "cancelled"))
            with store.database.connection() as db:
                status = db.execute("SELECT status FROM working_plans WHERE id=?", (plan.id,)).fetchone()[0]
            self.assertEqual(status, "cancelled")

    def test_legacy_update_accepts_unchanged_execution_after_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = TaskManager(EventStore(Path(directory) / "state.sqlite3"),
                                  workspace_id="project", session_id="same-execution")
            task_data = {
                "description": "Read the source file", "acceptance": ["source was read"],
                "execution": {"action": "read_file", "arguments": {"path": "src/main.py"}},
            }
            manager.update_plan(complexity="complex", tasks=[task_data])
            task_id = manager.tasks[0]["id"]
            task = dict(manager.tasks[0])
            task["checkpoint"] = {
                **task["checkpoint"], "ordered_receipt": True,
                "args_sha256": "stable-fingerprint",
            }
            self.assertTrue(manager._persist(task, expected_status="pending"))
            manager.tasks[0].update(task)
            manager.record_evidence(task_id=task_id, action="read_file", args='{"path":"src/main.py"}',
                                    args_fingerprint="stable-fingerprint", result="source", success=True)

            manager.update_plan(complexity="complex", tasks=[{"id": task_id, **task_data}])

            self.assertEqual(manager.tasks[0]["checkpoint"]["args_sha256"], "stable-fingerprint")

    def test_working_plan_isolated_by_user_workspace_and_session(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            owner = TaskManager(store, user_id="alice", workspace_id="project", session_id="one")
            other = TaskManager(store, user_id="alice", workspace_id="project", session_id="two")
            owner.update_plan(complexity="complex", tasks=[{"description": "owner task", "acceptance": []}])

            other.recover()

            self.assertIsNone(other.working_plan)
            self.assertEqual(other.tasks, [])

    def test_clear_cancels_old_plan_before_recovering_a_new_complex_request(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="new-request")
            old_plan = manager.update_plan(complexity="complex", tasks=[
                {"description": "Old request step", "acceptance": ["old result exists"]},
            ])

            manager.clear()
            recovered = TaskManager(store, workspace_id="project", session_id="new-request")
            recovered.recover()

            self.assertIsNone(recovered.working_plan)
            self.assertEqual(recovered.tasks, [])
            with store.database.connection() as db:
                self.assertEqual(db.execute(
                    "SELECT status FROM working_plans WHERE id=?", (old_plan.id,),
                ).fetchone()["status"], "cancelled")

            new_plan = recovered.update_plan(complexity="complex", tasks=[
                {"description": "New request step", "acceptance": ["new result exists"]},
            ])
            self.assertNotEqual(new_plan.id, old_plan.id)
            self.assertEqual([task["description"] for task in recovered.tasks], ["New request step"])

    def test_legacy_plan_adapter_preserves_existing_task_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            manager = TaskManager(store, workspace_id="project", session_id="legacy")
            manager.from_llm_block('TaskList:\n```json\n["Inspect the workspace"]\n```')
            task_id = manager.tasks[0]["id"]

            plan = manager.adopt_legacy_plan()

            self.assertEqual(plan.task_ids, (task_id,))
            recovered = TaskManager(store, workspace_id="project", session_id="legacy")
            recovered.recover()
            self.assertEqual(recovered.working_plan.id, plan.id)

    def test_migration_version_includes_working_plan_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(Path(directory) / "state.sqlite3")
            self.assertEqual(database.SCHEMA_VERSION, 6)
            with database.connection() as db:
                self.assertIsNotNone(db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='working_plans'"
                ).fetchone())
                self.assertIsNotNone(db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='working_plan_tasks'"
                ).fetchone())
                self.assertIn("revision", {row["name"] for row in db.execute("PRAGMA table_info(working_plans)")})

    def test_schema_four_working_plan_is_upgraded_without_losing_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = EventStore(path)
            manager = TaskManager(store, workspace_id="project", session_id="schema-four")
            plan = manager.update_plan(complexity="complex", tasks=[
                {"description": "Preserve me", "acceptance": ["still present"]},
            ])
            with store.database.connection() as db:
                db.execute("ALTER TABLE working_plans DROP COLUMN revision")
                db.execute("DELETE FROM schema_migrations WHERE version>=5")
            migrated = SQLiteDatabase(path)
            recovered = TaskManager(EventStore(path), workspace_id="project", session_id="schema-four")
            recovered.recover()
            self.assertEqual(recovered.working_plan.id, plan.id)
            self.assertEqual(recovered.working_plan.revision, 0)
            self.assertEqual(recovered.tasks[0]["description"], "Preserve me")
            self.assertEqual(migrated.SCHEMA_VERSION, 6)

    def test_update_plan_is_a_typed_registry_tool_with_object_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            memory = build_memory(path, user_id="alice", index=None)
            app = build_application(memory=memory, tools=ToolAdapters(directory), user_id="alice")
            runtime = app.runtime
            registry = runtime.tool_registry
            spec = registry.get_spec("update_plan")
            self.assertEqual(spec.input_schema["type"], "object")
            self.assertIn("operations", spec.input_schema["properties"])
            token = issue_capability_token("test", {"write"})

            missing_revision = registry.invoke_result("update_plan", {
                "operations": [{"op": "add", "ref": "bad", "task": {
                    "description": "Must not persist", "acceptance": ["never created"],
                }}],
            }, token)
            self.assertFalse(missing_revision.success)
            self.assertEqual(missing_revision.error.code, "invalid_arguments")
            self.assertIsNone(runtime.tasks.working_plan)

            mixed = registry.invoke_result("update_plan", {
                "operations": [{"op": "add", "ref": "canonical", "task": {
                    "description": "Canonical branch", "acceptance": ["canonical"],
                }}],
                "expected_revision": 0,
                "complexity": "complex",
                "tasks": [{"description": "Legacy branch", "acceptance": ["legacy"]}],
            }, token)
            self.assertFalse(mixed.success)
            self.assertEqual(mixed.error.code, "invalid_arguments")
            self.assertIsNone(runtime.tasks.working_plan)

            result = registry.invoke_result("update_plan", {
                "complexity": "complex",
                "tasks": [{"description": "Inspect the repository", "acceptance": ["files are identified"]}],
            }, token)

            self.assertIsInstance(result, ToolResult)
            self.assertTrue(result.success)
            self.assertEqual(result.data["complexity"], "complex")
            self.assertEqual(len(result.data["task_ids"]), 1)
            self.assertNotIn("tool_registry", result.data)
            plan_id = result.data["plan_id"]

            canonical = registry.invoke_result("update_plan", {
                "plan_id": plan_id, "expected_revision": 1,
                "operations": [{"op": "add", "ref": "verify", "task": {
                    "description": "Verify result", "acceptance": ["verification passes"],
                }}],
            }, token)
            self.assertTrue(canonical.success)
            self.assertEqual(canonical.data["revision"], 2)
            self.assertEqual(len(canonical.data["tasks"]), 2)
            updated = registry.invoke_result("update_plan", {
                "plan_id": plan_id, "expected_revision": 2,
                "operations": [{"op": "update", "task": canonical.data["task_ids"][0],
                                "fields": {"description": "Inspect the updated repository"}}],
            }, token)
            self.assertTrue(updated.success, updated.error)
            self.assertEqual(updated.data["revision"], 3)
            first_id, second_id = updated.data["task_ids"]
            for revision, operation in enumerate((
                {"op": "add_dependency", "task": second_id, "depends_on": first_id},
                {"op": "remove_dependency", "task": second_id, "depends_on": first_id},
                {"op": "reorder", "tasks": [second_id, first_id]},
                {"op": "cancel", "task": second_id},
            ), start=3):
                result = registry.invoke_result("update_plan", {
                    "plan_id": plan_id,
                    "expected_revision": revision,
                    "operations": [operation],
                }, token)
                self.assertTrue(result.success, result.error)
                self.assertEqual(result.data["revision"], revision + 1)

    def test_runtime_call_persists_a_typed_working_plan(self):
        from openkyrozen.providers.models import ToolCall

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            memory = build_memory(path, user_id="alice", index=None)
            app = build_application(surface="cli", memory=memory, tools=ToolAdapters(directory), user_id="alice")
            try:
                receipt = app.runtime.execute_call(
                    app.runtime.current_session,
                    ToolCall("plan-call", "update_plan", {
                        "complexity": "complex",
                        "tasks": [{"description": "Inspect the repository", "acceptance": ["calculate the answer"],
                                   "execution": {"action": "calculate", "arguments": {"expression": "2+2"}}}],
                    }),
                )
                self.assertTrue(receipt.success)
                self.assertEqual(receipt.result_data["plan_id"], app.runtime.tasks.working_plan.id)
                self.assertEqual(receipt.result_data["task_ids"], list(app.runtime.tasks.working_plan.task_ids))
                self.assertEqual(receipt.as_dict()["result_data"], receipt.result_data)
                self.assertIsNotNone(app.runtime.tasks.working_plan)
                self.assertEqual(app.runtime.tasks.working_plan.task_ids, tuple(
                    task["id"] for task in app.runtime.tasks.tasks
                ))
                task = app.runtime.tasks.tasks[0]
                self.assertEqual(task["checkpoint"]["action"], "calculate")
                mismatched = app.runtime.execute_call(
                    app.runtime.current_session,
                    ToolCall("wrong-calc", "calculate", {"expression": "2+3"}),
                )
                self.assertTrue(mismatched.success)
                self.assertEqual(app.runtime.tasks.tasks[0]["status"], "pending")
                self.assertEqual(app.runtime.tasks.tasks[0]["evidence"], [])
                action_receipt = app.runtime.execute_call(
                    app.runtime.current_session,
                    ToolCall("calc-call", "calculate", {"expression": "2+2"}),
                    task_id=task["id"],
                )
                self.assertTrue(action_receipt.success)
                self.assertEqual(app.runtime.tasks.tasks[0]["evidence"][0]["task_id"], task["id"])
                self.assertEqual(app.runtime.tasks.tasks[0]["status"], "pending")
                self.assertEqual(app.runtime.tasks.working_plan.status, "active")
            finally:
                app.close()

    def test_structured_update_plan_is_not_advertised_as_a_legacy_text_action(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            memory = build_memory(path, user_id="alice", index=None)
            app = build_application(memory=memory, tools=ToolAdapters(directory), user_id="alice")
            try:
                prompt_tools = app.runtime._agent_prompt_tools_list({"capabilities": ["read", "write"]})
                self.assertNotIn("update_plan", prompt_tools)
                self.assertNotIn("update_plan", app.runtime._permitted_tool_names({"capabilities": ["read", "write"]}))
                self.assertTrue(app.runtime._discover_tools("update_plan").startswith("Error:"))
                self.assertIn("update_plan", app.runtime.tool_registry.specs)
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
