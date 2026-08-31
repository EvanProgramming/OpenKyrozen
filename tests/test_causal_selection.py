import tempfile
import unittest
from pathlib import Path

from learning_engine import LearningEngine
from memory import MemoryBank
from skill_registry import SkillRegistry


BODY = """# Guidance

## Trigger
Use for pytest failures.

## Steps
1. Inspect the shared cause.

## Verify
Run pytest successfully.
"""


class CausalSelectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.memory = MemoryBank(root / "state.sqlite3", workspace_id="project")
        self.registry = SkillRegistry(self.memory.store, workspace_id="project", root=root / "skills")
        self.engine = LearningEngine(self.memory, registry=self.registry)

    def tearDown(self):
        self.directory.cleanup()

    def _artifact(self, name):
        result = self.engine.propose_artifact(name, {
            "name": name, "profile": "coder", "triggers": ["pytest"],
            "verification": ["pytest succeeds"], "body": BODY.replace("Guidance", name),
        })
        self.registry.set_learned_status(result["skill_id"], "active")
        return result

    def test_repeated_pair_failure_prevents_co_injection(self):
        left, right = self._artifact("left-guide"), self._artifact("right-guide")
        receipts = [{"skill_id": left["skill_id"]}, {"skill_id": right["skill_id"]}]
        for _ in range(2):
            run = self.engine.begin_run("coder", "pytest")
            self.engine.record_outcome(run, receipts, verified=True, success=False)
        self.registry.set_learned_status(left["skill_id"], "active")
        self.registry.set_learned_status(right["skill_id"], "active")
        selected = self.registry.match("coder", "pytest")
        self.assertEqual(len(selected), 1)

    def test_non_regressing_omission_allows_reversible_retirement(self):
        artifact = self._artifact("retire-guide")
        cases = [{"case_id": "one", "verified_success": True}]
        result = self.engine.record_omission_trial(artifact["proposal_id"], cases, cases)
        self.assertTrue(result["retirement_eligible"])
        self.assertGreater(result["context_chars_saved"], 0)
        self.assertTrue(self.engine.retire_artifact(artifact["proposal_id"]))
        self.assertEqual(self.registry.match("coder", "pytest"), [])
        self.assertTrue(self.engine.restore_retired(artifact["proposal_id"]))
        self.assertEqual(len(self.registry.match("coder", "pytest")), 1)

    def test_regressing_omission_cannot_retire(self):
        artifact = self._artifact("keep-guide")
        with_item = [{"case_id": "one", "verified_success": True}]
        without_item = [{"case_id": "one", "verified_success": False}]
        result = self.engine.record_omission_trial(artifact["proposal_id"], with_item, without_item)
        self.assertFalse(result["retirement_eligible"])
        self.assertFalse(self.engine.retire_artifact(artifact["proposal_id"]))

    def test_success_after_preflight_counts_prevented_near_miss(self):
        artifact = self._artifact("preflight-guide")
        first = self.engine.begin_run("coder", "pytest")
        _, receipts = self.engine.artifact_context(first)
        self.engine.record_outcome(first, receipts, verified=True, success=False, correction=True)
        second = self.engine.begin_run("coder", "pytest")
        self.engine.artifact_context(second)
        self.engine.record_outcome(second, [], verified=True, success=True)
        self.assertEqual(self.engine.metrics()["prevented_near_misses"], 1)


if __name__ == "__main__":
    unittest.main()
