from __future__ import annotations

import os
from functools import lru_cache
from dataclasses import dataclass
from openkyrozen.providers.registry import AMBIENT_CREDENTIAL_PROVIDERS, PROVIDER_AUTO_SELECTION, PROVIDER_BASE_URLS, PROVIDER_DEFAULT_MODELS, PROVIDER_ENV_VARS

@dataclass
class ProviderConfig:
    provider: str = "deepseek"
    api_key: str = ""
    base_url: str = ""
    model_simple: str = ""
    model_complex: str = ""
    context_window_tokens: int | None = None

    def __post_init__(self) -> None:
        if not self.model_simple:
            self.model_simple = os.environ.get(
                "KYROZEN_MODEL_SIMPLE", "",
            ) or PROVIDER_DEFAULT_MODELS.get(self.provider, ("", ""))[0]
        if not self.model_complex:
            self.model_complex = os.environ.get(
                "KYROZEN_MODEL_COMPLEX", "",
            ) or PROVIDER_DEFAULT_MODELS.get(self.provider, ("", ""))[1]
        if not self.base_url:
            self.base_url = os.environ.get(
                "KYROZEN_BASE_URL", "",
            ) or PROVIDER_BASE_URLS.get(self.provider, "")
        if self.provider == "azure_openai" and not self.base_url:
            self.base_url = os.environ.get("AZURE_OPENAI_ENDPOINT", "")

    def validate(self) -> list[str]:
        """Validate the configuration. Returns a list of warnings/errors."""
        issues: list[str] = []
        if self.provider not in PROVIDER_DEFAULT_MODELS:
            issues.append(f"Unknown provider '{self.provider}'")
        if self.provider in {"azure_openai", "bedrock", "vertex"} and not self.model_simple:
            issues.append(f"No model/deployment configured for {self.provider}")
        if self.provider in AMBIENT_CREDENTIAL_PROVIDERS and not _ambient_provider_available(self.provider):
            issues.append(f"No ambient credentials available for {self.provider}")
        if self.provider not in AMBIENT_CREDENTIAL_PROVIDERS and not self.api_key and not _provider_env_key(self.provider):
            env_var = PROVIDER_ENV_VARS.get(self.provider, "")
            if self.provider == "azure_openai" and self.base_url and _azure_identity_available(self.base_url):
                pass
            else:
                issues.append(f"No API key for {self.provider} (set {env_var} or KYROZEN_API_KEY)")
        if self.model_simple and self.model_simple not in ("", "auto"):
            pass  # model name is user-specified, can't validate here
        return issues


def _provider_env_key(provider: str) -> str:
    env_var = PROVIDER_ENV_VARS.get(provider, "")
    return os.environ.get("KYROZEN_API_KEY", "") or (os.environ.get(env_var, "") if env_var else "")


def _azure_identity_available(endpoint: str | None = None) -> bool:
    """Return whether Azure Entra credentials can be resolved without prompting."""
    if not (endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT")):
        return False
    try:
        from azure.identity import DefaultAzureCredential
        DefaultAzureCredential(exclude_interactive_browser_credential=True)
        return True
    except Exception:
        return False


def _ambient_provider_available(provider: str) -> bool:
    if provider == "ollama":
        return True
    if provider == "bedrock":
        if not (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")):
            return False
        try:
            import boto3
            return boto3.Session().get_credentials() is not None
        except ImportError:
            return bool(os.environ.get("AWS_PROFILE") or os.environ.get("AWS_ACCESS_KEY_ID"))
        except Exception:
            return False
    if provider == "vertex":
        if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
            return False
        if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.environ.get("GOOGLE_GENAI_USE_VERTEXAI") == "True":
            return True
        try:
            import google.auth
            google.auth.default()
            return True
        except Exception:
            return False
    if provider == "azure_openai":
        return _azure_identity_available()
    return False


def provider_is_configured(config: ProviderConfig) -> bool:
    """Whether a provider has a key or can use its documented ambient auth."""
    ambient = _azure_identity_available(config.base_url) if config.provider == "azure_openai" else _ambient_provider_available(config.provider)
    return bool(config.api_key or _provider_env_key(config.provider)) or ambient


def model_for_complexity(config: ProviderConfig, complex_task: bool) -> str:
    """Resolve a provider-specific simple/complex slot without cross-provider names."""
    if config.provider in PROVIDER_AUTO_SELECTION:
        return (config.model_complex if complex_task else config.model_simple) or "auto"
    return config.model_complex or config.model_simple or "auto"


@lru_cache(maxsize=8)
def discover_ollama_models(base_url: str) -> tuple[tuple[str, str, int], ...]:
    """Return locally installed models as (name, modified_at, parameter_bytes)."""
    try:
        import requests
        endpoint = (base_url or "http://localhost:11434/v1").rstrip("/")
        endpoint = endpoint[:-3] if endpoint.endswith("/v1") else endpoint
        response = requests.get(f"{endpoint}/api/tags", timeout=2)
        response.raise_for_status()
        models = response.json().get("models", [])
        result: list[tuple[str, str, int]] = []
        for item in models:
            name = str(item.get("name") or item.get("model") or "").strip()
            if not name:
                continue
            result.append((name, str(item.get("modified_at") or ""), int(item.get("size") or 0)))
        return tuple(sorted(result, key=lambda item: (item[1], item[2], item[0]), reverse=True))
    except Exception:
        return ()


def resolve_ollama_models(config: ProviderConfig) -> tuple[str, str]:
    """Use explicit Ollama models, then the newest installed model inventory."""
    simple = os.environ.get("OLLAMA_MODEL_SIMPLE", "").strip()
    complex_model = os.environ.get("OLLAMA_MODEL_COMPLEX", "").strip()
    installed = discover_ollama_models(config.base_url)
    names = {item[0] for item in installed}
    configured_simple = config.model_simple not in {"", PROVIDER_DEFAULT_MODELS["ollama"][0]}
    configured_complex = config.model_complex not in {"", PROVIDER_DEFAULT_MODELS["ollama"][1]}
    if not simple and configured_simple:
        simple = config.model_simple
    if not complex_model and configured_complex:
        complex_model = config.model_complex
    if installed:
        simple = simple or (config.model_simple if config.model_simple in names else installed[0][0])
        complex_model = complex_model or (config.model_complex if config.model_complex in names else max(installed, key=lambda item: item[2])[0])
    return simple or config.model_simple, complex_model or config.model_complex
