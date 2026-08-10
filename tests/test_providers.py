import base64
import os
import stat
import tempfile
import unittest
from pathlib import Path

from providers import (
    ProviderConfig,
    _get_encryption_key,
    decrypt_api_key,
    detect_provider,
    save_provider_config_encrypted,
)


class ProviderConfigTests(unittest.TestCase):
    def setUp(self):
        self._home = os.environ.get("HOME")
        self._temp_home = tempfile.TemporaryDirectory()
        os.environ["HOME"] = self._temp_home.name
        for key in ("KYROZEN_PROVIDER", "KYROZEN_API_KEY", "DEEPSEEK_API_KEY"):
            os.environ.pop(key, None)

    def tearDown(self):
        if self._home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._home
        self._temp_home.cleanup()

    def test_config_uses_fernet_and_private_permissions(self):
        save_provider_config_encrypted(ProviderConfig(provider="deepseek", api_key="sk-test"))
        config_path = Path.home() / ".kyrozen_config.json"
        secret_path = Path.home() / ".kyrozen_secret"
        self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(secret_path.stat().st_mode), 0o600)
        raw = config_path.read_text()
        self.assertIn('"encryption": "fernet"', raw)
        self.assertNotIn("sk-test", raw)
        self.assertEqual(detect_provider().api_key, "sk-test")

    def test_legacy_xor_ciphertext_remains_readable(self):
        plaintext = "sk-legacy"
        key = _get_encryption_key()
        encrypted = bytes(char ^ key[index % len(key)] for index, char in enumerate(plaintext.encode()))
        ciphertext = base64.b64encode(encrypted).decode()
        self.assertEqual(decrypt_api_key(ciphertext), plaintext)


if __name__ == "__main__":
    unittest.main()
