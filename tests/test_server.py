import unittest
from unittest.mock import patch

import server


class ServerBoundaryTests(unittest.TestCase):
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
