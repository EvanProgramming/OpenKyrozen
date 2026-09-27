import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import providers


class ProviderRegistryTests(unittest.TestCase):
    def test_registry_contains_all_supported_transports(self):
        expected = {
            "deepseek", "openai", "anthropic", "google", "ollama", "glm", "kimi",
            "openrouter", "groq", "mistral", "xai", "together", "fireworks", "cohere",
            "azure_openai", "perplexity", "bedrock", "vertex",
        }
        self.assertEqual(set(providers.PROVIDER_REGISTRY), expected)
        self.assertEqual(providers.PROVIDER_BASE_URLS["together"], "https://api.together.ai/v1")
        self.assertEqual(providers.PROVIDER_DEFAULT_MODELS["openrouter"], (
            "~openai/gpt-sol-latest", "~openai/gpt-sol-latest",
        ))
        self.assertNotIn("deepseek-chat", providers.PROVIDER_DEFAULT_MODELS["deepseek"])
        self.assertNotIn("deepseek-reasoner", providers.PROVIDER_DEFAULT_MODELS["deepseek"])

    def test_only_common_providers_use_complexity_slots(self):
        for provider, models in providers.PROVIDER_DEFAULT_MODELS.items():
            self.assertEqual(
                providers.model_for_complexity(providers.ProviderConfig(provider=provider), False),
                models[0] or "auto",
            )
            self.assertEqual(
                providers.model_for_complexity(providers.ProviderConfig(provider=provider), True),
                models[1] or "auto",
            )
        for provider in providers.PROVIDER_AUTO_SELECTION:
            self.assertNotEqual(providers.PROVIDER_DEFAULT_MODELS[provider][0], "")

    def test_ollama_discovers_and_resolves_installed_models(self):
        response = MagicMock()
        response.json.return_value = {"models": [
            {"name": "small:latest", "modified_at": "2026-01-01T00:00:00Z", "size": 10},
            {"name": "large:latest", "modified_at": "2025-01-01T00:00:00Z", "size": 100},
        ]}
        response.raise_for_status.return_value = None
        providers.discover_ollama_models.cache_clear()
        with patch("requests.get", return_value=response) as get:
            config = providers.ProviderConfig(provider="ollama", base_url="http://127.0.0.1:11434/v1")
            self.assertEqual(providers.resolve_ollama_models(config), ("small:latest", "large:latest"))
            get.assert_called_once_with("http://127.0.0.1:11434/api/tags", timeout=2)

    def test_openai_compatible_contract_uses_provider_base_url_and_normalized_usage(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2, reasoning_tokens=1),
        )
        with patch("openai.OpenAI") as client_factory, patch("providers._track_cost"):
            client = client_factory.return_value
            client.chat.completions.create.return_value = response
            provider = providers.OpenAICompatProvider(
                providers.ProviderConfig(provider="cohere", api_key="cohere-key"),
            )
            text, usage = provider.chat([{"role": "user", "content": "hi"}], "command-a-plus-05-2026")
            client_factory.assert_called_once_with(
                api_key="cohere-key", base_url="https://api.cohere.ai/compatibility/v1",
            )
            client.chat.completions.create.assert_called_once_with(
                model="command-a-plus-05-2026", messages=[{"role": "user", "content": "hi"}],
            )
            self.assertEqual((text, usage), ("ok", {
                "prompt_tokens": 3, "completion_tokens": 2,
                "prompt_cache_hit_tokens": None, "prompt_cache_miss_tokens": None,
                "reasoning_tokens": None,
            }))

    def test_openai_responses_contract_and_stream_events(self):
        response = SimpleNamespace(
            output_text="done",
            usage=SimpleNamespace(input_tokens=4, output_tokens=5,
                                  output_tokens_details=SimpleNamespace(reasoning_tokens=2)),
        )
        events = [
            SimpleNamespace(type="response.output_text.delta", delta="to"),
            SimpleNamespace(type="response.output_text.delta", delta="ken"),
        ]
        with patch("openai.OpenAI") as client_factory, patch("providers._track_cost"):
            client = client_factory.return_value
            client.responses.create.side_effect = [response, iter(events)]
            provider = providers.OpenAIResponsesProvider(
                providers.ProviderConfig(provider="openai", api_key="openai-key"),
            )
            self.assertEqual(provider.chat([{"role": "user", "content": "hi"}], "gpt-6-luna"), (
                "done", {"prompt_tokens": 4, "completion_tokens": 5, "reasoning_tokens": 2},
            ))
            self.assertEqual("".join(provider.chat_stream([], "gpt-6-luna")), "token")
            self.assertEqual(client.responses.create.call_args_list[0].kwargs, {
                "model": "gpt-6-luna", "input": [{"role": "user", "content": "hi"}],
            })
            self.assertEqual(client.responses.create.call_args_list[1].kwargs, {
                "model": "gpt-6-luna", "input": [], "stream": True,
            })

    def test_bedrock_converse_contract(self):
        fake_boto3 = types.ModuleType("boto3")
        fake_client = MagicMock()
        fake_boto3.client = MagicMock()
        fake_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "ok"}]}},
            "usage": {"inputTokens": 7, "outputTokens": 3},
        }
        fake_boto3.client.return_value = fake_client
        with patch.dict(sys.modules, {"boto3": fake_boto3}), patch.dict(
                os.environ, {"AWS_REGION": "us-east-1"}, clear=False), patch("providers._track_cost"):
            provider = providers.BedrockProvider(
                providers.ProviderConfig(provider="bedrock", model_simple="anthropic.claude-sonnet-5"),
            )
            self.assertEqual(provider.chat([
                {"role": "system", "content": "be concise"},
                {"role": "user", "content": "hi"},
            ]), ("ok", {"prompt_tokens": 7, "completion_tokens": 3}))
            fake_boto3.client.assert_called_once_with("bedrock-runtime", region_name="us-east-1")
            fake_client.converse.assert_called_once_with(
                modelId="anthropic.claude-sonnet-5",
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
                inferenceConfig={"maxTokens": 4096},
                system=[{"text": "be concise"}],
            )


if __name__ == "__main__":
    unittest.main()
