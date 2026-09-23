import time
import unittest
from unittest.mock import patch

from capability_tokens import CapabilityToken, issue_capability_token
import main
from tool_registry import ToolRegistry


class CapabilityTests(unittest.TestCase):
    def test_token_expires_and_registry_filters_and_blocks_tools(self):
        registry = ToolRegistry()
        registry.register("read", lambda args: "ok", capability="read")
        registry.register("write", lambda args: "changed", capability="write")
        token = issue_capability_token("test", {"read"}, ttl_seconds=1)
        self.assertEqual([item["name"] for item in registry.catalog(token)], ["read"])
        self.assertEqual(registry.invoke("read", "", token), "ok")
        with self.assertRaises(PermissionError):
            registry.invoke("write", "", token)
        self.assertTrue(token.allows("read"))
        time.sleep(1.05)
        self.assertFalse(token.allows("read"))

    def test_active_tool_authorization_renews_without_widening(self):
        expired = CapabilityToken(
            subject="surface:tui", capabilities=frozenset({"read"}),
            issued_at=0, expires_at=0, token_id="expired",
        )
        with (patch.object(main, "_execution_capability_token", expired),
              patch.object(main, "effective_capabilities", return_value=frozenset({"read", "write"})),
              patch.object(main, "load_agent_config", return_value=object()),
              patch.object(main, "_confirm_tool_action", return_value=True)):
            result = main._run_tool("list_dir", ".")
            renewed = main._execution_capability_token
            self.assertNotEqual(renewed.token_id, "expired")
            self.assertEqual(renewed.subject, "surface:tui")
            self.assertEqual(renewed.capabilities, frozenset({"read"}))
            self.assertNotIn("Error:", result)
            self.assertIn("requires capability 'write'", main._run_tool("write_file", "x|x"))


if __name__ == "__main__":
    unittest.main()
