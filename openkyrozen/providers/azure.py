from __future__ import annotations

import os
import sys
from typing import Any
from openkyrozen.providers.base import LLMProvider
from openkyrozen.providers.openai import OpenAICompatProvider
from openkyrozen.providers.config import ProviderConfig

class AzureOpenAIProvider(OpenAICompatProvider):
    """Azure OpenAI v1 Chat Completions; model is the deployment name."""

    def __init__(self, config: ProviderConfig) -> None:
        LLMProvider.__init__(self, config)
        try:
            from openai import OpenAI
        except ImportError:
            sys.exit("The 'openai' package is required for Azure OpenAI. Install it with: pip install openai")
        endpoint = (config.base_url or os.environ.get("AZURE_OPENAI_ENDPOINT", "")).rstrip("/")
        base_url = endpoint if endpoint.endswith("/openai/v1") else f"{endpoint}/openai/v1/"
        kwargs: dict[str, Any] = {
            "api_key": config.api_key or os.environ.get("AZURE_OPENAI_API_KEY", "") or "azure-placeholder",
            "base_url": base_url,
        }
        if not config.api_key and not os.environ.get("AZURE_OPENAI_API_KEY"):
            try:
                from azure.identity import DefaultAzureCredential, get_bearer_token_provider
                kwargs["api_key"] = get_bearer_token_provider(
                    DefaultAzureCredential(exclude_interactive_browser_credential=True),
                    "https://cognitiveservices.azure.com/.default",
                )
            except ImportError:
                pass
        self._client = OpenAI(**kwargs)
