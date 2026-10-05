from __future__ import annotations

import os
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from openkyrozen.providers.config import ProviderConfig

def _get_encryption_key() -> bytes:
    """Derive a machine-specific encryption key from hostname + platform."""
    import hashlib, platform, socket
    seed = f"{socket.gethostname()}:{platform.node()}:openkyrozen"
    return hashlib.sha256(seed.encode()).digest()


def _get_fernet(*, create: bool = True):
    """Return the per-install Fernet cipher, optionally without creating it."""
    from cryptography.fernet import Fernet

    secret_path = os.path.expanduser("~/.kyrozen_secret")
    try:
        with open(secret_path, "rb") as f:
            key = f.read().strip()
    except FileNotFoundError:
        if not create:
            return None
        key = Fernet.generate_key()
        try:
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            with open(secret_path, "rb") as f:
                key = f.read().strip()
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(key)
    os.chmod(secret_path, 0o600)
    return Fernet(key)


def encrypt_api_key(plaintext: str) -> str:
    """Encrypt an API key with a random per-install Fernet key."""
    if not plaintext:
        return ""
    return "v2:" + _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_api_key(ciphertext: str) -> str:
    """Decrypt a Fernet key, with backward-compatible support for legacy XOR."""
    import base64
    if not ciphertext:
        return ""
    if ciphertext.startswith("v2:"):
        try:
            fernet = _get_fernet(create=False)
            return fernet.decrypt(ciphertext[3:].encode()).decode() if fernet else ""
        except Exception:
            return ""
    try:
        # Legacy configs used reversible XOR with a machine-derived key.
        key = _get_encryption_key()
        encrypted = base64.b64decode(ciphertext)
        decrypted = bytes(e ^ key[i % len(key)] for i, e in enumerate(encrypted))
        return decrypted.decode()
    except Exception:
        return ciphertext  # return as-is if not encrypted (backward compat)


def save_provider_config_encrypted(config: ProviderConfig) -> None:
    """Save provider settings with encrypted API key."""
    import json
    config_path = os.path.expanduser("~/.kyrozen_config.json")
    existing: dict[str, Any] = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    existing["provider"] = config.provider
    existing["base_url"] = config.base_url
    existing["api_key"] = encrypt_api_key(config.api_key)
    existing["model_simple"] = config.model_simple
    existing["model_complex"] = config.model_complex
    if config.model_main:
        existing["model_main"] = config.model_main
    else:
        existing.pop("model_main", None)
    if config.custom_profile:
        existing["custom_profile"] = config.custom_profile
    else:
        existing.pop("custom_profile", None)
    if config.context_window_tokens is None:
        existing.pop("context_window_tokens", None)
    else:
        existing["context_window_tokens"] = config.context_window_tokens
    existing["encrypted"] = True
    existing["encryption"] = "fernet"
    try:
        with open(config_path, "w") as f:
            json.dump(existing, f, indent=2)
        os.chmod(config_path, 0o600)
    except OSError:
        pass
