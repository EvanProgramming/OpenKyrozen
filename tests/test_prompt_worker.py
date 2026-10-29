import unittest

from benchmarks.prompt_worker import _accepted_scope_preserved


class PromptWorkerPlanEvidenceTests(unittest.TestCase):
    def test_completed_accepted_plan_preserves_scope_after_active_state_is_cleared(self):
        history = [{"event_type": "interaction.plan_completed", "payload": {
            "plan_id": "plan-1", "version": 2, "fingerprint": "accepted-plan", "status": "completed",
        }}]

        self.assertTrue(_accepted_scope_preserved(
            history, ["accepted-plan"],
            {"plan_id": "plan-1", "version": 2, "fingerprint": "accepted-plan"}, []))

    def test_cancelled_or_different_plan_does_not_preserve_scope(self):
        for fingerprint, version, status in (("accepted-plan", 2, "cancelled"),
                                             ("other-plan", 2, "completed"),
                                             ("accepted-plan", 1, "completed")):
            with self.subTest(fingerprint=fingerprint, version=version, status=status):
                history = [{"event_type": "interaction.plan_completed", "payload": {
                    "plan_id": "plan-1", "version": version,
                    "fingerprint": fingerprint, "status": status,
                }}]
                self.assertFalse(_accepted_scope_preserved(
                    history, ["accepted-plan"],
                    {"plan_id": "plan-1", "version": 2, "fingerprint": "accepted-plan"}, []))

    def test_scope_requires_an_accepted_plan_and_keeps_active_bindings(self):
        plan = {"plan_id": "plan-1", "version": 2, "fingerprint": "accepted-plan"}
        history = []
        tasks = [{"status": "succeeded", "strategic_binding": {"fingerprint": "accepted-plan"}}]

        self.assertFalse(_accepted_scope_preserved(history, [], plan, tasks))
        self.assertTrue(_accepted_scope_preserved(history, ["accepted-plan"], plan, tasks))
        tasks[0]["status"] = "cancelled"
        self.assertFalse(_accepted_scope_preserved(history, ["accepted-plan"], plan, tasks))


if __name__ == "__main__":
    unittest.main()
