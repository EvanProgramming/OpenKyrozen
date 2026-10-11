import dataclasses
import unittest

from openkyrozen.tools import models


ToolError = getattr(models, "ToolError", None)
ToolResult = getattr(models, "ToolResult", None)
render_tool_result = getattr(models, "render_tool_result", None)


class ToolResultContractTests(unittest.TestCase):
    def require_api(self):
        self.assertTrue(all((ToolError, ToolResult, render_tool_result)), "typed tool result API is missing")
        if not all((ToolError, ToolResult, render_tool_result)):
            self.skipTest("typed tool result API is not implemented yet")

    def test_success_depends_on_typed_error_not_display_text(self):
        self.require_api()
        success = ToolResult(data={"contents": "Error: this is a valid file"}, text="Error: this is a valid file")
        failure = ToolResult(data={"partial": True}, text="Completed", error=ToolError("not_found", "Missing file"))
        self.assertTrue(success.success)
        self.assertFalse(failure.success)

    def test_contracts_are_frozen_and_return_defensive_json_copies(self):
        self.require_api()
        source = {"nested": [1]}
        result = ToolResult(data=source, metadata={"audit": {"tags": ["safe"]}})
        source["nested"].append(2)
        observed = result.data
        observed["nested"].append(3)
        observed_meta = result.metadata
        observed_meta["audit"]["tags"].append("changed")
        self.assertEqual(result.data, {"nested": [1]})
        self.assertEqual(result.metadata, {"audit": {"tags": ["safe"]}})
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.text = "mutated"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.error = ToolError("x", "y")

    def test_invalid_code_message_and_non_json_values_are_rejected(self):
        self.require_api()
        for kwargs in ({"code": "", "message": "bad"}, {"code": "execution_error", "message": "  "}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ToolError(**kwargs)
        with self.assertRaises((TypeError, ValueError)):
            ToolResult(data={"bad": object()})
        with self.assertRaises((TypeError, ValueError)):
            ToolResult(metadata={"bad": float("nan")})
        with self.assertRaises(TypeError):
            ToolResult(text=123)

    def test_renderer_preserves_text_and_renders_structured_fallback(self):
        self.require_api()
        self.assertEqual(render_tool_result(ToolResult(text="Error: valid text", error=None)), "Error: valid text")
        self.assertEqual(render_tool_result(ToolResult(error=ToolError("not_found", "Missing"))), "Error: Missing")
        self.assertEqual(render_tool_result(ToolResult(data={"count": 2})), '{"count": 2}')
