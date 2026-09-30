from __future__ import annotations

import json
import os
import openkyrozen.routing.policy as system_one_policy
import openkyrozen.routing.models as routing_models
import openkyrozen.routing.transport as _routing_transport
import openkyrozen.routing.kev as _routing_kev

def decision_assist_state() -> dict[str, object]:
    """Return workspace-level assist settings without exposing credentials."""
    backend = os.environ.get("KYROZEN_DECISION_ASSIST_BACKEND", "").strip().lower()
    consent = False
    try:
        value = json.loads(_routing_transport._config_path().read_text(encoding="utf-8"))
        saved = value.get(routing_models._DECISION_ASSIST_KEY, {}) if isinstance(value, dict) else {}
        if not backend:
            backend = str(saved.get("backend") or "off").lower()
        consent = bool(saved.get("kev_private_consent"))
    except (OSError, ValueError, TypeError):
        pass
    if backend not in routing_models.DECISION_ASSIST_BACKENDS:
        backend = "off"
    model_info = _routing_transport.jev_model_info() if backend == "jev" or _routing_transport.jev_key() else dict(routing_models._JEV_MODEL_CACHE)
    calibration = system_one_policy.status()
    return {
        "backend": backend,
        "kev_private_consent": consent,
        "jev_configured": bool(_routing_transport.jev_key()),
        # A revoked workspace must not report the private local runtime as ready.
        # Jev may still fall back to Kev only after the same explicit consent.
        "kev_ready": _routing_kev._kev_ready() if consent else False,
        "jev_model_alias": model_info.get("alias", routing_models.JEV_MODEL),
        "jev_model_release_date": model_info.get("release_date"),
        "jev_health": model_info.get("health", "unknown"),
        "jev_fallback_reason": model_info.get("fallback_reason", ""),
        "calibration": calibration,
    }


def set_decision_assist(backend: str, *, kev_private_consent: bool = False,
                        api_key: str | None = None) -> dict[str, object]:
    """Persist assist selection; Kev setup is explicit and Jev keys stay separate."""
    backend = str(backend or "").strip().lower()
    if backend not in routing_models.DECISION_ASSIST_BACKENDS:
        raise ValueError("decision assist backend must be off, jev, or kev")
    if api_key:
        _routing_transport.save_jev_key(api_key)
    if backend == "jev" and not _routing_transport.jev_key():
        raise ValueError("Jev API key is required")
    if backend == "kev":
        if not kev_private_consent:
            raise ValueError("Kev private-context consent is required")
        _routing_kev.setup_kev()
    path = _routing_transport._config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[routing_models._DECISION_ASSIST_KEY] = {
        "backend": backend,
        "kev_private_consent": bool(kev_private_consent),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(data, output, indent=2)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return decision_assist_state()


def revoke_decision_assist_consent() -> dict[str, object]:
    path = _routing_transport._config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    saved = data.get(routing_models._DECISION_ASSIST_KEY, {})
    if not isinstance(saved, dict):
        saved = {}
    saved["kev_private_consent"] = False
    data[routing_models._DECISION_ASSIST_KEY] = saved
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(data, output, indent=2)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return decision_assist_state()
