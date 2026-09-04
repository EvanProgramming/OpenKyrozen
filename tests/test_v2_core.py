import tempfile
import unittest
from pathlib import Path

from learning_engine import LearningEngine
from memory import MemoryBank
from task_engine import TaskManager, TaskWorker


class V2CoreTests(unittest.TestCase):
    def test_memory_persists_deduplicates_and_deletes_by_document_compatibly(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite3"
            first = MemoryBank(db_path, workspace_id="project-a")
            file_id = first.add_file("src/app.py", "print('hello')")
            self.assertEqual(file_id, first.add_file("src/app.py", "print('hello')"))
            first.add_log("FACT: durable memory", kind="fact")
            self.assertEqual(first.count_logs(), 1)

            reopened = MemoryBank(db_path, workspace_id="project-a")
            self.assertIn("FACT: durable memory", reopened.get_recent(10))
            self.assertEqual(reopened.delete_logs(["FACT: durable memory"]), 1)
            self.assertEqual(reopened.count_logs(), 0)

    def test_memory_isolated_by_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite3"
            MemoryBank(db_path, workspace_id="one").add_log("FACT: secret one", kind="fact")
            other = MemoryBank(db_path, workspace_id="two")
            self.assertEqual(other.get_recent(10), [])
            self.assertEqual(other.recall("secret"), [])

    def test_global_personal_state_can_share_while_file_snapshots_stay_project_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite3"
            first = MemoryBank(db_path, workspace_id="default", file_scope_id="source-one")
            second = MemoryBank(db_path, workspace_id="default", file_scope_id="source-two")
            first.add_log("FACT: shared personal preference", kind="fact")
            first.add_file("app.py", "print('one')")
            second.add_file("app.py", "print('two')")

            self.assertIn("FACT: shared personal preference", second.get_recent(10))
            self.assertEqual(first.remove_stale_files(set()), 1)
            remaining = second.store.connection()
            with remaining as db:
                rows = db.execute(
                    "SELECT content FROM files WHERE workspace_id=?", ("source-two",)
                ).fetchall()
            self.assertEqual([row["content"] for row in rows], ["print('two')"])

    def test_task_list_merge_and_evidence_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryBank(Path(directory) / "state.sqlite3")
            tasks = TaskManager(memory.store, workspace_id="project", session_id="session")
            index = tasks.add_task("run tests")
            block = 'TaskList:\n```json\n[{"description":"run tests"}]\n```'
            tasks.from_llm_block(block)
            self.assertEqual(len(tasks.tasks), 1)
            tasks.mark_done_from_text("TaskDone: 0")
            self.assertEqual(tasks.tasks[index]["status"], "pending")
            tasks.record_evidence(action="list_dir", result="OK", success=True)
            self.assertEqual(tasks.tasks[index]["status"], "pending")
            tasks.record_evidence(action="pytest", result="OK", success=True, acceptance="pytest passed")
            self.assertEqual(tasks.tasks[index]["status"], "succeeded")

    def test_tasks_are_collision_free_and_evidence_targets_one_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryBank(Path(directory) / "state.sqlite3").store
            alice = TaskManager(store, user_id="alice", workspace_id="project", session_id="alice-session")
            bob = TaskManager(store, user_id="bob", workspace_id="project", session_id="bob-session")
            alice_index = alice.add_task("deploy the service", task_id="step-1")
            bob_index = bob.add_task("deploy the service", task_id="step-1")
            self.assertNotEqual(alice.tasks[alice_index]["id"], bob.tasks[bob_index]["id"])

            alice.request_completion(alice_index)
            bob.request_completion(bob_index)
            alice.record_evidence(task_id=alice.tasks[alice_index]["id"], action="pytest",
                                  result="passed", success=True, acceptance="pytest passed")
            self.assertEqual(alice.tasks[alice_index]["status"], "succeeded")
            self.assertEqual(bob.tasks[bob_index]["status"], "pending")
            self.assertEqual(store.list_tasks(workspace_id="project", user_id="bob")[0]["status"], "pending")

    def test_running_task_recovery_is_scoped_and_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryBank(Path(directory) / "state.sqlite3").store
            owner = TaskManager(store, user_id="alice", workspace_id="project", session_id="session")
            index = owner.add_task("resume me")
            owner.set_status(index, "running")
            recovered = TaskManager(store, user_id="alice", workspace_id="project", session_id="session").recover()
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0]["status"], "pending")
            events = store.list_events(workspace_id="project", user_id="alice")
            self.assertTrue(any(event["event_type"] == "task.recovered" for event in events))
            self.assertEqual(TaskManager(store, user_id="bob", workspace_id="project", session_id="session").recover(), [])

    def test_task_worker_restarts_and_produces_a_real_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MemoryBank(root / "state.sqlite3").store
            manager = TaskManager(store, user_id="local", workspace_id="project", session_id="restart")
            manager.add_task("write marker", task_id="marker", checkpoint={"action": "write_file", "args": "marker.txt|ok"})

            def execute(task):
                (root / "marker.txt").write_text("ok", encoding="utf-8")
                return {"success": True, "action": "write_file", "args": "marker.txt|ok",
                        "result": "wrote marker", "acceptance": "marker exists"}

            result = TaskWorker(manager, execute).run_once()
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual((root / "marker.txt").read_text(encoding="utf-8"), "ok")
            reopened = TaskManager(store, user_id="local", workspace_id="project", session_id="restart")
            self.assertEqual(reopened.recover(), [])
            row = store.list_tasks(user_id="local", workspace_id="project", session_id="restart")[0]
            self.assertEqual(row["status"], "succeeded")
            self.assertTrue(any(event["event_type"] == "task.execution_completed"
                                for event in store.list_events(user_id="local", workspace_id="project")))

    def test_learning_requires_repeated_observation_and_can_roll_back(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryBank(Path(directory) / "state.sqlite3")
            engine = LearningEngine(memory)
            first = engine.submit("fact", "The project uses Python", evidence_id="event-1")
            self.assertEqual(first["status"], "candidate")
            second = engine.submit("fact", "The project uses Python", evidence_id="event-2")
            self.assertEqual(second["status"], "active")
            self.assertTrue(engine.rollback(second["proposal_id"]))
            self.assertEqual(memory.recall("project Python"), [])


if __name__ == "__main__":
    unittest.main()
