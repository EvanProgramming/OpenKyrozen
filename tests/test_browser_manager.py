import unittest
import tempfile
from unittest.mock import patch

from browser_manager import BrowserManager, validate_browser_url


class BrowserManagerTests(unittest.TestCase):
    def test_browser_url_boundary(self):
        self.assertTrue(validate_browser_url("https://example.com")[0])
        self.assertFalse(validate_browser_url("file:///tmp/a")[0])
        with patch.dict("os.environ", {"KYROZEN_BROWSER_ALLOW_PRIVATE": ""}):
            self.assertFalse(validate_browser_url("http://127.0.0.1:8000")[0])

    def test_missing_playwright_is_actionable(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = BrowserManager(root=directory)
            result = manager.open("https://example.com")
            self.assertTrue("playwright" in result.lower() or "opened" in result.lower())


if __name__ == "__main__":
    unittest.main()
