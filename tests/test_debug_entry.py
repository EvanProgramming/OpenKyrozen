import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class DebugEntryTests(unittest.TestCase):
    def test_make_debug_runs_real_isolated_smoke_without_prompting(self):
        repository = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as state:
            env = os.environ.copy()
            env.update({
                "HOME": home,
                "KYROZEN_DB_PATH": str(Path(state) / "caller.sqlite3"),
                "KYROZEN_DISABLE_VECTOR_INDEX": "1",
            })
            for variable in (
                "KYROZEN_PROVIDER", "KYROZEN_API_KEY", "DEEPSEEK_API_KEY",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
            ):
                env.pop(variable, None)
            result = subprocess.run(
                ["make", "debug"],
                cwd=repository,
                env=env,
                input="",
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OpenKyrozen debug smoke passed.", result.stdout)
        self.assertIn('"workspace_tool_effect": true', result.stdout)
        self.assertIn('"memory_path_isolated": true', result.stdout)
        self.assertNotIn("Enter your", result.stdout + result.stderr)

    def test_help_is_available_without_importing_or_initialising_provider(self):
        repository = Path(__file__).parents[1]
        result = subprocess.run(
            [sys.executable, "main_debug.py", "--help"],
            cwd=repository,
            input="",
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--interactive", result.stdout)
        self.assertNotIn("Enter your", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
