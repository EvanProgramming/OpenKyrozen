"""Native planner feedback and explicit read verification; no real provider."""
from contextlib import ExitStack, closing
from copy import deepcopy
from dataclasses import replace
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
from openkyrozen.tools.models import tool_failure, tool_success


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
        self.runtime.set_interaction_mode("ask")
        messages = []
        token = self.runtime._active_interaction_mode.set("ask")
        try:
            _refresh_working_plan_context(self.runtime, messages)
        finally:
            self.runtime._active_interaction_mode.reset(token)
        self.assertEqual(messages, [])

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

    def _chat(self, provider, *, cancelled=None):
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
            answer = runtime.chat(runtime.current_session, "Inspect catalog", on_runtime_event=events.append)
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

    def test_native_verified_read_then_completion_confirmation_is_read_only(self):
        case, runtime = self, self.runtime
        before = {}
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
                    call = ToolCall("create-start", "update_plan", {"expected_revision": 0, "operations": [
                        {"op": "add", "ref": "inspect", "task": case.task()},
                        {"op": "start", "task": "inspect"}]})
                elif self.calls == 2:
                    call = ToolCall("read", "read_file", {"path": "catalog.py"})
                elif self.calls == 3:
                    case.assertEqual(runtime.tasks.working_plan.status, "completed")
                    case.assertTrue(runtime.tasks._task_has_evidence(runtime.tasks.tasks[0]))
                    before["plan"] = runtime.tasks.working_plan
                    before["tasks"] = deepcopy(runtime.tasks.tasks)
                    before["attempts"] = runtime.tasks._plan_commit_attempts
                    call = ToolCall("confirm", "update_plan", {
                        "plan_id": runtime.tasks.working_plan.id,
                        "expected_revision": runtime.tasks.working_plan.revision,
                        "operations": [{"op": "suggest_complete", "task": runtime.tasks.working_plan.task_ids[0]}]})
                else:
                    return ModelResponse(text="sorted_products sorts by casefold and original spelling.",
                                         finish_reason=FinishReason.FINAL)
                return ModelResponse(tool_calls=(call,), finish_reason=FinishReason.TOOL_REQUEST,
                    continuation=provider_continuation(self, model, "fixture", {"step": self.calls}))
        provider = Provider(ProviderConfig(provider="custom", model_simple="fixture"))
        answer, events = self._chat(provider)
        self.assertEqual(provider.calls, 4)
        self.assertEqual([event.payload["status"] for event in events if event.kind == "turn.completed"],
                         ["completed"], answer)
        self.assertEqual(runtime.tasks.working_plan, before["plan"])
        self.assertEqual(runtime.tasks.tasks, before["tasks"])
        self.assertEqual(runtime.tasks._plan_commit_attempts, before["attempts"])
        rows = runtime.tasks.store.list_working_plan_tasks(before["plan"].id,
            user_id=runtime.tasks.user_id, workspace_id=runtime.tasks.workspace_id, session_id=runtime.tasks.session_id)
        self.assertEqual(rows[0]["evidence"], before["tasks"][0]["evidence"])
        receipts = runtime.memory_bank.store.list_events("execution.receipt", user_id=runtime.tasks.user_id,
            workspace_id=runtime.tasks.workspace_id, session_id=runtime.tasks.session_id)
        confirm = next(row["payload"] for row in receipts if row["payload"]["call_id"] == "confirm")
        self.assertTrue(confirm["success"])
        self.assertEqual(confirm["verification"]["method"], "working_plan_confirmation")
        self.assertEqual(confirm["verification"]["observed"]["revision"], before["plan"].revision)
        self.assertFalse(any(event.kind in {"protocol_recovery", "protocol.recovery"} for event in events))

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
        with patch.object(runtime.tasks, "working_plan", replace(runtime.tasks.working_plan, task_ids=())):
            self.assertFalse(_corrected_plan_validation(runtime.tasks, invalid, [committed], turn_id))
        for altered in ([{**task, "status": "cancelled"} for task in runtime.tasks.tasks],
                        [{**task, "evidence": []} for task in runtime.tasks.tasks]):
            with patch.object(runtime.tasks, "tasks", altered):
                self.assertFalse(_corrected_plan_validation(runtime.tasks, invalid, [committed], turn_id))
