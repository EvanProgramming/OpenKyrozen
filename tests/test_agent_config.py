import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import main
from agent_config import AgentConfigError, effective_capabilities, load_agent_config
from memory import MemoryBank


def _config_text(role_name: str, capabilities: str = "full") -> str:
    return f"""version: 1
role:
  name: {role_name}
  system: |
    You are the {role_name} role.
instructions: |
  Follow the {role_name} instructions.
examples:
  - user: show an example
    assistant: use a listed tool
capabilities:
  - {capabilities}
"""


class AgentConfigTests(unittest.TestCase):
    def test_repository_agent_yaml_and_prompt_templates_are_loadable(self):
        config = load_agent_config(Path(__file__).parents[1])
        self.assertEqual(config["version"], 1)
        self.assertEqual(config["role"]["name"], "assistant")
        self.assertTrue(config["role"]["system"])
        self.assertTrue(config["instructions"])
        self.assertTrue(config["examples"])
        self.assertIn("read", effective_capabilities(config))

    def test_precedence_is_environment_then_explicit_then_workspace_then_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace_config = root / "agent.yaml"
            explicit_config = root / "explicit.yaml"
            workspace_config.write_text(_config_text("workspace", "read"), encoding="utf-8")
            explicit_config.write_text(_config_text("explicit", "workspace"), encoding="utf-8")
            config = load_agent_config(
                root,
                explicit_path=explicit_config,
                environ={
                    "KYROZEN_ROLE": "environment",
                    "KYROZEN_INSTRUCTIONS": "environment instructions",
                    "KYROZEN_AGENT_CAPABILITIES": "read",
                },
            )
            self.assertEqual(config["role"]["name"], "environment")
            self.assertEqual(config["instructions"], "environment instructions")
            self.assertEqual(config["capabilities"], ["read"])

    def test_schema_rejects_unknown_keys_bad_types_and_duplicate_yaml_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = root / "bad.yaml"
            bad.write_text("version: 1\nunknown: true\n", encoding="utf-8")
            with self.assertRaises(AgentConfigError):
                load_agent_config(root, explicit_path=bad)

            bad.write_text(_config_text("reviewer") + "capabilities: [not-a-capability]\n", encoding="utf-8")
            with self.assertRaises(AgentConfigError):
                load_agent_config(root, explicit_path=bad)

            bad.write_text("version: 1\nversion: 1\n", encoding="utf-8")
            with self.assertRaises(AgentConfigError):
                load_agent_config(root, explicit_path=bad)

    def test_custom_role_influences_prompt_and_capability_has_real_effect(self):
        original_memory = main.memory_bank
        original_root = main._get_workspace_root()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent.yaml").write_text(_config_text("reviewer", "read"), encoding="utf-8")
            main.memory_bank = MemoryBank(root / "state.sqlite3", workspace_id="config", session_id="test")
            main._set_workspace_root(root)
            try:
                with patch.object(main, "_retrieve_failure", return_value=[]), \
                     patch.object(main, "_build_memory_context", return_value=""):
                    messages = main._build_messages("review this file")
                system_prompt = messages[0]["content"]
                self.assertIn("Name: reviewer", system_prompt)
                self.assertIn("Follow the reviewer instructions", system_prompt)
                available_tools = system_prompt.split("## Available Tools\n", 1)[1].split(
                    "## Tool invocation format", 1
                )[0]
                self.assertIn("read_file", available_tools)
                self.assertNotIn("write_file", available_tools)

                result = main._run_tool("write_file", "blocked.txt|nope")
                self.assertIn("outside the configured agent capability bound", result)
                self.assertFalse((root / "blocked.txt").exists())
            finally:
                main.memory_bank = original_memory
                main._set_workspace_root(original_root)

    def test_installed_wheel_contains_config_and_templates_and_loads_custom_role(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel_dir = root / "wheel"
            target = root / "target"
            workspace = root / "workspace"
            wheel_dir.mkdir()
            target.mkdir()
            workspace.mkdir()
            (workspace / "agent.yaml").write_text(_config_text("installed-reviewer", "read"), encoding="utf-8")
            build = subprocess.run(
                [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--wheel-dir", str(wheel_dir)],
                cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            wheels = sorted(wheel_dir.glob("openkyrozen-*.whl"))
            self.assertEqual(len(wheels), 1)
            with zipfile.ZipFile(wheels[0]) as archive:
                names = set(archive.namelist())
            self.assertIn("agent_config.py", names)
            self.assertIn("prompts/role.md", names)
            self.assertTrue(any(name.endswith("agent.yaml") for name in names))

            install = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(target), str(wheels[0])],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(target)
            probe = subprocess.run(
                [sys.executable, "-c", (
                    "from agent_config import load_agent_config; "
                    f"c=load_agent_config({str(workspace)!r}); "
                    "print(c['role']['name']); print(c['examples'][0]['assistant'])"
                )],
                cwd=workspace, env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)
            self.assertIn("installed-reviewer", probe.stdout)
            self.assertIn("use a listed tool", probe.stdout)


if __name__ == "__main__":
    unittest.main()
