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
            "graph_refresh": "internal", "calculate": "read_only",
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
            ToolCall("wrong-operation", "run_cmd", {"command": "pytest --version"}),
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
