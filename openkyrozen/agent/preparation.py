from __future__ import annotations

import time
from typing import Any
from openkyrozen.agent.modes import mode_capabilities
from openkyrozen.security.capabilities import issue_capability_token
from openkyrozen.security.tool_policy import resolve_capabilities
from openkyrozen.agent.compaction import ContextState

def _prepare_turn(self, turn):
    """One user turn: build context, get LLM reply, execute tool calls
    with automatic retries and failure memory."""
    fast_mode = self.fast_mode

    turn.interaction_mode = self._active_interaction_mode.get()
    turn.fast_backend = self._interaction_controller.state().get("system_one_backend",
                                                         self._interaction_controller.state().get("fast_backend", "off"))
    fast_route: dict[str, Any] = {}
    if turn.fast_backend != "off" and not self._interaction_controller.state().get("executing_plan"):
        try:
            fast_route = fast_mode.route(turn.fast_backend, turn.user_input)
            chosen = {key: fast_route.get(key) for key in ("model", "complexity", "profile")
                      if fast_route.get(key)}
            self._record_fast_decision(turn.fast_backend, {
                "stage": "routing", "choices": chosen,
                "model_version": fast_route["model_version"],
                "model_release_date": fast_route.get("model_release_date"),
                "latency_ms": fast_route["latency_ms"],
                "input_tokens": fast_route["input_tokens"],
                "output_tokens": fast_route["output_tokens"],
                "confidences": fast_route["confidences"],
                "fallbacks": fast_route.get("fallbacks"),
                "policies": fast_route.get("policies"),
                "fallback_reason": "low_confidence" if len(chosen) < 3 else "",
            })
            if chosen:
                if not self._fast_used_backend.get():
                    self._fast_used_backend.set(turn.fast_backend)
                    self._emit_stream_event({"event": "fast_decision", "backend": turn.fast_backend})
        except Exception as exc:
            self._record_fast_decision(turn.fast_backend, {"stage": "routing", "choices": {},
                                                  "fallback_reason": type(exc).__name__})
    self._last_user_interaction = time.time()
    self._touch_detached_learning_heartbeat()
    feedback = self.learning_engine.feedback_signal(turn.user_input)
    if feedback and self._last_learning_run:
        previous = self._last_learning_run
        self._learning_notices.extend(self.learning_engine.record_outcome(
            previous["run"], previous["receipts"], verified=True,
            success=feedback == "success", correction=feedback == "failure", source="user_feedback",
        ))
        self._last_learning_run = None
    resolved_profile = self.learning_engine.route_profile(turn.user_input, turn.profile or self._agent_profile_mode)
    if fast_route.get("profile") and (turn.profile or self._agent_profile_mode) == "auto":
        resolved_profile = fast_route["profile"]
    self.DEEPSEEK_MODEL = (self._provider_config.model_simple if fast_route.get("model") == "simple" else
                      self._provider_config.model_complex if fast_route.get("model") == "reasoning" else
                      self._select_model(turn.user_input)) if self._provider_config else self._select_model(turn.user_input)
    context_state = ContextState(
        self.DEEPSEEK_MODEL,
        self._provider_config.context_window_tokens if self._provider_config else None,
    )
    context_state.history_message_ids = {id(item) for item in self.short_term_memory}
    self._active_context_state.set(context_state)
    provider_model = f"{self._provider_config.provider}:{self.DEEPSEEK_MODEL}" if self._provider_config else f"unknown:{self.DEEPSEEK_MODEL}"
    turn.learning_run = self.learning_engine.begin_run(resolved_profile, turn.user_input, provider_model=provider_model)
    self._active_usage_run_id.set(turn.learning_run["run_id"])
    turn.learned_context, turn.learning_receipts = self.learning_engine.artifact_context(turn.learning_run)
    ponytail_context = self._ponytail_context(resolved_profile)
    if ponytail_context:
        turn.learned_context = (turn.learned_context + "\n" + ponytail_context).strip()
    base_capabilities = resolve_capabilities(
        self._surface_capabilities or ("full" if self._EXECUTION_SURFACE == "cli" else "workspace"),
        default="workspace",
    )
    self._execution_capability_token = issue_capability_token(
        f"surface:{self._EXECUTION_SURFACE}",
        mode_capabilities(base_capabilities, turn.interaction_mode),
    )

    if turn.clear_tasks and turn.interaction_mode == "agent":
        self.tasks.clear()

    turn.fix_workflow = self._prepare_fix_workflow(turn.user_input) if turn.interaction_mode == "agent" else None

    # Input-dependent learning belongs to the same durable dispatcher as idle
    # and scheduled work.  It is still bounded and cannot grant capabilities:
    # preference detection records scoped signals, while technology discovery
    # only queues the existing background documentation fetch.
    self.dispatch_learning_cycle(
        surface=self._EXECUTION_SURFACE, trigger="turn", max_features=2,
        user_input=turn.user_input,
        feature_names=("detect_user_preferences", "auto_patch_technology"),
    )

    # Auto-select the best model for this turn based on task complexity
    turn.complexity = fast_route.get("complexity") or self._classify_complexity(turn.user_input)

    turn.turn_start = time.time()
    turn.turn_prompt_total = 0
    turn.turn_completion_total = 0
