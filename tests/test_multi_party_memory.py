import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from learning_engine import LearningEngine
from memory import MemoryBank
from fastapi.testclient import TestClient
import server


class MultiPartyMemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.memory = MemoryBank(Path(self.directory.name) / "state.sqlite3", workspace_id="project",
                                 session_id="session")
        self.engine = LearningEngine(self.memory)

    def tearDown(self):
        self.directory.cleanup()

    def _belief(self, speaker, value):
        return self.engine.remember_claim(
            key="release date", value=value, authority="owner", claim_type="attributed_belief",
            speaker=speaker, audiences=["team"], visibility="group",
        )

    def test_beliefs_remain_attributed_and_ambiguous_query_abstains(self):
        self._belief("alice", "Monday")
        self._belief("bob", "Tuesday")
        result = self.engine.resolve_claim("release date", audience="team")
        self.assertTrue(result["conflict"])
        self.assertIsNone(result["value"])
        self.assertEqual(result["attributed_values"], {"alice": "Monday", "bob": "Tuesday"})

    def test_update_supersedes_only_the_correct_speaker(self):
        alice_old = self._belief("alice", "Monday")
        bob = self._belief("bob", "Tuesday")
        self._belief("alice", "Wednesday")
        self.assertEqual(self.engine.explain_claim(alice_old["memory_id"])["status"], "superseded")
        self.assertEqual(self.engine.explain_claim(bob["memory_id"])["status"], "active")
        self.assertEqual(self.engine.resolve_claim("release date", speaker="alice", audience="team")
                         ["attributed_values"], {"alice": "Wednesday"})

    def test_private_claim_does_not_leak_to_another_speaker(self):
        claim = self.engine.remember_claim(
            key="medical note", value="diagnosis-7x", authority="owner", claim_type="private_fact",
            speaker="alice", audiences=["alice"], channel="direct",
        )
        alice = self.memory.recall_records(
            "medical note diagnosis-7x", n_results=5, speaker="alice", audience="alice", channel="direct",
            authorized_speakers={"alice"},
        )
        bob = self.memory.recall_records(
            "medical note diagnosis-7x", n_results=5, speaker="bob", audience="bob", channel="direct",
            authorized_speakers={"bob"},
        )
        self.assertEqual([item["id"] for item in alice], [claim["memory_id"]])
        self.assertEqual(bob, [])
        self.assertEqual(self.memory.recall("medical note diagnosis-7x", n_results=5), [])
        self.assertNotIn("diagnosis-7x", " ".join(self.memory.get_recent()))
        events = self.memory.store.list_events(workspace_id="project", session_id="session")
        self.assertNotIn("diagnosis-7x", str([item["payload"] for item in events]))

    def test_group_agreement_is_audience_and_channel_scoped(self):
        self.engine.remember_claim(
            key="deploy window", value="Friday", authority="owner", claim_type="group_agreement",
            audiences=["ops"], channel="release",
        )
        allowed = self.memory.recall_records(
            "deploy window Friday", n_results=5, audience="ops", channel="release")
        wrong_audience = self.memory.recall_records(
            "deploy window Friday", n_results=5, audience="sales", channel="release")
        wrong_channel = self.memory.recall_records(
            "deploy window Friday", n_results=5, audience="ops", channel="general")
        self.assertEqual(len(allowed), 1)
        self.assertEqual(wrong_audience, [])
        self.assertEqual(wrong_channel, [])

    def test_recall_receipt_explains_whose_memory_was_used(self):
        self._belief("alice", "Monday")
        self.memory.recall_records("release date Monday", n_results=5, audience="team")
        receipt = self.memory.store.list_events("memory.recalled", workspace_id="project", session_id="session")[0]
        self.assertEqual(receipt["payload"]["attributions"][0]["speaker"], "alice")

    def test_memory_bank_reads_are_scoped_to_its_user(self):
        alice = MemoryBank(self.memory.db_path, user_id="alice", workspace_id="project", session_id="alice-session")
        bob = MemoryBank(self.memory.db_path, user_id="bob", workspace_id="project", session_id="bob-session")
        memory_id = alice.add_log(
            "Private fact from alice — medical note: diagnosis-8x",
            kind="fact", metadata={"visibility": "private", "speaker": "alice", "claim_type": "private_fact"},
        )
        self.assertEqual(alice.get_all()[0], [memory_id])
        self.assertEqual(bob.get_all()[0], [])
        self.assertEqual(bob.store.list_events(workspace_id="project", user_id="bob"), [])

    def test_chroma_recall_filters_user_and_session_before_returning_documents(self):
        with patch.dict(os.environ, {"KYROZEN_VECTOR_PATH": str(Path(self.directory.name) / "chroma")}):
            alice = MemoryBank(self.memory.db_path, user_id="alice", workspace_id="shared",
                               session_id="alice-session")
            bob = MemoryBank(self.memory.db_path, user_id="bob", workspace_id="shared",
                             session_id="bob-session")
            self.assertIsNotNone(alice._collection)
            alice.add_log(
                "PRIVATE_MARKER_ALICE",
                kind="fact",
                metadata={"visibility": "private", "speaker": "alice"},
            )
            alice.add_log("SESSION_MARKER_ALICE", kind="fact")
            MemoryBank(self.memory.db_path, user_id="alice", workspace_id="shared").add_log(
                "GLOBAL_MARKER_ALICE", kind="fact"
            )

            self.assertEqual(bob.recall("PRIVATE_MARKER_ALICE", n_results=5), [])
            self.assertEqual(bob.recall_records("PRIVATE_MARKER_ALICE", n_results=5), [])
            other_session = MemoryBank(self.memory.db_path, user_id="alice", workspace_id="shared",
                                       session_id="other-session")
            self.assertNotIn("SESSION_MARKER_ALICE", other_session.recall("SESSION_MARKER_ALICE", n_results=5))
            self.assertIn("GLOBAL_MARKER_ALICE", alice.recall("GLOBAL_MARKER_ALICE", n_results=5))
            self.assertIn(
                "PRIVATE_MARKER_ALICE",
                [item["content"] for item in alice.recall_records(
                    "PRIVATE_MARKER_ALICE", n_results=5,
                    speaker="alice", authorized_speakers={"alice"})],
            )

    def test_api_memory_recall_does_not_return_another_user_vector_document(self):
        with tempfile.TemporaryDirectory(prefix="openkyrozen-memory-api-") as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"KYROZEN_VECTOR_PATH": str(root / "chroma")}):
                owner = MemoryBank(root / "state.sqlite3", user_id=server._SERVER_ACTOR_ID,
                                   workspace_id="api-project", session_id="api-session")
                other = MemoryBank(root / "state.sqlite3", user_id="other-user",
                                   workspace_id="api-project", session_id="api-session")
                other.add_log(
                    "API_PRIVATE_MARKER_OTHER",
                    kind="fact",
                    metadata={"visibility": "private", "speaker": "other-user"},
                )
                original = server._agent.memory_bank
                server._agent.memory_bank = owner
                try:
                    response = TestClient(server.app).get(
                        "/api/v2/memory",
                        params={"q": "API_PRIVATE_MARKER_OTHER", "session_id": "api-session"},
                    )
                finally:
                    server._agent.memory_bank = original
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["results"], [])


if __name__ == "__main__":
    unittest.main()
