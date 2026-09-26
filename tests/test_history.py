import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from event_store import EventStore
from history import HistoryError, HistoryManager
from interaction import InteractionController
from workspace_context import source_scope_id


class HistoryManagerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "workspace"
        self.state = Path(self.directory.name) / "state"
        self.root.mkdir()
        self.state.mkdir()
        self.store = EventStore(self.state / "history.sqlite3")
        self.manager = HistoryManager(
            self.store, self.root, self.state, source_scope_id=source_scope_id(self.root),
            user_id="alice", workspace_id="project", session_id="conversation",
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_branching_restore_preserves_files_descendants_memory_tasks_and_git(self):
        (self.root / "file.txt").write_text("before", encoding="utf-8")
        (self.root / "old.txt").write_text("old", encoding="utf-8")
        (self.root / ".hidden").write_bytes(b"\x00before\xff")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "HEAD").write_text("git-state-before", encoding="utf-8")
        os.chmod(self.root / "file.txt", 0o640)
        self.symlink_supported = False
        try:
            os.symlink("file.txt", self.root / "link")
            self.symlink_supported = True
        except (OSError, NotImplementedError):
            pass

        self.store.append_event("memory.test", {"content": "keep"}, user_id="alice", workspace_id="project")
        baseline = self.manager.ensure_root(
            conversation=[{"role": "assistant", "content": "baseline"}],
            interaction={"preference_mode": "auto", "pending_question": None},
            tasks=[{"id": "task-1", "description": "baseline", "status": "pending"}],
        )

        (self.root / "file.txt").write_text("first turn", encoding="utf-8")
        os.chmod(self.root / "file.txt", 0o600)
        (self.root / "old.txt").unlink()
        (self.root / "new.bin").write_bytes(bytes(range(256)))
        (self.root / ".hidden").write_bytes(b"\x01after\xfe")
        if self.symlink_supported:
            (self.root / "link").unlink()
            os.symlink("new.bin", self.root / "link")
        n1 = self.manager.commit_turn(
            self.manager.begin_turn(conversation=[], interaction={}, tasks=[]),
            user_message="first turn", assistant_message="done",
            conversation=[{"role": "user", "content": "first turn"}],
            interaction={"preference_mode": "plan", "pending_question": {"id": "q1"}},
            tasks=[{"id": "task-1", "description": "first", "status": "completed"}],
        )

        (self.root / "file.txt").write_text("second branch", encoding="utf-8")
        (self.root / "new.bin").unlink()
        (self.root / ".git" / "HEAD").write_text("git-state-current", encoding="utf-8")
        n2 = self.manager.commit_turn(
            self.manager.begin_turn(conversation=[], interaction={}, tasks=[]),
            user_message="second turn", assistant_message="branch",
            conversation=[{"role": "user", "content": "second turn"}],
            interaction={"preference_mode": "agent"},
            tasks=[{"id": "task-1", "description": "second", "status": "in_progress"}],
        )

        self.manager.rollback(baseline["id"], confirm="rollback", expected_head=n2["id"])
        self.assertEqual(self.manager.current()["id"], baseline["id"])
        self.assertEqual((self.root / "file.txt").read_text(encoding="utf-8"), "before")
        self.assertEqual((self.root / "old.txt").read_text(encoding="utf-8"), "old")
        self.assertFalse((self.root / "new.bin").exists())
        self.assertEqual((self.root / ".hidden").read_bytes(), b"\x00before\xff")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE((self.root / "file.txt").stat().st_mode), 0o640)
        self.assertEqual((self.root / ".git" / "HEAD").read_text(encoding="utf-8"), "git-state-current")
        if self.symlink_supported:
            self.assertEqual(os.readlink(self.root / "link"), "file.txt")
        self.assertEqual(self.store.list_tasks(user_id="alice", workspace_id="project", session_id="conversation")[0]["status"], "pending")
        self.assertEqual(len(self.store.list_events("memory.test", user_id="alice", workspace_id="project")), 1)

        (self.root / "branch.txt").write_text("new branch", encoding="utf-8")
        n3 = self.manager.commit_turn(
            self.manager.begin_turn(conversation=[], interaction={}, tasks=[]),
            user_message="new branch", assistant_message="branch",
            conversation=[], interaction={}, tasks=[],
        )
        nodes = {node["id"]: node for node in self.manager.list()}
        self.assertIn(n1["id"], nodes)
        self.assertIn(n2["id"], nodes)
        self.assertEqual(nodes[n3["id"]]["parent_id"], baseline["id"])

        self.manager.rollback(n1["id"], confirm="rollback", expected_head=n3["id"])
        self.assertEqual(self.manager.current()["id"], n1["id"])
        self.assertEqual((self.root / "file.txt").read_text(encoding="utf-8"), "first turn")
        self.assertTrue((self.root / "new.bin").is_file())
        self.assertFalse((self.root / "old.txt").exists())
        self.assertEqual(
            InteractionController(self.store, user_id="alice", workspace_id="project", session_id="conversation").state()["preference_mode"],
            "plan",
        )

        reopened = HistoryManager(
            EventStore(self.state / "history.sqlite3"), self.root, self.state,
            source_scope_id=source_scope_id(self.root), user_id="alice", workspace_id="project",
            session_id="conversation",
        )
        self.assertEqual(reopened.current()["id"], n1["id"])

    def test_scope_and_stale_head_are_rejected(self):
        root = self.manager.ensure_root()
        other = HistoryManager(
            self.store, self.root, self.state, source_scope_id=source_scope_id(self.root),
            user_id="bob", workspace_id="project", session_id="conversation",
        )
        self.assertIsNone(other.current())
        with self.assertRaises(HistoryError):
            self.manager.rollback(root["id"], confirm="nope")
        with self.assertRaises(HistoryError):
            self.manager.rollback(root["id"], confirm="rollback", expected_head="hist_stale")

    def test_failed_restore_returns_to_recovery_snapshot_and_head(self):
        baseline = self.manager.ensure_root()
        (self.root / "file.txt").write_text("current", encoding="utf-8")
        current = self.manager.commit_turn(
            self.manager.begin_turn(conversation=[], interaction={}, tasks=[]),
            user_message="current", assistant_message="done", conversation=[], interaction={}, tasks=[],
        )
        original_restore = self.manager._restore
        calls = []

        def fail_after_partial_restore(node):
            if not calls:
                calls.append(node["id"])
                original_restore(node)
                raise HistoryError("simulated restore failure")
            original_restore(node)

        with patch.object(self.manager, "_restore", side_effect=fail_after_partial_restore):
            with self.assertRaises(HistoryError):
                self.manager.rollback(baseline["id"], confirm="rollback", expected_head=current["id"])
        self.assertEqual(self.manager.current()["id"], current["id"])
        self.assertEqual((self.root / "file.txt").read_text(encoding="utf-8"), "current")

    def test_invalid_source_scope_cannot_escape_snapshot_root(self):
        with self.assertRaises(HistoryError):
            HistoryManager(self.store, self.root, self.state, source_scope_id=str(self.root), session_id="x")


if __name__ == "__main__":
    unittest.main()
