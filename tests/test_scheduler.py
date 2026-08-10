import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from event_store import EventStore
from scheduler import JobScheduler


class SchedulerTests(unittest.TestCase):
    def test_due_one_shot_is_claimed_once_and_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.sqlite3")
            scheduler = JobScheduler(store, workspace_id="project")
            seen = []
            scheduler.register_callback("test", lambda job: seen.append(job["id"]))
            run_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            job_id = scheduler.schedule_once("one-shot", run_at, payload={"type": "test"})
            self.assertEqual(scheduler.tick(), 1)
            self.assertEqual(seen, [job_id])
            self.assertEqual(scheduler.tick(), 0)
            events = store.list_events(workspace_id="project", limit=10)
            self.assertTrue(any(event["event_type"] == "scheduler.completed" for event in events))


if __name__ == "__main__":
    unittest.main()
