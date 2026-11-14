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
    """Edit a revision-checked Working Plan. To create one, omit plan_id, set
    expected_revision=0 and use operations=[{op:add,ref:inspect,task:{description,
    acceptance}}]. In the complexity/tasks form omit id for every new task;
    task.id identifies an existing runtime task, never a new client-chosen ID.
    Do not mix tasks with operations, plan_id, expected_revision or reason.
    Use returned plan_id/revision for edits and add.ref for same-batch references.
    Read acceptance can use criterion/verification objects: verification names
    read_file, exact arguments and expect={equals:content} or {contains:text}.
    All predicates must match actual complete content after explicit start;
    prose acceptance never becomes verified merely because a read succeeded.
    For a test task, add a criterion/verification object with action run_cmd and
    the exact arguments you will run, including any options. Start the task
    before running that command. A verified zero exit status satisfies this
    criterion. A failed reproduction remains evidence of the bug; use a
    separate task with a successful check for the fix.
    Refine a pending prose-only task with an update before starting it. If
    its verification changes after start, cancel it and add a new task.
    Use start with a stable returned task ID before
    executing native task work, then suggest_complete to check existing verified
    evidence; a completion suggestion never supplies evidence. A pure exact-revision
    suggestion for verified tasks in the current completed plan is a read-only
    confirmation: use ONLY plan_id, expected_revision and operations containing
    suggest_complete; omit reason, complexity, tasks and all other metadata.
    The snapshot reason is saved state, not a confirmation argument.
    Historical plans and other terminal edits remain rejected. Split of an
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

    commit_attempts = self.tasks._plan_commit_attempts
    try:
        if "operations" in arguments:
            result = self.tasks.update_plan(
                complexity=arguments.get("complexity"), plan_id=arguments.get("plan_id"),
                expected_revision=arguments["expected_revision"], operations=arguments["operations"],
                reason=arguments.get("reason"),
                strategy_validator=validate_strategy,
            )
            pending = [operation["task"] for operation in arguments["operations"]
                       if operation.get("op") == "suggest_complete"
                       and any(task["id"] == operation["task"] and task["status"] not in {"succeeded", "cancelled"}
                               for task in result["tasks"])]
            confirmation = result.get("confirmation") is True
            text = ("Working Plan already completed; verified tasks confirmed without changes."
                    if confirmation else "Working Plan updated.")
            if pending:
                text += (" Completion remains unverified; inspect the returned task acceptance and execution contract. "
                         "Set the execution contract to name the exact verification tool and arguments before starting work. "
                         "To revise a running task's contract, cancel it and add a new task.")
            return tool_success(text, data=result,
                                metadata={"working_plan_confirmation": True} if confirmation else None)
        plan = self.tasks.update_plan(complexity=arguments["complexity"], tasks=arguments["tasks"])
    except (KeyError, TypeError, ValueError) as exc:
        ordinary = not self._interaction_controller.state().get("executing_plan") and not any(
            (task.get("checkpoint") or {}).get("strategic_plan") for task in self.tasks.tasks)
        return tool_failure("invalid_arguments", (
            f"Error: invalid Working Plan: {str(exc)[:300]}. Omit task.id for new tasks; use add.ref for local references. "
            "Use the runtime plan_id/revision below for edits; if no plan exists, omit plan_id and use expected_revision=0."
        ), retriable=True, data={"working_plan": self.tasks.working_plan_snapshot()},
            metadata={"plan_validation_no_effect": self.tasks._plan_commit_attempts == commit_attempts and ordinary})
    return tool_success(
        "Working Plan updated.",
        data={"plan_id": plan.id, "complexity": plan.complexity,
              "status": plan.status, "revision": plan.revision,
              "task_ids": list(plan.task_ids), "tasks": self.tasks.working_plan_snapshot()["tasks"]},
    )
