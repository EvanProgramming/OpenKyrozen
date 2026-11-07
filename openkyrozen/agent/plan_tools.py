from __future__ import annotations

from openkyrozen.tools.models import legacy_text_api, tool_failure, tool_success


def _control_error(self):
    if not self._interaction_controls_enabled.get() or getattr(self.execution_context, "child_run_id", None):
        return tool_failure("permission_denied", "Interactive controls are unavailable on this execution surface.")
    return None


@legacy_text_api
def _propose_plan(self, arguments: dict):
    """Propose a versioned Strategic Plan after read-only inspection. This never
    accepts or executes the proposal; material changes require user approval.
    """
    denied = _control_error(self)
    if denied is not None:
        return denied
    state = self._interaction_controller.state()
    if (self._active_interaction_mode.get() != "plan" and not state.get("executing_plan")):
        return tool_failure("permission_denied", "Strategic proposals require Plan Mode or an accepted plan revision.")
    if not getattr(self.execution_context, "native_plan_inspected", False):
        return tool_failure("invalid_arguments", "Inspect the relevant workspace or source with a read-only tool first.")
    from openkyrozen.agent.modes import render_plan
    try:
        plan = self._interaction_controller.propose_plan(
            arguments, original_input=self.execution_context.native_control_input,
        )
    except (ValueError, TypeError, KeyError) as exc:
        return tool_failure("invalid_arguments", f"Invalid Strategic Plan: {exc}")
    self._emit_stream_event({"event": "interaction", "interaction": self.interaction_envelope()})
    requires_approval = plan.get("revision_materiality") != "within_bounds"
    text = (render_plan(plan) if requires_approval else
            f"Strategic Plan v{plan['version']} updated within approved scope; no renewed approval is required.")
    return tool_success(text, data={"plan": plan, "requires_approval": requires_approval})


@legacy_text_api
def _request_question(self, arguments: dict):
    """Ask 1-3 structured clarification questions. Questions cannot request
    credentials, grant authority, replace tool approval or execute work.
    """
    denied = _control_error(self)
    if denied is not None:
        return denied
    from openkyrozen.agent.modes import render_question
    try:
        question = self._interaction_controller.request_question(
            arguments, original_input=self.execution_context.native_control_input,
        )
    except (ValueError, TypeError, KeyError) as exc:
        return tool_failure("invalid_arguments", f"Invalid clarification request: {exc}")
    self._emit_stream_event({"event": "interaction", "interaction": self.interaction_envelope()})
    return tool_success(render_question(question), data={"question": question})


@legacy_text_api
def _update_plan(self, arguments: dict):
    """Edit a revision-checked Working Plan. Use start with a stable task ID before
    executing native task work, then suggest_complete to check existing verified
    evidence; a completion suggestion never supplies evidence. Split of an
    approved objective retains its immutable acceptance anchor and adds strategy
    children. Add anchor=<approved task ID> for in-scope investigation/verification.
    """
    def validate_strategy(binding, execution):
        from openkyrozen.agent.executor import _strategic_plan_denial

        active = self._interaction_controller.state().get("executing_plan")
        if not active or active.get("plan_id") != binding.get("plan_id"):
            raise ValueError("strategy has no active approved objective")
        objective = next((step for step in active.get("steps", [])
                          if step.get("id") == binding.get("objective_id")), None)
        if not objective or objective.get("acceptance_ids") != binding.get("acceptance_ids"):
            raise ValueError("strategy objective acceptance changed; approval is required")
        spec = self.tool_registry.get_spec(execution["action"])
        if spec is None or self.tool_registry._argument_error(spec.input_schema, execution["arguments"]):
            raise ValueError("strategy execution has no valid tool contract")
        denial = _strategic_plan_denial(active, execution["action"], execution["arguments"],
                                        spec, self._get_workspace_root())
        if denial:
            raise ValueError(denial)

    try:
        if "operations" in arguments:
            result = self.tasks.update_plan(
                complexity=arguments.get("complexity"), plan_id=arguments.get("plan_id"),
                expected_revision=arguments["expected_revision"], operations=arguments["operations"],
                reason=arguments.get("reason"),
                strategy_validator=validate_strategy,
            )
            return tool_success("Working Plan updated.", data=result)
        plan = self.tasks.update_plan(complexity=arguments["complexity"], tasks=arguments["tasks"])
    except (KeyError, TypeError, ValueError) as exc:
        return tool_failure("invalid_arguments", f"Error: invalid Working Plan: {exc}")
    return tool_success(
        "Working Plan updated.",
        data={"plan_id": plan.id, "complexity": plan.complexity,
              "status": plan.status, "task_ids": list(plan.task_ids)},
    )
