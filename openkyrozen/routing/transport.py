from __future__ import annotations

import json
import os
import time
from pathlib import Path
import requests
from openkyrozen.security.credentials import decrypt_api_key, encrypt_api_key
import openkyrozen.routing.models as routing_models

def _config_path() -> Path:
    return Path.home() / ".kyrozen_config.json"


def jev_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    try:
        value = json.loads(_config_path().read_text(encoding="utf-8"))
        return decrypt_api_key(str(value.get("jev_api_key") or ""))
    except (OSError, ValueError, TypeError):
        return ""


def jev_model_info(*, force: bool = False) -> dict[str, object]:
    """Discover the provider's stable alias without exposing the API key."""
    now = time.monotonic()
    key = jev_key()
    if not key:
        routing_models._JEV_MODEL_CACHE.update({"checked_at": now, "health": "unconfigured",
                                 "fallback_reason": "api_key_missing", "alias": routing_models.JEV_MODEL})
        return dict(routing_models._JEV_MODEL_CACHE)
    if not force and routing_models._JEV_MODEL_CACHE.get("health") != "unconfigured" \
            and now - float(routing_models._JEV_MODEL_CACHE.get("checked_at", 0)) < routing_models._JEV_MODEL_REFRESH_SECONDS:
        return dict(routing_models._JEV_MODEL_CACHE)
    try:
        response = requests.get(routing_models._JEV_URL + "/v1/models",
                                headers={"Authorization": f"Bearer {key}"}, timeout=5)
        response.raise_for_status()
        models = response.json().get("models", [])
        names = {str(item.get("name")) for item in models if isinstance(item, dict)}
        alias = routing_models.JEV_MODEL if routing_models.JEV_MODEL in names else next(
            (name for name in sorted(names) if name.startswith("jev-")), routing_models.JEV_MODEL,
        )
        release_date = next((item.get("release_date") for item in models
                             if isinstance(item, dict) and item.get("name") == alias), None)
        routing_models._JEV_MODEL_CACHE.update({"checked_at": now, "alias": alias, "release_date": release_date,
                                 "health": "ready", "fallback_reason": ""})
    except (OSError, ValueError, TypeError, requests.RequestException) as exc:
        routing_models._JEV_MODEL_CACHE.update({"checked_at": now, "health": "degraded",
                                 "fallback_reason": type(exc).__name__})
    return dict(routing_models._JEV_MODEL_CACHE)


def save_jev_key(key: str) -> None:
    key = str(key).strip()
    if not key:
        raise ValueError("Jev API key is required")
    path = _config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["jev_api_key"] = encrypt_api_key(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(data, output, indent=2)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    routing_models._JEV_MODEL_CACHE["checked_at"] = 0.0


def _request(backend: str, questions: dict, state: object, *, timeout: float = 3.0) -> dict:
    from .kev import _kev_key
    if backend == "jev":
        key, base, model = jev_key(), routing_models._JEV_URL, jev_model_info().get("alias", routing_models.JEV_MODEL)
        if not key:
            raise RuntimeError("Jev API key is missing")
    elif backend == "kev":
        key, base, model = _kev_key(), routing_models.KEV_URL, routing_models.KEV_MODEL
    else:
        raise ValueError("System One backend must be jev or kev")
    started = time.perf_counter()
    response = requests.post(
        base + "/v1/systemone",
        json={"state": state, "model": model, "questions": questions},
        headers={"Authorization": f"Bearer {key}"}, timeout=timeout,
    )
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        raise ValueError("System One response has no answers")
    result["wall_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result
