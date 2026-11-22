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
    Working Plan task selection requires the write capability and internal effect
    to be included in the bounds shown for approval.
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
    working = self.tasks.working_plan
    completed_objective = (working is not None and working.status == "completed"
                           and (state.get("executing_plan") or {}).get("working_plan_id") == working.id)
    try:
        plan = self._interaction_controller.propose_plan(
            arguments, original_input=self.execution_context.native_control_input,
            material_only=completed_objective,
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
    expected_revision to zero and add each task with a unique ref, description
    and acceptance criteria. In the complexity/tasks form omit id for every new task;
    task.id identifies an existing runtime task, never a new client-chosen ID.
    Do not mix tasks with operations, plan_id, expected_revision or reason.
    Use returned plan_id/revision for edits and add.ref for same-batch references.
    In ordinary plans, add_read_check with ref and path creates a task for a
    complete read. Start it by ref before reading. Read acceptance can also
    use criterion/verification objects: verification names
    read_file, exact arguments and expect={equals:content} or {contains:text}.
    In ordinary plans, use expect={complete:true} with the exact path to observe
    unknown contents. This criterion requires a complete native file read after
    explicit start. Use equals or contains for expected contents. Prose
    acceptance needs its own verification evidence.
    For a test task, add a criterion/verification object with action run_cmd and
    the exact arguments you will run, including any options. Start the task
    before running that command. A verified zero exit status satisfies this
    criterion. A failed reproduction remains evidence of the bug; use a
    separate task with a successful check for the fix.
    Refine a pending prose-only task with an update before starting it. If
    its verification changes after start, cancel it and add a new task.
    Use start with a stable returned task ID before
    executing native task work, then suggest_complete to check existing verified
    evidence; a completion suggestion never supplies evidence. A succeeded task
    is already verified. Use add with task.dependencies for ordinary follow-up work.
    A pure exact-revision
    suggestion for verified tasks in the current completed plan is a read-only
    confirmation: use ONLY plan_id, expected_revision and operations containing
    suggest_complete; omit reason, complexity, tasks and all other metadata.
    The snapshot reason is saved state, not a confirmation argument.
    If ordinary work remains after completion, create a separate plan at revision
    zero with no plan_id. Historical plans and other terminal edits remain rejected.
    Split of an approved objective retains its immutable acceptance anchor and adds
    strategy children. Set add.anchor only for an approved Strategic Plan objective.
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
        operations = arguments.get("operations")
        if arguments.get("plan_id") is None:
            interaction = self._interaction_controller.state()
            if interaction.get("executing_plan") or interaction.get("pending_plan"):
                return tool_failure("permission_denied",
                                    "A Strategic Plan is active or awaiting approval. Use its approval workflow.")
            if self.execution_context.native_plan_completed:
                actions = {"add" if op["op"] == "add_read_check" else op["op"] for op in operations or ()}
                if actions != {"add", "start"}:
                    raise ValueError("A follow-up plan requires add and start operations in the same call")
        if operations is not None:
            if any(isinstance(op, dict) and op.get("op") == "replace_command_check" for op in operations):
                interaction = self._interaction_controller.state()
                if interaction.get("executing_plan") or interaction.get("pending_plan"):
                    return tool_failure("permission_denied",
                                        "Command checks cannot be replaced during a Strategic Plan or its approval.")
            result = self.tasks.update_plan(
                complexity=arguments.get("complexity"), plan_id=arguments.get("plan_id"),
                expected_revision=arguments["expected_revision"], operations=arguments["operations"],
                reason=arguments.get("reason"),
                strategy_validator=validate_strategy,
            )
            suggested = {operation["task"] for operation in arguments["operations"]
                         if operation.get("op") == "suggest_complete"}
            unverified = {task["status"] for task in result["tasks"]
                          if task["id"] in suggested and task["status"] not in {"succeeded", "cancelled"}}
            confirmation = result.get("confirmation") is True
            text = "Working Plan updated."
            if confirmation:
                text = ("Working Plan already completed; verified tasks confirmed without changes."
                        if result["status"] == "completed"
                        else "Task already verified. Select the next eligible task from the current Working Plan.")
            if "pending" in unverified:
                text += (" Start the pending task with update_plan before running its verification. "
                         "Tool calls before start are unbound.")
            if unverified:
                text += (" Completion remains unverified. Typed acceptance supplies the verification contract. "
                         "Use its exact tool and arguments from the current Working Plan. For prose-only tasks, "
                         "declare typed acceptance or an execution contract before starting work. "
                         "To correct an ordinary running task, use cancel, add, then start the new ref. "
                         "Redirect pending dependencies to the new ref in the same edit. "
                         "Changes to approved criteria require renewed approval.")
            return tool_success(text, data=result,
                                metadata={"working_plan_confirmation": True} if confirmation else None)
        plan = self.tasks.update_plan(complexity=arguments["complexity"], tasks=arguments["tasks"])
    except (KeyError, TypeError, ValueError) as exc:
        ordinary = not self._interaction_controller.state().get("executing_plan") and not any(
            (task.get("checkpoint") or {}).get("strategic_plan") for task in self.tasks.tasks)
        snapshot = self.tasks.working_plan_snapshot()
        if self.execution_context.native_plan_completed and arguments.get("plan_id") is None:
            hint = " Omit plan_id, set expected_revision to 0, and include add and start in the same call."
        elif "operations" not in arguments:
            hint = " Omit task.id for new tasks; use add.ref for local references."
        elif snapshot is None:
            hint = " To create a plan, omit plan_id and use expected_revision=0."
        else:
            hint = " Use the current plan_id and revision below."
        return tool_failure("invalid_arguments", f"Error: invalid Working Plan: {str(exc)[:300]}.{hint}",
            retriable=True, data={"working_plan": snapshot},
            metadata={"plan_validation_no_effect": self.tasks._plan_commit_attempts == commit_attempts and ordinary})
    return tool_success(
        "Working Plan updated.",
        data={"plan_id": plan.id, "complexity": plan.complexity,
              "status": plan.status, "revision": plan.revision,
              "task_ids": list(plan.task_ids), "tasks": self.tasks.working_plan_snapshot()["tasks"]},
    )
