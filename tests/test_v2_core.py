import tempfile
import unittest
from pathlib import Path

from learning_engine import LearningEngine
from memory import MemoryBank
from task_engine import TaskManager


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
