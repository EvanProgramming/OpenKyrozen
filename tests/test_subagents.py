import tempfile
import unittest
from pathlib import Path

from event_store import EventStore
from memory import MemoryBank
from subagents import AgentProfile, SubAgentManager


class SubAgentTests(unittest.TestCase):
    def test_profile_has_independent_session_memory_and_capabilities(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = MemoryBank(Path(directory) / "state.sqlite3", workspace_id="project")
            manager = SubAgentManager(memory, runner=lambda profile, task, context, tools: f"done:{profile.name}")
            manager.register(AgentProfile("tester", "Return a test result", "readonly"))
            result = manager.run("tester", "check the repository")
            self.assertTrue(result["run_id"].startswith("subagent_"))
            self.assertNotIn("write_file", result["tools"])
            events = memory.store.list_events(workspace_id="project", session_id=result["run_id"], limit=10)
            event_types = [event["event_type"] for event in events]
            self.assertIn("subagent.started", event_types)
            self.assertIn("subagent.completed", event_types)


if __name__ == "__main__":
    unittest.main()
