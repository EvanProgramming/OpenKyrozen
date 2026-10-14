from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openkyrozen.persistence.store import EventStore
from openkyrozen.tasks.engine import TaskManager
from openkyrozen.tasks.models import WorkingPlan, requires_working_plan
from openkyrozen.persistence.database import SQLiteDatabase
from openkyrozen.app.bootstrap import build_application, build_memory
from openkyrozen.security.capabilities import issue_capability_token
from openkyrozen.tools.adapters import ToolAdapters
from openkyrozen.tools.models import ToolResult


class WorkingPlanModelTests(unittest.TestCase):
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
            revised = manager.update_plan(complexity="complex", tasks=[
                {"id": first_task_id, "description": "Inspect the current workspace", "acceptance": ["record relevant files"]},
                {"id": plan.task_ids[1], "description": "Run focused checks", "acceptance": ["tests pass"]},
            ])
            self.assertEqual(revised.id, plan.id)
            self.assertEqual(revised.task_ids[0], first_task_id)
            self.assertEqual(manager.tasks[0]["status"], "pending")
            self.assertEqual(manager.tasks[0]["evidence"], [])
            self.assertEqual(manager.tasks[0]["acceptance"], ["record relevant files"])

            recovered = TaskManager(store, workspace_id="project", session_id="plan")
            recovered.recover()
            self.assertEqual(recovered.working_plan, revised)
            self.assertEqual(recovered.tasks[0]["id"], first_task_id)

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
            self.assertEqual(database.SCHEMA_VERSION, 4)
            with database.connection() as db:
                self.assertIsNotNone(db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='working_plans'"
                ).fetchone())
                self.assertIsNotNone(db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='working_plan_tasks'"
                ).fetchone())

    def test_update_plan_is_a_typed_registry_tool_with_object_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            memory = build_memory(path, user_id="alice", index=None)
            app = build_application(memory=memory, tools=ToolAdapters(directory), user_id="alice")
            runtime = app.runtime
            registry = runtime.tool_registry
            spec = registry.get_spec("update_plan")
            self.assertEqual(spec.input_schema["type"], "object")
            self.assertIn("tasks", spec.input_schema["required"])
            token = issue_capability_token("test", {"write"})

            result = registry.invoke_result("update_plan", {
                "complexity": "complex",
                "tasks": [{"description": "Inspect the repository", "acceptance": ["files are identified"]}],
            }, token)

            self.assertIsInstance(result, ToolResult)
            self.assertTrue(result.success)
            self.assertEqual(result.data["complexity"], "complex")
            self.assertEqual(len(result.data["task_ids"]), 1)
            self.assertNotIn("tool_registry", result.data)

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
                        "tasks": [{"description": "Inspect the repository", "acceptance": ["find relevant files"]}],
                    }),
                )
                self.assertTrue(receipt.success)
                self.assertIsNotNone(app.runtime.tasks.working_plan)
                self.assertEqual(app.runtime.tasks.working_plan.task_ids, tuple(
                    task["id"] for task in app.runtime.tasks.tasks
                ))
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
                self.assertIn("update_plan", app.runtime.tool_registry.specs)
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
