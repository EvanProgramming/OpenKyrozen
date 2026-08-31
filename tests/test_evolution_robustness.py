import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from learning_engine import LearningEngine
from memory import MemoryBank
from skill_registry import SkillRegistry


BODY = """# Robust guidance

## Trigger
Use for pytest.

## Steps
1. Run the focused test.

## Verify
Require a zero exit status.
"""


class EvolutionRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.memory = MemoryBank(root / "state.sqlite3", workspace_id="project")
        self.registry = SkillRegistry(self.memory.store, workspace_id="project", root=root / "skills")
        self.engine = LearningEngine(self.memory, registry=self.registry)

    def tearDown(self):
        self.directory.cleanup()

    def _candidate(self, run_id="source"):
        return self.engine.propose_artifact(run_id, {
            "name": "robust-guide", "profile": "coder", "triggers": ["pytest"],
            "verification": ["pytest succeeds"], "body": BODY,
        })

    def test_model_bound_artifact_requires_matching_model_or_cross_model_proof(self):
        source = self.engine.begin_run("coder", "pytest", provider_model="provider:model-a")
        candidate = self._candidate(source["run_id"])
        self.registry.set_learned_status(candidate["skill_id"], "active")
        other = self.engine.begin_run("coder", "pytest", provider_model="provider:model-b")
        self.assertEqual(self.engine.artifact_context(other), ("", []))
        self.registry.set_learned_status(candidate["skill_id"], "active")
        paired = [
            {"case_id": "a", "verified_success": True, "provider_model": "provider:model-a"},
            {"case_id": "b", "verified_success": True, "provider_model": "provider:model-b"},
        ]
        self.engine.record_shadow_replay(candidate["proposal_id"], paired, paired)
        context, receipts = self.engine.artifact_context(other)
        self.assertIn("Robust guidance", context)
        self.assertEqual(receipts[0]["skill_id"], candidate["skill_id"])

    def test_verifier_reliability_detects_later_correction(self):
        run = self.engine.begin_run("coder", "pytest")
        self.engine.record_outcome(run, [], verified=True, success=True, source="test-verifier")
        self.engine.record_outcome(run, [], verified=True, success=False, correction=True, source="user_feedback")
        reliability = self.engine.verifier_reliability("test-verifier")
        self.assertEqual(reliability["false_accepts"], 1)
        self.assertEqual(reliability["reliability"], 0.0)

    def test_constitution_is_read_only_and_blocks_disallowed_artifact(self):
        path = Path(self.directory.name) / "constitution.json"
        path.write_text(json.dumps({"allowed_artifact_types": ["policy"]}))
        before = path.read_bytes()
        with patch.dict(os.environ, {"KYROZEN_LEARNING_CONSTITUTION": str(path)}):
            result = self._candidate()
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(self.engine.constitution()["allowed_artifact_types"], ["policy"])
        self.assertEqual(path.read_bytes(), before)

    def test_capsule_round_trip_never_activates_import(self):
        candidate = self._candidate()
        capsule = self.engine.export_capsule(candidate["proposal_id"])
        imported = self.engine.import_capsule(capsule)
        self.assertFalse(imported["active"])
        proposal = next(item for item in self.engine.status(100) if item["id"] == imported["proposal_id"])
        self.assertEqual(proposal["status"], "candidate")
        self.assertEqual(proposal["validation"]["stage"], "imported")

    def test_review_budget_prefers_verified_failure(self):
        routine = self.engine.begin_run("coder", "routine pytest")
        self.engine.complete_run(routine, result="done", receipts=[], tools=[{"success": True}, {"success": True}],
                                 tokens=10, latency=0.1)
        failure = self.engine.begin_run("coder", "important pytest")
        self.engine.complete_run(failure, result="failed", receipts=[], tools=[], tokens=5000, latency=0.1)
        self.engine.record_outcome(failure, [], verified=True, success=False)
        self.assertEqual(self.engine.pending_reviews(min_age_seconds=0, limit=1)[0]["run_id"], failure["run_id"])


if __name__ == "__main__":
    unittest.main()
