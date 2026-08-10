import time
import unittest

from capability_tokens import issue_capability_token
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


if __name__ == "__main__":
    unittest.main()
