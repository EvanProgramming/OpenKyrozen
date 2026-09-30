from __future__ import annotations

import urllib.parse
import os
import threading
from typing import Any
from openkyrozen.providers import ProviderConfig, usage_scope


def learning_runtime(self) -> dict[str, str]:
    """Return the user-selected learning runtime for this workspace."""
    return self.memory_bank.store.get_learning_runtime(
        user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
    )


def learning_policy(self) -> str:
    """Compatibility accessor for the selected learning mode."""
    return self.learning_runtime()["mode"]


def _set_learning_runtime(self, mode: str, status: str, *, model: str = "", detail: str = "") -> dict[str, str]:
    self.memory_bank.store.set_learning_runtime(
        mode, status, model=model, detail=detail,
        user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
    )
    return self.learning_runtime()


def set_learning_policy(self, mode: str) -> str:
    """Select Remote immediately or start the explicitly requested Local setup."""
    if mode not in {"local", "remote"}:
        raise ValueError("learning mode must be local or remote")
    if mode == "remote":
        self._set_learning_runtime("remote", "ready", detail="Uses the configured chat provider and model.")
    else:
        self._set_learning_runtime("local", "installing", model=self._LOCAL_LEARNING_MODEL,
                              detail="Preparing local Ollama learning runtime.")
        threading.Thread(target=self._bootstrap_local_learning, daemon=True,
                         name="kyrozen-local-learning-setup").start()
    return mode


def _restore_self_learning_flags(self) -> dict[str, bool]:
    """Load persisted feature switches without widening the learning scope."""
    store_owner = getattr(self, "memory_bank", None)
    if store_owner is None:
        return {}
    try:
        persisted = store_owner.store.list_learning_feature_flags(
            user_id=store_owner.user_id, workspace_id=store_owner.workspace_id,
        )
    except Exception:
        return {}
    restored: dict[str, bool] = {}
    for name in self._LEARNING_FEATURE_ORDER:
        enabled = bool(persisted.get(name, True))
        self._SELF_LEARNING_FLAGS[name] = enabled
        restored[name] = enabled
    return restored


def _learning_provider_class(self) -> str:
    runtime = self.learning_runtime()
    if runtime["status"] != "ready":
        return "none"
    return "ollama" if runtime["mode"] == "local" else "remote"


def learning_cost_source(self) -> str:
    """Describe where the selected learning runtime consumes resources."""
    runtime = self.learning_runtime()
    if runtime["mode"] == "local":
        return "Local CPU/RAM/disk; no API cost"
    if runtime["mode"] == "remote":
        return "Configured chat provider API"
    return "No learning model selected"


def _learning_model_response(self, messages: list[dict[str, str]], *, feature: str) -> str | None:
    """Route semantic learning only through the user-selected runtime."""
    runtime = self.learning_runtime()
    if runtime["status"] != "ready":
        self._record_learning_event("learning.model_skipped", {
            "feature": feature, "mode": runtime["mode"], "reason": runtime["detail"],
        })
        return None

    if runtime["mode"] == "remote":
        if self.llm_provider is None or self._provider_config is None:
            self._record_learning_event("learning.model_skipped", {
                "feature": feature, "mode": "remote", "reason": "Configure a chat provider first.",
            })
            return None
        try:
            with usage_scope(
                    store=self.memory_bank.store, user_id=self.memory_bank.user_id,
                    workspace_id=self.memory_bank.workspace_id, session_id=self.memory_bank.session_id,
                    run_id=self._active_usage_run_id.get(), surface="learning"):
                text, _ = self._bounded_provider_call(
                    lambda: self.llm_provider.chat(messages, self._provider_config.model_simple)
                )
            return text
        except Exception as exc:
            self._record_learning_event("learning.model_failed", {
                "feature": feature, "provider": self._provider_config.provider, "error": str(exc)[:500],
            })
            return None

    base_url = os.environ.get("KYROZEN_LEARNING_OLLAMA_BASE_URL", "http://localhost:11434/v1")
    hostname = urllib.parse.urlparse(base_url).hostname
    if hostname not in {"localhost", "127.0.0.1", "::1"}:
        self._record_learning_event("learning.model_skipped", {
            "feature": feature, "mode": "local",
            "reason": "Ollama learning endpoint must be local",
        })
        return None
    model = runtime["model"] or self._LOCAL_LEARNING_MODEL
    config = ProviderConfig(provider="ollama_native", base_url=base_url, model_simple=model, model_complex=model)
    provider = self.get_provider(config)  # Deliberately not get_fallback_provider().
    try:
        with usage_scope(
                store=self.memory_bank.store, user_id=self.memory_bank.user_id,
                workspace_id=self.memory_bank.workspace_id, session_id=self.memory_bank.session_id,
                run_id=self._active_usage_run_id.get(), surface="learning"):
            text, _ = provider.chat(messages, model)
        return text
    except Exception as exc:
        self._record_learning_event("learning.model_failed", {
            "feature": feature, "provider": "ollama", "error": str(exc)[:500],
        })
        return None
