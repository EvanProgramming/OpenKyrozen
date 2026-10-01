import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openkyrozen.app.bootstrap import build_application
from openkyrozen.app.config import load_agent_config
from openkyrozen.memory.service import MemoryBank
from openkyrozen.persistence.store import EventStore
from openkyrozen.security.capabilities import issue_capability_token
from openkyrozen.agent.turn import scoped_turn


class ToolDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {"KYROZEN_PROMPT_PROFILE": "compact", "KYROZEN_DISABLE_VECTOR_INDEX": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        store = EventStore(Path(self.directory.name) / "state.sqlite3")
        self.application = build_application(surface="cli", memory=MemoryBank(store.path, store=store))
        self.addCleanup(self.application.close)
        self.agent = self.application.runtime
        self.agent._set_workspace_root(self.directory.name)
        self.agent._active_interaction_mode.set("agent")
        self.agent._execution_capability_token = issue_capability_token("test", frozenset({"read", "write", "shell", "git", "dynamic"}))
        self.config = load_agent_config(self.directory.name)

    def test_compact_catalog_and_discovered_descriptions(self):
        before = self.agent._agent_prompt_tools_list(self.config)
        self.assertIn("git_log", before)
        self.assertNotIn("- git_log:", before)
        self.assertIn("- read_file:", before)
        result, success = self.agent._run_tool("discover_tools", "git_log,git_show", return_success=True)
        self.assertTrue(success)
        self.assertIn("- git_log:", result)
        after = self.agent._agent_prompt_tools_list(self.config)
        self.assertIn("- git_log:", after)
        self.assertIn("- git_show:", after)
        self.assertLess(len(self.agent._system_prompt(before, self.config)), 10000)
        with patch.dict(os.environ, {"KYROZEN_PROMPT_PROFILE": "classic"}):
            classic = self.agent._system_prompt(self.agent._agent_prompt_tools_list(self.config), self.config)
        self.assertLess(len(self.agent._system_prompt(before, self.config)), len(classic))

    def test_discovery_and_repairs_respect_config_token_and_modes(self):
        self.agent._execution_capability_token = issue_capability_token("test", frozenset({"read"}))
        catalog = self.agent._discover_tools("")
        self.assertNotIn("write_file", catalog)
        self.assertNotIn("git_push", catalog)
        self.assertTrue(self.agent._discover_tools("git_push").startswith("Error:"))
        self.assertEqual(self.agent.execution_context.discovered_tools, frozenset())
        self.agent._execution_capability_token = issue_capability_token("test", frozenset({"read", "write", "git"}))
        bounded = dict(self.config, capabilities=["read"])
        with patch("openkyrozen.agent.prompts.load_agent_config", return_value=bounded):
            self.assertNotIn("write_file", self.agent._discover_tools(""))
        for mode in ("ask", "plan"):
            self.agent._active_interaction_mode.set(mode)
            catalog = self.agent._agent_prompt_tools_list(self.config)
            self.assertNotIn("write_file", catalog)
            self.assertNotIn("git_push", catalog)
            self.assertTrue(self.agent._discover_tools("write_file").startswith("Error:"))
            prompt = self.agent._system_prompt(catalog, self.config)
            self.assertIn("read-only", prompt)

    def test_turn_and_session_isolation_and_dynamic_inventory(self):
        @scoped_turn
        def turn(runtime, name):
            self.assertEqual(runtime.execution_context.discovered_tools, frozenset())
            runtime._discover_tools(name)
            return runtime._agent_prompt_tools_list(self.config)
        first = self.agent.open_session("one")
        second = self.agent.open_session("two")
        with self.agent.use_session(first):
            self.assertIn("- git_log:", turn(self.agent, "git_log"))
            self.assertNotIn("- git_log:", turn(self.agent, "git_show"))
        with self.agent.use_session(second):
            self.assertNotIn("- git_log:", turn(self.agent, "git_show"))
        self.agent.AVAILABLE_TOOLS["pure_helper"] = lambda args: args.strip()
        self.assertIn("pure_helper", self.agent._agent_prompt_tools_list(self.config))
        self.assertIn("- pure_helper:", self.agent._discover_tools("pure_helper"))
        self.agent._execution_capability_token = issue_capability_token("test", frozenset({"read"}))
        self.assertNotIn("pure_helper", self.agent._agent_prompt_tools_list(self.config))
        self.assertNotIn("- git_log:", self.agent._agent_prompt_tools_list(self.config))

    def test_real_turn_continuation_retains_discovered_tools_and_resets_next_turn(self):
        self.agent.set_interaction_mode("agent")
        from openkyrozen.providers import ProviderConfig
        self.agent._provider_config = ProviderConfig(provider="deepseek", model_simple="deepseek-v4-flash", model_complex="deepseek-v4-flash")
        self.agent.llm_provider = object()
        replies = [
            'Action:\n```json\n{"action":"discover_tools","args":"git_log"}\n```',
            'Action:\n```json\n{"action":"git_log","args":"-1"}\n```',
            'The workspace has no Git repository, so no history is available.',
        ]
        captured = []
        def respond(messages):
            captured.append(messages)
            return replies.pop(0)
        with patch.object(self.agent, "_call_llm_with_spinner", side_effect=respond), \
                patch.object(self.agent, "dispatch_learning_cycle"), \
                patch.object(self.agent, "_classify_complexity", return_value="simple"), \
                patch.object(self.agent, "_get_llm_response", return_value="No Git repository was found."), \
                patch.object(self.agent.console, "print"):
            self.agent.chat(self.agent.current_session, "What changed in Git history?")
        self.assertNotIn("- git_log:", captured[0][0]["content"])
        self.assertIn("- git_log:", captured[1][0]["content"])
        self.assertIn("- git_log:", captured[2][0]["content"])
        self.assertEqual(self.agent.execution_context.discovered_tools, frozenset())

    def test_invalid_profile_is_explicit_error(self):
        with patch.dict(os.environ, {"KYROZEN_PROMPT_PROFILE": "invalid"}):
            with self.assertRaisesRegex(ValueError, "classic or compact"):
                self.agent._agent_prompt_tools_list(self.config)
