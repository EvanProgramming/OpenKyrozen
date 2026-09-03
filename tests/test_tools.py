import tempfile
import unittest
import os
import sys
from unittest.mock import patch
from pathlib import Path

from tools import AVAILABLE_TOOLS, allowed_tool_names, read_file, run_cmd, set_workspace_root, write_file


class WorkspaceToolTests(unittest.TestCase):
    def test_file_tools_stay_inside_workspace(self):
        original_root = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            set_workspace_root(root)
            try:
                self.assertIn("Wrote", write_file("inside.txt|hello"))
                self.assertEqual(read_file("inside.txt"), "hello")
                self.assertTrue(read_file(str(root.parent / "outside.txt")).startswith("Error"))
                self.assertTrue(write_file(str(root.parent / "outside.txt") + "|nope").startswith("Error"))
            finally:
                set_workspace_root(original_root)

    def test_capability_profiles_keep_rich_access_without_irreversible_reset(self):
        workspace = allowed_tool_names(AVAILABLE_TOOLS, "workspace")
        full = allowed_tool_names(AVAILABLE_TOOLS, "full")
        self.assertIn("run_cmd", workspace)
        self.assertIn("write_file", workspace)
        self.assertNotIn("git_reset", workspace)
        self.assertIn("git_reset", full)

    def test_run_cmd_resolves_bare_python_from_active_environment(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False):
            result = run_cmd("python -c 'import sys; print(sys.executable)' ")
        self.assertEqual(Path(result).resolve(), Path(sys.executable).resolve())

    def test_run_cmd_preserves_explicit_interpreter_paths(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False):
            result = run_cmd(f"{sys.executable} -c 'print(\"explicit\")'")
        self.assertEqual(result, "explicit")


if __name__ == "__main__":
    unittest.main()
