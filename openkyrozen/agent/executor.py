from __future__ import annotations

import hashlib
import ast
import json
import uuid
import time
from typing import Any
from openkyrozen.persistence.models import stable_hash, utc_now
from openkyrozen.app.config import AgentConfigError, effective_capabilities, load_agent_config
from openkyrozen.security.capabilities import issue_capability_token
from openkyrozen.tools.models import (CommandResult, ToolError, ToolResult, command_tool_result,
                                      render_tool_result, tool_failure)
from openkyrozen.security.tool_policy import tool_capability
from openkyrozen.agent.types import ExecutionReceipt
from openkyrozen.tasks.models import _receipt_args


def _notify_tool_execute(self, action: str, args: Any, result: ToolResult) -> None:
    """Emit the typed outcome to observers; display text cannot change status."""
    result = result if isinstance(result, ToolResult) else tool_failure(
        "contract_error", "Tool observers require a typed result"
    )
    try:
        self._plugin_runtime_for_surface().tool_execute(
            action=action, args=args, result=render_tool_result(result),
            success=result.success, error=result.error.message if result.error else None,
            user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
            session_id=self.memory_bank.session_id,
        )
    except Exception:
        pass


def _operation_action(self, action: str) -> str:
    """Normalise aliases that perform the same state-changing operation."""
    action = self.TOOL_ALIASES.get(str(action).strip(), str(action).strip())
    return "run_cmd" if action == "execute_terminal_command" else action


def _operation_args(self, action: str, args: Any) -> str:
    value = str(args)
    return value.strip() if action == "run_cmd" else value


def _operation_id(self, scope: str, action: str, args: Any) -> str:
    canonical = self._operation_action(action)
    return stable_hash(f"{scope}\0{canonical}\0{self._operation_args(canonical, args)}")[:32]


def _is_state_changing_action(self, action: str, args: str) -> bool:
    action = self._operation_action(action)
    if action == "git_branch":
        return bool(str(args).strip()) and not str(args).lstrip().startswith(("-a", "-v"))
    if action == "git_remote":
        return str(args).lstrip().startswith(("add ", "remove "))
    return action in {
        "write_file", "edit_file", "run_cmd", "git_clone", "git_add", "git_commit", "git_push",
        "git_pull", "git_checkout", "git_stash", "git_reset", "git_remote", "github_cli", "define_tool",
    }


def _make_execution_receipt(self, *, action: str, args: Any, authorized: bool, started_at: str,
                            success: bool, result: Any, operation_scope: str,
                            failure: str | None = None, raw_result: Any = None, tool_error: ToolError | None = None) -> ExecutionReceipt:
    canonical = self._operation_action(action)
    from openkyrozen.agent.delegation import TOOLS
    result_text = self._fix_safe_text(result, max(2000, len(str(result))) if canonical in TOOLS else 200000 if canonical in {"read_file", "read_webpage", "git_diff", "git_show", "graph_query"} else 2000, preserve_lines=True)
    command = raw_result if isinstance(raw_result, CommandResult) else None
    effect, effect_verified = self._subagent_tool_evidence(canonical, str(args), result_text, success=success)
    acceptance, accepted = self._acceptance_for_tool(canonical, str(args), result_text, success=success)
    if not accepted and effect_verified:
        acceptance = effect
    return ExecutionReceipt(
        receipt_id=f"receipt_{uuid.uuid4().hex}",
        operation_id=self._operation_id(operation_scope, canonical, args),
        action=canonical,
        args=self._fix_safe_text(self._operation_args(canonical, args), 1000),
        args_sha256=hashlib.sha256(
            _receipt_args(self._operation_args(canonical, args)).encode("utf-8")
        ).hexdigest(),
        authorized=authorized,
        started_at=started_at,
        completed_at=utc_now(),
        success=bool(success),
        result=result_text,
        failure=failure if not success else None,
        exit_code=command.exit_code if command is not None else None,
        verified_effect=effect if effect_verified else None,
        acceptance=acceptance if success and (accepted or effect_verified) else None,
        tool_error=({"code": tool_error.code, "message": self._fix_safe_text(tool_error.message, 1000),
                     "retriable": tool_error.retriable, "metadata": tool_error.metadata} if tool_error else None),
    )


def _run_tool(self, action: str, args: str, *, return_success: bool = False,
              return_receipt: bool = False, operation_scope: str = "") -> str | tuple[str, bool] | ExecutionReceipt:
    started_at = utc_now()
    action = self.TOOL_ALIASES.get(action, action)
    if isinstance(args, dict):
        if action in ("run_cmd", "execute_terminal_command"):
            args = args.get("cmd") or args.get("command") or ""
        elif action == "write_file":
            args = f"{args.get('file_path') or args.get('path') or ''}|{args.get('content') or args.get('text') or ''}"
        else:
            args = json.dumps(args)
    elif isinstance(args, str) and args.startswith("{"):
        try:
            parsed = ast.literal_eval(args)
            if isinstance(parsed, dict):
                if action in ("run_cmd", "execute_terminal_command"):
                    args = parsed.get("cmd") or parsed.get("command") or ""
                elif action == "write_file":
                    args = f"{parsed.get('file_path') or parsed.get('path') or ''}|{parsed.get('content') or parsed.get('text') or ''}"
                else:
                    args = json.dumps(parsed)
        except (ValueError, SyntaxError):
            pass
    def finish(outcome: ToolResult, authorized: bool = False, raw_result: Any = None):
        text = render_tool_result(outcome)
        if return_receipt:
            return self._make_execution_receipt(
                action=action, args=args, authorized=authorized, started_at=started_at,
                success=outcome.success, result=text, operation_scope=operation_scope,
                failure=outcome.error.code if outcome.error else None, raw_result=raw_result,
                tool_error=outcome.error,
            )
        return (text, outcome.success) if return_success else text
    if action not in self.AVAILABLE_TOOLS:
        outcome = tool_failure("invalid_arguments", f"Error: unknown tool '{action}'")
        self._notify_tool_execute(action, args, outcome)
        return finish(outcome)
    required_capability = tool_capability(action, self.AVAILABLE_TOOLS)
    try:
        if required_capability not in effective_capabilities(load_agent_config(self._get_workspace_root())):
            outcome = tool_failure("permission_denied", f"Error: tool '{action}' requires capability '{required_capability}', which is outside the configured agent capability bound")
            self._notify_tool_execute(action, args, outcome)
            return finish(outcome)
    except AgentConfigError as exc:
        outcome = tool_failure("execution_error", f"Error: invalid agent configuration: {exc}")
        self._notify_tool_execute(action, args, outcome)
        return finish(outcome)
    if time.time() >= self._execution_capability_token.expires_at:
        self._execution_capability_token = issue_capability_token(
            self._execution_capability_token.subject, self._execution_capability_token.capabilities,
        )
    if not self._execution_capability_token.allows(required_capability):
        outcome = tool_failure("permission_denied", f"Error: tool '{action}' requires capability '{required_capability}'")
        self._notify_tool_execute(action, args, outcome)
        return finish(outcome)
    authorized = (self._authorize_tool_action(action, args) if self._EXECUTION_SURFACE == "tui"
                  else self._confirm_tool_action(action, str(args)))
    if not authorized:
        outcome = tool_failure("approval_denied", f"Error: {action} requires confirmation. Approve it interactively or set KYROZEN_APPROVAL_MODE=never for an explicitly automated CLI.")
        self._notify_tool_execute(action, args, outcome)
        return finish(outcome)
    start = time.time()
    raw_result = None
    try:
        with self._delegation_tool_access(action, args):
            if action in {"run_cmd", "execute_terminal_command"}:
                raw_result = self.run_command(args)
                outcome = command_tool_result(raw_result)
            else:
                outcome = self.tool_registry._invoke_executor(self.tool_registry.get_spec(action), args)
    except KeyboardInterrupt:
        outcome = tool_failure("cancelled", "Tool execution interrupted by user (Ctrl+C).")
    except Exception as exc:
        outcome = tool_failure("execution_error", f"Error: {type(exc).__name__}: {exc}")
    elapsed = time.time() - start
    text = render_tool_result(outcome)
    self._track_tool_performance(action, outcome.success, elapsed)
    self._notify_tool_execute(action, args, outcome)
    if outcome.success and action in {"git_pull", "git_checkout", "git_reset", "git_clone"} and self._project_graph is not None:
        self._project_graph.refresh_async()
    return finish(outcome, authorized=True, raw_result=raw_result)


def _execute_turn_action(self, action: str, args: Any, *, operation_scope: str,
                         successful_operations: set[str]) -> ExecutionReceipt:
    """Execute one model action, refusing a duplicate successful mutation."""
    canonical = self._operation_action(action)
    started_at = utc_now()
    if isinstance(args, dict):
        result = (
            f"Error: `{action}` requires a plain string as the `args` field.\n"
            "You passed a JSON object. Convert to a plain string.\n"
            "Example: `\"args\": \"python3 process_logs.py\"`.\n"
            "Do NOT use `\"args\": {\"cmd\": ...}`.\n"
        )
        outcome = tool_failure("invalid_arguments", result)
        self._notify_tool_execute(action, args, outcome)
        return self._make_execution_receipt(
            action=canonical, args=args, authorized=False, started_at=started_at, success=False,
            result=result, operation_scope=operation_scope, failure=outcome.error.code, tool_error=outcome.error,
        )
    args = str(args)
    operation_id = self._operation_id(operation_scope, canonical, args)
    if (self._is_state_changing_action(canonical, args)
            and self._interaction_controller.state().get("executing_plan")):
        allowed, reason = self.tasks.mutation_matches_current_task(canonical, args)
        if not allowed:
            result = f"Error: action does not match the accepted plan; {reason}."
            outcome = tool_failure("permission_denied", result)
            self._notify_tool_execute(canonical, args, outcome)
            return self._make_execution_receipt(
                action=canonical, args=args, authorized=False, started_at=started_at,
                success=False, result=result, operation_scope=operation_scope,
                failure=outcome.error.code, tool_error=outcome.error,
            )
    if (canonical != "run_cmd" and self._is_state_changing_action(canonical, args)
            and operation_id in successful_operations):
        result = "Error: duplicate successful state-changing action refused for this turn."
        self._notify_tool_execute(canonical, args, tool_failure("permission_denied", result))
        return self._make_execution_receipt(
            action=canonical, args=args, authorized=True, started_at=started_at, success=False,
            result=result, operation_scope=operation_scope, failure="duplicate_operation",
        )
    receipt = self._run_tool(canonical, args, return_receipt=True, operation_scope=operation_scope)
    assert isinstance(receipt, ExecutionReceipt)
    if (receipt.success and receipt.action != "run_cmd"
            and self._is_state_changing_action(receipt.action, args)):
        successful_operations.add(receipt.operation_id)
    return receipt


def _record_turn_receipt(self, receipt: ExecutionReceipt) -> dict[str, Any]:
    """Persist a receipt and reconcile the current planned task from verified evidence."""
    from openkyrozen.agent.delegation import TOOLS
    evidence = {} if receipt.action in TOOLS else self.tasks.record_evidence(
        action=receipt.action, args=receipt.args, args_fingerprint=receipt.args_sha256, result=receipt.result,
        success=receipt.success, acceptance=receipt.acceptance, receipt_id=receipt.receipt_id,
    )
    task_id = evidence.get("task_id")
    effective_acceptance = evidence.get("acceptance") or receipt.acceptance
    if task_id:
        task = next((item for item in self.tasks.tasks if item["id"] == task_id), None)
        if task and not (task.get("checkpoint") or {}).get("action"):
            self.tasks.update_checkpoint(task_id, {"action": receipt.action, "args": receipt.args})
    if task_id and receipt.success and effective_acceptance:
        index = next(index for index, task in enumerate(self.tasks.tasks) if task["id"] == task_id)
        self.tasks.set_status(index, "succeeded")
    receipt_payload = receipt.as_dict()
    if effective_acceptance and not receipt_payload.get("acceptance"):
        receipt_payload["acceptance"] = effective_acceptance
    if evidence.get("unmatched_reason"):
        receipt_payload["unmatched_reason"] = evidence["unmatched_reason"]
    self.tasks.store.append_event(
        "execution.receipt", {**receipt_payload, "task_id": task_id}, user_id=self.tasks.user_id,
        workspace_id=self.tasks.workspace_id, session_id=self.tasks.session_id, task_id=task_id,
    )
    result = {
        "receipt_id": receipt.receipt_id, "operation_id": receipt.operation_id,
        "action": receipt.action, "args": receipt.args, "result": receipt.result,
        "success": receipt.success, "authorized": receipt.authorized,
        "acceptance": effective_acceptance, "failure": receipt.failure, "tool_error": receipt.tool_error, "task_id": task_id,
    }
    if evidence.get("unmatched_reason"):
        result["unmatched_reason"] = evidence["unmatched_reason"]
    self._emit_stream_event({"event": "tool_receipt", "tool_receipt": result})
    self._emit_stream_event({"event": "tasks", "tasks": [
        {"id": item["id"], "description": item["description"], "status": item["status"]}
        for item in self.tasks.tasks
    ]})
    return result


def _tool_result_for_prompt(self, receipt: ExecutionReceipt) -> str:
    """Apply optional instruction review to prompt text while keeping receipts intact."""
    fast_mode = self.fast_mode
    public_source = receipt.action in {"search_web", "read_webpage"}
    reviewed, details = fast_mode.review_tool_output(
        receipt.action, receipt.result, private=not public_source,
    )
    if details:
        self._record_decision_assist("tool_output_review", {
            "backend": details.get("backend"), "latency_ms": details.get("latency_ms"),
            "model_version": details.get("model_version"),
            "input_tokens": details.get("input_tokens"), "output_tokens": details.get("output_tokens"),
            "model_release_date": details.get("model_release_date"),
            "fallback_reason": details.get("fallback_reason") or (
                "quality_gate" if details.get("quality_gate") is False else None),
            "quality_gate": details.get("quality_gate"),
            "policy_version": details.get("policy_version"),
            "fallback_behavior": details.get("fallback_behavior"),
            "confidence_threshold": details.get("confidence_threshold"),
            "probability_threshold": details.get("probability_threshold"),
            "probability_margin": details.get("probability_margin"),
            "confidence": details.get("confidence"), "probability": details.get("probability"),
            "outcome": details.get("outcome", "fallback"),
        })
    return reviewed
