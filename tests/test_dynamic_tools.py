import unittest
import tempfile
from pathlib import Path

from openkyrozen.app.bootstrap import build_application, build_memory
from openkyrozen.security.tool_policy import resolve_capabilities


class DynamicToolRemovalTests(unittest.TestCase):
    def test_define_tool_text_is_rejected_without_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_application(
                memory=build_memory(Path(directory) / "state.sqlite3"), surface="cli",
            ).runtime
            before = set(runtime.tool_registry.specs)
            response = (
                "DefineTool:\n```python\n"
                "def generated_helper(args):\n    return args\n"
                "```"
            )

            parsed = runtime._observe_model_response(response)

            self.assertTrue(parsed["unsupported_action_protocol"])
            self.assertEqual(parsed["protocol_error"], "Textual tool creation is no longer supported.")
            self.assertEqual(parsed["clean"], "")
            self.assertEqual(set(runtime.tool_registry.specs), before)
            self.assertNotIn("generated_helper", runtime.AVAILABLE_TOOLS)
            self.assertFalse(hasattr(runtime, "_register_tool"))
            self.assertFalse(hasattr(runtime, "ALLOW_DYNAMIC_TOOLS"))

    def test_full_capability_profile_does_not_grant_dynamic_registration(self):
        self.assertNotIn("dynamic", resolve_capabilities("full"))


if __name__ == "__main__":
    unittest.main()
