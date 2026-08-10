import tempfile
import unittest
from pathlib import Path

from instruction_loader import format_instructions, load_instructions


class InstructionLoaderTests(unittest.TestCase):
    def test_hierarchical_instructions_are_loaded_with_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_text("project rule", encoding="utf-8")
            nested = root / "src"
            nested.mkdir()
            layers = load_instructions(nested, user_home=root / "missing-home")
            self.assertEqual(len(layers), 1)
            self.assertIn("project rule", format_instructions(nested))


if __name__ == "__main__":
    unittest.main()
