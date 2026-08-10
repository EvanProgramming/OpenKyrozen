import unittest

from dynamic_tools import validate_tool_source


class DynamicToolTests(unittest.TestCase):
    def test_static_validator_accepts_small_pure_tool(self):
        valid, reason = validate_tool_source("def add(args):\n    return str(len(args))\n", "add")
        self.assertTrue(valid, reason)

    def test_static_validator_rejects_imports_and_process_access(self):
        self.assertFalse(validate_tool_source("import os\ndef bad(args):\n    return os.system(args)\n", "bad")[0])
        self.assertFalse(validate_tool_source("def bad(args):\n    return open(args).read()\n", "bad")[0])


if __name__ == "__main__":
    unittest.main()
