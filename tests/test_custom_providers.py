from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, ANY

from openkyrozen.providers.config import ProviderConfig, model_for_complexity, provider_is_configured
from openkyrozen.providers.custom import (list_custom_provider_profiles, remove_custom_provider_profile,
    save_custom_provider_profile, select_custom_provider_profile)
from openkyrozen.providers.factory import detect_provider, get_provider
from openkyrozen.app.config import load_agent_config


class CustomProviderTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home.name

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home
        self.home.cleanup()

    def profile(self, name="Local gateway"):
        return {"name": name, "base_url": "https://gateway.example/v1", "api_key": "secret-token",
                "model_simple": "fast-model", "model_complex": "reasoning-model", "context_window_tokens": 32000}

    def test_profiles_create_edit_select_encrypt_and_remove(self):
        save_custom_provider_profile(self.profile())
        edited = self.profile()
        edited["name"] = "Office"
        edited["model_simple"] = "new-fast"
        save_custom_provider_profile(edited, replace_name="Local gateway")
        path = os.path.join(self.home.name, ".kyrozen_config.json")
        with open(path, encoding="utf-8") as stream:
            raw = stream.read()
        self.assertNotIn("secret-token", raw)
        self.assertEqual(len(json.loads(raw)["custom_profiles"]), 1)
        self.assertEqual(list_custom_provider_profiles()[0]["api_key"], "secret-token")
        selected = select_custom_provider_profile("office")
        self.assertEqual((selected.provider, selected.model_simple, selected.custom_profile),
                         ("custom", "new-fast", "Office"))
        detected = detect_provider()
        self.assertEqual((detected.provider, detected.base_url, detected.model_complex),
                         ("custom", "https://gateway.example/v1", "reasoning-model"))
        self.assertTrue(provider_is_configured(detected))
        from openkyrozen.providers.custom import load_custom_provider_profile
        subagent_profile = load_custom_provider_profile("OFFICE")
        self.assertEqual((subagent_profile.api_key, subagent_profile.model_complex),
                         ("secret-token", "reasoning-model"))
        self.assertTrue(remove_custom_provider_profile("Office"))
        self.assertFalse(list_custom_provider_profiles())
        self.assertEqual(detect_provider().provider, "deepseek")

    def test_custom_provider_uses_openai_compatible_adapter(self):
        config = ProviderConfig(provider="custom", base_url="https://gateway.example/v1",
                                model_simple="fast", model_complex="reasoning")
        self.assertTrue(provider_is_configured(config))
        self.assertEqual(model_for_complexity(config, False), "fast")
        self.assertEqual(model_for_complexity(config, True), "reasoning")
        with patch.dict(os.environ, {"KYROZEN_API_KEY": "main-provider-secret"}), \
                patch("openai.OpenAI") as openai_client:
            provider = get_provider(config)
            openai_client.assert_called_once_with(max_retries=0, timeout=90.0, api_key="sk-placeholder", base_url="https://gateway.example/v1")
            openai_client.return_value.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))], usage=None, model="fast",
            )
            text, _ = provider.chat([], model="fast")
            self.assertEqual(text, "ok")
            openai_client.return_value.chat.completions.create.assert_called_once_with(model="fast", messages=[], timeout=ANY)

    def test_subagent_role_can_select_named_custom_profile(self):
        source = Path(__file__).parents[1] / "agent.yaml"
        import yaml
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
        data["subagents"] = {"roles": {"reviewer": {
            "provider": "custom", "custom_profile": "office-gateway", "model": "manual-reviewer-id",
        }}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.yaml"
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            loaded = load_agent_config(Path(directory), explicit_path=path)
        self.assertEqual(loaded["subagents"]["roles"]["reviewer"]["custom_profile"], "office-gateway")
        self.assertEqual(loaded["subagents"]["roles"]["reviewer"]["model"], "manual-reviewer-id")

    def test_profile_rejects_invalid_endpoint_and_missing_model_ids(self):
        invalid = self.profile()
        invalid["base_url"] = "file:///tmp/provider"
        with self.assertRaises(ValueError):
            save_custom_provider_profile(invalid)
        invalid = self.profile()
        invalid["model_complex"] = ""
        with self.assertRaises(ValueError):
            save_custom_provider_profile(invalid)


if __name__ == "__main__":
    unittest.main()
