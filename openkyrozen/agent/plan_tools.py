from __future__ import annotations

from openkyrozen.tools.models import legacy_text_api, tool_failure, tool_success


@legacy_text_api
def _update_plan(self, arguments: dict):
    """Create a plan or add/edit identified steps; omitted existing steps remain."""
    try:
        plan = self.tasks.update_plan(
            complexity=arguments["complexity"], tasks=arguments["tasks"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        return tool_failure("invalid_arguments", f"Error: invalid Working Plan: {exc}")
    return tool_success(
        "Working Plan updated.",
        data={"plan_id": plan.id, "complexity": plan.complexity,
              "status": plan.status, "task_ids": list(plan.task_ids)},
    )
