import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dynamic_tools import validate_tool_source
from capability_tokens import issue_capability_token
import main
import server
from memory import MemoryBank
from task_engine import TaskManager
from fastapi.testclient import TestClient


class DynamicToolTests(unittest.TestCase):
    def test_static_validator_accepts_small_pure_tool(self):
        valid, reason = validate_tool_source("def add(args):\n    return str(len(args))\n", "add")
        self.assertTrue(valid, reason)

    def test_static_validator_rejects_imports_and_process_access(self):
        self.assertFalse(validate_tool_source("import os\ndef bad(args):\n    return os.system(args)\n", "bad")[0])
        self.assertFalse(validate_tool_source("def bad(args):\n    return open(args).read()\n", "bad")[0])

    def test_chat_observer_registers_and_runs_a_real_dynamic_tool(self):
        source = (
            "DefineTool:\n```python\n"
            "def count_chars(args: str) -> str:\n"
            "    return str(len(args))\n"
            "```"
        )
        original_tools = dict(main.AVAILABLE_TOOLS)
        original_tools_list = main.TOOLS_LIST
        original_tasks = main.tasks
        with tempfile.TemporaryDirectory() as directory:
            original_memory = main.memory_bank
            main.memory_bank = MemoryBank(Path(directory) / "state.sqlite3")
            main.tasks = TaskManager(main.memory_bank.store, workspace_id="dynamic", session_id="test")
            try:
                with patch.object(main, "ALLOW_DYNAMIC_TOOLS", True), \
                     patch.object(main, "_execution_capability_token", issue_capability_token(
                         "test", {"read", "write", "shell", "network", "git", "browser", "dynamic"})), \
                     patch.object(main, "_confirm_tool_action", return_value=True):
                    parsed = main._observe_model_response(source)
                    self.assertTrue(parsed["define_tool_registered"])
                    self.assertIn("count_chars", main.AVAILABLE_TOOLS)
                    self.assertEqual(main._run_tool("count_chars", "hello"), "5")
                    self.assertIn("count_chars", main.TOOLS_LIST)
                    events = main.memory_bank.store.list_events(
                        event_type="tool.registered", workspace_id="default",
                    )
                    self.assertEqual(events[0]["payload"]["name"], "count_chars")
            finally:
                main.memory_bank = original_memory
                main.tasks = original_tasks
                main.AVAILABLE_TOOLS.clear()
                main.AVAILABLE_TOOLS.update(original_tools)
                main.TOOLS_LIST = original_tools_list

    def test_real_chat_turn_uses_the_registered_tool_on_the_next_action(self):
        class StubLearning:
            def feedback_signal(self, _text):
                return None

            def route_profile(self, _text, _profile=None):
                return "coder"

            def begin_run(self, profile, _task, provider_model=None):
                return {"run_id": "define-chat", "profile": profile, "provider_model": provider_model}

            def artifact_context(self, _run):
                return "", []

        source = (
            "DefineTool:\n```python\n"
            "def count_chars(args: str) -> str:\n"
            "    return str(len(args))\n"
            "```"
        )
        responses = [
            source,
            'Action: {"action":"count_chars","args":"hello"}',
            "The dynamic tool returned five.",
        ]
        original_tools = dict(main.AVAILABLE_TOOLS)
        original_tools_list = main.TOOLS_LIST
        original_tasks = main.tasks
        original_memory = main.memory_bank
        original_root = main._get_workspace_root()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main.memory_bank = MemoryBank(root / "state.sqlite3")
            main.tasks = TaskManager(main.memory_bank.store, workspace_id="dynamic", session_id="chat")
            main._set_workspace_root(root)
            try:
                with patch.object(main, "ALLOW_DYNAMIC_TOOLS", True), \
                     patch.object(main, "_execution_capability_token", issue_capability_token(
                         "test", {"read", "write", "shell", "network", "git", "browser", "dynamic"})), \
                     patch.object(main, "_confirm_tool_action", return_value=True), \
                     patch.object(main, "_classify_complexity", return_value="simple"), \
                     patch.object(main, "_build_messages", return_value=[]), \
                     patch.object(main, "_build_memory_context", return_value=""), \
                     patch.object(main, "_call_llm_with_spinner", side_effect=responses), \
                     patch.object(main, "_update_tasks_panel"), \
                     patch.object(main, "_finish_learning_run", side_effect=lambda run, receipts, task,
                                  result, records, tokens, started: result):
                    reply = main._chat_turn("use a helper", clear_tasks=True)
            finally:
                main._set_workspace_root(original_root)
                main.memory_bank = original_memory
                main.tasks = original_tasks
                main.AVAILABLE_TOOLS.clear()
                main.AVAILABLE_TOOLS.update(original_tools)
                main.TOOLS_LIST = original_tools_list
        self.assertEqual(reply, "The dynamic tool returned five.")

    def test_web_session_and_mcp_surface_use_the_same_dynamic_inventory(self):
        class StubLearning:
            def feedback_signal(self, _text):
                return None

            def route_profile(self, _text, _profile=None):
                return "coder"

            def begin_run(self, profile, _task, provider_model=None):
                return {"run_id": "define-web", "profile": profile, "provider_model": provider_model}

            def artifact_context(self, _run):
                return "", []

        source = (
            "DefineTool:\n```python\n"
            "def count_chars(args: str) -> str:\n"
            "    return str(len(args))\n"
            "```"
        )
        original_tools = dict(main.AVAILABLE_TOOLS)
        original_tools_list = main.TOOLS_LIST
        original_tasks = main.tasks
        original_memory = main.memory_bank
        original_root = main._get_workspace_root()
        original_execution_surface = main._EXECUTION_SURFACE
        original_surface_capabilities = main._surface_capabilities
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main.memory_bank = MemoryBank(root / "state.sqlite3")
            main.tasks = TaskManager(main.memory_bank.store, workspace_id="dynamic", session_id="web")
            main._set_workspace_root(root)
            main._EXECUTION_SURFACE = "web"
            main._surface_capabilities = "full"
            session_id = "dynamic-web-test"
            session = {"messages": [], "user_id": "local", "session_id": session_id,
                       "profile": "auto", "created": 0}
            try:
                with patch.dict(os.environ, {
                    "KYROZEN_WEB_CAPABILITIES": "full",
                    "KYROZEN_MCP_CAPABILITIES": "full",
                }), patch.object(main, "ALLOW_DYNAMIC_TOOLS", True), \
                     patch.object(main, "_execution_capability_token", issue_capability_token(
                         "test", {"read", "write", "shell", "network", "git", "browser", "dynamic"})), \
                     patch.object(main, "_confirm_tool_action", return_value=True), \
                     patch.object(main, "_classify_complexity", return_value="simple"), \
                     patch.object(main, "_build_messages", return_value=[]), \
                     patch.object(main, "_build_memory_context", return_value=""), \
                     patch.object(main, "_call_llm_with_spinner", side_effect=[
                         source,
                         'Action: {"action":"count_chars","args":"hello"}',
                         "Web chat used the dynamic tool.",
                     ]), \
                     patch.object(main, "_update_tasks_panel"), \
                     patch.object(main, "_finish_learning_run", side_effect=lambda run, receipts, task,
                                  result, records, tokens, started: result), \
                     patch.object(main, "learning_engine", StubLearning()):
                    web_reply = server._run_session_chat(session, "use a helper")
                    self.assertEqual(web_reply, "Web chat used the dynamic tool.")
                    self.assertIn("count_chars", main.AVAILABLE_TOOLS)

                    with TestClient(server.app) as client:
                        listing = client.post("/mcp", json={
                            "jsonrpc": "2.0", "id": 31, "method": "tools/list", "params": {},
                        }).json()
                        self.assertIn("count_chars", {item["name"] for item in listing["result"]["tools"]})
                        called = client.post("/mcp", json={
                            "jsonrpc": "2.0", "id": 32, "method": "tools/call",
                            "params": {"name": "count_chars", "arguments": {"args": "hello"}},
                        }).json()
                    self.assertEqual(called["result"]["content"][0]["text"], "5")
                    self.assertFalse(called["result"]["isError"])
            finally:
                server._sessions.pop(session_id, None)
                main.memory_bank = original_memory
                main.tasks = original_tasks
                main._EXECUTION_SURFACE = original_execution_surface
                main._surface_capabilities = original_surface_capabilities
                main._set_workspace_root(original_root)
                main.AVAILABLE_TOOLS.clear()
                main.AVAILABLE_TOOLS.update(original_tools)
                main.TOOLS_LIST = original_tools_list

    def test_dynamic_tool_rejections_are_logged_without_permission_or_secret_bypass(self):
        original_memory = main.memory_bank
        original_tools = dict(main.AVAILABLE_TOOLS)
        original_tools_list = main.TOOLS_LIST
        with tempfile.TemporaryDirectory() as directory:
            main.memory_bank = __import__("memory").MemoryBank(Path(directory) / "state.sqlite3")
            try:
                with patch.object(main, "ALLOW_DYNAMIC_TOOLS", True), \
                     patch.object(main, "_execution_capability_token", issue_capability_token(
                         "test", {"read", "write", "shell", "network", "git", "browser", "dynamic"})), \
                     patch.object(main, "_confirm_tool_action", side_effect=AssertionError("approval bypassed")):
                    self.assertFalse(main._register_tool(
                        "secret_tool", "def secret_tool(args):\n    return 'sk-1234567890abcdef'\n"
                    ))
                    self.assertFalse(main._register_tool(
                        "grant_tool", "def grant_tool(args):\n    return issue_capability_token(args)\n"
                    ))
                events = main.memory_bank.store.list_events(
                    event_type="tool.rejected", workspace_id="default",
                )
                self.assertEqual(len(events), 2)
                self.assertTrue(all("source" not in event["payload"] for event in events))
            finally:
                main.memory_bank = original_memory
                main.AVAILABLE_TOOLS.clear()
                main.AVAILABLE_TOOLS.update(original_tools)
                main.TOOLS_LIST = original_tools_list


if __name__ == "__main__":
    unittest.main()
