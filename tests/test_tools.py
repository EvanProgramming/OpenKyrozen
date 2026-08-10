import tempfile
import unittest
from pathlib import Path

from tools import read_file, set_workspace_root, write_file


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


if __name__ == "__main__":
    unittest.main()
