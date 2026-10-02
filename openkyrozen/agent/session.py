from __future__ import annotations

from typing import Any
from openkyrozen.tasks.engine import TaskManager
from openkyrozen.agent.modes import InteractionController, InteractionError
from openkyrozen.workspace.context import LaunchContext, source_scope_id


def _restore_ponytail_level(self) -> str:
    events = self.memory_bank.store.list_events(
        "ponytail.preference", limit=1, workspace_id=self.memory_bank.workspace_id,
        user_id=self.memory_bank.user_id,
    )
    value = str(events[0]["payload"].get("level", "full")) if events else "full"
    self._ponytail_level = value if value in {"off", "lite", "full", "ultra"} else "full"
    if self._ponytail_level == "off":
        self.skill_registry.disabled_builtins.add("ponytail")
    else:
        self.skill_registry.disabled_builtins.discard("ponytail")
    return self._ponytail_level


def set_ponytail_level(self, level: str) -> str:
    value = str(level).strip().lower()
    if value not in {"off", "lite", "full", "ultra"}:
        raise ValueError("Ponytail level must be off, lite, full, or ultra")
    self._ponytail_level = value
    if value == "off":
        self.skill_registry.disabled_builtins.add("ponytail")
    else:
        self.skill_registry.disabled_builtins.discard("ponytail")
    self.memory_bank.store.append_event(
        "ponytail.preference", {"level": value}, user_id=self.memory_bank.user_id,
        workspace_id=self.memory_bank.workspace_id, session_id=self.memory_bank.session_id,
    )
    return value


def _ponytail_context(self, profile: str) -> str:
    if self._active_interaction_mode.get() == "ask" or profile != "coder" or self._ponytail_level == "off":
        return ""
    variants = {
        "lite": "Build the requested behavior, and mention a materially simpler alternative when one exists.",
        "full": "Use the first adequate rung: existing code, standard library, native platform, installed dependency, then minimum new code.",
        "ultra": "Delete or decline speculative machinery; implement only behavior required by current acceptance criteria.",
    }
    return f"<ponytail level=\"{self._ponytail_level}\">{variants[self._ponytail_level]} Never simplify away safety or verification.</ponytail>"


def interaction_envelope(self, user_input: str = "") -> dict[str, Any]:
    return self._interaction_controller.envelope(user_input)


def interaction_workspace_id(self, context: LaunchContext | None = None) -> str:
    """Keep project interaction state separate without splitting global memory."""
    active = context if context is not None else self._launch_context
    if isinstance(active, LaunchContext) and not active.is_global:
        return active.source_scope_id
    return self.memory_bank.workspace_id


def bind_interaction_scope(self, session_id: str, *, user_id: str | None = None) -> None:
    session = self.open_session(session_id, user_id=user_id)
    self._default_session = session
    current = self.history_manager(session_id).current()
    session.messages = (list(current.get("conversation", [])) if current is not None and current.get("kind") != "recovery" else [])
    self._restore_ponytail_level()


def set_interaction_mode(self, mode: str) -> dict[str, Any]:
    envelope = self._interaction_controller.set_mode(mode)
    self._emit_stream_event({"event": "interaction", "interaction": envelope})
    return envelope


def set_system_one_backend(self, backend: str, *, api_key: str | None = None) -> dict[str, Any]:
    fast_mode = self.fast_mode
    backend = str(backend or "").strip().lower()
    if backend == "jev":
        if api_key:
            fast_mode.save_jev_key(api_key)
        if not fast_mode.jev_key():
            raise InteractionError("Jev API key is required; set TYPESAFE_API_KEY or enter it in System One settings")
    elif backend == "kev":
        fast_mode.setup_kev()
    envelope = self._interaction_controller.set_system_one_backend(backend)
    self._emit_stream_event({"event": "interaction", "interaction": envelope})
    return envelope


def set_fast_backend(self, backend: str, *, api_key: str | None = None) -> dict[str, Any]:
    """Deprecated compatibility alias for System One."""
    return self.set_system_one_backend(backend, api_key=api_key)


def _record_fast_decision(self, backend: str, details: dict[str, Any]) -> None:
    try:
        self.memory_bank.store.append_event(
            "decision.fast", {"backend": backend, **details},
            user_id=self._interaction_controller.user_id, workspace_id=self._interaction_controller.workspace_id,
            session_id=self._interaction_controller.session_id,
        )
    except Exception:
        pass  # Diagnostics must never block the ordinary LLM path.


def _record_decision_assist(self, kind: str, details: dict[str, Any] | None = None) -> None:
    """Persist decision metadata without retaining the assessed content."""
    try:
        payload = {"kind": kind, **(details or {})}
        self.memory_bank.store.append_event(
            "decision.assist", payload, user_id=self.memory_bank.user_id,
            workspace_id=self.memory_bank.workspace_id, session_id=self.memory_bank.session_id,
        )
    except Exception:
        pass


def decision_assist_state(self) -> dict[str, object]:
    fast_mode = self.fast_mode
    return fast_mode.decision_assist_state()


def set_decision_assist(self, backend: str, *, private_consent: bool = False,
                        api_key: str | None = None) -> dict[str, object]:
    fast_mode = self.fast_mode
    state = fast_mode.set_decision_assist(
        backend, kev_private_consent=private_consent, api_key=api_key,
    )
    self._record_decision_assist("settings", {"backend": state["backend"],
                                          "private_consent": state["kev_private_consent"]})
    return state


def revoke_decision_assist_consent(self) -> dict[str, object]:
    fast_mode = self.fast_mode
    state = fast_mode.revoke_decision_assist_consent()
    self._record_decision_assist("consent_revoked", {"backend": state["backend"]})
    return state


def resolve_interaction_question(self, request_id: str, answers: Any, *, action: str = "answer") -> dict[str, Any]:
    return self._interaction_controller.resolve_question(request_id, answers, action=action)


def accept_interaction_plan(self, plan_id: str | None = None, version: int | None = None) -> tuple[dict[str, Any], bool]:
    return self._interaction_controller.accept_plan(self.tasks, plan_id=plan_id, version=version)


def cancel_interaction_plan(self, plan_id: str | None = None, version: int | None = None) -> dict[str, Any]:
    plan = self._interaction_controller.cancel_plan(plan_id, version)
    self._emit_stream_event({"event": "interaction", "interaction": self.interaction_envelope()})
    return plan
