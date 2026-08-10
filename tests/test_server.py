import os
import unittest
from unittest.mock import patch

import server


class ServerBoundaryTests(unittest.TestCase):
    def test_server_profiles_keep_workspace_tools_and_gate_reset(self):
        workspace = server._allowed_server_tools("mcp")
        self.assertIn("run_cmd", workspace)
        self.assertIn("write_file", workspace)
        self.assertNotIn("git_reset", workspace)
        with patch.dict(os.environ, {"KYROZEN_MCP_CAPABILITIES": "full"}):
            self.assertIn("git_reset", server._allowed_server_tools("mcp"))

    def test_cli_approval_denies_noninteractive_high_impact_action(self):
        with patch.object(server._agent, "_EXECUTION_SURFACE", "cli"):
            with patch.dict(os.environ, {"KYROZEN_APPROVAL_MODE": "dangerous"}):
                with patch("sys.stdin.isatty", return_value=False):
                    self.assertFalse(server._agent._confirm_tool_action("git_push", "origin main"))

    def test_cli_approval_can_be_explicitly_disabled_for_automation(self):
        with patch.object(server._agent, "_EXECUTION_SURFACE", "cli"):
            with patch.dict(os.environ, {"KYROZEN_APPROVAL_MODE": "never"}):
                self.assertTrue(server._agent._confirm_tool_action("git_push", "origin main"))

    def test_webhook_validation_rejects_private_destinations(self):
        self.assertTrue(server._validate_webhook_url("https://example.com/hook"))
        self.assertFalse(server._validate_webhook_url("http://127.0.0.1/hook"))
        self.assertFalse(server._validate_webhook_url("file:///tmp/hook"))

    def test_session_context_isolated_from_agent_global(self):
        original_messages = server._agent.short_term_memory
        original_tasks = server._agent.tasks.tasks
        seen = []

        def fake_chat(message, clear_tasks=False):
            seen.append([item["content"] for item in server._agent.short_term_memory])
            return "reply:" + message

        session = {"messages": [], "updated": 0}
        with patch.object(server._agent, "_chat_turn", side_effect=fake_chat):
            self.assertEqual(server._run_session_chat(session, "hello"), "reply:hello")
            self.assertEqual(server._run_session_chat(session, "again"), "reply:again")

        self.assertEqual(seen, [[], ["hello", "reply:hello"]])
        self.assertIs(server._agent.short_term_memory, original_messages)
        self.assertIs(server._agent.tasks.tasks, original_tasks)


if __name__ == "__main__":
    unittest.main()
