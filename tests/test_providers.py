import base64
import os
import stat
import tempfile
import unittest
from pathlib import Path

from providers import (
    FallbackProvider,
    LLMProvider,
    ProviderConfig,
    _get_encryption_key,
    decrypt_api_key,
    detect_provider,
    save_provider_config_encrypted,
)


class _ProbeProvider(LLMProvider):
    def __init__(self, provider: str, simple: str, complex_model: str,
                 response: str = "ok", error: Exception | None = None) -> None:
        super().__init__(ProviderConfig(provider=provider, model_simple=simple,
                                         model_complex=complex_model))
        self.models: list[str | None] = []
        self.stream_models: list[str | None] = []
        self.response = response
        self.error = error

    def chat(self, messages, model=None):
        self.models.append(model)
        if self.error:
            raise self.error
        return self.response, None

    def chat_stream(self, messages, model=None):
        self.stream_models.append(model)
        if self.error:
            raise self.error
        yield self.response


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


class FallbackModelTests(unittest.TestCase):
    def _fallback(self, primary, fallback):
        wrapper = FallbackProvider.__new__(FallbackProvider)
        wrapper._primary = primary
        wrapper._fallbacks = [fallback]
        return wrapper

    def test_sync_maps_deepseek_complex_model_to_openai_default(self):
        primary = _ProbeProvider("deepseek", "deepseek-chat", "deepseek-reasoner", error=RuntimeError("primary down"))
        fallback = _ProbeProvider("openai", "gpt-4o", "gpt-4o", response="fallback")
        result = self._fallback(primary, fallback).chat([], "deepseek-reasoner")
        self.assertEqual(result[0], "fallback")
        self.assertEqual(primary.models, ["deepseek-reasoner"])
        self.assertEqual(fallback.models, ["gpt-4o"])

    def test_stream_maps_anthropic_simple_model_to_deepseek_default(self):
        primary = _ProbeProvider("anthropic", "claude-sonnet-4-20250514", "claude-sonnet-4-20250514",
                                 error=RuntimeError("primary down"))
        fallback = _ProbeProvider("deepseek", "deepseek-chat", "deepseek-reasoner", response="stream fallback")
        result = list(self._fallback(primary, fallback).chat_stream([], "claude-sonnet-4-20250514"))
        self.assertEqual(result, ["stream fallback"])
        self.assertEqual(primary.stream_models, ["claude-sonnet-4-20250514"])
        self.assertEqual(fallback.stream_models, ["deepseek-chat"])

    def test_auto_and_unknown_explicit_models_have_documented_behavior(self):
        primary = _ProbeProvider("deepseek", "deepseek-chat", "deepseek-reasoner", error=RuntimeError("primary down"))
        fallback = _ProbeProvider("openai", "gpt-4o", "gpt-4o", response="fallback")
        wrapper = self._fallback(primary, fallback)
        wrapper.chat([], "auto")
        wrapper.chat([], "shared-model")
        self.assertEqual(fallback.models, ["gpt-4o", "shared-model"])

    def test_all_provider_failures_keep_primary_error_as_cause_and_include_both(self):
        primary_error = RuntimeError("primary outage")
        fallback_error = RuntimeError("fallback outage")
        primary = _ProbeProvider("deepseek", "deepseek-chat", "deepseek-reasoner", error=primary_error)
        fallback = _ProbeProvider("openai", "gpt-4o", "gpt-4o", error=fallback_error)
        with self.assertRaises(RuntimeError) as context:
            self._fallback(primary, fallback).chat([], "auto")
        self.assertIs(context.exception.__cause__, primary_error)
        self.assertIn("primary outage", str(context.exception))
        self.assertIn("fallback outage", str(context.exception))


if __name__ == "__main__":
    unittest.main()
