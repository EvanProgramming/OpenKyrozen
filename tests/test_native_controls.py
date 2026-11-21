import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openkyrozen.app.bootstrap import build_application, build_memory
from openkyrozen.tools import ToolAdapters
from openkyrozen.tools.models import tool_success


class NativeControlTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "workspace"
        self.root.mkdir()
        environment = patch.dict(os.environ, {"HOME": directory.name,
            "KYROZEN_WORKSPACE_ROOT": str(self.root),
            "KYROZEN_DB_PATH": str(self.root / "state.sqlite3"),
            "KYROZEN_SKILLS_PATH": str(self.root / "skills"),
            "KYROZEN_DISABLE_VECTOR_INDEX": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        self.app = build_application(memory=build_memory(self.root / "state.sqlite3", index=None),
                                     tools=ToolAdapters(self.root))
        self.addCleanup(self.app.close)
        self.runtime = self.app.runtime
        self.runtime.set_interaction_mode("agent")
        self.runtime.execution_context.native_control_input = "Revise the approved page outcome"
        self.runtime.execution_context.native_plan_inspected = True
        self.proposal = {"title": "Page", "summary": "Create one approved page", "bounds": {
            "workspace_paths": ["page.txt"], "capabilities": ["read", "write"],
            "effects": ["read_only", "filesystem"], "risk_ceiling": "normal",
        }, "steps": [{"id": "page", "title": "Page", "description": "Write page.txt", "acceptance": [
            {"criterion": "Page contains approved content", "verification": {
                "action": "read_file", "arguments": {"path": "page.txt"}, "expect": {"equals": "approved"},
            }}]}]}

    def accept(self):
        controller = self.runtime._interaction_controller
        pending = controller.propose_plan(self.proposal)
        return controller.accept_plan(self.runtime.tasks, plan_id=pending["plan_id"], version=1)[0]

    def execute_control(self, name, arguments):
        return self.runtime._execute_turn_action(name, arguments, operation_scope="native-control-test",
            successful_operations=set(), call_id="control", call_id_source="native")

    def test_native_material_proposal_preserves_bounds_and_pauses_mutations(self):
        accepted = self.accept()
        revised = copy.deepcopy(self.proposal)
        revised["summary"] = "Create a different page outcome"
        revised["bounds"]["workspace_paths"] = ["page.txt", "additional.txt"]
        receipt = self.execute_control("propose_plan", revised)
        self.assertTrue(receipt.success, receipt.result)
        state = self.runtime._interaction_controller.state()
        self.assertEqual(state["pending_plan"]["version"], 2)
        self.assertEqual(state["executing_plan"]["bounds"], accepted["bounds"])
        self.assertEqual(state["pending_plan"]["original_input"], "Revise the approved page outcome")
        self.assertIsNone(receipt.task_id)
        self.assertEqual(self.runtime.tasks.tasks[0]["evidence"], [])
        write = self.runtime._execute_turn_action("write_file", {"path": "page.txt", "content": "unapproved"},
            operation_scope="native-control-test", successful_operations=set(), call_id="blocked-write", call_id_source="native")
        self.assertFalse(write.success)
        self.assertFalse((self.root / "page.txt").exists())

    def test_nonmaterial_narrowing_updates_scope_without_requesting_new_approval(self):
        self.proposal["bounds"]["workspace_paths"] = ["."]
        accepted = self.accept()
        revised = copy.deepcopy(self.proposal)
        revised["bounds"]["workspace_paths"] = ["page.txt"]
        receipt = self.execute_control("propose_plan", revised)
        self.assertTrue(receipt.success, receipt.result)
        self.assertIs(receipt.result_data["requires_approval"], False)
        self.assertIn("within approved scope", receipt.result)
        state = self.runtime._interaction_controller.state()
        self.assertIsNone(state["pending_plan"])
        self.assertEqual(state["executing_plan"]["version"], accepted["version"] + 1)
        self.assertEqual(state["executing_plan"]["bounds"]["workspace_paths"], ["page.txt"])

    def test_dynamic_control_replacement_cannot_inherit_accepted_scope_exemption(self):
        self.accept()
        called = []
        registry = self.runtime.tool_registry
        original = registry.get_spec("propose_plan")
        def replacement(arguments):
            called.append(arguments)
            return tool_success("Replacement ran")
        registry.register_spec(original.replace(executor=replacement), replace=True)
        receipt = self.execute_control("propose_plan", self.proposal)
        self.assertFalse(receipt.success, receipt.result)
        self.assertEqual(called, [])

    def test_questions_preserve_pending_ui_and_cannot_request_secrets_or_approval(self):
        valid = {"questions": [{"id": "format", "header": "Format", "prompt": "Which page format?", "choices": [
            {"id": "plain", "label": "Plain"}, {"id": "rich", "label": "Rich"},
        ]}]}
        for prompt in ("What is your API key?", "May I execute this command?"):
            request = copy.deepcopy(valid)
            request["questions"][0]["prompt"] = prompt
            receipt = self.execute_control("request_question", request)
            self.assertFalse(receipt.success)
            self.assertIsNone(self.runtime._interaction_controller.state()["pending_question"])
        receipt = self.execute_control("request_question", valid)
        self.assertTrue(receipt.success, receipt.result)
        pending = self.runtime._interaction_controller.state()["pending_question"]
        self.assertEqual(pending["questions"][0]["id"], "format")
        self.assertEqual(pending["original_input"], "Revise the approved page outcome")
        self.assertIsNone(receipt.task_id)

    def test_child_and_noninteractive_surfaces_cannot_stage_controls(self):
        accepted = self.accept()
        request = {"questions": [{"id": "format", "header": "Format", "prompt": "Which format?", "choices": [
            {"id": "a", "label": "A"}, {"id": "b", "label": "B"},
        ]}]}
        for child, interactive in (("child", True), (None, False)):
            self.runtime.execution_context.child_run_id = child
            token = self.runtime._interaction_controls_enabled.set(interactive)
            try:
                self.assertFalse(self.execute_control("propose_plan", self.proposal).success)
                self.assertFalse(self.execute_control("request_question", request).success)
            finally:
                self.runtime._interaction_controls_enabled.reset(token)
        state = self.runtime._interaction_controller.state()
        self.assertIsNone(state["pending_question"])
        self.assertIsNone(state["pending_plan"])
        self.assertEqual(state["executing_plan"], accepted)
