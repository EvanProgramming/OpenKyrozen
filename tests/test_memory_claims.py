import tempfile
import unittest
from pathlib import Path

from learning_engine import LearningEngine
from memory import MemoryBank
from skill_registry import SkillRegistry


class MemoryClaimTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.memory = MemoryBank(root / "state.sqlite3", workspace_id="project")
        self.registry = SkillRegistry(self.memory.store, workspace_id="project", root=root / "skills")
        self.engine = LearningEngine(self.memory, registry=self.registry)

    def tearDown(self):
        self.directory.cleanup()

    def test_inferred_claim_needs_two_observations(self):
        first = self.engine.remember_claim(key="editor", value="vim", evidence_id="one")
        self.assertEqual(first["status"], "candidate")
        self.assertIsNone(self.engine.resolve_claim("editor"))
        second = self.engine.remember_claim(key="editor", value="vim", evidence_id="two")
        self.assertEqual(second["status"], "active")
        self.assertEqual(self.engine.resolve_claim("editor")["value"], "vim")

    def test_owner_claim_supersedes_and_scope_precedence_is_deterministic(self):
        old = self.engine.remember_claim(key="language", value="Python", authority="owner")
        self.engine.remember_claim(key="language", value="Rust", authority="owner")
        self.engine.remember_claim(key="language", value="Swift", authority="owner",
                                   scope="profile", scope_value="coder")
        self.assertEqual(self.engine.explain_claim(old["memory_id"])["status"], "superseded")
        self.assertEqual(self.engine.resolve_claim("language", profile="coder")["value"], "Swift")
        self.assertEqual(self.engine.resolve_claim("language", profile="researcher")["value"], "Rust")

    def test_conflicting_inference_requests_clarification(self):
        self.engine.remember_claim(key="format", value="table", authority="owner")
        candidate = self.engine.remember_claim(key="format", value="prose", evidence_id="one")
        self.assertTrue(candidate["needs_clarification"])
        again = self.engine.remember_claim(key="format", value="prose", evidence_id="two")
        self.assertEqual(again["status"], "candidate")
        self.assertTrue(again["needs_clarification"])

    def test_forget_deactivates_dependent_artifact(self):
        claim = self.engine.remember_claim(key="test command", value="pytest", authority="owner")
        body = """# Test\n\n## Trigger\nUse pytest.\n\n## Steps\n1. Run it.\n\n## Verify\nRequire success.\n"""
        artifact = self.engine.propose_artifact("run", {
            "name": "claim-dependent", "profile": "coder", "triggers": ["pytest"],
            "verification": ["pytest succeeds"], "body": body, "dependencies": [claim["memory_id"]],
        })
        self.assertTrue(self.engine.forget_claim(claim["memory_id"]))
        proposal = next(item for item in self.engine.status(100) if item["id"] == artifact["proposal_id"])
        self.assertEqual(proposal["status"], "rejected")
        self.assertEqual(proposal["lifecycle_stage"], "rolled_back")

    def test_secret_claim_is_rejected(self):
        with self.assertRaises(ValueError):
            self.engine.remember_claim(key="api_key", value="supersecretvalue", authority="owner")

    def test_profile_scoped_claim_is_not_recalled_by_other_profile(self):
        self.engine.remember_claim(key="test runner", value="pytest", authority="owner",
                                   scope="profile", scope_value="coder")
        coder = self.memory.recall_records("test runner pytest", n_results=5, profile="coder")
        researcher = self.memory.recall_records("test runner pytest", n_results=5, profile="researcher")
        self.assertEqual(len(coder), 1)
        self.assertEqual(researcher, [])


if __name__ == "__main__":
    unittest.main()
