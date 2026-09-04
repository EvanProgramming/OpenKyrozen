import os
import asyncio
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from fastapi import HTTPException
from fastapi.testclient import TestClient
from providers import ProviderConfig
from memory import MemoryBank
from subagents import SubAgentManager


class ServerBoundaryTests(unittest.TestCase):
    def test_subagent_api_executes_allowed_file_action_and_rejects_disallowed_action(self):
        client = TestClient(server.app)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            manager = SubAgentManager(
                MemoryBank(root / "state.sqlite3", user_id="local",
                           workspace_id=server._agent.memory_bank.workspace_id),
                runner=server._agent._run_subagent_llm,
            )
            original_manager = server._agent.subagent_manager
            original_root = server._agent._get_workspace_root()
            server._agent.subagent_manager = manager
            server._agent._set_workspace_root(root)
            responses = [
                'Action: {"action":"write_file","args":"allowed.txt|created"}',
                "Allowed file operation completed.",
                'Action: {"action":"write_file","args":"denied.txt|must not write"}',
                "The write was rejected by the reviewer profile.",
            ]
            try:
                with patch.object(server._agent, "_get_llm_response", side_effect=responses):
                    allowed = client.post("/api/v2/agents/run", json={
                        "profile": "coder", "task": "Create allowed.txt",
                    })
                    denied = client.post("/api/v2/agents/run", json={
                        "profile": "reviewer", "task": "Create denied.txt",
                    })
            finally:
                server._agent.subagent_manager = original_manager
                server._agent._set_workspace_root(original_root)
            self.assertEqual(allowed.status_code, 200, allowed.text)
            self.assertTrue((root / "allowed.txt").is_file())
            self.assertTrue(allowed.json()["tool_receipts"][0]["success"])
            self.assertEqual(denied.status_code, 200, denied.text)
            self.assertFalse((root / "denied.txt").exists())
            self.assertFalse(denied.json()["tool_receipts"][0]["success"])
            self.assertFalse(denied.json()["tool_receipts"][0]["authorized"])
            events = manager.memory.store.list_events(
                workspace_id=server._agent.memory_bank.workspace_id,
                session_id=denied.json()["run_id"], limit=20,
            )
            self.assertTrue(any(event["event_type"] == "subagent.tool_failed" for event in events))

    def test_mcp_jsonrpc_initialize_discover_list_and_call_sequence(self):
        client = TestClient(server.app)
        init = client.post("/mcp", json={
            "id": "init-1", "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "test"}},
        })
        self.assertEqual(init.status_code, 200, init.text)
        self.assertEqual(init.json()["id"], "init-1")
        self.assertEqual(init.json()["result"]["protocolVersion"], "2024-11-05")

        initialized = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 2, "method": "notifications/initialized",
        }).json()
        self.assertEqual(initialized, {"jsonrpc": "2.0", "id": 2, "result": {}})

        listing = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {},
        }).json()
        self.assertEqual(listing["id"], 3)
        read_descriptor = next(item for item in listing["result"]["tools"] if item["name"] == "read_file")
        self.assertEqual(read_descriptor["inputSchema"]["properties"]["path"]["type"], "string")
        self.assertEqual(read_descriptor["inputSchema"]["required"], ["path"])

        discovered = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 4, "method": "server/discover", "params": {},
        }).json()
        self.assertEqual(discovered["id"], 4)
        self.assertEqual(discovered["result"]["serverInfo"]["name"], "openkyrozen")
        self.assertTrue(discovered["result"]["tools"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "mcp.txt").write_text("MCP object mapping works", encoding="utf-8")
            previous_root = server._agent._get_workspace_root()
            server._agent._set_workspace_root(root)
            try:
                called = client.post("/mcp", json={
                    "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                    "params": {"name": "read_file", "arguments": {"path": "mcp.txt"}},
                }).json()
            finally:
                server._agent._set_workspace_root(previous_root)
            self.assertEqual(called["id"], 5)
            self.assertEqual(called["result"]["content"][0]["text"], "MCP object mapping works")
            self.assertFalse(called["result"]["isError"])

    def test_mcp_protocol_and_execution_errors_echo_ids_and_respect_capabilities(self):
        client = TestClient(server.app)
        unknown = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 20, "method": "tools/call",
            "params": {"name": "does_not_exist", "arguments": {}},
        }).json()
        self.assertEqual(unknown["id"], 20)
        self.assertEqual(unknown["error"]["code"], -32601)

        invalid = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 21, "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": 123}},
        }).json()
        self.assertEqual(invalid["id"], 21)
        self.assertEqual(invalid["error"]["code"], -32602)

        execution = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 22, "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "missing-mcp-file"}},
        }).json()
        self.assertEqual(execution["id"], 22)
        self.assertIn("error", execution["result"]["content"][0]["text"].lower())
        self.assertTrue(execution["result"]["isError"])

        with patch.dict(os.environ, {"KYROZEN_MCP_CAPABILITIES": "readonly"}):
            denied = client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 23, "method": "tools/call",
                "params": {"name": "write_file", "arguments": {"path": "denied.txt", "content": "no"}},
            }).json()
        self.assertEqual(denied["id"], 23)
        self.assertEqual(denied["error"]["code"], -32001)

        method_error = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 24, "method": "unknown/method", "params": {},
        }).json()
        self.assertEqual(method_error["id"], 24)
        self.assertEqual(method_error["error"]["code"], -32601)

    def test_ollama_initialization_never_prompts_for_a_key(self):
        original_config = server._agent._provider_config
        original_provider = server._agent.llm_provider
        config = ProviderConfig(provider="ollama", base_url="http://127.0.0.1:11434/v1")
        try:
            with patch.object(server._agent, "detect_provider", return_value=config):
                with patch.object(server._agent, "get_fallback_provider", return_value=object()) as create:
                    with patch.object(server._agent.console, "input", side_effect=AssertionError("prompted")):
                        self.assertTrue(server._agent._prompt_and_init_deepseek(interactive=False))
            create.assert_called_once_with(config)
            self.assertEqual(server._agent._provider_config.provider, "ollama")
        finally:
            server._agent._provider_config = original_config
            server._agent.llm_provider = original_provider

    def test_headless_missing_remote_key_enters_deterministic_degraded_state(self):
        original_config = server._agent._provider_config
        original_provider = server._agent.llm_provider
        config = ProviderConfig(provider="deepseek", api_key="")
        try:
            with patch.object(server._agent, "detect_provider", return_value=config):
                with patch.object(server._agent.console, "input", side_effect=AssertionError("prompted")):
                    self.assertFalse(server._agent._prompt_and_init_deepseek(interactive=False))
            self.assertIsNone(server._agent.llm_provider)
            self.assertEqual(server._agent._provider_config.provider, "deepseek")
        finally:
            server._agent._provider_config = original_config
            server._agent.llm_provider = original_provider

    def test_interactive_missing_remote_key_still_accepts_a_key(self):
        original_config = server._agent._provider_config
        original_provider = server._agent.llm_provider
        config = ProviderConfig(provider="deepseek", api_key="")
        try:
            with patch.object(server._agent, "detect_provider", return_value=config):
                with patch.object(server._agent.console, "input", return_value="sk-interactive"):
                    with patch.object(server._agent, "save_provider_config_encrypted"):
                        with patch.object(server._agent, "get_fallback_provider", return_value=object()):
                            self.assertTrue(server._agent._prompt_and_init_deepseek(interactive=True))
            self.assertEqual(server._agent._provider_config.api_key, "sk-interactive")
        finally:
            server._agent._provider_config = original_config
            server._agent.llm_provider = original_provider

    def test_server_startup_initializes_headlessly(self):
        with patch.object(server._agent, "_prompt_and_init_deepseek") as init_provider:
            with patch.object(server._agent, "_set_workspace_root"):
                with patch.object(server._agent, "_load_project_files_into_memory"):
                    with patch.object(server, "_load_plugins"):
                        with patch.object(server, "_trigger_hook"):
                            with patch.object(server, "_recover_task_scopes", return_value=[]):
                                with patch.object(server._scheduler, "list_jobs", return_value=[
                                    {"payload": {"type": "task_worker"}},
                                ]):
                                    with patch.object(server._scheduler, "schedule_every"), \
                                         patch.object(server._scheduler, "start"):
                                        errors = []

                                        def run_startup():
                                            try:
                                                asyncio.run(server.startup())
                                            except Exception as exc:  # pragma: no cover - assertion below reports it
                                                errors.append(exc)

                                        thread = threading.Thread(target=run_startup)
                                        thread.start()
                                        thread.join(timeout=5)
                                        self.assertFalse(thread.is_alive())
                                        self.assertEqual(errors, [])
        init_provider.assert_called_once_with(interactive=False)

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
