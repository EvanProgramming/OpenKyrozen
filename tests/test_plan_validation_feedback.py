"""Native planner feedback and explicit read verification; no real provider."""
from contextlib import ExitStack, closing
from copy import deepcopy
from dataclasses import replace
from functools import wraps
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from openkyrozen.app.bootstrap import build_application, build_memory
from openkyrozen.agent.prompts import _refresh_working_plan_context
from openkyrozen.providers import ProviderConfig
from openkyrozen.providers.base import LLMProvider
from openkyrozen.providers.models import FinishReason, ModelResponse, ToolCall, ToolReply
from openkyrozen.providers.models import provider_identity, validate_tool_history
from openkyrozen.providers.models import provider_continuation
from openkyrozen.security.capabilities import issue_capability_token
from openkyrozen.tools import ToolAdapters
from openkyrozen.tools.models import invoke_tool_result, legacy_text_api, tool_failure, tool_success


class PlanValidationFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.owners = ExitStack()
        self.addCleanup(self.owners.close)
        home = self.owners.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(home) / "workspace"
        self.root.mkdir()
        self.owners.enter_context(patch.dict(os.environ, {
            "HOME": home, "KYROZEN_WORKSPACE_ROOT": str(self.root),
            "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_APPROVAL_MODE": "never",
        }))
        memory = self.owners.enter_context(closing(build_memory(Path(home) / "state.sqlite3", index=None)))
        self.app = build_application(surface="cli", memory=memory, tools=ToolAdapters(self.root))
        self.owners.callback(self.app.close)
        self.runtime = self.app.runtime
        self.runtime._set_workspace_root(self.root)
        self.runtime.set_interaction_mode("agent")
        self.registry = self.runtime.tool_registry
        self.token = issue_capability_token("fixture", {"read", "write"})
        self.content = "def sorted_products(names):\n    return sorted(names, key=lambda n: (n.casefold(), n))\n"
        (self.root / "catalog.py").write_text(self.content)

    def invoke(self, arguments):
        return self.registry.invoke_result("update_plan", arguments, self.token)

    def task(self, expected=None):
        return {"description": "Inspect catalog.py", "acceptance": [{
            "criterion": "Exact catalog source inspected",
            "verification": {"action": "read_file", "arguments": {"path": "catalog.py"},
                             "expect": {"equals": self.content if expected is None else expected}},
        }]}

    def create_read_task(self, expected=None):
        result = self.invoke({"complexity": "simple", "expected_revision": 0,
            "operations": [{"op": "add", "ref": "inspect", "task": self.task(expected)}]})
        self.assertTrue(result.success, result.text)
        return result.data

    def start(self, plan):
        result = self.invoke({"plan_id": plan["plan_id"], "expected_revision": self.runtime.tasks.working_plan.revision,
            "operations": [{"op": "start", "task": plan["task_ids"][0]}]})
        self.assertTrue(result.success, result.text)

    def read(self, call_id="read", path="catalog.py"):
        task_id = self.runtime.tasks.execution_task_id
        return self.runtime.execute_call(self.runtime.current_session,
            ToolCall(call_id, "read_file", {"path": path}), task_id=task_id)

    def test_compact_read_check_requires_bound_complete_read(self):
        result = self.invoke({"expected_revision": 0, "operations": [
            {"op": "add_read_check", "ref": "inspect", "path": "catalog.py"},
            {"op": "start", "task": "inspect"},
        ]})
        self.assertTrue(result.success, result.text)
        task = self.runtime.tasks.tasks[0]
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["acceptance"][0]["expect"], {"complete": True})
        (self.root / "different.py").write_text(self.content)
        self.assertTrue(self.read("different", "different.py").success)
        self.assertEqual(self.runtime.tasks.tasks[0]["status"], "running")
        self.assertTrue(self.read("exact", "catalog.py").success)
        self.assertEqual(self.runtime.tasks.tasks[0]["status"], "succeeded")

    def test_pinned_plan_contracts_can_be_copied_into_a_native_edit(self):
        task = self.task()
        task["execution"] = {"action": "read_file", "arguments": {"path": "catalog.py"}}
        created = self.invoke({"expected_revision": 0, "operations": [
            {"op": "add", "ref": "inspect", "task": task},
        ]})
        self.assertTrue(created.success, created.text)
        messages = [{"role": "user", "content": "Inspect catalog.py"}]
        _refresh_working_plan_context(self.runtime, messages)
        guidance = next(item["content"] for item in messages
                        if item["content"].startswith("## Pinned Working Plan state\n"))
        plan = json.loads(guidance.splitlines()[-1])
        pinned = plan["tasks"][0]
        copied = {name: pinned[name] for name in ("execution", "acceptance")}
        reused = self.invoke({"plan_id": plan["plan_id"], "expected_revision": plan["revision"],
            "operations": [{"op": "update", "task": pinned["id"], "fields": copied}]})
        self.assertTrue(reused.success, reused.text)
        self.assertEqual(copied, {name: task[name] for name in copied})

    def test_pinned_plan_retains_legacy_execution_as_descriptive_state(self):
        created = self.create_read_task()
        execution = {"action": "read_file", "args": "catalog.py"}
        self.runtime.tasks.update_checkpoint(created["task_ids"][0], execution)
        messages = [{"role": "user", "content": "Inspect catalog.py"}]
        _refresh_working_plan_context(self.runtime, messages)
        guidance = next(item["content"] for item in messages
                        if item["content"].startswith("## Pinned Working Plan state\n"))
        pinned = json.loads(guidance.splitlines()[-1])["tasks"][0]
        self.assertIsNone(pinned["execution"])
        self.assertEqual(pinned["legacy_execution"], execution)

    def test_captured_mixed_shapes_report_actionable_schema_paths_without_execution(self):
        # Exact first investigation-1 request retained in the d668 snapshot.
        arguments = {"complexity": "simple", "expected_revision": 0, "plan_id": "catalog-read",
            "reason": "Read-only inspection task; single minimal step.", "tasks": [{
                "id": "inspect", "description": "Read catalog.py and locate the product-sorting function",
                "acceptance": ["catalog.py contents read", "sorting function identified with exact tie-break rule quoted from source"],
            }]}
        result = self.invoke(arguments)
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "invalid_arguments")
        self.assertIn("operations", result.error.message)
        self.assertIn("allowed properties: complexity, tasks", result.error.message)
        self.assertIsNone(self.runtime.tasks.working_plan)
        self.assertEqual(self.runtime.tasks.tasks, [])

    def test_captured_new_id_rejects_with_current_state_and_omission_guidance(self):
        # Exact complete later request, not a reconstructed truncated receipt.
        arguments = {"complexity": "simple", "tasks": [{"id": "inspect-catalog",
            "description": "Read catalog.py read-only and identify the product-sorting function with its exact tie-break rule.",
            "acceptance": ["catalog.py is read via an available read capability", "The sorting function's exact name and tie-break rule are quoted from source"]}]}
        result = self.invoke(arguments)
        self.assertFalse(result.success)
        self.assertTrue(result.error.retriable)
        self.assertIsNone(result.data["working_plan"])
        self.assertIn("omit", result.text.lower())
        self.assertIn("id", result.text)
        self.assertEqual(self.runtime.tasks.tasks, [])
        del arguments["tasks"][0]["id"]
        created = self.invoke(arguments)
        self.assertTrue(created.success, created.text)
        self.assertEqual(created.data["revision"], 1)
        self.assertNotEqual(created.data["task_ids"][0], "inspect-catalog")
        self.assertEqual(created.data["tasks"][0]["id"], created.data["task_ids"][0])
        wrong = self.invoke({"plan_id": "catalog-read", "expected_revision": 1,
            "operations": [{"op": "start", "task": "inspect"}]})
        self.assertFalse(wrong.success)
        self.assertEqual(wrong.data["working_plan"]["plan_id"], created.data["plan_id"])
        self.assertIsNone(self.runtime.tasks.execution_task_id)

    def test_schema_feedback_does_not_echo_unknown_keys_or_values(self):
        secret = "fixture-only-secret-not-an-argument-name"
        result = self.invoke({"complexity": "simple", "tasks": [], secret: secret})
        self.assertFalse(result.success)
        self.assertNotIn(secret, result.text)
        self.assertLess(len(result.text), 1200)

    def test_native_planner_receives_complete_guidance_and_authoritative_absent_state(self):
        schema = next(s for s in self.registry.provider_schemas(names={"update_plan"}) if s["name"] == "update_plan")
        self.assertIn("suggest_complete", schema["description"])
        self.assertIn("never supplies evidence", schema["description"])
        messages = []
        _refresh_working_plan_context(self.runtime, messages)
        self.assertEqual(len(messages), 1)
        self.assertIn('"plan_id": null', messages[0]["content"])
        self.assertIn('"revision": 0', messages[0]["content"])
        self.assertIn("python -m unittest", messages[0]["content"])
        self.assertIn("Leave the test command open until inspection identifies the runner", messages[0]["content"])
        self.runtime.set_interaction_mode("ask")
        messages = []
        token = self.runtime._active_interaction_mode.set("ask")
        try:
            _refresh_working_plan_context(self.runtime, messages)
        finally:
            self.runtime._active_interaction_mode.reset(token)
        self.assertEqual(messages, [])

    def test_pending_plan_explains_why_unbound_reads_do_not_verify_it(self):
        plan = self.create_read_task()
        self.assertTrue(self.read(call_id="unbound").success)
        result = self.invoke({"plan_id": plan["plan_id"], "expected_revision": plan["revision"],
            "operations": [{"op": "suggest_complete", "task": plan["task_ids"][0]}]})
        self.assertTrue(result.success, result.text)
        self.assertIn("Start the pending task with update_plan before running its verification", result.text)
        self.assertIn("Tool calls before start are unbound", result.text)
        self.assertEqual(self.runtime.tasks.tasks[0]["status"], "pending")
        self.assertEqual(self.runtime.tasks.tasks[0]["evidence"], [])
        messages = []
        _refresh_working_plan_context(self.runtime, messages)
        self.assertIn("Start the next eligible task before running its verification", messages[0]["content"])
        self.assertIn("python -m unittest", messages[0]["content"])
        self.start(plan)
        self.assertTrue(self.read(call_id="selected").success)
        self.assertEqual(self.runtime.tasks.tasks[0]["status"], "succeeded")

    def test_active_replan_confirms_verified_work_and_adds_a_dependent_task_atomically(self):
        existing = self.invoke({"expected_revision": 0, "operations": [
            {"op": "add", "ref": "inspect", "task": self.task()},
            {"op": "add", "ref": "repair", "task": {"description": "Repair the issue", "acceptance": []}},
            {"op": "start", "task": "inspect"},
        ]})
        self.assertTrue(existing.success, existing.text)
        self.assertTrue(self.read().success)
        manager = self.runtime.tasks
        snapshot = manager.working_plan_snapshot()
        verified, repair = (task["id"] for task in snapshot["tasks"])
        self.assertEqual(snapshot["tasks"][0]["status"], "succeeded")
        scope = {"user_id": manager.user_id, "workspace_id": manager.workspace_id,
                 "session_id": manager.session_id}
        original = manager.store.list_working_plan_tasks(snapshot["plan_id"], **scope)[0]
        edit = {"op": "add", "ref": "verify", "task": {**self.task(), "dependencies": [repair]}}
        arguments = {"plan_id": snapshot["plan_id"], "expected_revision": snapshot["revision"],
                     "reason": "Add follow-up verification", "operations": [
                         {"op": "suggest_complete", "task": verified}, edit,
                     ]}
        for anchor in (repair, verified):
            with self.subTest(anchor=anchor):
                arguments["operations"][1] = {**edit, "anchor": anchor}
                rejected = self.invoke(arguments)
                self.assertFalse(rejected.success)
                self.assertIn("omit anchor and use task.dependencies", rejected.text)
                self.assertNotIn("Omit task.id", rejected.text)
                self.assertEqual(manager.working_plan_snapshot(), snapshot)

        arguments["operations"][1] = edit
        added = self.invoke(arguments)
        self.assertTrue(added.success, added.text)
        self.assertEqual(added.data["revision"], snapshot["revision"] + 1)
        durable = manager.store.list_working_plan_tasks(snapshot["plan_id"], **scope)
        self.assertEqual(durable[0], original)
        self.assertEqual(added.data["tasks"][-1]["dependencies"], [repair])
        self.assertEqual(added.data["tasks"][-1]["status"], "pending")
        self.assertFalse(self.invoke(arguments).success)

    def test_explicit_read_predicate_needs_start_and_exact_content_and_survives_reopen(self):
        plan = self.create_read_task()
        receipt = self.read()
        self.assertTrue(receipt.success)
        self.assertEqual(self.runtime.tasks.tasks[0]["status"], "pending")
        self.start(plan)
        self.read("bound")
        self.assertEqual(self.runtime.tasks.tasks[0]["status"], "succeeded")
        self.assertEqual(self.runtime.tasks.working_plan.status, "completed")
        from openkyrozen.persistence.store import EventStore
        tasks = self.runtime.tasks
        with closing(EventStore(self.runtime.memory_bank.store.database.path)) as reopened:
            restored = reopened.list_working_plan_tasks(plan["plan_id"], user_id=tasks.user_id,
                workspace_id=tasks.workspace_id, session_id=tasks.session_id)
        self.assertEqual(restored[0]["status"], "succeeded")
        self.assertEqual(restored[0]["acceptance"], tasks.tasks[0]["acceptance"])

    def test_read_predicate_does_not_match_response_metadata_wrong_path_or_failure(self):
        task = self.task()
        task["acceptance"][0]["verification"]["expect"] = {"contains": "catalog.py"}
        created = self.invoke({"complexity": "simple", "tasks": [task]})
        self.assertTrue(created.success, created.text)
        plan = created.data  # Metadata path is absent from actual content.
        self.start(plan)
        self.read()
        self.assertEqual(self.runtime.tasks.tasks[0]["status"], "running")
        (self.root / "other.py").write_text("catalog.py")
        self.read("wrong-path", "other.py")
        self.assertEqual(self.runtime.tasks.tasks[0]["status"], "running")
        self.read("denied", "../outside.py")
        self.assertEqual(self.runtime.tasks.tasks[0]["status"], "running")

    def test_full_read_with_explicit_limits_satisfies_selected_path_only_criterion(self):
        task = self.task()
        task["acceptance"][0]["verification"]["expect"] = {"contains": self.content.splitlines()[-1]}
        created = self.invoke({"complexity": "simple", "tasks": [
            task, {"description": "Legacy source check", "acceptance": ["Source checked"]},
        ]})
        self.assertTrue(created.success, created.text)
        self.runtime.tasks.update_checkpoint(created.data["task_ids"][1], {"action": "read_file", "args": "catalog.py"})
        self.start(created.data)
        session = self.runtime.current_session
        task_id = self.runtime.tasks.execution_task_id
        self.assertEqual(task_id, created.data["task_ids"][0])
        args = {"path": "catalog.py", "start_line": 1, "max_lines": 200, "max_chars": 4000}
        self.assertTrue(self.runtime.execute_call(session, ToolCall("unbound", "read_file", args)).success)
        self.assertEqual(self.runtime.tasks.tasks[0]["status"], "running")
        for call_id, limits in (("suffix", {"start_line": 2}), ("short", {"max_chars": 1})):
            receipt = self.runtime.execute_call(
                session, ToolCall(call_id, "read_file", {"path": "catalog.py", **limits}), task_id=task_id)
            self.assertTrue(receipt.success)
            self.assertEqual(self.runtime.tasks.tasks[0]["status"], "running")
        (self.root / "other.py").write_text(self.content)
        wrong = self.runtime.execute_call(
            session, ToolCall("wrong", "read_file", {**args, "path": "other.py"}), task_id=task_id)
        self.assertTrue(wrong.success)
        self.assertEqual(self.runtime.tasks.tasks[0]["status"], "running")
        original_read = ToolAdapters.read_file

        @legacy_text_api
        @wraps(original_read)
        def proxied_read(adapter, arguments):
            return invoke_tool_result(original_read.__get__(adapter), arguments)

        adapter = self.registry.get_spec("read_file").executor.__self__
        with patch.object(ToolAdapters, "read_file", proxied_read):
            self.registry.register("read_file", adapter.read_file)
            receipt = self.runtime._execute_turn_action(
                "read_file", args, operation_scope="read-review", successful_operations=set(),
                call_id="full", call_id_source="native", task_id=task_id, task_plan_id=created.data["plan_id"])
        recorded = self.runtime._record_turn_receipt(receipt)
        self.assertTrue(receipt.success)
        self.assertEqual(receipt.arguments, args)
        self.assertEqual(recorded["task_id"], task_id)
        completed = self.runtime.tasks.tasks[0]
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["evidence"][-1]["args_sha256"], receipt.args_sha256)
        self.assertEqual(self.runtime.tasks.tasks[1]["status"], "pending")

    def test_archived_read_still_verifies_the_bound_task(self):
        marker = "verified near the end of the file"
        content = "# " + "x" * 8000 + marker + "\n"
        for name in ("catalog.py", "other.py"):
            (self.root / name).write_text(content)
        task = self.task()
        task["acceptance"][0]["verification"]["expect"] = {"contains": marker}
        created = self.invoke({"complexity": "simple", "tasks": [task]})
        self.assertTrue(created.success, created.text)
        self.start(created.data)
        runtime = self.runtime
        task_id = runtime.tasks.execution_task_id
        for name, arguments in (
            ("wrong-path", {"path": "other.py"}),
            ("truncated", {"path": "catalog.py", "max_chars": 7900}),
        ):
            receipt = runtime.execute_call(
                runtime.current_session, ToolCall(name, "read_file", arguments), task_id=task_id)
            self.assertTrue(receipt.success)
            self.assertIn("artifact_id", receipt.tool_result.data)
            self.assertEqual(runtime.tasks.tasks[0]["status"], "running")

        receipt = runtime.execute_call(
            runtime.current_session, ToolCall("full", "read_file", {"path": "catalog.py", "max_chars": 12000}),
            task_id=task_id)
        self.assertTrue(receipt.success)
        self.assertIn("artifact_id", receipt.tool_result.data)
        self.assertNotIn(marker, json.dumps(receipt.as_dict()))
        self.assertEqual(runtime.tasks.tasks[0]["status"], "succeeded")

    def test_prose_read_and_invalid_predicates_are_not_certified(self):
        for verification in ({"action": "read_file", "arguments": {"path": "catalog.py"}},
                             {"action": "read_file", "arguments": {"path": "catalog.py"}, "expect": {"contains": ""}},
                             {"action": "write_file", "arguments": {"path": "catalog.py"}, "expect": {"equals": self.content}}):
            task = self.task()
            task["acceptance"][0]["verification"] = verification
            result = self.invoke({"complexity": "simple", "tasks": [task]})
            self.assertFalse(result.success)
            self.assertIsNone(self.runtime.tasks.working_plan)
        created = self.invoke({"complexity": "simple", "tasks": [{"description": "Read catalog.py", "acceptance": ["Read complete"]}]})
        self.assertTrue(created.success)
        self.start(created.data)
        self.read()
        self.assertEqual(self.runtime.tasks.tasks[0]["status"], "running")
        suggested = self.invoke({"plan_id": created.data["plan_id"],
            "expected_revision": self.runtime.tasks.working_plan.revision,
            "operations": [{"op": "suggest_complete", "task": created.data["task_ids"][0]}]})
        self.assertTrue(suggested.success)
        self.assertIn("remains unverified", suggested.text)
        self.assertIn("execution contract", suggested.text)
        self.assertEqual(self.runtime.tasks.tasks[0]["status"], "running")

    def test_read_predicates_require_every_criterion_and_complete_content(self):
        task = self.task()
        second = deepcopy(task["acceptance"][0])
        second["criterion"] = "Another required observation"
        second["verification"]["expect"] = {"contains": "absent from file"}
        task["acceptance"].append(second)
        plan = self.invoke({"complexity": "simple", "tasks": [task]})
        self.assertTrue(plan.success)
        self.start(plan.data)
        self.read()
        self.assertEqual(self.runtime.tasks.tasks[0]["status"], "running")
        self.assertEqual(len(self.runtime.tasks.tasks[0]["evidence"][-1]["acceptance_ids"]), 1)
        self.assertFalse(self.runtime.tasks._task_has_evidence(self.runtime.tasks.tasks[0]))
        (self.root / "catalog.py").write_text(self.content + "absent from file\n")
        for index, data in enumerate((None, {}, {"content": True, "truncated": False},
                {"content": self.content + "absent from file\n", "truncated": True},
                {"content": self.content + "absent from file\n", "truncated": 0})):
            with self.subTest(data=data):
                self.registry.register("read_file", lambda _args, data=data: tool_success("partial", data=data))
                self.read(f"incomplete-data-{index}")
                self.assertEqual(self.runtime.tasks.tasks[0]["status"], "running")

    def test_only_native_builtin_precommit_rejections_receive_no_effect_marker(self):
        runtime = self.runtime
        token = runtime._conversation_turn_id.set("fixture-turn")
        self.addCleanup(runtime._conversation_turn_id.reset, token)
        invalid = runtime.execute_call(runtime.current_session,
            ToolCall("invalid-schema", "update_plan", {"tasks": []}))
        self.assertEqual(invalid.verification["method"], "plan_input_validation")
        self.assertFalse(invalid.verification["observed"]["committed"])
        denied = runtime.execute_call(runtime.current_session, ToolCall("denied-plan", "update_plan", {
            "complexity": "simple", "tasks": [{"description": "Do work", "acceptance": []}]}), capabilities={"read"})
        self.assertEqual(denied.failure, "permission_denied")
        self.assertNotEqual(denied.verification["method"], "plan_input_validation")
        self.registry.register("update_plan", lambda _args: tool_failure("invalid_arguments", "plugin input",
            metadata={"plan_validation_no_effect": True}))
        forged = runtime.execute_call(runtime.current_session, ToolCall("plugin", "update_plan", {
            "complexity": "simple", "tasks": [{"description": "Do work", "acceptance": []}]}))
        self.assertNotEqual(forged.verification["method"], "plan_input_validation")

    def test_commit_then_exception_is_not_a_no_effect_validation(self):
        runtime = self.runtime
        token = runtime._conversation_turn_id.set("fixture-turn")
        self.addCleanup(runtime._conversation_turn_id.reset, token)
        original = runtime.tasks.update_plan
        def commit_then_raise(**arguments):
            original(**arguments)
            raise ValueError("post-commit failure")
        with patch.object(runtime.tasks, "update_plan", side_effect=commit_then_raise):
            receipt = runtime.execute_call(runtime.current_session, ToolCall("committed-error", "update_plan", {
                "complexity": "simple", "tasks": [self.task()]}))
        self.assertIsNotNone(runtime.tasks.working_plan)
        self.assertFalse(receipt.success)
        self.assertFalse(receipt.tool_error["metadata"]["plan_validation_no_effect"])
        self.assertNotEqual(receipt.verification["method"], "plan_input_validation")

    def test_terminal_commit_error_is_not_forgiven_by_a_later_verified_plan(self):
        from openkyrozen.agent.completion import _corrected_plan_validation
        runtime = self.runtime
        token = runtime._conversation_turn_id.set("terminal-commit-turn")
        self.addCleanup(runtime._conversation_turn_id.reset, token)
        for boundary in ("manager", "storage", "storage-rollback"):
            owner = runtime.tasks if boundary == "manager" else runtime.tasks.store
            method = "update_plan" if boundary == "manager" else "apply_working_plan_edit"
            original = getattr(owner, method)
            def commit_then_raise(*args, **kwargs):
                if boundary == "storage-rollback":
                    # Reach the real transaction, then force its revision check to roll back.
                    return original(*args, **{**kwargs, "expected_revision": 1})
                original(*args, **kwargs)
                raise ValueError("post-commit observation error")
            # A cancelled plan has no active store head, but its plan/task commit is real.
            with patch.object(owner, method, side_effect=commit_then_raise):
                failed = runtime.execute_call(runtime.current_session,
                    ToolCall(f"terminal-error-{boundary}", "update_plan", {
                        "expected_revision": 0, "operations": [
                            {"op": "add", "ref": "discard", "task": {
                                "description": f"Cancelled work {boundary}", "acceptance": []}},
                            {"op": "cancel", "task": "discard"}]}))
            self.assertFalse(failed.success)
            scope = {"user_id": runtime.tasks.user_id, "workspace_id": runtime.tasks.workspace_id,
                     "session_id": runtime.tasks.session_id}
            self.assertIsNone(runtime.tasks.store.get_working_plan(**scope))
            persisted = runtime.tasks.store.list_tasks(**scope)
            committed_cancelled = any(task["description"] == f"Cancelled work {boundary}"
                                      and task["status"] == "cancelled" for task in persisted)
            self.assertEqual(committed_cancelled, boundary != "storage-rollback")
            later = runtime.execute_call(runtime.current_session, ToolCall(f"real-plan-{boundary}", "update_plan", {
                "expected_revision": 0, "operations": [
                    {"op": "add", "ref": "inspect", "task": self.task()},
                    {"op": "start", "task": "inspect"}]}))
            self.assertTrue(later.success, later.result)
            self.read(f"verified-{boundary}")
            self.assertEqual(runtime.tasks.working_plan.status, "completed")
            confirmation = runtime.execute_call(runtime.current_session,
                ToolCall(f"confirm-{boundary}", "update_plan", self._confirmation_arguments()))
            self.assertTrue(confirmation.success, confirmation.result)
            self.assertEqual(confirmation.verification["method"], "working_plan_confirmation")
            with self.subTest(boundary=boundary, check="no-effect-proof"):
                self.assertFalse(failed.tool_error["metadata"]["plan_validation_no_effect"])
            with self.subTest(boundary=boundary, check="receipt-marker"):
                self.assertNotEqual(failed.verification["method"], "plan_input_validation")
            with self.subTest(boundary=boundary, check="completion"):
                self.assertFalse(_corrected_plan_validation(runtime.tasks, failed.as_dict(),
                                                           [later.as_dict(), confirmation.as_dict()],
                                                           "terminal-commit-turn"))

    def _confirmation_arguments(self):
        plan = self.runtime.tasks.working_plan
        return {"plan_id": plan.id, "expected_revision": plan.revision,
                "operations": [{"op": "suggest_complete", "task": plan.task_ids[0]}]}

    def test_completed_confirmation_reason_reports_omission_without_plan_mutation(self):
        created = self.invoke({"expected_revision": 0, "reason": "Verify the planning preflight",
            "operations": [{"op": "add", "ref": "inspect", "task": self.task()},
                           {"op": "start", "task": "inspect"}]})
        self.assertTrue(created.success, created.text)
        self.assertTrue(self.read().success)
        tasks = self.runtime.tasks
        scope = {"user_id": tasks.user_id, "workspace_id": tasks.workspace_id,
                 "session_id": tasks.session_id}
        plan_id = created.data["plan_id"]
        before = (tasks.store.get_working_plan_by_id(plan_id, **scope),
                  tasks.store.list_working_plan_tasks(plan_id, **scope))
        attempts = tasks._plan_commit_attempts
        arguments = {**self._confirmation_arguments(), "reason": "Verify the planning preflight"}
        rejected = self.runtime.execute_call(self.runtime.current_session,
            ToolCall("confirm-with-reason", "update_plan", arguments))
        self.assertFalse(rejected.success)
        self.assertTrue(rejected.tool_error["metadata"]["plan_validation_no_effect"])
        self.assertEqual(rejected.verification["method"], "plan_input_validation")
        self.assertIn("ONLY plan_id, expected_revision and operations", rejected.result)
        self.assertIn("Omit reason, complexity, tasks", rejected.result)
        del arguments["reason"]
        confirmed = self.runtime.execute_call(self.runtime.current_session,
            ToolCall("confirm-without-reason", "update_plan", arguments))
        self.assertTrue(confirmed.success, confirmed.result)
        self.assertEqual(confirmed.verification["method"], "working_plan_confirmation")
        self.assertEqual(confirmed.verification["observed"]["plan_id"], plan_id)
        self.assertEqual(confirmed.verification["observed"]["revision"], 1)
        self.assertEqual(tasks._plan_commit_attempts, attempts)
        self.assertEqual((tasks.store.get_working_plan_by_id(plan_id, **scope),
                          tasks.store.list_working_plan_tasks(plan_id, **scope)), before)

    def test_completed_confirmation_rejects_bad_scope_revision_evidence_and_edits(self):
        created = self.invoke({"expected_revision": 0, "operations": [
            {"op": "add", "ref": "inspect", "task": self.task()},
            {"op": "add", "ref": "discard", "task": {"description": "Discard", "acceptance": []}},
            {"op": "cancel", "task": "discard"}, {"op": "start", "task": "inspect"}]})
        self.assertTrue(created.success)
        self.read()
        tasks = self.runtime.tasks
        arguments = self._confirmation_arguments()
        scope = {"user_id": tasks.user_id, "workspace_id": tasks.workspace_id, "session_id": tasks.session_id}
        plan = tasks.store.get_working_plan_by_id(tasks.working_plan.id, **scope)
        rows = tasks.store.list_working_plan_tasks(plan["id"], **scope)
        attempts = tasks._plan_commit_attempts
        for _ in range(2):
            self.assertTrue(self.invoke(arguments).success)
        for overrides in ({"expected_revision": plan["revision"] - 1}, {"plan_id": "foreign"},
                {"operations": [{"op": "suggest_complete", "task": "unknown"}]},
                {"operations": [{"op": "suggest_complete", "task": created.data["references"]["discard"]}]},
                {"operations": arguments["operations"] + [{"op": "cancel", "task": plan["id"]}]},
                {"complexity": plan["complexity"]}, {"reason": "No edits requested"}):
            with self.subTest(overrides=overrides):
                result = self.invoke({**arguments, **overrides})
                self.assertFalse(result.success, result.text)
        for changes in ({"evidence": []}, {"status": "running"},
                        {"checkpoint": {**rows[0]["checkpoint"], "recovery_effect": {"status": "unknown"}}}):
            with self.subTest(persisted=changes):
                damaged = {**rows[0], **changes}
                self.assertTrue(tasks.store.write_task(damaged, **scope, expected_status="succeeded"))
                try:
                    self.assertFalse(self.invoke(arguments).success)
                finally:
                    self.assertTrue(tasks.store.write_task(rows[0], **scope, expected_status=damaged["status"]))
        denied = self.runtime.execute_call(self.runtime.current_session,
            ToolCall("confirmation-denied", "update_plan", arguments), capabilities={"read"})
        self.assertEqual(denied.failure, "permission_denied")
        self.assertNotEqual(denied.verification["method"], "working_plan_confirmation")
        self.assertEqual(tasks.store.get_working_plan_by_id(plan["id"], **scope), plan)
        self.assertEqual(tasks.store.list_working_plan_tasks(plan["id"], **scope), rows)
        self.assertEqual(tasks._plan_commit_attempts, attempts)

    def test_completed_confirmation_checks_latest_scope_even_with_timestamp_ties(self):
        from openkyrozen.tasks.engine import TaskManager
        created = self.create_read_task()
        self.start(created)
        self.read()
        tasks = self.runtime.tasks
        arguments = self._confirmation_arguments()
        scope = {"user_id": tasks.user_id, "workspace_id": tasks.workspace_id, "session_id": tasks.session_id}
        plan = tasks.store.get_working_plan_by_id(tasks.working_plan.id, **scope)
        foreign = TaskManager(tasks.store, **{**scope, "user_id": "other-actor"})
        other = foreign.edit_plan(expected_revision=0, operations=[
            {"op": "add", "ref": "other", "task": {"description": "Other scope", "acceptance": []}}])
        self.assertTrue(self.invoke(arguments).success)  # Newer foreign rows do not change this scope.
        self.assertFalse(self.invoke({**arguments, "plan_id": other["plan_id"]}).success)
        self.assertFalse(self.invoke({**arguments, "operations": [
            {"op": "suggest_complete", "task": other["task_ids"][0]}]}).success)
        same_scope = TaskManager(tasks.store, **scope)
        with patch("openkyrozen.tasks.engine.utc_now", return_value=plan["created_at"]):
            newer = same_scope.edit_plan(expected_revision=0, operations=[
                {"op": "add", "ref": "newer", "task": {"description": "New plan", "acceptance": []}}])
        self.assertFalse(self.invoke(arguments).success)
        with patch("openkyrozen.tasks.engine.utc_now", return_value=plan["updated_at"]):
            same_scope.edit_plan(plan_id=newer["plan_id"], expected_revision=1, operations=[
                {"op": "cancel", "task": newer["task_ids"][0]}])
        self.assertIsNone(tasks.store.get_working_plan(**scope))
        terminal = tasks.store.get_working_plan_by_id(newer["plan_id"], **scope)
        self.assertEqual(terminal["created_at"], plan["created_at"])
        self.assertEqual(terminal["updated_at"], plan["updated_at"])
        self.assertFalse(self.invoke(arguments).success)  # Cached completed pointer is now historical.
        self.assertEqual(tasks.working_plan.id, plan["id"])
        self.assertEqual(tasks.store.get_working_plan_by_id(plan["id"], **scope), plan)

    def test_confirmation_receipt_survives_artifact_but_is_not_a_later_commit(self):
        from openkyrozen.agent.completion import _corrected_plan_validation
        runtime = self.runtime
        token = runtime._conversation_turn_id.set("confirm-turn")
        self.addCleanup(runtime._conversation_turn_id.reset, token)
        plan = self.create_read_task()
        self.start(plan)
        self.read()
        invalid = runtime.execute_call(runtime.current_session, ToolCall("bad-confirm", "update_plan", {
            **self._confirmation_arguments(), "expected_revision": 0}))
        self.assertEqual(invalid.verification["method"], "plan_input_validation")
        with patch("openkyrozen.agent.executor._TOOL_ARTIFACT_THRESHOLD", 100):
            confirm = runtime.execute_call(runtime.current_session,
                ToolCall("confirm", "update_plan", self._confirmation_arguments()))
        self.assertTrue(confirm.success)
        self.assertIn("artifact_id", confirm.tool_result.metadata)
        self.assertEqual(confirm.verification["method"], "working_plan_confirmation")
        self.assertFalse(_corrected_plan_validation(runtime.tasks, invalid.as_dict(), [confirm.as_dict()], "confirm-turn"))
        self.registry.register("update_plan", lambda _args: tool_success("forged", metadata={"working_plan_confirmation": True}))
        forged = runtime.execute_call(runtime.current_session,
            ToolCall("plugin-confirm", "update_plan", self._confirmation_arguments()))
        self.assertNotEqual(forged.verification["method"], "working_plan_confirmation")

    def _invalid_provider(self):
        class Provider(LLMProvider):
            calls = 0
            def get_capabilities(self, model=None):
                from openkyrozen.providers.models import ProviderCapabilities
                return ProviderCapabilities(native_tools=True)
            def chat(self, *_args, **_kwargs):
                raise AssertionError("legacy API used")
            def chat_response(self, messages, model=None, *, tools=None):
                self.calls += 1
                validate_tool_history(messages, provider_id=provider_identity(self), model=model, native=True)
                return ModelResponse(tool_calls=(ToolCall(f"bad-{self.calls}", "update_plan", {
                    "complexity": "simple", "tasks": [{"id": "invented", "description": "Inspect", "acceptance": []}]}),),
                    finish_reason=FinishReason.TOOL_REQUEST,
                    continuation=provider_continuation(self, model, "fixture", {"step": self.calls}))
        return Provider(ProviderConfig(provider="custom", model_simple="fixture"))

    def _chat(self, provider, *, cancelled=None, user_input="Inspect catalog"):
        runtime = self.runtime
        runtime.llm_provider = runtime._default_turn.provider = provider
        def prepare(turn):
            turn.interaction_mode, turn.complexity = "agent", "complex"
            turn.learning_run, turn.turn_start, turn.learned_context = {"run_id": "fixture"}, time.time(), ""
            runtime.execution_context.cancelled = cancelled
        events = []
        with patch.object(runtime, "_prepare_turn", side_effect=prepare), \
                patch.object(runtime, "_finish_learning_run", side_effect=lambda _run, _receipts, _task, answer, *_args: answer), \
                patch.object(runtime, "_schedule_post_turn_learning"):
            answer = runtime.chat(runtime.current_session, user_input, on_runtime_event=events.append)
        return answer, events

    def test_repeated_invalid_ids_consume_existing_step_budget_without_side_effects(self):
        provider = self._invalid_provider()
        with patch.object(self.runtime, "MAX_STEPS_PER_TURN", 2):
            answer, events = self._chat(provider)
        self.assertEqual(provider.calls, 3)
        self.assertIn("step limit", answer)
        self.assertIsNone(self.runtime.tasks.working_plan)
        self.assertEqual(self.runtime.tasks.tasks, [])
        self.assertEqual((self.root / "catalog.py").read_text(), self.content)
        self.assertFalse(any(e.kind in {"protocol_recovery", "protocol.recovery"} for e in events))

    def test_cancellation_after_invalid_id_feedback_stops_before_another_request(self):
        from openkyrozen.providers.errors import ProviderError, ProviderErrorKind
        provider, cancelled = self._invalid_provider(), threading.Event()
        original = self.runtime._notify_tool_execute
        def notify(action, args, result):
            original(action, args, result)
            if action == "update_plan":
                cancelled.set()
        with patch.object(self.runtime, "_notify_tool_execute", side_effect=notify):
            with self.assertRaises(ProviderError) as caught:
                self._chat(provider, cancelled=cancelled)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(caught.exception.kind, ProviderErrorKind.CANCELLED)
        self.assertIsNone(self.runtime.tasks.working_plan)
        self.assertEqual(self.runtime.tasks.tasks, [])

    @unittest.skipIf(os.name == "nt", "requires POSIX multiline shell commands")
    def test_native_command_mismatch_preserves_exact_arguments_and_guides_replanning(self):
        case, runtime = self, self.runtime
        actual = {"command": "printf first\nprintf second"}
        declared = {"command": actual["command"].replace("\n", r"\n")}
        corrected = {"command": "printf verified"}

        def task(arguments):
            return {"description": "Verify command output", "acceptance": [
                {"criterion": "The declared command exits successfully",
                 "verification": {"action": "run_cmd", "arguments": arguments}},
            ]}

        original = runtime.tasks.edit_plan(expected_revision=0, operations=[
            {"op": "add", "ref": "verify", "task": task(declared)},
            {"op": "add", "ref": "inspect", "task": {**self.task(), "dependencies": ["verify"]}},
            {"op": "start", "task": "verify"},
        ])
        task_id, dependent_id = original["task_ids"]
        confirmation = self.invoke({"plan_id": original["plan_id"], "expected_revision": original["revision"],
            "operations": [{"op": "suggest_complete", "task": task_id}]})
        self.assertTrue(confirmation.success)
        self.assertIn("Typed acceptance supplies the verification contract", confirmation.text)
        self.assertIn("use cancel, add, then start", confirmation.text)
        self.assertIn("Redirect pending dependencies", confirmation.text)

        class Provider(LLMProvider):
            calls = 0

            def get_capabilities(self, model=None):
                from openkyrozen.providers.models import ProviderCapabilities
                return ProviderCapabilities(native_tools=True)

            def chat(self, *_args, **_kwargs):
                raise AssertionError("legacy API used")

            def chat_response(self, messages, model=None, *, tools=None):
                self.calls += 1
                if self.calls == 1:
                    plan_tool = next(tool for tool in tools if tool["name"] == "update_plan")
                    case.assertIn("exits zero only when the expected failure is observed", plan_tool["description"])
                    call = ToolCall("mismatch", "run_cmd", actual)
                elif self.calls == 2:
                    reply = next(item for item in messages if isinstance(item, ToolReply) and item.call_id == "mismatch")
                    case.assertFalse(reply.is_error)
                    case.assertEqual(reply.output["working_plan_progress"]["verification"], "unmatched")
                    case.assertEqual(reply.output["working_plan_progress"]["task_id"], task_id)
                    case.assertIn("Compare the exact arguments", reply.output["next_request"])
                    pin = next(item["content"] for item in messages if isinstance(item, dict)
                               and item.get("content", "").startswith("## Pinned Working Plan state\n"))
                    state = json.loads(pin.split("\n", 2)[2])
                    case.assertEqual(state["tasks"][0]["acceptance"][0]["verification"]["arguments"], declared)
                    case.assertEqual(state["tasks"][0]["status"], "running")
                    call = ToolCall("correct-plan", "update_plan", {"plan_id": state["plan_id"],
                        "expected_revision": state["revision"], "operations": [
                            {"op": "cancel", "task": task_id},
                            {"op": "add", "ref": "corrected", "task": task(corrected)},
                            {"op": "update", "task": dependent_id, "fields": {"dependencies": ["corrected"]}},
                            {"op": "start", "task": "corrected"},
                        ]})
                elif self.calls == 3:
                    call = ToolCall("verify", "run_cmd", corrected)
                elif self.calls == 4:
                    plan = runtime.tasks.working_plan
                    call = ToolCall("select-inspect", "update_plan", {"plan_id": plan.id,
                        "expected_revision": plan.revision, "operations": [{"op": "start", "task": dependent_id}]})
                elif self.calls == 5:
                    call = ToolCall("inspect", "read_file", {"path": "catalog.py"})
                else:
                    return ModelResponse(text="Verified.", finish_reason=FinishReason.FINAL)
                return ModelResponse(tool_calls=(call,), finish_reason=FinishReason.TOOL_REQUEST,
                    continuation=provider_continuation(self, model, "fixture", {"step": self.calls}))

        provider = Provider(ProviderConfig(provider="custom", model_simple="fixture"))
        shell = self.registry.get_spec("run_cmd").capability
        with patch.object(runtime, "_effective_execution_capabilities", return_value={"read", "write", shell}):
            answer, _ = self._chat(provider)
        self.assertEqual(provider.calls, 6)
        self.assertIn("Plan status: completed", answer)
        original_task, dependent, replacement = runtime.tasks.tasks
        self.assertEqual([item["status"] for item in (original_task, dependent, replacement)],
                         ["cancelled", "succeeded", "succeeded"])
        self.assertEqual(dependent["dependencies"], [replacement["id"]])
        self.assertEqual(runtime.tasks.tasks[0]["evidence"], [])

    def test_native_verified_read_then_completion_confirmation_is_read_only(self):
        case, runtime = self, self.runtime
        before = {}
        task = self.task()
        class Provider(LLMProvider):
            calls = 0
            def get_capabilities(self, model=None):
                from openkyrozen.providers.models import ProviderCapabilities
                return ProviderCapabilities(native_tools=True)
            def chat(self, *_args, **_kwargs):
                raise AssertionError("legacy API used")
            def chat_response(self, messages, model=None, *, tools=None):
                self.calls += 1
                validate_tool_history(messages, provider_id=provider_identity(self), model=model, native=True)
                if self.calls == 1:
                    from benchmarks.v3_deepseek import (
                        decode_strict_chat_arguments,
                        planning_preflight_fixture,
                        strict_chat_schema,
                    )
                    from openkyrozen.tools.manifest import ToolRegistry
                    plan_tool = next(tool for tool in tools if tool["name"] == "update_plan")
                    creation = plan_tool["parameters"]
                    case.assertEqual(set(creation["properties"]), {"expected_revision", "operations"})
                    case.assertEqual(set(creation["required"]), set(creation["properties"]))
                    operations = creation["properties"]["operations"]["items"]["anyOf"]
                    case.assertEqual([op["properties"]["op"]["enum"] for op in operations],
                                     [["add"], ["add_read_check"], ["start"]])
                    case.assertNotIn("op:add", plan_tool["description"])
                    case.assertNotIn("plan_id", plan_tool["description"])
                    pinned = next(item["content"] for item in messages if isinstance(item, dict)
                                  and item.get("role") == "system"
                                  and item.get("content", "").startswith("## Pinned Working Plan state\n"))
                    case.assertNotIn("complexity/tasks", pinned)
                    case.assertNotIn("Start a returned task", pinned)
                    preflight = planning_preflight_fixture("a" * 40)["prompt"]
                    example = json.loads(preflight.split("using this object: ", 1)[1].splitlines()[0])
                    case.assertIsNone(ToolRegistry._argument_error(creation, example))
                    wire = strict_chat_schema(creation)
                    case.assertEqual(set(wire["properties"]), {"expected_revision", "operations"})
                    case.assertNotIn("anyOf", wire["properties"]["operations"])
                    arguments = {"expected_revision": 0, "operations": [
                        {"op": "add", "ref": "inspect", "task": task},
                        {"op": "start", "task": "inspect"}]}
                    case.assertEqual(json.loads(decode_strict_chat_arguments(json.dumps(arguments), creation)),
                                     arguments)
                    call = ToolCall("create-start", "update_plan", arguments)
                elif self.calls == 2:
                    state = self.plan_state(messages)
                    case.assertEqual(state["status"], "active")
                    from benchmarks.v3_deepseek import decode_strict_chat_arguments, strict_chat_schema
                    plan_tool = next(tool for tool in tools if tool["name"] == "update_plan")
                    plan_schema = plan_tool["parameters"]
                    case.assertNotIn('"anchor"', json.dumps(plan_schema))
                    case.assertEqual(set(plan_schema["properties"]), {
                        "plan_id", "expected_revision", "operations", "reason",
                    })
                    case.assertEqual(set(plan_schema["required"]), {"plan_id", "expected_revision", "operations"})
                    case.assertEqual(plan_schema["properties"]["operations"]["minItems"], 1)
                    case.assertNotIn("complexity/tasks", plan_tool["description"])
                    case.assertIn("before start in the same call", plan_tool["description"])
                    operations = plan_schema["properties"]["operations"]["items"]["anyOf"]
                    case.assertIn(["reorder"], [op["properties"]["op"]["enum"] for op in operations])
                    wire = strict_chat_schema(plan_schema)
                    case.assertEqual(set(wire["required"]), set(plan_schema["properties"]))
                    case.assertEqual(set(wire["properties"]), set(plan_schema["properties"]))
                    rationale, omission = wire["properties"]["reason"]["anyOf"]
                    case.assertEqual(rationale["type"], "string")
                    case.assertEqual(omission["required"], ["omit"])
                    edit = {"plan_id": state["plan_id"], "expected_revision": state["revision"],
                            "operations": [{"op": "suggest_complete", "task": state["tasks"][0]["id"]}],
                            "reason": "Check current work"}
                    case.assertEqual(json.loads(decode_strict_chat_arguments(
                        json.dumps(edit), plan_schema)), edit)
                    omitted = {**edit, "reason": {"omit": True}}
                    del edit["reason"]
                    case.assertEqual(json.loads(decode_strict_chat_arguments(
                        json.dumps(omitted), plan_schema)), edit)
                    hybrid = {"plan_id": edit["plan_id"],
                              "arguments": {key: value for key, value in edit.items() if key != "plan_id"}}
                    from openkyrozen.tools.manifest import ToolRegistry
                    decoded = json.loads(decode_strict_chat_arguments(json.dumps(hybrid), plan_schema))
                    case.assertIsNotNone(ToolRegistry._argument_error(plan_schema, decoded))
                    guidance = next(item["content"] for item in messages if isinstance(item, dict)
                                    and item.get("content", "").startswith("## Current native tool availability\n"))
                    case.assertIn("before the final verification", guidance)
                    created = next(item for item in messages if isinstance(item, ToolReply)
                                   and item.call_id == "create-start")
                    case.assertEqual(created.output["data"]["plan_id"], state["plan_id"])
                    before["visible_plan"] = deepcopy(state)
                    call = ToolCall("read", "read_file", {"path": "catalog.py"})
                elif self.calls == 3:
                    state = self.plan_state(messages)
                    case.assertEqual(state["status"], "completed")
                    case.assertEqual(state["plan_id"], before["visible_plan"]["plan_id"])
                    case.assertEqual(state["revision"], before["visible_plan"]["revision"])
                    case.assertEqual(state["tasks"][0]["status"], "succeeded")
                    plan_schema = next(tool["parameters"] for tool in tools if tool["name"] == "update_plan")
                    case.assertEqual(set(plan_schema["properties"]), {"plan_id", "expected_revision", "operations"})
                    case.assertEqual(set(plan_schema["required"]), {"expected_revision", "operations"})
                    operations = plan_schema["properties"]["operations"]["items"]["anyOf"]
                    case.assertEqual([op["properties"]["op"]["enum"] for op in operations],
                                     [["add"], ["add_read_check"], ["start"], ["suggest_complete"]])
                    read = next(item for item in messages if isinstance(item, ToolReply) and item.call_id == "read")
                    case.assertEqual(read.output["working_plan_progress"], {
                        "plan_id": state["plan_id"], "revision": state["revision"],
                        "task_id": state["tasks"][0]["id"], "task_status": "succeeded",
                    })
                    # Bounded results stay available until the current turn ends.
                    created = next(item for item in messages if isinstance(item, ToolReply)
                                   and item.call_id == "create-start")
                    case.assertNotIn("artifact_id", created.output["data"])
                    case.assertEqual(created.output["data"]["plan_id"], state["plan_id"])
                    call = ToolCall("confirm", "update_plan", {
                        "plan_id": state["plan_id"], "expected_revision": state["revision"],
                        "operations": [{"op": "suggest_complete", "task": state["tasks"][0]["id"]}]})
                else:
                    return ModelResponse(text="sorted_products sorts by casefold and original spelling.",
                                         finish_reason=FinishReason.FINAL)
                return ModelResponse(tool_calls=(call,), finish_reason=FinishReason.TOOL_REQUEST,
                    continuation=provider_continuation(self, model, "fixture", {"step": self.calls}))
            def plan_state(self, messages):
                pins = [item["content"] for item in messages if isinstance(item, dict)
                        and item.get("role") == "system"
                        and item.get("content", "").startswith("## Pinned Working Plan state\n")]
                case.assertEqual(len(pins), 1, "Current plan identity must be provider-visible, including after verified completion")
                return json.loads(pins[0].split("\n", 2)[2])
        provider = Provider(ProviderConfig(provider="custom", model_simple="fixture"))
        answer, events = self._chat(provider)
        self.assertEqual(provider.calls, 4)
        self.assertEqual([event.payload["status"] for event in events if event.kind == "turn.completed"],
                         ["completed"], answer)
        self.assertEqual(runtime.tasks.working_plan.id, before["visible_plan"]["plan_id"])
        self.assertEqual(runtime.tasks.working_plan.revision, before["visible_plan"]["revision"])
        self.assertEqual(runtime.tasks._plan_commit_attempts, 1)
        rows = runtime.tasks.store.list_working_plan_tasks(runtime.tasks.working_plan.id,
            user_id=runtime.tasks.user_id, workspace_id=runtime.tasks.workspace_id, session_id=runtime.tasks.session_id)
        self.assertEqual(len(rows[0]["evidence"]), 1)
        receipts = runtime.memory_bank.store.list_events("execution.receipt", user_id=runtime.tasks.user_id,
            workspace_id=runtime.tasks.workspace_id, session_id=runtime.tasks.session_id)
        confirm = next(row["payload"] for row in receipts if row["payload"]["call_id"] == "confirm")
        self.assertTrue(confirm["success"])
        self.assertEqual(confirm["verification"]["method"], "working_plan_confirmation")
        self.assertEqual(confirm["verification"]["observed"]["revision"], before["visible_plan"]["revision"])
        self.assertFalse(any(event.kind in {"protocol_recovery", "protocol.recovery"} for event in events))

    def test_completed_inspection_allows_a_fresh_plan_for_remaining_edits(self):
        from benchmarks.v3_deepseek import decode_strict_chat_arguments, strict_chat_tools
        from openkyrozen.providers.models import ProviderCapabilities
        from openkyrozen.tools.manifest import ToolRegistry

        case, manager = self, self.runtime.tasks
        scope = {"user_id": manager.user_id, "workspace_id": manager.workspace_id, "session_id": manager.session_id}
        content = self.content.replace("sorted_products", "sort_products")
        def plan(ref, task):
            return {"expected_revision": 0, "operations": [
                {"op": "add", "ref": ref, "task": task}, {"op": "start", "task": ref},
            ]}

        inspect = plan("inspect", self.task())
        followup = plan("refactor", self.task(content))
        old = {}

        class Provider(LLMProvider):
            calls = 0

            def get_capabilities(self, model=None):
                return ProviderCapabilities(native_tools=True)

            def chat(self, *_args, **_kwargs):
                raise AssertionError("legacy API used")

            def chat_response(self, messages, model=None, *, tools=None):
                self.calls += 1
                names = {tool["name"] for tool in tools or ()}
                if self.calls == 1:
                    call = ToolCall("inspect-plan", "update_plan", inspect)
                elif self.calls == 2:
                    call = ToolCall("inspect", "read_file", {"path": "catalog.py"})
                elif self.calls == 3:
                    case.assertEqual(manager.working_plan.status, "completed")
                    case.assertNotIn("write_file", names)
                    case.assertEqual((case.root / "catalog.py").read_text(), case.content)
                    old["plan"] = manager.store.get_working_plan_by_id(manager.working_plan.id, **scope)
                    old["tasks"] = manager.store.list_working_plan_tasks(manager.working_plan.id, **scope)
                    definition = next(tool for tool in tools if tool["name"] == "update_plan")
                    schema = definition["parameters"]
                    case.assertIsNone(ToolRegistry._argument_error(schema, followup))
                    wire = strict_chat_tools([definition])[0]["function"]["parameters"]
                    case.assertEqual(set(wire["required"]), {"plan_id", "expected_revision", "operations"})
                    raw = json.dumps({"plan_id": {"omit": True}, **followup})
                    decoded = json.loads(decode_strict_chat_arguments(raw, schema, tool_name="update_plan"))
                    case.assertEqual(decoded, followup)
                    call = ToolCall("add-only", "update_plan", {**decoded, "operations": decoded["operations"][:1]})
                elif self.calls == 4:
                    case.assertEqual(manager.working_plan.id, old["plan"]["id"])
                    case.assertNotIn("write_file", names)
                    rejected = next(item for item in messages if isinstance(item, ToolReply) and item.call_id == "add-only")
                    case.assertTrue(rejected.is_error)
                    case.assertIn("Omit plan_id, set expected_revision to 0", rejected.output["text"])
                    call = ToolCall("refactor-plan", "update_plan", followup)
                elif self.calls == 5:
                    case.assertIn("write_file", names)
                    case.assertNotEqual(manager.working_plan.id, old["plan"]["id"])
                    case.assertEqual(manager.working_plan.revision, 1)
                    call = ToolCall("refactor", "write_file", {"path": "catalog.py", "content": content})
                elif self.calls == 6:
                    call = ToolCall("verify", "read_file", {"path": "catalog.py"})
                else:
                    return ModelResponse(text="Verified the refactor.", finish_reason=FinishReason.FINAL)
                return ModelResponse(tool_calls=(call,), finish_reason=FinishReason.TOOL_REQUEST,
                    continuation=provider_continuation(self, model, "fixture", {"step": self.calls}))

        provider = Provider(ProviderConfig(provider="custom", model_simple="fixture"))
        with patch.object(self.runtime, "_effective_execution_capabilities", return_value={"read", "write"}):
            answer, events = self._chat(provider, user_input="Refactor catalog.py and verify the new function name.")
        self.assertEqual(provider.calls, 7)
        self.assertIn("Verified the refactor.", answer)
        self.assertEqual(manager.working_plan.status, "completed")
        self.assertEqual((self.root / "catalog.py").read_text(), content)
        self.assertEqual(manager.store.get_working_plan_by_id(old["plan"]["id"], **scope), old["plan"])
        self.assertEqual(manager.store.list_working_plan_tasks(old["plan"]["id"], **scope), old["tasks"])
        self.assertFalse(any(event.kind == "protocol.recovery" for event in events))

    def test_fresh_ordinary_creation_cannot_replace_a_strategic_plan(self):
        completed = self.create_read_task()
        self.start(completed)
        self.assertTrue(self.read().success)
        manager = self.runtime.tasks
        before = manager.working_plan_snapshot()
        fresh = {"expected_revision": 0, "operations": [
            {"op": "add", "ref": "inspect", "task": self.task()}, {"op": "start", "task": "inspect"},
        ]}
        legacy = {"complexity": "simple", "tasks": [self.task()]}
        for state in ({"executing_plan": {"working_plan_id": completed["plan_id"]}},
                      {"pending_plan": {"plan_id": "awaiting-approval"}}):
            with self.subTest(state=state), patch.object(self.runtime._interaction_controller, "state", return_value=state):
                for arguments in (fresh, legacy):
                    denied = self.invoke(arguments)
                    self.assertFalse(denied.success)
                    self.assertEqual(denied.error.code, "permission_denied")
                self.assertEqual(manager.working_plan_snapshot(), before)

    def test_completed_plan_can_read_its_archived_result_without_broadening_tools(self):
        case, task = self, self.task()
        task["description"] += " details" * 2000
        class Provider(LLMProvider):
            calls = 0
            def get_capabilities(self, model=None):
                from openkyrozen.providers.models import ProviderCapabilities
                return ProviderCapabilities(native_tools=True)
            def chat(self, *_args, **_kwargs):
                raise AssertionError("legacy API used")
            def chat_response(self, messages, model=None, *, tools=None):
                self.calls += 1
                names = {item["name"] for item in tools}
                if self.calls == 1:
                    call = ToolCall("create-start", "update_plan", {"expected_revision": 0, "operations": [
                        {"op": "add", "ref": "inspect", "task": task}, {"op": "start", "task": "inspect"}]})
                elif self.calls == 2:
                    call = ToolCall("read", "read_file", {"path": "catalog.py"})
                elif self.calls == 3:
                    case.assertIn("read_tool_artifact", names)
                    case.assertNotIn("write_file", names)
                    archived = next(item for item in messages if isinstance(item, ToolReply)
                                    and item.call_id == "create-start")
                    self.artifact_id = archived.output["data"]["artifact_id"]
                    call = ToolCall("archive", "read_tool_artifact", {"artifact_id": self.artifact_id})
                elif self.calls == 4:
                    reply = next(item for item in messages if isinstance(item, ToolReply) and item.call_id == "archive")
                    case.assertFalse(reply.is_error)
                    page = reply.output["data"]
                    case.assertEqual(page["artifact_id"], self.artifact_id)
                    case.assertGreater(page["total_chars"], 12000)
                    case.assertIn("Inspect catalog.py details", page["content"])
                    pin = next(item["content"] for item in messages if isinstance(item, dict)
                               and item.get("content", "").startswith("## Pinned Working Plan state\n"))
                    state = json.loads(pin.split("\n", 2)[2])
                    call = ToolCall("confirm", "update_plan", {"plan_id": state["plan_id"],
                        "expected_revision": state["revision"],
                        "operations": [{"op": "suggest_complete", "task": state["tasks"][0]["id"]}]})
                else:
                    return ModelResponse(text="Source inspected and plan confirmed.", finish_reason=FinishReason.FINAL)
                return ModelResponse(tool_calls=(call,), finish_reason=FinishReason.TOOL_REQUEST,
                    continuation=provider_continuation(self, model, "fixture", {"step": self.calls}))
        provider = Provider(ProviderConfig(provider="custom", model_simple="fixture"))
        answer, events = self._chat(provider)
        self.assertEqual(provider.calls, 5, answer)
        self.assertEqual([event.payload["status"] for event in events if event.kind == "turn.completed"], ["completed"])

    def test_completed_plan_context_is_bounded_fresh_and_scoped(self):
        task = {**self.task(), "description": "Inspect catalog.py " + "details " * 3000}
        created_result = self.invoke({"expected_revision": 0, "operations": [{"op": "add", "ref": "inspect", "task": task}]})
        self.assertTrue(created_result.success)
        created = created_result.data
        self.start(created)
        self.read()
        tasks = self.runtime.tasks
        messages = [{"role": "system", "content": "fixed"}]
        _refresh_working_plan_context(self.runtime, messages)
        pin = messages[1]["content"]
        self.assertLessEqual(len(pin), 12000)
        self.assertIn(created["plan_id"], pin)
        self.assertIn(created["task_ids"][0], pin)
        self.assertIn('"status": "completed"', pin)
        self.assertNotIn(self.content, pin)
        _refresh_working_plan_context(self.runtime, messages)
        self.assertEqual(sum(item.get("content", "").startswith("## Pinned Working Plan state\n")
                             for item in messages), 1)
        for field in ("user_id", "workspace_id", "session_id"):
            with self.subTest(field=field), patch.object(tasks, field, "foreign"):
                projected = deepcopy(messages)
                _refresh_working_plan_context(self.runtime, projected)
                self.assertNotIn(created["plan_id"], json.dumps(projected))
        for changed in (replace(tasks.working_plan, revision=0), replace(tasks.working_plan, status="cancelled")):
            with self.subTest(plan=changed), patch.object(tasks, "working_plan", changed):
                projected = deepcopy(messages)
                _refresh_working_plan_context(self.runtime, projected)
                self.assertNotIn(created["plan_id"], json.dumps(projected))

    def test_completed_context_does_not_resurrect_history_after_another_manager(self):
        from openkyrozen.tasks.engine import TaskManager
        created = self.create_read_task()
        self.start(created)
        self.read()
        tasks = self.runtime.tasks
        messages = []
        _refresh_working_plan_context(self.runtime, messages)
        self.assertIn(created["plan_id"], json.dumps(messages))
        scope = {"user_id": tasks.user_id, "workspace_id": tasks.workspace_id, "session_id": tasks.session_id}
        another = TaskManager(tasks.store, **scope)
        saved = tasks.store.get_working_plan_by_id(created["plan_id"], **scope)
        with patch("openkyrozen.tasks.engine.utc_now", return_value=saved["created_at"]):
            newer = another.edit_plan(expected_revision=0, operations=[
                {"op": "add", "ref": "new", "task": {"description": "Newer plan", "acceptance": []}}])
            another.edit_plan(plan_id=newer["plan_id"], expected_revision=1,
                              operations=[{"op": "cancel", "task": newer["task_ids"][0]}])
        _refresh_working_plan_context(self.runtime, messages)
        self.assertNotIn(created["plan_id"], json.dumps(messages))

    def test_previous_completed_plan_cannot_finish_a_new_turn_or_grant_artifact_read(self):
        created = self.create_read_task()
        self.start(created)
        self.read()
        task_count = len(self.runtime.tasks.tasks)
        for capabilities in ({"read", "write"}, {"write"}):
            case = self
            class Provider(LLMProvider):
                calls = 0
                def get_capabilities(self, model=None):
                    from openkyrozen.providers.models import ProviderCapabilities
                    return ProviderCapabilities(native_tools=True)
                def chat(self, *_args, **_kwargs):
                    raise AssertionError("legacy API used")
                def chat_response(self, messages, model=None, *, tools=None):
                    self.calls += 1
                    case.assertEqual("read_tool_artifact" in {item["name"] for item in tools}, "read" in capabilities)
                    return ModelResponse(text="Previous plan was complete.", finish_reason=FinishReason.FINAL)
            provider = Provider(ProviderConfig(provider="custom", model_simple="fixture"))
            original = self.runtime._execution_capability_token
            self.runtime._execution_capability_token = issue_capability_token("fixture", capabilities)
            try:
                with self.subTest(capabilities=capabilities), patch.object(self.runtime, "MAX_STEPS_PER_TURN", 1):
                    answer, events = self._chat(provider)
            finally:
                self.runtime._execution_capability_token = original
            self.assertEqual(provider.calls, 2, answer)
            self.assertIn("not established", answer)
            self.assertEqual([event.payload["status"] for event in events if event.kind == "turn.completed"], ["incomplete"])
            self.assertEqual(len(self.runtime.tasks.tasks), task_count)
            self.assertEqual(self.runtime.tasks._plan_commit_attempts, 2)

    def test_native_recovery_keeps_failed_validation_receipt_but_finishes_verified_plan(self):
        case, runtime = self, self.runtime
        class Provider(LLMProvider):
            calls = 0
            def get_capabilities(self, model=None):
                from openkyrozen.providers.models import ProviderCapabilities
                return ProviderCapabilities(native_tools=True)
            def chat(self, *_args, **_kwargs):
                raise AssertionError("legacy API used")
            def chat_response(self, messages, model=None, *, tools=None):
                self.calls += 1
                validate_tool_history(messages, provider_id=provider_identity(self), model=model, native=True)
                if self.calls == 1:
                    args = {"complexity": "simple", "tasks": [{"id": "inspect", "description": "Inspect", "acceptance": ["Read source"]}]}
                elif self.calls == 2:
                    reply = messages[-1]
                    case.assertIsInstance(reply, ToolReply)
                    case.assertTrue(reply.is_error)
                    case.assertIsNone(reply.output["data"]["working_plan"])
                    args = {"complexity": "simple", "expected_revision": 0, "operations": [
                        {"op": "add", "ref": "inspect", "task": case.task()}, {"op": "start", "task": "inspect"}]}
                elif self.calls == 3:
                    case.assertIsNotNone(runtime.tasks.execution_task_id)
                    return ModelResponse(tool_calls=(ToolCall("read", "read_file", {"path": "catalog.py"}),), finish_reason=FinishReason.TOOL_REQUEST,
                        continuation=provider_continuation(self, model, "fixture", {"step": self.calls}))
                else:
                    return ModelResponse(text="sorted_products sorts by casefold and original spelling.", finish_reason=FinishReason.FINAL)
                return ModelResponse(tool_calls=(ToolCall("invalid" if self.calls == 1 else "plan", "update_plan", args),), finish_reason=FinishReason.TOOL_REQUEST,
                    continuation=provider_continuation(self, model, "fixture", {"step": self.calls}))
        provider = Provider(ProviderConfig(provider="custom", model_simple="fixture"))
        answer, events = self._chat(provider)
        self.assertEqual(provider.calls, 4, answer)
        self.assertEqual(runtime.tasks.working_plan.status, "completed", answer)
        self.assertEqual([e.payload["status"] for e in events if e.kind == "turn.completed"], ["completed"], answer)
        records = runtime.memory_bank.store.list_events("execution.receipt", user_id=runtime.tasks.user_id,
            workspace_id=runtime.tasks.workspace_id, session_id=runtime.tasks.session_id)
        invalid = next(r["payload"] for r in records if r["payload"]["call_id"] == "invalid")
        self.assertFalse(invalid["success"])
        self.assertEqual(invalid["failure"], "invalid_arguments")
        self.assertFalse(any(e.kind in {"protocol_recovery", "protocol.recovery"} for e in events))
        from openkyrozen.agent.completion import _corrected_plan_validation
        committed = next(r["payload"] for r in records if r["payload"]["call_id"] == "plan")
        turn_id = invalid["verification"]["observed"]["turn_id"]
        self.assertTrue(_corrected_plan_validation(runtime.tasks, invalid, [committed], turn_id))
        for field, value in (("failure", "permission_denied"), ("action", "read_file"),
                             ("action", "write_file"), ("session_id", "foreign")):
            changed = {**invalid, field: value}
            with self.subTest(field=field, value=value):
                self.assertFalse(_corrected_plan_validation(runtime.tasks, changed, [committed], turn_id))
        for key, value in (("committed", True), ("turn_id", "prior-turn")):
            changed = deepcopy(invalid)
            changed["verification"]["observed"][key] = value
            self.assertFalse(_corrected_plan_validation(runtime.tasks, changed, [committed], turn_id))
        self.assertFalse(_corrected_plan_validation(runtime.tasks, invalid, [], turn_id))
        self.assertFalse(_corrected_plan_validation(runtime.tasks, invalid, [committed], "other-turn"))
        term = self._confirmation_arguments()
        token = runtime._conversation_turn_id.set(turn_id)
        try:
            stale = runtime.execute_call(runtime.current_session,
                ToolCall("stale-plan", "update_plan", {**term, "plan_id": "earlier-plan"})).as_dict()
        finally:
            runtime._conversation_turn_id.reset(token)
        self.assertFalse(stale["success"])
        self.assertTrue(stale["verification"]["observed"]["target_mismatch"])
        self.assertFalse(_corrected_plan_validation(runtime.tasks, stale, [committed], turn_id))
        with patch.object(runtime.tasks, "working_plan", replace(runtime.tasks.working_plan, task_ids=())):
            self.assertFalse(_corrected_plan_validation(runtime.tasks, invalid, [committed], turn_id))
        for altered in ([{**task, "status": "cancelled"} for task in runtime.tasks.tasks],
                        [{**task, "evidence": []} for task in runtime.tasks.tasks]):
            with patch.object(runtime.tasks, "tasks", altered):
                self.assertFalse(_corrected_plan_validation(runtime.tasks, invalid, [committed], turn_id))
