from __future__ import annotations

import json
import os
import sys
from openkyrozen.providers import (ProviderConfig, PROVIDER_DEFAULT_MODELS, PROVIDER_ENV_VARS,
    PROVIDER_BASE_URLS, save_provider_config_encrypted, encrypt_api_key, decrypt_api_key,
    provider_is_configured, PROVIDER_DISPLAY_NAMES, discover_ollama_models, resolve_ollama_models)
from openkyrozen.providers.custom import (list_custom_provider_profiles, save_custom_provider_profile,
    remove_custom_provider_profile, select_custom_provider_profile, load_custom_provider_profile)


def custom_provider_profiles(self) -> list[dict[str, object]]:
    return [{key: value for key, value in profile.items() if key != "api_key"}
            for profile in list_custom_provider_profiles()]


def save_custom_provider(self, profile: dict[str, object], *, activate: bool = False) -> str:
    original_name = str(profile.get("_original_name", ""))
    saved = save_custom_provider_profile(profile, replace_name=original_name)
    active = bool(self._provider_config and self._provider_config.provider == "custom"
                  and self._provider_config.custom_profile.casefold() in
                  {str(saved["name"]).casefold(), original_name.casefold()})
    if activate or active:
        self._provider_config = select_custom_provider_profile(str(saved["name"]))
        self.llm_provider = self.get_fallback_provider(self._provider_config)
        self.DEEPSEEK_MODEL_SIMPLE = self._provider_config.model_simple
        self.DEEPSEEK_MODEL_COMPLEX = self._provider_config.model_complex
        self.DEEPSEEK_MODEL = self._provider_config.model_simple
        self.MODEL_NAME = f"custom ({saved['name']})"
    return f"Custom provider profile {saved['name']} saved." + (" Active for the main agent." if activate or active else "")


def use_custom_provider(self, name: str) -> str:
    self._provider_config = select_custom_provider_profile(name)
    if not provider_is_configured(self._provider_config):
        raise ValueError("Custom provider profile is incomplete.")
    self.llm_provider = self.get_fallback_provider(self._provider_config)
    self.DEEPSEEK_MODEL_SIMPLE = self._provider_config.model_simple
    self.DEEPSEEK_MODEL_COMPLEX = self._provider_config.model_complex
    self.DEEPSEEK_MODEL = self._provider_config.model_simple
    self.MODEL_NAME = f"custom ({name})"
    return f"Custom provider profile {name} is active for the main agent."


def delete_custom_provider(self, name: str) -> str:
    if not remove_custom_provider_profile(name):
        raise ValueError(f"No custom provider profile named {name!r}.")
    if self._provider_config and self._provider_config.provider == "custom" and self._provider_config.custom_profile.casefold() == name.casefold():
        self._provider_config = self.detect_provider()
        self.llm_provider = None
    return f"Custom provider profile {name} removed."


def _load_config_key(self) -> str | None:
    if os.path.exists(self.CONFIG_PATH):
        try:
            with open(self.CONFIG_PATH, "r") as f:
                data = json.load(f)
            key = data.get("api_key")
            if data.get("encrypted") and isinstance(key, str):
                key = decrypt_api_key(key)
            if key and isinstance(key, str) and key.strip():
                os.environ["DEEPSEEK_API_KEY"] = key.strip()
                return key.strip()
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _save_config_key(self, key: str) -> None:
    try:
        existing = {}
        if os.path.exists(self.CONFIG_PATH):
            with open(self.CONFIG_PATH, "r") as f:
                existing = json.load(f)
        existing["api_key"] = encrypt_api_key(key.strip())
        existing["encrypted"] = True
        existing["encryption"] = "fernet"
        with open(self.CONFIG_PATH, "w") as f:
            json.dump(existing, f, indent=2)
        os.chmod(self.CONFIG_PATH, 0o600)
    except OSError:
        self.console.print(f"[{self._WARNING}]Warning: could not save API key to config file.[/{self._WARNING}]")


def _prompt_and_init_deepseek(self,
    *, interactive: bool = True, config: ProviderConfig | None = None
) -> bool:
    """Detect the provider and initialise the LLM client.

    Ollama is a local, keyless provider.  Headless surfaces pass
    ``interactive=False`` so a missing remote credential produces a usable
    degraded process instead of reading stdin or raising during ASGI startup.
    Interactive CLI setup retains the credential prompt for providers that
    require one.  The return value reports whether a provider was initialised.
    """

    self._provider_config = config or self.detect_provider()
    self.llm_provider = None

    if self._provider_config.provider == "ollama":
        simple, complex_model = resolve_ollama_models(self._provider_config)
        self._provider_config.model_simple = simple
        self._provider_config.model_complex = complex_model
        if not (simple or complex_model or self._provider_config.model_main not in {"", "auto"}):
            if interactive:
                self._prompt_ollama_model()
                simple, complex_model = resolve_ollama_models(self._provider_config)
                self._provider_config.model_simple = simple
                self._provider_config.model_complex = complex_model
            if not (simple or complex_model or self._provider_config.model_main not in {"", "auto"}):
                self.console.print("Ollama needs an explicitly selected model; choose one with /model or enter its exact tag during setup.")
                return False

    # Ollama, Bedrock, Vertex, and Azure Entra may use documented ambient auth.
    if not provider_is_configured(self._provider_config):
        if self._provider_config.provider in {"ollama", "bedrock", "vertex"}:
            self.console.print(
                f"{PROVIDER_DISPLAY_NAMES.get(self._provider_config.provider, self._provider_config.provider)} selected: "
                "using ambient/local credentials."
            )
        elif self._provider_config.provider == "azure_openai" and self._provider_config.base_url:
            self.console.print("Azure OpenAI selected: using the configured endpoint and ambient Entra credentials.")
        elif not interactive:
            env_var = PROVIDER_ENV_VARS.get(self._provider_config.provider, "")
            hint = f" Set {env_var} or KYROZEN_API_KEY before sending chat requests." if env_var else ""
            self.console.print(
                f"[yellow]Degraded startup: {self._provider_config.provider.title()} API key is not configured."
                f" Headless mode will not prompt for credentials.{hint}[/yellow]"
            )
            self.DEEPSEEK_MODEL_SIMPLE = self._provider_config.model_simple
            self.DEEPSEEK_MODEL_COMPLEX = self._provider_config.model_complex
            self.DEEPSEEK_MODEL = self.DEEPSEEK_MODEL_SIMPLE
            self.MODEL_NAME = f"{self._provider_config.provider} ({self.DEEPSEEK_MODEL_SIMPLE})"
            return False
        else:
            self.console.print(f"\n{self._provider_config.provider.title()} API key not set.")
            env_var = PROVIDER_ENV_VARS.get(self._provider_config.provider, "")
            hint = f" (set {env_var})" if env_var else ""
            try:
                key = self.console.input(
                    f"[bold yellow]Enter your {self._provider_config.provider.title()} API key{hint}: [/bold yellow]"
                ).strip()
            except (EOFError, KeyboardInterrupt):
                self.console.print("\nCancelled.")
                sys.exit(0)
            if not key:
                self.console.print("No API key entered – use /quit to exit.")
                sys.exit(0)
            self._provider_config.api_key = key
            save_provider_config_encrypted(self._provider_config)

    # Set provider-specific env var for subprocesses / SDK auto-detection.
    env_var = PROVIDER_ENV_VARS.get(self._provider_config.provider, "")
    if env_var:
        os.environ[env_var] = self._provider_config.api_key

    # Set the model names from provider config
    self.DEEPSEEK_MODEL_SIMPLE = self._provider_config.model_simple
    self.DEEPSEEK_MODEL_COMPLEX = self._provider_config.model_complex
    self.DEEPSEEK_MODEL = self.DEEPSEEK_MODEL_SIMPLE
    self.MODEL_NAME = f"{self._provider_config.provider} ({self.DEEPSEEK_MODEL_SIMPLE})"

    # Create the provider instance (with fallback chain)
    self.llm_provider = self.get_fallback_provider(self._provider_config)

    # Validate configuration
    issues = self._provider_config.validate()
    if issues:
        for issue in issues:
            self.console.print(f"[{self._WARNING}]Config: {issue}[/{self._WARNING}]")
    return True


def _prompt_ollama_model(self) -> bool:
    """Ask for an exact Ollama model tag; discovered models are suggestions only."""
    config = self._provider_config
    installed = discover_ollama_models(config.base_url)
    if installed:
        self.console.print("Installed Ollama models:")
        for name, _, _ in installed:
            self.console.print(f"  {name}")
    else:
        self.console.print("No Ollama models discovered. Enter an exact installed model tag.")
    try:
        model = self.console.input("Ollama model tag (or cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        return False
    if not model or model.lower() == "cancel":
        return False
    config.model_simple = config.model_complex = model
    self.save_provider_config_encrypted(config)
    return True


def set_main_model(self, model: str) -> str:
    """Pin every main-agent request to a model, or restore automatic routing."""
    config = self._provider_config or self.detect_provider()
    selected = model.strip()
    if not selected:
        raise ValueError("Enter a model name or 'auto'.")
    if selected.lower() == "auto":
        if config.provider == "ollama" and not (config.model_simple or config.model_complex):
            raise ValueError("Ollama requires a model; enter its exact model tag.")
        config.model_main = ""
        result = "Automatic simple/complex model selection restored."
    else:
        config.model_main = selected
        if config.provider == "ollama" and not (config.model_simple or config.model_complex):
            config.model_simple = config.model_complex = selected
        result = f"Main model pinned to {selected}."
    self.save_provider_config_encrypted(config)
    self._provider_config = config
    self.DEEPSEEK_MODEL_SIMPLE = config.model_simple
    self.DEEPSEEK_MODEL_COMPLEX = config.model_complex
    self.DEEPSEEK_MODEL = config.model_main or config.model_simple
    self.MODEL_NAME = f"{config.provider} ({config.model_main or config.model_simple})"
    if config.provider == "ollama" and not (config.model_simple or config.model_complex or config.model_main not in {"", "auto"}):
        self.llm_provider = None
        return result
    if not provider_is_configured(config):
        self.llm_provider = None
        return result
    self.llm_provider = self.get_fallback_provider(config)
    return result


def _switch_provider(self) -> None:
    """Interactive menu to switch LLM provider at runtime."""

    providers_list = list(PROVIDER_DEFAULT_MODELS.keys())
    self.console.print(f"\n[bold {self._ACCENT}]═══ Switch LLM Provider ═══[/bold {self._ACCENT}]")
    for i, p in enumerate(providers_list):
        marker = " ●" if self._provider_config.provider == p else "  "
        self.console.print(f"  [{self._MUTED}]{i+1}.[/{self._MUTED}]{marker} {PROVIDER_DISPLAY_NAMES.get(p, p)}")
    self.console.print()

    choice = self.console.input("[bold cyan]Choose provider (number) or 'cancel': [/bold cyan]").strip().lower()
    if choice == "cancel":
        return
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(providers_list):
            new_provider = providers_list[idx]
            if new_provider == "custom":
                self._handle_cli_command("/custom-provider create", None)
                return
            if new_provider == self._provider_config.provider:
                self.console.print(f"[{self._WARNING}]Already using {PROVIDER_DISPLAY_NAMES.get(new_provider, new_provider)}.[/{self._WARNING}]")
                return
            previous_provider = self._provider_config.provider
            self._provider_config.provider = new_provider
            if previous_provider == "custom":
                self._provider_config.api_key = ""
                self._provider_config.custom_profile = ""
                self._provider_config.base_url = PROVIDER_BASE_URLS.get(new_provider, "")
            self._provider_config.model_simple = PROVIDER_DEFAULT_MODELS[new_provider][0]
            self._provider_config.model_complex = PROVIDER_DEFAULT_MODELS[new_provider][1]
            self._provider_config.model_main = ""

            # Prompt for API key if needed
            env_var = PROVIDER_ENV_VARS.get(new_provider, "")
            if env_var:
                key = self.console.input(
                    f"[bold yellow]Enter {PROVIDER_DISPLAY_NAMES.get(new_provider, new_provider)} API key "
                    f"(or press Enter to use {env_var}): [/bold yellow]"
                ).strip()
                if key:
                    self._provider_config.api_key = key

            save_provider_config_encrypted(self._provider_config)

            # Re-initialize the provider
            env_var = PROVIDER_ENV_VARS.get(new_provider, "")
            if env_var and self._provider_config.api_key:
                os.environ[env_var] = self._provider_config.api_key

            self.DEEPSEEK_MODEL_SIMPLE = self._provider_config.model_simple
            self.DEEPSEEK_MODEL_COMPLEX = self._provider_config.model_complex
            self.DEEPSEEK_MODEL = self.DEEPSEEK_MODEL_SIMPLE
            self.MODEL_NAME = f"{new_provider} ({self.DEEPSEEK_MODEL_SIMPLE})"
            if new_provider == "ollama" and not self._prompt_ollama_model():
                self.llm_provider = None
                return
            self.llm_provider = self.get_provider(self._provider_config)

            self.console.print(f"[{self._SUCCESS}]Switched to {PROVIDER_DISPLAY_NAMES.get(new_provider, new_provider)} ({self.DEEPSEEK_MODEL_SIMPLE}).[/{self._SUCCESS}]")
        else:
            self.console.print(f"[{self._ERROR}]Invalid number.[/{self._ERROR}]")
    except ValueError:
        self.console.print(f"[{self._ERROR}]Please enter a number or 'cancel'.[/{self._ERROR}]")
