from __future__ import annotations

import os
import re
import time
import datetime
from typing import Any
from openkyrozen.persistence.models import stable_hash


def _learning_timestamp(self) -> str:
    return datetime.datetime.now(self.UTC).isoformat()


def _learning_safe_text(self, value: Any, limit: int = 300) -> str:
    """Return bounded, secret-redacted text suitable for learning events."""
    text = str(value or "").replace("\n", " ").replace("\r", " ")
    text = re.sub(
        r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*\S+",
        r"\1=<redacted>", text,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "sk-<redacted>", text)
    return text[:limit]


def _record_learning_event(self, event_type: str, payload: dict[str, Any]) -> str | None:
    """Persist a scoped learning lifecycle event without breaking the agent."""
    try:
        return self.memory_bank.store.append_event(
            event_type, payload, user_id=self.memory_bank.user_id,
            workspace_id=self.memory_bank.workspace_id, session_id=self.memory_bank.session_id,
        )
    except Exception:
        return None


def _learning_state_fingerprint(self) -> tuple[Any, ...]:
    """Return a small durable-state snapshot used to detect real feature effects."""
    try:
        store = self.memory_bank.store
        user_id = self.memory_bank.user_id
        workspace_id = self.memory_bank.workspace_id
        session_id = self.memory_bank.session_id
        durable_state = store.learning_state_fingerprint(user_id=user_id, workspace_id=workspace_id, session_id=session_id, file_scope_id=self.memory_bank.file_scope_id)
        preference_state = tuple(sorted((key, str(value)) for key, value in self._user_preferences.items()))
        graph_state = tuple(
            sorted((source, tuple(sorted(targets))) for source, targets in self._knowledge_graph.items())
        )
        return (
            *durable_state, preference_state, graph_state,
            tuple(sorted(self._known_libraries)),
        )
    except Exception:
        # Test doubles and degraded stores may not expose SQLite.  The in-memory
        # portions still provide a useful best-effort change signal.
        return (
            tuple(sorted((key, str(value)) for key, value in self._user_preferences.items())),
            tuple(sorted((source, tuple(sorted(targets))) for source, targets in self._knowledge_graph.items())),
            tuple(sorted(self._known_libraries)),
        )


def _learning_result(self, *, changed: bool = False, detail: str = "") -> dict[str, Any]:
    return {"changed": bool(changed), "detail": self._learning_safe_text(detail)}


def _run_learning_context_compression(self, _context: dict[str, Any]) -> dict[str, Any]:
    """Compatibility record: foreground calls own model-window compaction."""
    return self._learning_result(
        changed=False,
        detail="Context compaction is model-window managed during foreground chat calls.",
    )


def _run_learning_technology(self, context: dict[str, Any]) -> dict[str, Any]:
    if self.learning_policy() != "remote":
        return self._learning_result(detail="requires Remote learning mode to fetch technology documentation")
    before = set(self._known_libraries)
    self._auto_patch_new_technology(str(context.get("user_input") or ""))
    return self._learning_result(changed=before != self._known_libraries, detail="technology scan queued")


def _run_learning_dynamic_tools(self, _context: dict[str, Any]) -> dict[str, Any]:
    dynamic_names = sorted(name for name in self.AVAILABLE_TOOLS if name not in self._BUILTIN_TOOL_NAMES)
    if not self.ALLOW_DYNAMIC_TOOLS:
        return self._learning_result(
            detail="dynamic tools remain disabled; capability and approval gates are required",
        )
    return self._learning_result(
        detail=(f"{len(dynamic_names)} dynamic tool(s) available; registration remains response-time "
                "DefineTool plus capability and approval gates"),
    )


def _run_learning_preferences(self, context: dict[str, Any]) -> dict[str, Any]:
    user_input = str(context.get("user_input") or "").strip()
    if not user_input:
        return self._learning_result(detail="requires a user turn containing preference signals")
    before = dict(self._user_preferences)
    self._detect_user_preferences(user_input)
    changed = {key: value for key, value in self._user_preferences.items() if before.get(key) != value and value}
    if changed:
        self._record_learning_event("learning.preference_updated", {
            "fields": changed, "source": "turn", "trigger": context.get("trigger", "turn"),
        })
    return self._learning_result(
        changed=bool(changed),
        detail=f"updated {len(changed)} preference field(s)" if changed else "no new preference detected",
    )


def _run_learning_memory_scoring(self, _context: dict[str, Any]) -> dict[str, Any]:
    rows = self.memory_bank.store.list_memories(
        status="active", limit=20, workspace_id=self.memory_bank.workspace_id,
        session_id=self.memory_bank.session_id, user_id=self.memory_bank.user_id,
    )
    if not rows:
        return self._learning_result(detail="no active memories to score")
    scores = [{"memory_id": row["id"], "score": self._score_memory_importance(row["content"])} for row in rows]
    scores.sort(key=lambda item: item["score"], reverse=True)
    self._record_learning_event("learning.memory_scored", {
        "count": len(scores), "top": scores[:5], "scored_at": self._learning_timestamp(),
    })
    return self._learning_result(changed=True, detail=f"scored {len(scores)} active memories")


def _run_learning_graph(self, _context: dict[str, Any]) -> dict[str, Any]:
    before = len(self._knowledge_graph)
    self._extract_knowledge_graph()
    return self._learning_result(changed=len(self._knowledge_graph) != before,
                            detail=f"knowledge graph sources: {before} -> {len(self._knowledge_graph)}")


def _run_learning_project_graph(self, _context: dict[str, Any]) -> dict[str, Any]:
    state = self._load_project_files_into_memory()
    return self._learning_result(
        changed=state.get("status") == "indexing",
        detail=f"project graph {state.get('status', 'missing')}: {state.get('nodes', 0)} nodes",
    )


def _run_learning_skill_composition(self, context: dict[str, Any]) -> dict[str, Any]:
    user_input = str(context.get("user_input") or "").strip()
    if not user_input:
        return self._learning_result(detail="requires a task description")
    workflow = self._compose_skills(user_input)
    if not workflow:
        return self._learning_result(detail="no matching learned skill workflow")
    self._record_learning_event("learning.skill_composed", {
        "task_hash": stable_hash(user_input), "workflow": self._learning_safe_text(workflow, 1000),
    })
    return self._learning_result(changed=True, detail="recorded a bounded composed workflow")


def _run_learning_rollback(self, _context: dict[str, Any]) -> dict[str, Any]:
    # Rollback is intentionally a user-directed `/forget` or learning
    # rollback operation.  A scheduler must never delete learned state on its
    # own, but this registry entry makes the safety behavior observable.
    return self._learning_result(detail="automatic deletion is disabled; use /forget or explicit rollback")


def dispatch_learning_cycle(self, *, surface: str | None = None, trigger: str = "scheduled",
                            max_features: int = 4, user_input: str = "",
                            feature_names: tuple[str, ...] | list[str] | None = None) -> list[dict[str, Any]]:
    """Run a bounded, round-robin set of enabled learning features.

    Every attempted feature receives durable started/completed/failed events.
    The executor may report an explicit effect; otherwise the dispatcher also
    compares scoped durable and in-memory state before and after execution.
    """
    try:
        limit = max(1, min(int(max_features), len(self._LEARNING_FEATURE_ORDER)))
    except (TypeError, ValueError):
        limit = 4
    surface_name = self._learning_safe_text(surface or self._EXECUTION_SURFACE, 32)
    trigger_name = self._learning_safe_text(trigger, 32) or "scheduled"

    with self._learning_dispatch_lock:
        if feature_names is not None:
            requested = list(feature_names)
            selected = []
            for name in requested:
                if (name in self._LEARNING_FEATURE_REGISTRY and name in self._LEARNING_FEATURE_ORDER
                        and self._SELF_LEARNING_FLAGS.get(name, True) and name not in selected):
                    selected.append(name)
                if len(selected) >= limit:
                    break
        else:
            selected = []
            start = self._learning_dispatch_cursor % len(self._LEARNING_FEATURE_ORDER)
            for offset in range(len(self._LEARNING_FEATURE_ORDER)):
                name = self._LEARNING_FEATURE_ORDER[(start + offset) % len(self._LEARNING_FEATURE_ORDER)]
                if not self._SELF_LEARNING_FLAGS.get(name, True):
                    continue
                if name not in self._LEARNING_FEATURE_REGISTRY:
                    continue
                selected.append(name)
                if len(selected) >= limit:
                    break
            if selected:
                self._learning_dispatch_cursor = (
                    self._LEARNING_FEATURE_ORDER.index(selected[-1]) + 1
                ) % len(self._LEARNING_FEATURE_ORDER)

        summaries: list[dict[str, Any]] = []
        for name in selected:
            entry = self._LEARNING_FEATURE_REGISTRY[name]
            started_at = self._learning_timestamp()
            description = self._learning_safe_text(entry.get("description", ""), 240)
            self._record_learning_event("learning.feature_started", {
                "feature": name, "description": description, "trigger": trigger_name,
                "surface": surface_name, "started_at": started_at,
            })
            before = self._learning_state_fingerprint()
            context = {
                "feature": name, "trigger": trigger_name, "surface": surface_name,
                "user_input": str(user_input or "")[:4000],
            }
            try:
                executor = entry.get("executor")
                if not callable(executor):
                    raise TypeError("learning feature has no callable executor")
                result = executor(context)
                explicit_changed = False
                detail = ""
                if isinstance(result, dict):
                    explicit_changed = bool(result.get("changed", False))
                    detail = self._learning_safe_text(result.get("detail", ""))
                elif result is True:
                    explicit_changed = True
                elif result not in (None, False, ""):
                    detail = self._learning_safe_text(result)
                changed = explicit_changed or self._learning_state_fingerprint() != before
                finished_at = self._learning_timestamp()
                if not detail:
                    detail = "observable state changed" if changed else "no eligible evidence or state change"
                summary = {
                    "feature": name, "status": "completed", "changed": changed,
                    "last_run_at": finished_at, "detail": detail,
                }
                self._record_learning_event("learning.feature_completed", {
                    **summary, "trigger": trigger_name, "surface": surface_name,
                })
            except Exception as exc:
                finished_at = self._learning_timestamp()
                summary = {
                    "feature": name, "status": "failed", "changed": False,
                    "last_run_at": finished_at, "detail": self._learning_safe_text(exc),
                }
                self._record_learning_event("learning.feature_failed", {
                    **summary, "trigger": trigger_name, "surface": surface_name,
                })
            summaries.append(summary)
        return summaries


def learning_feature_status(self) -> list[dict[str, Any]]:
    """Return the user-visible status of every registered learning feature."""
    runtime = self.learning_runtime()
    events = self.memory_bank.store.list_events(
        limit=10000, workspace_id=self.memory_bank.workspace_id,
        user_id=self.memory_bank.user_id,
    )
    latest: dict[str, dict[str, Any]] = {}
    for event in events:  # list_events is newest-first
        payload = event.get("payload") or {}
        feature = payload.get("feature")
        if feature not in self._LEARNING_FEATURE_REGISTRY or feature in latest:
            continue
        if event.get("event_type") == "learning.feature_started":
            latest[feature] = {
                "status": "started", "changed": False,
                "last_run_at": payload.get("started_at") or event.get("created_at"),
                "detail": "cycle started",
            }
        elif event.get("event_type") == "learning.feature_completed":
            latest[feature] = {
                "status": "completed", "changed": bool(payload.get("changed")),
                "last_run_at": payload.get("last_run_at") or event.get("created_at"),
                "detail": self._learning_safe_text(payload.get("detail", "")),
            }
        elif event.get("event_type") == "learning.feature_failed":
            latest[feature] = {
                "status": "failed", "changed": False,
                "last_run_at": payload.get("last_run_at") or event.get("created_at"),
                "detail": self._learning_safe_text(payload.get("detail", "")),
            }
    return [
        {
            "name": name,
            "description": self._learning_safe_text(self._LEARNING_FEATURE_REGISTRY[name].get("description", ""), 240),
            "enabled": bool(self._SELF_LEARNING_FLAGS.get(name, True)),
            "policy": runtime["mode"],
            "provider_class": self._learning_provider_class(),
            "runtime_status": runtime["status"],
            "model": runtime["model"],
            "skip_reason": runtime["detail"] if runtime["status"] != "ready" and name in self._REMOTE_LEARNING_FEATURES else "",
            **latest.get(name, {"status": "never", "changed": False, "last_run_at": None, "detail": ""}),
        }
        for name in self._LEARNING_FEATURE_ORDER if name in self._LEARNING_FEATURE_REGISTRY
    ]


def _background_learning_loop(self) -> None:
    """Run bounded round-robin learning cycles while the CLI is idle."""
    if self.learning_runtime()["status"] != "ready":
        return
    while True:
        time.sleep(30)
        try:
            now = time.time()
            idle_duration = now - self._last_user_interaction

            # Skip heavy background work if the user has been active within the last 1 minute
            if idle_duration < 60:
                continue

            self.dispatch_learning_cycle(surface="cli", trigger="background", max_features=4)
        except Exception as exc:
            try:
                self.memory_bank.store.append_event(
                    "learning.background_failed", {"error": str(exc)[:1000]},
                    user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
                    session_id=self.memory_bank.session_id,
                )
            except Exception:
                pass


def _ensure_detached_learning_worker(self) -> bool:
    """Keep learning alive outside the interactive CLI process."""
    if os.environ.get("KYROZEN_LEARNING_WORKER") == "1":
        return True
    if self.learning_runtime()["status"] != "ready":
        return True
    context = self.get_launch_context()
    if context is None:
        return False
    try:
        return self.start_learning_worker(
            workspace_root=context.active_root,
            launch_mode=context.mode,
        )
    except Exception as exc:
        try:
            self.memory_bank.store.append_event(
                "learning.worker_start_failed", {"error": str(exc)[:1000]},
                user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
                session_id=self.memory_bank.session_id,
            )
        except Exception:
            pass
        return False


def _touch_detached_learning_heartbeat(self) -> None:
    context = self.get_launch_context()
    if context is None or os.environ.get("KYROZEN_LEARNING_WORKER") == "1":
        return
    try:
        self.touch_cli_heartbeat(context.active_root)
    except Exception:
        pass
