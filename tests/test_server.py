import os
import unittest
from unittest.mock import patch

import server
from fastapi import HTTPException
from fastapi.testclient import TestClient


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

        def fake_chat(message, clear_tasks=False, memory_context=None):
            seen.append([item["content"] for item in server._agent.short_term_memory])
            return "reply:" + message

        session = {"messages": [], "updated": 0}
        with patch.object(server._agent, "_chat_turn", side_effect=fake_chat):
            self.assertEqual(server._run_session_chat(session, "hello"), "reply:hello")
            self.assertEqual(server._run_session_chat(session, "again"), "reply:again")

        self.assertEqual(seen, [[], ["hello", "reply:hello"]])
        self.assertIs(server._agent.short_term_memory, original_messages)
        self.assertIs(server._agent.tasks.tasks, original_tasks)

    def test_memory_context_does_not_trust_a_claimed_speaker(self):
        session = {"user_id": "spoofed-client-value"}
        server._set_memory_context(session, {"speaker": "alice", "audience": "team", "channel": "project"})
        self.assertEqual(session["user_id"], server._SERVER_ACTOR_ID)
        self.assertEqual(session["authorized_speakers"], [server._SERVER_ACTOR_ID])
        self.assertEqual(session["audience"], "team")
        with self.assertRaises(HTTPException):
            server._set_memory_context(session, {"speaker": "alice/../../bob"})

    def test_private_claim_api_binds_owner_to_authenticated_actor(self):
        client = TestClient(server.app)
        claim = {"key": "private-api-check", "value": "hidden-7x", "claim_type": "private_fact",
                 "speaker": "alice", "authority": "owner"}
        self.assertEqual(client.post("/api/v2/memory/claims", json=claim).status_code, 403)
        claim["speaker"] = server._SERVER_ACTOR_ID
        created = client.post("/api/v2/memory/claims", json=claim)
        self.assertEqual(created.status_code, 200, created.text)
        hidden = client.get("/api/v2/memory/claims", params={"speaker": "alice"}).json()["claims"]
        visible = client.get("/api/v2/memory/claims",
                             params={"speaker": server._SERVER_ACTOR_ID}).json()["claims"]
        self.assertNotIn(created.json()["memory_id"], [item["id"] for item in hidden])
        self.assertIn(created.json()["memory_id"], [item["id"] for item in visible])

    def test_private_claim_without_speaker_is_bound_to_deployment_actor(self):
        client = TestClient(server.app)
        response = client.post("/api/v2/memory/claims", json={
            "key": "private-api-default", "value": "deployment-owned", "claim_type": "private_fact",
            "authority": "owner",
        })
        self.assertEqual(response.status_code, 200, response.text)
        claim_id = response.json()["memory_id"]
        visible = client.get("/api/v2/memory/claims",
                             params={"speaker": server._SERVER_ACTOR_ID}).json()["claims"]
        self.assertIn(claim_id, [item["id"] for item in visible])

    def test_server_actor_is_stable_across_authentication_modes(self):
        self.assertEqual(server._actor_for_request(None), server._SERVER_ACTOR_ID)
        self.assertEqual(server._get_or_create_session("single-user-test", "alice")["user_id"],
                         server._SERVER_ACTOR_ID)

    def test_profile_validation(self):
        self.assertEqual(server._normalise_profile(None), "auto")
        self.assertEqual(server._normalise_profile("CODER"), "coder")
        with self.assertRaises(HTTPException):
            server._normalise_profile("reviewer")

    def test_research_acceptance_requires_requested_observable_evidence(self):
        tools = [{"action": "search_web", "success": True}, {"action": "write_file", "success": True}]
        evidence = server._agent._research_acceptance(
            "write a sourced report", "See [source](https://example.com).", tools,
        )
        self.assertEqual([item["success"] for item in evidence], [True, True])
        self.assertEqual(server._agent._research_acceptance("explain gravity", "done", tools), [])


if __name__ == "__main__":
    unittest.main()
