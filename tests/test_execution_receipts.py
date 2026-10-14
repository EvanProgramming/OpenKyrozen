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


if __name__ == "__main__":
    unittest.main()
