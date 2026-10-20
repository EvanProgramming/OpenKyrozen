import dataclasses
import json
import unittest
from types import SimpleNamespace

from openkyrozen.agent.types import ExecutionReceipt
from openkyrozen.tools.models import ToolError, ToolResult


class ExecutionReceiptContractTests(unittest.TestCase):
    def receipt(self, **overrides):
        values = {
            "receipt_id": "receipt-1",
            "operation_id": "operation-1",
            "action": "write_file",
            "args": '{"path":"notes.txt"}',
            "authorized": True,
            "started_at": "2026-10-14T00:00:00Z",
            "completed_at": "2026-10-14T00:00:01Z",
            "success": False,
            "result": "Error: denied",
            "call_id": "call-1",
            "arguments": {"path": "notes.txt", "content": "private text"},
            "tool_result": ToolResult(
                data={"written": True}, text="saved", error=None,
            ),
            "user_id": "user-1",
            "workspace_id": "workspace-1",
            "session_id": "session-1",
            "plan_id": "plan-1",
            "task_id": "task-1",
            "tool_version": "2.0.0",
            "tool_source": "builtin",
            "side_effects": "filesystem",
            "verification": {"status": "verified", "method": "content_match", "observed": {"path": "notes.txt"}},
        }
        values.update(overrides)
        return ExecutionReceipt(**values)

    def test_receipt_preserves_native_call_identity_and_structured_operation(self):
        receipt = self.receipt()
        self.assertEqual(receipt.call_id, "call-1")
        self.assertEqual(receipt.arguments, {"path": "notes.txt", "content": "private text"})
        self.assertEqual(receipt.tool_result.data, {"written": True})
        self.assertEqual(receipt.task_id, "task-1")
        self.assertEqual(receipt.verification["status"], "verified")

    def test_receipt_result_and_arguments_are_defensive_snapshots(self):
        args = {"path": "notes.txt", "content": ["one"]}
        result = ToolResult(data={"items": [1]}, text="saved")
        verification = {"status": "verified", "observed": {"items": [1]}}
        receipt = self.receipt(arguments=args, tool_result=result, verification=verification)
        args["content"].append("two")
        verification["observed"]["items"].append(2)
        receipt.arguments["content"].append("caller mutation")
        receipt.verification["observed"]["items"].append(3)
        self.assertEqual(receipt.arguments["content"], ["one"])
        self.assertEqual(receipt.verification["observed"]["items"], [1])
        self.assertEqual(receipt.tool_result.data, {"items": [1]})
        with self.assertRaises(dataclasses.FrozenInstanceError):
            receipt.call_id = "changed"

    def test_success_is_derived_from_typed_result_when_present(self):
        receipt = self.receipt(
            success=False,
            tool_result=ToolResult(error=ToolError("command_failed", "exit status 1")),
        )
        self.assertFalse(receipt.success)
        successful = self.receipt(success=False, tool_result=ToolResult(text="Error: ordinary file content"))
        self.assertTrue(successful.success)

    def test_compatibility_projection_keeps_receipt_identity_bounded(self):
        receipt = self.receipt(arguments={"path": "notes.txt", "token": "secret-value"})
        projected = receipt.as_dict()
        self.assertEqual(projected["call_id"], "call-1")
        self.assertEqual(projected["task_id"], "task-1")
        self.assertNotIn("secret-value", str(projected))
        self.assertEqual(receipt.arguments["token"], "secret-value")

    def test_compatibility_args_projection_redacts_secret_like_object_fields(self):
        arguments = {"path": "notes.txt", "access_token": "secret-value", "client_secret": "other-secret"}
        receipt = self.receipt(
            args=json.dumps(arguments), arguments=arguments,
        )
        projected = receipt.as_dict()
        self.assertNotIn("secret-value", projected["args"])
        self.assertNotIn("other-secret", projected["args"])
        self.assertEqual(json.loads(projected["args"])["access_token"], "<redacted>")

    def test_compatibility_projection_redacts_secrets_in_result_text(self):
        receipt = self.receipt(
            result='{"access_token":"result-secret","client_secret":"client-secret"}',
            tool_result=ToolResult(text='{"access_token":"text-secret"}'),
        )
        projected = receipt.as_dict()
        self.assertNotIn("result-secret", str(projected))
        self.assertNotIn("client-secret", str(projected))
        self.assertNotIn("text-secret", str(projected))

    def test_compatibility_projection_redacts_json_string_results(self):
        receipt = self.receipt(
            result='"access_token=scalar-result"',
            tool_result=ToolResult(text='"access_token=scalar-text"'),
        )
        projected = receipt.as_dict()
        self.assertNotIn("scalar-result", str(projected))
        self.assertNotIn("scalar-text", str(projected))

    def test_compatibility_projection_redacts_json_objects_encoded_as_result_data(self):
        import json

        nested = '{"api_key":"deep-data-secret"}'
        for _ in range(5):
            nested = json.dumps(nested)
        receipt = self.receipt(
            result_data='{"api_key":"result-data-secret"}',
            tool_result=ToolResult(data='{"client_secret":"tool-data-secret","padding":"' + ("x" * 1200) + '"}'),
        )
        projected = receipt.as_dict()
        self.assertNotIn("result-data-secret", str(projected))
        self.assertNotIn("tool-data-secret", str(projected))
        nested_projection = self.receipt(tool_result=ToolResult(data=nested)).as_dict()
        self.assertNotIn("deep-data-secret", str(nested_projection))

    def test_compatibility_argument_projection_respects_size_budget(self):
        arguments = {("k" * 80) + str(index): "x" * 50 for index in range(40)}
        projected = self.receipt(arguments=arguments).as_dict()
        self.assertLessEqual(len(projected["args"]), 1000)
        self.assertLessEqual(len(json.dumps(projected["arguments"], ensure_ascii=False)), 4000)

    def test_historical_and_current_persisted_receipts_rehydrate(self):
        current = self.receipt().as_dict()
        restored = ExecutionReceipt.from_dict(current)
        self.assertEqual(restored.call_id, "call-1")
        self.assertEqual(restored.arguments["path"], "notes.txt")
        self.assertEqual(restored.tool_result.data, {"written": True})
        historical = {key: value for key, value in current.items()
                      if key not in {"call_id", "call_id_source", "arguments", "tool_result", "verification"}}
        historical["success"] = False
        historical["failure"] = "permission_denied"
        historical["tool_error"] = {"code": "permission_denied", "message": "denied", "retriable": False}
        restored_legacy = ExecutionReceipt.from_dict(historical)
        self.assertEqual(restored_legacy.call_id_source, "legacy")
        self.assertEqual(restored_legacy.failure, "permission_denied")

    def test_rehydrating_truncated_failed_result_preserves_failure(self):
        receipt = self.receipt(
            success=False,
            tool_error={"code": "execution_error", "message": "command failed"},
            tool_result=ToolResult(
                data={f"field{index}": "x" * 300 for index in range(20)},
                error=ToolError("execution_error", "command failed"),
            ),
        )
        restored = ExecutionReceipt.from_dict(receipt.as_dict())
        self.assertFalse(restored.success)
        self.assertEqual(restored.tool_result.error.code, "execution_error")

    def test_rehydrating_large_failed_result_uses_compatibility_failure(self):
        error = ToolError(
            "execution_error", "command failed",
            metadata={f"field{index}": "x" * 1000 for index in range(40)},
        )
        receipt = self.receipt(
            success=False, failure="execution_error", tool_error={
                "code": "execution_error", "message": "command failed",
                "metadata": {f"field{index}": "x" * 1000 for index in range(40)},
            }, tool_result=ToolResult(error=error),
        )
        restored = ExecutionReceipt.from_dict(receipt.as_dict())
        self.assertFalse(restored.success)
        self.assertEqual(restored.tool_result.error.code, "execution_error")

    def test_operation_identity_ignores_call_id_but_is_session_scoped(self):
        from openkyrozen.agent.executor import _operation_id

        def runtime(session_id):
            return SimpleNamespace(
                memory_bank=SimpleNamespace(user_id="user", workspace_id="workspace", session_id=session_id),
                _operation_action=lambda action: action,
                _operation_args=lambda _action, args: json.dumps(args, sort_keys=True, separators=(",", ":")),
            )

        args = {"content": "same", "path": "notes.txt"}
        first = _operation_id(runtime("session-a"), "turn-1", "write_file", args)
        repeated_with_another_call = _operation_id(runtime("session-a"), "turn-1", "write_file", args)
        other_session = _operation_id(runtime("session-b"), "turn-1", "write_file", args)
        self.assertEqual(first, repeated_with_another_call)
        self.assertNotEqual(first, other_session)

    def test_verification_state_is_independent_from_acceptance_and_display_text(self):
        from openkyrozen.learning.evolution import _acceptance_for_tool

        runtime = SimpleNamespace(_ACCEPTANCE_COMMAND_RE=__import__("re").compile(r"pytest"))
        acceptance, accepted = _acceptance_for_tool(
            runtime, "run_cmd", {"command": "pytest -q"}, "FAILED (failures=1)", success=True,
        )
        self.assertEqual(acceptance, "pytest -q")
        self.assertTrue(accepted)

    def test_builtin_side_effects_use_specific_classes(self):
        from openkyrozen.tools.catalog import builtin_metadata

        expected = {
            "read_file": "read_only", "write_file": "filesystem", "run_cmd": "process",
            "search_web": "remote", "git_push": "remote", "git_reset": "destructive",
            "browser_snapshot": "remote", "graph_refresh": "internal", "calculate": "read_only",
        }
        for name, side_effects in expected.items():
            with self.subTest(name=name):
                self.assertEqual(builtin_metadata(name)["side_effects"], side_effects)
        allowed = {"read_only", "filesystem", "process", "remote", "destructive", "internal"}
        from openkyrozen.tools.catalog import builtin_names
        self.assertTrue(all(builtin_metadata(name)["side_effects"] in allowed for name in builtin_names()))


class RuntimeReceiptBindingTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.tools import ToolAdapters

        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self._environment = patch.dict("os.environ", {
            "KYROZEN_DISABLE_VECTOR_INDEX": "1",
            "KYROZEN_CLI_CAPABILITIES": "full",
            "KYROZEN_APPROVAL_MODE": "never",
            "KYROZEN_WORKSPACE_ROOT": self._temporary.name,
        })
        self._environment.start()
        self.addCleanup(self._environment.stop)
        root = Path(self._temporary.name)
        self.root = root
        (root / "note.txt").write_text("requested content", encoding="utf-8")
        self.app = build_application(
            surface="cli", memory=build_memory(root / "state.sqlite3", user_id="alice", index=None),
            tools=ToolAdapters(root), user_id="alice",
        )
        self.addCleanup(self.app.close)

    def plan(self, action="calculate", arguments=None):
        return self.app.runtime.tasks.update_plan(complexity="complex", tasks=[{
            "description": "Calculate the requested value",
            "acceptance": ["the user requested content is checked"],
            "execution": {"action": action, "arguments": arguments or {"expression": "2+2"}},
        }])

    def test_native_call_does_not_infer_task_association(self):
        from openkyrozen.providers.models import ToolCall

        plan = self.plan()
        receipt = self.app.runtime.execute_call(
            self.app.runtime.current_session,
            ToolCall("native-call", "calculate", {"expression": "2+2"}),
        )
        task = self.app.runtime.tasks.tasks[0]
        self.assertEqual(receipt.call_id, "native-call")
        self.assertEqual(receipt.call_id_source, "native")
        self.assertEqual(receipt.plan_id, plan.id)
        self.assertIsNone(receipt.task_id)
        self.assertEqual(task["evidence"], [])
        self.assertEqual(task["status"], "pending")

    def test_native_foreground_call_executes_structured_arguments_and_keeps_call_id(self):
        receipt = self.app.runtime._execute_turn_action(
            "calculate", {"expression": "2+2"}, operation_scope="native-turn",
            successful_operations=set(), call_id="provider-call-1", call_id_source="native",
        )
        self.assertTrue(receipt.success, receipt.result)
        self.assertEqual(receipt.call_id, "provider-call-1")
        self.assertEqual(receipt.call_id_source, "native")
        self.assertEqual(receipt.arguments, {"expression": "2+2"})
        self.assertIn("4", receipt.result)

    def test_legacy_tool_receipt_uses_canonical_arguments_for_matching(self):
        receipt = self.app.runtime._run_tool(
            "write_file", "notes.txt|legacy pipe content", return_receipt=True,
            operation_scope="review-regression",
        )
        self.assertEqual(receipt.arguments, {"path": "notes.txt", "content": "legacy pipe content"})
        self.assertEqual(receipt.args_sha256, __import__("hashlib").sha256(
            json.dumps(receipt.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest())

    def test_invalid_native_arguments_publish_correlated_tool_call(self):
        from openkyrozen.agent.events import RuntimeEventPublisher

        runtime = self.app.runtime
        events = []
        token = runtime._runtime_event_publisher.set(RuntimeEventPublisher(
            user_id="alice", workspace_id="workspace", session_id="session",
            on_runtime_event=events.append,
        ))
        try:
            receipt = runtime._execute_turn_action(
                "write_file", {"path": "note.txt"},
                operation_scope="test", successful_operations=set(),
                call_id="native-invalid", call_id_source="native",
            )
            runtime._record_turn_receipt(receipt)
        finally:
            runtime._runtime_event_publisher.reset(token)

        self.assertFalse(receipt.success)
        self.assertEqual(receipt.failure, "invalid_arguments")
        self.assertEqual(receipt.call_id, "native-invalid")
        self.assertEqual(receipt.call_id_source, "native")
        call = next(event for event in events if event.kind == "tool.call")
        result = next(event for event in events if event.kind == "tool.result")
        self.assertEqual(call.call_id, receipt.call_id)
        self.assertEqual(result.call_id, receipt.call_id)

    def test_legacy_protocol_object_arguments_remain_rejected(self):
        receipt = self.app.runtime._execute_turn_action(
            "write_file", {"path": "note.txt", "content": "x"},
            operation_scope="legacy-test", successful_operations=set(),
        )
        self.assertFalse(receipt.success)
        self.assertEqual(receipt.failure, "invalid_arguments")
        self.assertEqual(receipt.call_id_source, "synthetic")

    def test_receipt_workspace_uses_project_task_scope(self):
        runtime = self.app.runtime
        runtime.memory_bank.workspace_id = "global-memory"
        runtime.tasks.workspace_id = "project-workspace"
        receipt = runtime._make_execution_receipt(
            action="calculate", args={"expression": "1+1"}, authorized=True,
            started_at="start", success=True, result="2", operation_scope="test",
            tool_result=ToolResult(data=2, text="2"),
        )
        self.assertEqual(receipt.workspace_id, "project-workspace")

    def test_oversized_tool_result_is_paged_and_never_embedded_in_receipt(self):
        from openkyrozen.agent.conversation import tool_result_to_dict

        runtime = self.app.runtime
        original = ToolResult(data={"records": ["東京" * 5000]}, text="東京" * 10_000)
        receipt = runtime._make_execution_receipt(
            action="read_file", args={"path": "large.txt"}, authorized=True,
            started_at="start", success=True, result=original.text, operation_scope="test",
            tool_result=original,
        )
        artifact_id = receipt.tool_result.data["artifact_id"]
        read_artifact = runtime.tool_registry.get_spec("read_tool_artifact").executor
        self.assertLess(len(receipt.result), 2_000)
        self.assertLess(len(receipt.tool_result.text), 1_500)
        first = read_artifact({"artifact_id": artifact_id, "limit": 1000})
        second = read_artifact({"artifact_id": artifact_id, "offset": 1000, "limit": 1000})
        self.assertEqual(first.data["content"] + second.data["content"],
                         runtime.memory_bank.store.artifacts.read_page(
                             artifact_id, 0, 2000, user_id=receipt.user_id,
                             workspace_id=receipt.workspace_id, session_id=receipt.session_id,
                         )["content"])
        self.assertIsNotNone(first.data["next_offset"])
        content = []
        offset = 0
        while True:
            page = read_artifact({"artifact_id": artifact_id, "offset": offset}).data
            content.append(page["content"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        self.assertEqual(json.loads("".join(content)), tool_result_to_dict(original))
        for scope in (
            {"user_id": "bob", "workspace_id": receipt.workspace_id, "session_id": receipt.session_id},
            {"user_id": receipt.user_id, "workspace_id": "other", "session_id": receipt.session_id},
            {"user_id": receipt.user_id, "workspace_id": receipt.workspace_id, "session_id": "other"},
        ):
            self.assertIsNone(runtime.memory_bank.store.artifacts.read_page(artifact_id, 0, 10, **scope))
        invalid = read_artifact({"artifact_id": artifact_id, "limit": 4001})
        self.assertEqual(invalid.error.code, "invalid_arguments")
        data_receipt = runtime._make_execution_receipt(
            action="read_file", args={"path": "large-data.json"}, authorized=True,
            started_at="start", success=True, result="structured output", result_data={"rows": ["x" * 20_000]},
            operation_scope="test",
        )
        self.assertIn("artifact_id", data_receipt.tool_result.data)
        from openkyrozen.tools.models import ToolError
        error_receipt = runtime._make_execution_receipt(
            action="read_file", args={"path": "large-error.txt"}, authorized=True,
            started_at="start", success=False, result="Error: failed", failure="large_error",
            tool_error=ToolError("large_error", "x" * 20_000), operation_scope="test",
        )
        self.assertFalse(error_receipt.success)
        self.assertLess(len(json.dumps(error_receipt.as_dict())), 5_000)
        self.assertIn("artifact_id", error_receipt.tool_result.data)

    def test_strategic_criterion_coverage_requires_receipt_verification(self):
        def verified(receipt, status):
            return ExecutionReceipt.from_dict({
                **receipt.as_dict(),
                "verification": {"status": status, "method": "fixture", "observed": None},
            })

        runtime = self.app.runtime
        arguments = {"path": "criterion.txt", "content": "verified"}
        self.plan("write_file", arguments)
        task = runtime.tasks.tasks[0]
        task["acceptance"] = [{
            "id": "criterion-content", "criterion": "File has approved content", "action": "write_file",
            "args": json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        }]
        task.setdefault("checkpoint", {})["strategic_plan"] = {"acceptance_ids": ["criterion-content"]}

        for status in ("failed", "unavailable"):
            receipt = runtime._make_execution_receipt(
                action="write_file", args=arguments, authorized=True, started_at="start", success=True,
                result="write reported", operation_scope="test", task_id=task["id"],
                tool_result=ToolResult(text="write reported"),
            )
            runtime._record_turn_receipt(verified(receipt, status))
            self.assertNotIn("acceptance_ids", task["evidence"][-1])

        (self.root / "criterion.txt").write_text("verified", encoding="utf-8")
        receipt = runtime._make_execution_receipt(
            action="write_file", args=arguments, authorized=True, started_at="start", success=True,
            result="write verified", operation_scope="test", task_id=task["id"],
            tool_result=ToolResult(text="write verified"),
        )
        self.assertEqual(receipt.verification["status"], "verified")
        runtime._record_turn_receipt(receipt)
        self.assertEqual(task["evidence"][-1]["acceptance_ids"], ["criterion-content"])

    def test_strategic_task_stays_active_until_every_criterion_has_a_verified_receipt(self):
        def verified(receipt):
            return ExecutionReceipt.from_dict({
                **receipt.as_dict(),
                "verification": {"status": "verified", "method": "fixture", "observed": None},
            })

        runtime = self.app.runtime
        write_args = {"path": "criterion.txt", "content": "verified"}
        read_args = {"path": "criterion.txt"}
        runtime.tasks.update_plan(complexity="complex", tasks=[{
            "description": "Verify requested content",
            "acceptance": ["all declared criteria pass"],
        }])
        task = runtime.tasks.tasks[0]
        criteria = [("criterion-content", "write_file", write_args),
                    ("criterion-readable", "read_file", read_args)]
        task["acceptance"] = [{
            "id": criterion_id, "criterion": criterion_id, "action": action,
            "args": json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            **({"expect": {"contains": "verified"}} if action == "read_file" else {}),
        } for criterion_id, action, arguments in criteria]
        task.setdefault("checkpoint", {})["strategic_plan"] = {
            "acceptance_ids": [criterion_id for criterion_id, _, _ in criteria],
        }

        for criterion_id, action, arguments in criteria:
            if action == "write_file":
                (self.root / "criterion.txt").write_text("verified", encoding="utf-8")
            receipt = runtime._make_execution_receipt(
                action=action, args=arguments, authorized=True, started_at="start", success=True,
                result="verified", operation_scope="test", task_id=task["id"],
                tool_result=ToolResult(text="verified"),
            )
            runtime._record_turn_receipt(verified(receipt))
            if criterion_id == "criterion-content":
                self.assertIn(task["status"], {"pending", "running"})
                self.assertIsNotNone(runtime.tasks.active_working_task(task["id"]))
        self.assertEqual(runtime.tasks.tasks[0]["status"], "succeeded")

    def test_legacy_execute_persists_receipt_in_project_scope(self):
        runtime = self.app.runtime
        session = runtime.current_session
        session.tasks.workspace_id = "project-workspace"
        receipt = runtime.execute(session, "calculate", "1+1")
        self.assertEqual(receipt.workspace_id, "project-workspace")
        events = runtime.memory_bank.store.list_events(
            "execution.receipt", workspace_id="project-workspace",
            session_id=session.session_id,
        )
        self.assertEqual(len(events), 1)

    def test_explicit_task_association_records_evidence_without_completing_task(self):
        from openkyrozen.providers.models import ToolCall

        self.plan()
        task_id = self.app.runtime.tasks.tasks[0]["id"]
        receipt = self.app.runtime.execute_call(
            self.app.runtime.current_session, ToolCall("native-call", "calculate", {"expression": "2+2"}),
            task_id=task_id,
        )
        self.assertTrue(receipt.success, receipt.as_dict())
        self.assertEqual(receipt.task_id, task_id)
        self.assertEqual(self.app.runtime.tasks.tasks[0]["evidence"][0]["task_id"], task_id)
        self.assertEqual(self.app.runtime.tasks.tasks[0]["status"], "pending")
        self.assertEqual(self.app.runtime.tasks.working_plan.status, "active")

    def test_unmatched_explicit_acceptance_cannot_complete_task(self):
        from openkyrozen.providers.models import ToolCall

        self.plan("write_file", {"path": "note.txt", "content": "requested content"})
        task = self.app.runtime.tasks.tasks[0]
        events = []
        self.app.runtime._emit_stream_event = events.append
        receipt = self.app.runtime.execute_call(
            self.app.runtime.current_session,
            ToolCall("wrong-operation", "run_cmd", {"command": "python -m unittest --help"}),
            task_id=task["id"],
        )

        self.assertTrue(receipt.success)
        self.assertEqual(receipt.task_id, task["id"])
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["evidence"], [])
        tool_receipt = next(event["tool_receipt"] for event in events if event.get("event") == "tool_receipt")
        self.assertIsNone(tool_receipt["task_id"])
        self.assertEqual(tool_receipt["requested_task_id"], task["id"])
        self.assertIn("unmatched_reason", tool_receipt)

    def test_generic_test_command_does_not_satisfy_unrelated_acceptance(self):
        from openkyrozen.providers.models import ToolCall

        self.plan("run_cmd", {"command": "python -m unittest --help"})
        task = self.app.runtime.tasks.tasks[0]
        receipt = self.app.runtime.execute_call(
            self.app.runtime.current_session,
            ToolCall("test-version", "run_cmd", {"command": "python -m unittest --help"}),
            task_id=task["id"],
        )
        self.assertTrue(receipt.success)
        self.assertEqual(task["status"], "pending")
        self.assertFalse(any(item.get("acceptance") for item in task["evidence"]))

    def test_partial_command_match_does_not_satisfy_multi_part_acceptance(self):
        from openkyrozen.providers.models import ToolCall

        self.app.runtime.tasks.update_plan(complexity="complex", tasks=[{
            "description": "Verify and review the changed files",
            "acceptance": ["run python -m unittest --help and independently review the changed files"],
            "execution": {"action": "run_cmd", "arguments": {"command": "python -m unittest --help"}},
        }])
        task = self.app.runtime.tasks.tasks[0]
        receipt = self.app.runtime.execute_call(
            self.app.runtime.current_session,
            ToolCall("partial-acceptance", "run_cmd", {"command": "python -m unittest --help"}),
            task_id=task["id"],
        )
        self.assertTrue(receipt.success)
        self.assertEqual(task["status"], "pending")
        self.assertFalse(any(item.get("acceptance") for item in task["evidence"]))

    def test_receipt_task_evidence_redacts_persisted_arguments_and_result(self):
        from openkyrozen.providers.models import ToolCall

        secret = "top-secret-value"
        self.plan("run_cmd", {"command": f"printf access_token={secret}"})
        task = self.app.runtime.tasks.tasks[0]
        stream_events = []
        self.app.runtime._emit_stream_event = stream_events.append
        receipt = self.app.runtime.execute_call(
            self.app.runtime.current_session,
            ToolCall("secret-output", "run_cmd", {"command": f"printf access_token={secret}"}),
            task_id=task["id"],
        )
        self.assertTrue(receipt.success)
        event = self.app.runtime.tasks.store.list_events(
            "execution.receipt", user_id="alice", workspace_id=self.app.runtime.tasks.workspace_id,
            session_id=self.app.runtime.tasks.session_id,
        )
        evidence = task["evidence"][-1]
        self.assertNotIn(secret, json.dumps(evidence))
        self.assertNotIn(secret, json.dumps(event[0]["payload"]))
        self.assertNotIn(secret, json.dumps(stream_events))

    def test_inferred_receipt_persists_only_projected_arguments(self):
        import hashlib

        manager = self.app.runtime.tasks
        raw = '{"command":"printf access_token=private-value"}'
        safe = '{"command": "printf access_token=<redacted>"}'
        index = manager.add_task("Run the requested command", acceptance=[{
            "action": "run_cmd", "args": raw,
        }])
        manager.record_evidence(
            action="run_cmd", args=raw, args_projection=safe,
            args_fingerprint=hashlib.sha256(raw.encode()).hexdigest(),
            result="done", success=True,
        )
        task = manager.tasks[index]
        self.assertNotIn("private-value", json.dumps(task["evidence"]))
        self.assertNotIn("private-value", json.dumps(task["checkpoint"]))
        self.assertEqual(task["checkpoint"]["args"], safe)

    def test_invalid_task_binding_is_rejected_before_executor(self):
        from openkyrozen.providers.models import ToolCall
        from openkyrozen.tools import ToolSpec
        from openkyrozen.tools.models import ToolResult

        calls = []
        spec = ToolSpec(
            "receipt_probe", lambda arguments: calls.append(arguments) or ToolResult(text="ran"),
            description="Receipt binding probe.",
            input_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            capability="read", side_effects="read",
        )
        self.app.runtime.tool_registry.register_spec(spec)
        self.plan("receipt_probe", {})
        for task_id in ("missing-task",):
            with self.subTest(task_id=task_id):
                receipt = self.app.runtime.execute_call(
                    self.app.runtime.current_session, ToolCall("stable-call", "receipt_probe", {}),
                    task_id=task_id,
                )
                self.assertEqual(receipt.call_id, "stable-call")
                self.assertEqual(receipt.failure, "invalid_arguments")
                self.assertIsNone(receipt.task_id)
        self.assertEqual(calls, [])

    def test_task_binding_rejects_mismatched_manager_scope(self):
        from openkyrozen.providers.models import ToolCall

        self.plan()
        task_id = self.app.runtime.tasks.tasks[0]["id"]
        manager = self.app.runtime.tasks
        for field, other in (("user_id", "other-user"), ("workspace_id", "other-workspace"),
                             ("session_id", "other-session")):
            with self.subTest(field=field):
                original = getattr(manager, field)
                setattr(manager, field, other)
                try:
                    receipt = self.app.runtime.execute_call(
                        self.app.runtime.current_session,
                        ToolCall(f"scope-{field}", "calculate", {"expression": "2+2"}),
                        task_id=task_id,
                    )
                finally:
                    setattr(manager, field, original)
                self.assertEqual(receipt.failure, "invalid_arguments")
                self.assertEqual(receipt.call_id, f"scope-{field}")

    def test_terminal_and_other_session_tasks_cannot_be_bound(self):
        from openkyrozen.providers.models import ToolCall

        self.plan()
        task_id = self.app.runtime.tasks.tasks[0]["id"]
        other_session = self.app.runtime.open_session("another-session", user_id="alice")
        cases = ((self.app.runtime.current_session, task_id), (other_session, task_id))
        self.app.runtime.tasks.set_status(0, "cancelled")
        for session, candidate_id in cases:
            with self.subTest(session=session.session_id):
                receipt = self.app.runtime.execute_call(
                    session, ToolCall("scoped-call", "calculate", {"expression": "2+2"}),
                    task_id=candidate_id,
                )
                self.assertEqual(receipt.failure, "invalid_arguments")
                self.assertEqual(receipt.call_id, "scoped-call")

    def test_native_call_id_survives_schema_errors_and_executor_exceptions(self):
        from openkyrozen.providers.models import ToolCall
        from openkyrozen.tools import ToolSpec

        invalid = self.app.runtime.execute_call(
            self.app.runtime.current_session, ToolCall("invalid-call", "read_file", {}),
        )
        self.assertEqual(invalid.call_id, "invalid-call")
        self.assertEqual(invalid.failure, "invalid_arguments")

        self.app.runtime.tool_registry.register_spec(ToolSpec(
            "receipt_boom", lambda _arguments: (_ for _ in ()).throw(RuntimeError("fixture failure")),
            description="Raises for receipt regression coverage.",
            input_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            capability="read", side_effects="read",
        ))
        failed = self.app.runtime.execute_call(
            self.app.runtime.current_session, ToolCall("exception-call", "receipt_boom", {}),
        )
        self.assertEqual(failed.call_id, "exception-call")
        self.assertEqual(failed.failure, "execution_error")

    def test_native_call_id_survives_approval_denial_and_cancellation(self):
        from openkyrozen.providers.models import ToolCall
        from openkyrozen.tools import ToolSpec
        from openkyrozen.tools.models import ToolResult

        calls = []
        self.app.runtime.tool_registry.register_spec(ToolSpec(
            "receipt_approval", lambda args: calls.append(args) or ToolResult(text="ran"),
            description="Approval result identity probe.",
            input_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            capability="write", side_effects="mutation", risk="high",
        ))
        from unittest.mock import patch
        with patch.dict("os.environ", {"KYROZEN_APPROVAL_MODE": "ask"}):
            denied = self.app.runtime.execute_call(
                self.app.runtime.current_session, ToolCall("denied-call", "receipt_approval", {}),
                approve=lambda *_args, **_kwargs: False,
            )
            cancelled = self.app.runtime.execute_call(
                self.app.runtime.current_session, ToolCall("cancelled-call", "receipt_approval", {}),
                approve=lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
            )
        self.assertEqual((denied.call_id, denied.failure), ("denied-call", "approval_denied"))
        self.assertEqual((cancelled.call_id, cancelled.failure), ("cancelled-call", "cancelled"))
        self.assertEqual(calls, [])

    def test_observer_failure_after_execution_keeps_single_typed_receipt(self):
        from openkyrozen.providers.models import ToolCall
        from openkyrozen.tools import ToolSpec
        from openkyrozen.tools.models import ToolResult

        calls = []
        self.app.runtime.tool_registry.register_spec(ToolSpec(
            "receipt_observer", lambda args: calls.append(args) or ToolResult(text="completed"),
            description="Observer failure receipt probe.",
            input_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            capability="read", side_effects="read",
        ))
        self.app.runtime._track_tool_performance = lambda *_args: (_ for _ in ()).throw(RuntimeError("observer"))
        receipt = self.app.runtime.execute_call(
            self.app.runtime.current_session, ToolCall("observer-call", "receipt_observer", {}),
        )
        self.assertEqual(receipt.call_id, "observer-call")
        self.assertTrue(receipt.success)
        self.assertEqual(calls, [{}])

    def test_legacy_execute_receipts_get_marked_synthetic_ids(self):
        receipt = self.app.runtime.execute(
            self.app.runtime.current_session, "read_file", "note.txt", capabilities={"read"},
        )
        self.assertTrue(receipt.call_id.startswith("synthetic_"))
        self.assertEqual(receipt.call_id_source, "synthetic")

    def test_task_binding_is_rechecked_after_approval_callback(self):
        from openkyrozen.providers.models import ToolCall
        from openkyrozen.tools import ToolSpec
        from openkyrozen.tools.models import ToolResult

        calls = []
        self.app.runtime.tool_registry.register_spec(ToolSpec(
            "approval_probe", lambda arguments: calls.append(arguments) or ToolResult(text="ran"),
            description="Approval binding probe.",
            input_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            capability="write", side_effects="mutation", risk="high",
        ))
        self.plan("approval_probe", {})
        task_id = self.app.runtime.tasks.tasks[0]["id"]

        def revoke_task(*_args, **_kwargs):
            self.app.runtime.tasks.clear()
            return True

        from unittest.mock import patch
        with patch.dict("os.environ", {"KYROZEN_APPROVAL_MODE": "ask"}):
            receipt = self.app.runtime.execute_call(
                self.app.runtime.current_session,
                ToolCall("approval-call", "approval_probe", {}),
                task_id=task_id, approve=revoke_task,
            )
        self.assertEqual(receipt.call_id, "approval-call")
        self.assertFalse(receipt.success)
        self.assertEqual(calls, [])

    def test_file_effect_verification_compares_requested_contents(self):
        from openkyrozen.providers.models import ToolCall
        from openkyrozen.tools import ToolSpec
        from openkyrozen.tools.models import ToolResult

        path = self.root / "note.txt"
        self.app.runtime.tool_registry.register_spec(ToolSpec(
            "write_file", lambda _arguments: ToolResult(text="Wrote requested file"),
            description="Test replacement that reports success without writing.",
            input_schema=self.app.runtime.tool_registry.get_spec("write_file").input_schema,
            capability="write", side_effects="filesystem",
        ), replace=True)
        receipt = self.app.runtime.execute_call(
            self.app.runtime.current_session,
            ToolCall("write-call", "write_file", {"path": "note.txt", "content": "different content"}),
        )
        self.assertTrue(receipt.success)
        self.assertEqual(path.read_text(encoding="utf-8"), "requested content")
        self.assertEqual(receipt.verification["status"], "failed")
        self.assertEqual(receipt.verification["method"], "file_content_match")
        self.assertIsNone(receipt.verified_effect)
        self.assertIsNone(receipt.acceptance)

    def test_command_verification_uses_exit_status_not_output_words(self):
        from openkyrozen.providers.models import ToolCall

        receipt = self.app.runtime.execute_call(
            self.app.runtime.current_session,
            ToolCall("command-call", "run_cmd", {"command": "printf 'Error: not ok'"}),
        )
        self.assertTrue(receipt.success, receipt.as_dict())
        self.assertEqual(receipt.verification["status"], "verified")
        self.assertEqual(receipt.verification["method"], "command_exit_status")
        self.assertEqual(receipt.verification["observed"]["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
