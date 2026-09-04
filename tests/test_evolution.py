import json
import tempfile
import unittest
from pathlib import Path

from learning_engine import LearningEngine
from memory import MemoryBank
from skill_registry import SkillRegistry


BODY = """# Pytest recovery

## Trigger
Use for Python test failures.

## Steps
1. Reproduce the failing test.
2. Fix the shared root cause.

## Verify
Run pytest and require a zero exit status.
"""


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.memory = MemoryBank(root / "state.sqlite3", workspace_id="project")
        self.registry = SkillRegistry(self.memory.store, workspace_id="project", root=root / "skills")
        self.engine = LearningEngine(self.memory, registry=self.registry)

    def tearDown(self):
        self.directory.cleanup()

    def _candidate(self):
        return self.engine.propose_artifact("source-run", {
            "name": "pytest-recovery", "description": "Recover recurring Python test failures",
            "artifact_type": "skill", "profile": "coder", "triggers": ["pytest", "tests"],
            "verification": ["pytest exits zero"], "body": BODY,
        })

    def _pass_replay(self, candidate):
        return self.engine.record_shadow_replay(
            candidate["proposal_id"], [{"case_id": "case-1", "verified_success": True}],
            [{"case_id": "case-1", "verified_success": True}],
        )

    def test_profile_isolation_canary_promotion_and_correction_rollback(self):
        candidate = self._candidate()
        self.assertEqual(candidate["status"], "canary")
        self._pass_replay(candidate)
        self.assertEqual(self.registry.match("researcher", "research pytest history"), [])

        for _ in range(2):
            run = self.engine.begin_run("coder", "run pytest tests")
            _, receipts = self.engine.artifact_context(run)
            self.assertEqual([item["skill_id"] for item in receipts], [candidate["skill_id"]])
            changes = self.engine.record_outcome(run, receipts, verified=True, success=True)

        self.assertIn("Promoted", changes[0])
        self.assertEqual(next(item for item in self.registry.list() if item["id"] == candidate["skill_id"])["status"], "active")

        run = self.engine.begin_run("coder", "run pytest tests")
        _, receipts = self.engine.artifact_context(run)
        changes = self.engine.record_outcome(run, receipts, verified=True, success=False, correction=True)
        self.assertIn("Rolled back", changes[0])
        self.assertEqual(next(item for item in self.registry.list() if item["id"] == candidate["skill_id"])["status"], "rolled_back")

    def test_learned_artifact_rejects_secrets_and_cannot_mutate_local_skill(self):
        rejected = self.engine.propose_artifact("source-run", {
            "name": "bad-secret", "artifact_type": "policy", "profile": "coder", "triggers": ["pytest"],
            "verification": ["tests pass"], "body": BODY + "\napi_key=supersecretvalue",
        })
        self.assertEqual(rejected["status"], "rejected")

        source = Path(self.directory.name) / "local"
        source.mkdir()
        (source / "SKILL.md").write_text("# User skill\n", encoding="utf-8")
        (source / "skill.json").write_text(json.dumps({
            "name": "local-user-skill", "version": "1.0.0", "profiles": ["coder"],
            "triggers": ["pytest"], "permissions": [],
        }), encoding="utf-8")
        local = self.registry.install(source)
        self.assertFalse(self.registry.rollback_learned(local["id"]))

    def test_refinement_records_predecessor_and_rejects_dynamic_tools(self):
        first = self._candidate()
        self._pass_replay(first)
        for _ in range(2):
            run = self.engine.begin_run("coder", "run pytest tests")
            _, receipts = self.engine.artifact_context(run)
            self.engine.record_outcome(run, receipts, verified=True, success=True)
        refined = self.engine.propose_artifact("next-run", {
            "name": "pytest-recovery", "artifact_type": "skill", "profile": "coder",
            "triggers": ["pytest"], "verification": ["pytest exits zero"],
            "body": BODY.replace("shared root cause", "smallest shared root cause"),
        })
        skill = next(item for item in self.registry.list() if item["id"] == refined["skill_id"])
        self.assertEqual(skill["manifest"]["parent_version"],
                         next(item for item in self.registry.list() if item["id"] == first["skill_id"])["version"])
        self.assertEqual([item["id"] for item in self.registry.match("coder", "run pytest")],
                         [refined["skill_id"]])
        rejected = self.engine.propose_artifact("bad-run", {
            "name": "dynamic", "artifact_type": "skill", "profile": "coder",
            "triggers": ["pytest"], "verification": ["pytest exits zero"],
            "body": BODY + "\nDefineTool: forbidden",
        })
        self.assertEqual(rejected["status"], "rejected")

    def test_profile_router_honours_override(self):
        self.assertEqual(self.engine.route_profile("fix the Python tests"), "coder")
        self.assertEqual(self.engine.route_profile("write a sourced market report"), "researcher")
        self.assertEqual(self.engine.route_profile("fix tests", "researcher"), "researcher")

    def test_verified_one_step_failure_is_reviewable(self):
        run = self.engine.begin_run("researcher", "write a sourced report")
        self.engine.complete_run(run, result="missing source", receipts=[], tools=[], tokens=5, latency=0.1)
        self.engine.record_outcome(run, [], verified=True, success=False)
        self.assertEqual(self.engine.pending_reviews(min_age_seconds=0)[0]["run_id"], run["run_id"])

    def test_secret_bearing_run_is_never_reviewed(self):
        run = self.engine.begin_run("coder", "debug api_key=supersecretvalue")
        self.engine.complete_run(run, result="failed", receipts=[], tools=[{"success": False}], tokens=5, latency=0.1)
        self.engine.record_outcome(run, [], verified=True, success=False)
        self.assertEqual(self.engine.pending_reviews(min_age_seconds=0), [])

    def test_promotion_requires_non_regressing_shadow_replay(self):
        candidate = self._candidate()
        for _ in range(2):
            run = self.engine.begin_run("coder", "run pytest tests")
            _, receipts = self.engine.artifact_context(run)
            self.engine.record_outcome(run, receipts, verified=True, success=True)
        skill = next(item for item in self.registry.list() if item["id"] == candidate["skill_id"])
        self.assertEqual(skill["status"], "canary")
        replay = self._pass_replay(candidate)
        self.assertTrue(replay["non_regressing"])
        run = self.engine.begin_run("coder", "run pytest tests")
        _, receipts = self.engine.artifact_context(run)
        changes = self.engine.record_outcome(run, receipts, verified=True, success=True)
        self.assertIn("Promoted", changes[0])
        card = self.engine.evidence_card(candidate["proposal_id"])
        self.assertEqual(card["revalidation_status"], "valid")

    def test_environment_drift_requires_revalidation(self):
        candidate = self._candidate()
        self._pass_replay(candidate)
        skill = next(item for item in self.registry.list() if item["id"] == candidate["skill_id"])
        skill["manifest"]["applicability"]["environment_hash"] = "different"
        self.memory.store.upsert_skill(
            name=skill["name"], version=skill["version"], path=skill["path"],
            description=skill["description"], permissions=[], status="active", source="learned",
            manifest=skill["manifest"], workspace_id="project",
        )
        run = self.engine.begin_run("coder", "run pytest tests")
        context, receipts = self.engine.artifact_context(run)
        self.assertEqual((context, receipts), ("", []))
        self.assertEqual(next(item for item in self.registry.list() if item["id"] == candidate["skill_id"])["status"], "canary")

    def test_correction_creates_regression_case(self):
        candidate = self._candidate()
        run = self.engine.begin_run("coder", "run pytest tests")
        _, receipts = self.engine.artifact_context(run)
        self.engine.record_outcome(run, receipts, verified=True, success=False, correction=True)
        events = self.memory.store.list_events("learning.regression_case_created", workspace_id="project")
        self.assertEqual(events[0]["payload"]["task_signature"], run["task_signature"])


if __name__ == "__main__":
    unittest.main()
