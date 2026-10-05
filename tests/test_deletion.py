from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openkyrozen.persistence.store import EventStore


class DeletionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.temp.name) / "state.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def add_history(self, *, user_id: str, workspace_id: str, session_id: str):
        self.store.insert_history_node({
            "id": f"hist_{session_id.replace('-', ''):0<32}"[:37],
            "snapshot_relpath": "history/example",
            "user_id": user_id, "workspace_id": workspace_id,
            "session_id": session_id, "created_at": "2026-10-05T00:00:00+00:00",
        }, set_head=True)

    def test_delete_session_removes_only_session_owned_records(self):
        self.store.append_event("session.message", {"content": "remove"}, user_id="local",
                                workspace_id="project-a", session_id="chat-a")
        self.store.append_event("session.message", {"content": "keep"}, user_id="local",
                                workspace_id="project-a", session_id="chat-b")
        self.store.append_event("shared", {"content": "keep"}, user_id="local",
                                workspace_id="project-a")
        self.store.write_task({"id": "task-a", "description": "remove", "status": "pending"},
                              user_id="local", workspace_id="project-a", session_id="chat-a")
        self.store.upsert_memory("chat-only", user_id="local", workspace_id="project-a",
                                 session_id="chat-a")
        self.store.upsert_memory("chat-only-global", user_id="local", workspace_id="global",
                                 session_id="chat-a")
        self.store.upsert_memory("shared", user_id="local", workspace_id="project-a")
        self.store.record_usage_attempt(attempt_id="attempt-a", provider="test", model="test",
                                        surface="tui", user_id="local", workspace_id="project-a",
                                        session_id="chat-a")
        self.add_history(user_id="local", workspace_id="project-a", session_id="chat-a")
        self.add_history(user_id="local", workspace_id="project-a", session_id="chat-b")

        self.store.delete_session_data(user_id="local", workspace_id="project-a", session_id="chat-a")

        self.assertEqual(self.store.list_events(workspace_id="project-a", session_id="chat-a"), [])
        self.assertEqual(len(self.store.list_events(workspace_id="project-a", session_id="chat-b")), 1)
        self.assertEqual(len(self.store.list_events(workspace_id="project-a")), 2)
        self.assertEqual(self.store.list_tasks(user_id="local", workspace_id="project-a", session_id="chat-a"), [])
        self.assertFalse(any(item["session_id"] == "chat-a" for item in
                             self.store.list_memories(user_id="local", workspace_id="project-a")))
        self.assertEqual(len(self.store.list_memories(user_id="local", workspace_id="project-a")), 1)
        self.assertEqual(self.store.list_memories(user_id="local", workspace_id="global"), [])
        self.assertEqual(self.store.list_usage_attempts(user_id="local", workspace_id="project-a", session_id="chat-a"), [])
        self.assertIsNone(self.store.history_head(user_id="local", workspace_id="project-a", session_id="chat-a"))
        self.assertIsNotNone(self.store.history_head(user_id="local", workspace_id="project-a", session_id="chat-b"))

    def test_delete_project_scope_removes_project_data_and_registry_only(self):
        self.store.append_event("session.message", {"content": "remove"}, user_id="local",
                                workspace_id="project-a", session_id="chat-a")
        self.store.append_event("session.message", {"content": "keep"}, user_id="local",
                                workspace_id="project-b", session_id="chat-b")
        self.store.append_event("tui.chat_metadata", {"title": "A"}, user_id="local",
                                workspace_id="project-a", session_id="chat-a")
        self.store.append_event("tui.chat_metadata", {"title": "B"}, user_id="local",
                                workspace_id="project-b", session_id="chat-b")
        self.store.append_event("tui.project_opened", {
            "path": "/work/project-a", "source_scope_id": "project-a",
        }, user_id="local", workspace_id="global")
        self.store.append_event("tui.project_opened", {
            "path": "/work/project-b", "source_scope_id": "project-b",
        }, user_id="local", workspace_id="global")
        self.store.upsert_memory("project-a", user_id="local", workspace_id="project-a")
        self.store.upsert_memory("project-b", user_id="local", workspace_id="project-b")

        deleted_sessions = self.store.delete_workspace_data(
            user_id="local", workspace_id="project-a", registry_workspace_id="global",
        )

        self.assertEqual(deleted_sessions, {"chat-a"})
        self.assertEqual(self.store.list_events(workspace_id="project-a"), [])
        self.assertEqual(len(self.store.list_events(workspace_id="project-b")), 2)
        self.assertEqual([event["payload"]["source_scope_id"] for event in
                          self.store.list_events("tui.project_opened", workspace_id="global")],
                         ["project-b"])
        self.assertEqual(self.store.list_memories(user_id="local", workspace_id="project-a"), [])
        self.assertEqual(len(self.store.list_memories(user_id="local", workspace_id="project-b")), 1)

    def test_delete_global_workspace_scope_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "global workspace"):
            self.store.delete_workspace_data(
                user_id="local", workspace_id="global", registry_workspace_id="global",
            )


if __name__ == "__main__":
    unittest.main()
