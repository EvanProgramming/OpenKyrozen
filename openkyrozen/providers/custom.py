"""Persistence and selection for user-defined OpenAI-compatible profiles."""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

from openkyrozen.providers.config import ProviderConfig
from openkyrozen.security.credentials import decrypt_api_key, encrypt_api_key, save_provider_config_encrypted


def _read() -> tuple[str, dict]:
    path = os.path.expanduser("~/.kyrozen_config.json")
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
            return path, data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return path, {}


def list_custom_provider_profiles() -> list[dict[str, object]]:
    _, data = _read()
    profiles = data.get("custom_profiles", [])
    if not isinstance(profiles, list):
        return []
    result = []
    for item in profiles:
        if not isinstance(item, dict):
            continue
        profile = dict(item)
        if profile.get("encrypted"):
            profile["api_key"] = decrypt_api_key(str(profile.get("api_key", "")))
        result.append(profile)
    return result


def save_custom_provider_profile(profile: dict[str, object], *, replace_name: str = "") -> dict[str, object]:
    name = str(profile.get("name", "")).strip()
    endpoint = str(profile.get("base_url", "")).strip()
    simple = str(profile.get("model_simple", "")).strip()
    complex_model = str(profile.get("model_complex", "")).strip()
    parsed = urlsplit(endpoint)
    if not name or len(name) > 80:
        raise ValueError("Profile name must contain 1 to 80 characters.")
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or len(endpoint) > 2048):
        raise ValueError("Endpoint must be an HTTP or HTTPS URL.")
    if not simple or not complex_model:
        raise ValueError("Enter both simple and complex model IDs.")
    if len(simple) > 200 or len(complex_model) > 200:
        raise ValueError("Model IDs must be 200 characters or fewer.")
    raw_context = profile.get("context_window_tokens")
    if isinstance(raw_context, bool):
        raise ValueError("Context limit must be an integer.")
    if isinstance(raw_context, float) and not raw_context.is_integer():
        raise ValueError("Context limit must be an integer.")
    context = None if raw_context in (None, "") else int(raw_context)
    config = ProviderConfig(provider="custom", base_url=endpoint, api_key=str(profile.get("api_key", "")),
                            model_simple=simple, model_complex=complex_model, context_window_tokens=context)
    issues = config.validate()
    if issues:
        raise ValueError(" ".join(issues))
    path, data = _read()
    existing = data.get("custom_profiles", [])
    profiles = existing if isinstance(existing, list) else []
    encrypted = dict(name=name, base_url=endpoint, api_key=encrypt_api_key(config.api_key),
                     model_simple=simple, model_complex=complex_model,
                     context_window_tokens=context, encrypted=True)
    replaced = {name.casefold(), replace_name.strip().casefold()}
    profiles = [item for item in profiles if not isinstance(item, dict)
                or str(item.get("name", "")).casefold() not in replaced]
    profiles.append(encrypted)
    data["custom_profiles"] = profiles
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
    os.chmod(path, 0o600)
    return dict(name=name, base_url=endpoint, api_key=config.api_key, model_simple=simple,
                model_complex=complex_model, context_window_tokens=context)


def remove_custom_provider_profile(name: str) -> bool:
    path, data = _read()
    profiles = data.get("custom_profiles", [])
    if not isinstance(profiles, list):
        return False
    remaining = [item for item in profiles if not isinstance(item, dict) or str(item.get("name", "")).casefold() != name.strip().casefold()]
    if len(remaining) == len(profiles):
        return False
    data["custom_profiles"] = remaining
    if str(data.get("custom_profile", "")).casefold() == name.strip().casefold():
        data.update(provider="deepseek", api_key="", base_url="", model_simple="", model_complex="")
        data.pop("custom_profile", None)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
    os.chmod(path, 0o600)
    return True


def select_custom_provider_profile(name: str) -> ProviderConfig:
    config = load_custom_provider_profile(name)
    save_provider_config_encrypted(config)
    return config


def load_custom_provider_profile(name: str) -> ProviderConfig:
    profile = next((item for item in list_custom_provider_profiles()
                    if str(item.get("name", "")).casefold() == name.strip().casefold()), None)
    if profile is None:
        raise ValueError(f"No custom provider profile named {name!r}.")
    config = ProviderConfig(provider="custom", custom_profile=str(profile["name"]),
                           base_url=str(profile["base_url"]), api_key=str(profile.get("api_key", "")),
                           model_simple=str(profile["model_simple"]), model_complex=str(profile["model_complex"]),
                           context_window_tokens=profile.get("context_window_tokens"))
    return config
