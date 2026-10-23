import unittest
from types import SimpleNamespace
from unittest.mock import patch

from openkyrozen.interfaces.cli.rendering import _terminal_supports_unicode


class WindowsRenderingTests(unittest.TestCase):
    def test_terminal_claim_does_not_override_stdout_encoding(self):
        terminal = SimpleNamespace(_IS_WINDOWS=True)
        with patch.dict("os.environ", {"WT_SESSION": "1", "TERM_PROGRAM": "vscode"}), \
                patch("openkyrozen.interfaces.cli.rendering.sys.stdout", SimpleNamespace(encoding="cp1252")):
            self.assertFalse(_terminal_supports_unicode(terminal))


if __name__ == "__main__":
    unittest.main()
