from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

@dataclass(frozen=True)
class ProviderSpec:
    """The single source of truth for selectable provider metadata."""

    canonical_name: str
    display_name: str
    api_style: str
    base_url: str
    api_key_env: str
    model_simple: str
    model_complex: str
    context_window_tokens: int | None
    auto_selection: bool
    fallbacks: tuple[str, ...] = ()


PROVIDER_REGISTRY: dict[str, ProviderSpec] = {
    # Defaults are taken from the providers' current official model catalogs.
    "deepseek": ProviderSpec("deepseek", "DeepSeek", "openai_compat", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "deepseek-flash", "deepseek-v4-pro", 1_048_576, True, ("openai", "anthropic")),
    "openai": ProviderSpec("openai", "OpenAI", "responses", "https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-6-luna", "gpt-6-astra", 1_048_576, True, ("deepseek", "anthropic")),
    "anthropic": ProviderSpec("anthropic", "Anthropic", "messages", "https://api.anthropic.com", "ANTHROPIC_API_KEY", "claude-haiku-4-5", "claude-fable-5-1", 1_000_000, True, ("openai", "deepseek")),
    "google": ProviderSpec("google", "Google Gemini", "google_genai", "", "GEMINI_API_KEY", "gemini-3.5-flash-lite", "gemini-3.1-pro-preview", 1_048_576, True, ("openai", "deepseek")),
    "ollama": ProviderSpec("ollama", "Ollama", "openai_compat", "http://localhost:11434/v1", "", "", "", None, False),
    "glm": ProviderSpec("glm", "Z.AI / GLM", "openai_compat", "https://api.z.ai/api/paas/v4/", "ZAI_API_KEY", "glm-5.3-flash", "glm-5.3", 1_048_576, True, ("openai", "deepseek")),
    "kimi": ProviderSpec("kimi", "Moonshot / Kimi", "openai_compat", "https://api.moonshot.cn/v1", "MOONSHOT_API_KEY", "kimi-k2.6", "kimi-k3", 1_048_576, True, ("openai", "deepseek")),
    "openrouter": ProviderSpec("openrouter", "OpenRouter", "openai_compat", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "~openai/gpt-sol-latest", "~openai/gpt-sol-latest", 1_048_576, False, ("openai", "deepseek")),
    "groq": ProviderSpec("groq", "Groq", "openai_compat", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai/gpt-oss-120b", "openai/gpt-oss-120b", 131_072, False, ("openai", "deepseek")),
    "mistral": ProviderSpec("mistral", "Mistral", "openai_compat", "https://api.mistral.ai/v1", "MISTRAL_API_KEY", "mistral-small-2603", "mistral-small-2603", 256_000, False, ("openai", "deepseek")),
    "xai": ProviderSpec("xai", "xAI", "openai_compat", "https://api.x.ai/v1", "XAI_API_KEY", "grok-4.7", "grok-4.7", 500_000, False, ("openai", "deepseek")),
    "together": ProviderSpec("together", "Together AI", "openai_compat", "https://api.together.ai/v1", "TOGETHER_API_KEY", "MiniMaxAI/MiniMax-M3", "MiniMaxAI/MiniMax-M3", 196_608, False, ("openai", "deepseek")),
    "fireworks": ProviderSpec("fireworks", "Fireworks AI", "openai_compat", "https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY", "accounts/fireworks/models/deepseek-v3p1", "accounts/fireworks/models/deepseek-v3p1", 1_048_576, False, ("openai", "deepseek")),
    "cohere": ProviderSpec("cohere", "Cohere", "openai_compat", "https://api.cohere.ai/compatibility/v1", "COHERE_API_KEY", "command-a-plus-05-2026", "command-a-plus-05-2026", 256_000, False, ("openai", "deepseek")),
    # Azure deployments, Bedrock model access, and Vertex locations are
    # account/region scoped; leave their model slots explicit by design.
    "azure_openai": ProviderSpec("azure_openai", "Azure OpenAI", "azure_openai", "", "AZURE_OPENAI_API_KEY", "", "", None, False, ("openai", "deepseek")),
    "perplexity": ProviderSpec("perplexity", "Perplexity", "responses", "https://api.perplexity.ai", "PERPLEXITY_API_KEY", "perplexity/sonar", "perplexity/sonar", 1_000_000, False, ("openai", "deepseek")),
    "bedrock": ProviderSpec("bedrock", "Amazon Bedrock", "bedrock_converse", "", "", "", "", None, False, ("openai", "deepseek")),
    "vertex": ProviderSpec("vertex", "Google Vertex AI", "vertex_genai", "", "", "", "", None, False, ("openai", "deepseek")),
    "custom": ProviderSpec("custom", "Custom OpenAI-compatible", "openai_compat", "", "", "", "", None, False),
}


PROVIDER_DEFAULT_MODELS = {
    name: (spec.model_simple, spec.model_complex) for name, spec in PROVIDER_REGISTRY.items()
}


PROVIDER_DISPLAY_NAMES = {name: spec.display_name for name, spec in PROVIDER_REGISTRY.items()}


PROVIDER_AUTO_SELECTION = frozenset(
    name for name, spec in PROVIDER_REGISTRY.items() if spec.auto_selection
)


PROVIDER_ENV_VARS = {name: spec.api_key_env for name, spec in PROVIDER_REGISTRY.items()}


PROVIDER_BASE_URLS = {name: spec.base_url for name, spec in PROVIDER_REGISTRY.items()}


PROVIDER_FALLBACKS = {
    name: list(spec.fallbacks) for name, spec in PROVIDER_REGISTRY.items()
}


PROVIDER_CONTEXT_WINDOWS = {
    name: spec.context_window_tokens for name, spec in PROVIDER_REGISTRY.items()
    if spec.context_window_tokens is not None
}


PROVIDER_COSTS: dict[str, tuple[float, float]] = {
    "deepseek":  (0.27, 1.10),
    "openai":    (2.50, 10.00),
    "anthropic": (3.00, 15.00),
    "google":    (0.15, 0.60),
    "ollama":    (0.0, 0.0),
}


AMBIENT_CREDENTIAL_PROVIDERS = frozenset({"ollama", "bedrock", "vertex"})


_PICOS_PER_DOLLAR = 10**12


_TOKENS_PER_MILLION = 1_000_000


_DEEPSEEK_V4_EFFECTIVE_AT = datetime(2026, 8, 16, 16, tzinfo=timezone.utc)


_DEEPSEEK_V4_MODELS = {
    "deepseek-v4-flash": ("0.014", "0.44", "1.32"),
    "deepseek-v4-flash-vision-exp": ("0.014", "0.44", "1.32"),
    "deepseek-v4-pro": ("0.044", "1.32", "3.96"),
}


_DEEPSEEK_V4_ALIASES = {
    "deepseek-chat": "deepseek-v4-flash",
    "deepseek-reasoner": "deepseek-v4-flash",
}
