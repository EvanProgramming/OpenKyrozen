from __future__ import annotations

import hashlib
import ast
import json
import re
import uuid
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from typing import Any
from openkyrozen.persistence.models import stable_hash, utc_now
from openkyrozen.app.config import AgentConfigError, effective_capabilities, load_agent_config
from openkyrozen.security.capabilities import issue_capability_token
from openkyrozen.tools.models import (CommandResult, ToolError, ToolResult, command_tool_result,
                                      render_tool_result, tool_failure)
from openkyrozen.security.tool_policy import tool_capability
from openkyrozen.agent.types import ExecutionReceipt

_TOOL_ARTIFACT_THRESHOLD = 12_000
_TOOL_ARTIFACT_PREVIEW = 1_200


def _legacy_object_arguments(args, spec):
    if not isinstance(args, str) or not spec or not getattr(spec, "legacy_fields", ()):
        return args if isinstance(args, dict) else {}
    fields = args.split("|", len(spec.legacy_fields) - 1)
    if len(fields) != len(spec.legacy_fields):
        return {}
    candidate = dict(zip(spec.legacy_fields, fields))
    try:
        return candidate if spec.legacy_arguments(candidate) == args else {}
    except ValueError:
        return {}


def _file_mutation_matches_objective(task, arguments):
    path = arguments.get("path") if isinstance(arguments, dict) else None
    if not isinstance(path, str) or not path.strip():
        return False
    normalized = path.replace("\\", "/").strip("/").casefold()
    basename = normalized.rsplit("/", 1)[-1]
    description = " ".join(str(task.get(field, "")) for field in ("title", "description")).replace("\\", "/").casefold()
    explicit_paths = {match.removeprefix("./") for match in re.findall(
        r"(?<![a-z0-9_.-])(?:\./)?(?:[a-z0-9_.-]+/)+[a-z0-9_.-]+", description,
    )}
    for item in task.get("acceptance", []):
        if not isinstance(item, dict):
            continue
        try:
            arguments = json.loads(item.get("args", ""))
        except (TypeError, json.JSONDecodeError):
            continue
        if (isinstance(arguments, dict) and
                str(arguments.get("path", "")).replace("\\", "/").strip("/").casefold() == normalized):
            return True
    if normalized in explicit_paths:
        return True
    if explicit_paths:
        return False
    if normalized in description or ("/" not in normalized and basename in description):
        return True
    return False


def _strategic_plan_denial(plan, action, args, spec, workspace_root):
    """Return a denial when a tool exceeds the bounds the user accepted."""
    bounds = plan.get("bounds") if isinstance(plan, dict) else None
    if not isinstance(bounds, dict):
        return "accepted plan has no enforceable bounds"
    capabilities = set(bounds.get("capabilities", ()))
    effects = set(bounds.get("effects", ()))
    if not spec or spec.capability not in capabilities:
        return "tool capability is outside the accepted plan"
    effect = {
        "none": "read_only", "read": "read_only", "read_only": "read_only",
        "mutation": "filesystem", "internal_write": "internal", "orchestration": "internal",
        "stateful": "internal", "external": "remote", "unknown": "unknown",
    }.get(spec.side_effects, spec.side_effects)
    if effect not in effects:
        return "tool effect is outside the accepted plan"
    if effect == "process":
        return "process effects cannot be confined to approved workspace paths"
    if spec.risk == "high" and bounds.get("risk_ceiling") != "high":
        return "tool risk exceeds the accepted plan"

    structured = args if isinstance(args, dict) else None
    if structured is None and isinstance(args, str) and args.lstrip().startswith("{"):
        try:
            parsed = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        structured = parsed if isinstance(parsed, dict) else None
    if structured is None:
        structured = _legacy_object_arguments(args, spec)
    path_fields = {"path", "file_path", "directory", "destination", "target_path", "clone_path"}
    paths = {key: value for key, value in structured.items()
             if key in path_fields and isinstance(value, str) and value.strip()}
    for key in ("paths", "directories"):
        values = structured.get(key)
        if isinstance(values, list) and all(isinstance(value, str) and value.strip() for value in values):
            paths.update({f"{key}:{index}": value for index, value in enumerate(values)})
    if action in {"list_dir", "list_tree", "search_files"} and not paths:
        paths = {"path": structured.get("path", ".")}
    if action == "find_files" and not paths:
        paths = {"directory": structured.get("directory", ".")}
    if effect == "filesystem" and not paths:
        return "filesystem effect has no path that can be checked against the accepted scope"
    root = Path(workspace_root).expanduser().resolve()
    allowed_roots = [(root / str(path)).resolve() for path in bounds.get("workspace_paths", ())]
    if any(allowed != root and root not in allowed.parents for allowed in allowed_roots):
        return "accepted workspace scope resolves outside the active workspace"
    for value in paths.values():
        candidate = Path(value).expanduser()
        candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
        if not any(candidate == allowed or allowed in candidate.parents for allowed in allowed_roots):
            return "tool path is outside the accepted workspace scope"
    return None


def _awaiting_revision_mutation(state, action, args, spec, runtime) -> bool:
    return bool(action != "cancel_subagent" and state.get("pending_plan") and state.get("executing_plan") and (
        runtime._is_state_changing_action(action, args)
        or not spec
        or spec.capability in {"write", "shell", "destructive"}
        or spec.side_effects in {"filesystem", "process", "destructive"}
    ))


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
    value = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if isinstance(args, dict) else str(args)
    return value.strip() if action == "run_cmd" else value


def _operation_id(self, scope: str, action: str, args: Any) -> str:
    canonical = self._operation_action(action)
    memory = getattr(self, "memory_bank", None)
    current_session = getattr(self, "current_session", None)
    task_owner = getattr(current_session, "_delegation_parent", None) or self
    tasks = getattr(task_owner, "tasks", None)
    identity = "\0".join(str(value or "") for value in (
        getattr(memory, "user_id", None),
        getattr(tasks, "workspace_id", None) or getattr(memory, "workspace_id", None),
        getattr(tasks, "session_id", None) or getattr(memory, "session_id", None),
    ))
    return stable_hash(f"{identity}\0{scope}\0{canonical}\0{self._operation_args(canonical, args)}")[:32]


def _is_state_changing_action(self, action: str, args: str) -> bool:
    action = self._operation_action(action)
    if isinstance(args, dict):
        if action == 'git_branch':
            options = args.get('options', [])
            return bool(options and (options[0] in {'-d','-D','--delete','-m','-M','--move','-c','-C','--copy','-f','--force'} or not options[0].startswith('-')))
        if action == 'git_remote': return args.get('operation', 'list') in {'add','remove'}
        return action in {
            "write_file", "edit_file", "apply_patch", "run_cmd", "git_clone", "git_add", "git_commit", "git_push",
            "git_pull", "git_checkout", "git_stash", "git_reset", "github_cli",
            "browser_open", "browser_click", "browser_type", "graph_refresh", "spawn_agents",
            "browser_close", "send_subagent", "cancel_subagent", "update_plan",
        }
    if action == "git_branch":
        return bool(str(args).strip()) and not str(args).lstrip().startswith(("-a", "-v"))
    if action == "git_remote":
        return str(args).lstrip().startswith(("add ", "remove "))
    return action in {
        "write_file", "edit_file", "apply_patch", "run_cmd", "git_clone", "git_add", "git_commit", "git_push",
        "git_pull", "git_checkout", "git_stash", "git_reset", "git_remote", "github_cli",
        "browser_open", "browser_click", "browser_type", "graph_refresh", "spawn_agents", "send_subagent",
        "browser_close", "cancel_subagent", "update_plan",
    }


def _make_execution_receipt(self, *, action: str, args: Any, authorized: bool, started_at: str,
                            success: bool, result: Any, operation_scope: str,
                            failure: str | None = None, raw_result: Any = None,
                            tool_error: ToolError | None = None, result_data: Any = None,
                            tool_result: ToolResult | None = None, call_id: str | None = None,
                            call_id_source: str = "legacy", task_id: str | None = None,
                            receipt_arguments: dict[str, Any] | None = None,
                            expected_spec=None) -> ExecutionReceipt:
    canonical = self._operation_action(action)
    from openkyrozen.agent.delegation import TOOLS
    current_session = getattr(self, "current_session", None)
    task_owner = getattr(current_session, "_delegation_parent", None) or self
    task_manager = getattr(task_owner, "tasks", None)
    result_limit = (max(2000, len(str(result))) if canonical in TOOLS else
                    8000 if canonical == "read_tool_artifact" else
                    200000 if canonical in {"read_file", "read_webpage", "git_diff", "git_show", "graph_query"} else 2000)
    result_text = self._fix_safe_text(result, result_limit, preserve_lines=True)
    command = raw_result if isinstance(raw_result, CommandResult) else None
    if tool_result is None and isinstance(result, ToolResult):
        tool_result = result
        result = render_tool_result(tool_result)
    elif tool_result is None and (tool_error is not None or result_data is not None
                                  or len(str(result)) > _TOOL_ARTIFACT_THRESHOLD):
        candidate = ToolResult(data=result_data, text=str(result), error=tool_error)
        from openkyrozen.agent.conversation import tool_result_to_dict
        candidate_json = json.dumps(tool_result_to_dict(candidate), ensure_ascii=False,
                                    sort_keys=True, separators=(",", ":"))
        if len(candidate_json) > _TOOL_ARTIFACT_THRESHOLD:
            tool_result = candidate
    # Keep large outputs out of provider messages and the conversation journal.
    if tool_result is not None and canonical != "read_tool_artifact":
        from openkyrozen.agent.conversation import tool_result_to_dict
        full_output = json.dumps(tool_result_to_dict(tool_result), ensure_ascii=False,
                                 sort_keys=True, separators=(",", ":"))
        if len(full_output) > _TOOL_ARTIFACT_THRESHOLD:
            memory = getattr(self, "memory_bank", None)
            store = getattr(memory, "store", None)
            user_id = getattr(task_manager, "user_id", None) or getattr(memory, "user_id", None)
            workspace_id = getattr(task_manager, "workspace_id", None) or getattr(memory, "workspace_id", None)
            session_id = getattr(task_manager, "session_id", None) or getattr(memory, "session_id", None)
            artifacts = getattr(store, "artifacts", None)
            if artifacts is None or not all((user_id, workspace_id, session_id)):
                raise RuntimeError("Cannot retain oversized tool output without a session-scoped artifact store")
            artifact_id = artifacts.create(full_output, user_id=user_id, workspace_id=workspace_id,
                                           session_id=session_id)
            preview = tool_result.text[:_TOOL_ARTIFACT_PREVIEW]
            suffix = " …" if len(tool_result.text) > len(preview) else ""
            bounded_error = (ToolError(
                tool_result.error.code, tool_result.error.message[:_TOOL_ARTIFACT_PREVIEW],
                tool_result.error.retriable, {"artifact_id": artifact_id},
            ) if tool_result.error else None)
            summary = {"artifact_id": artifact_id, "total_chars": len(full_output),
                       "preview": f"{preview}{suffix}"}
            tool_result = ToolResult(
                data=summary,
                text=json.dumps(summary, ensure_ascii=False),
                error=bounded_error,
                metadata={"artifact_id": artifact_id},
            )
            tool_error = bounded_error
            result = render_tool_result(tool_result)
            result_text = self._fix_safe_text(result, 2000, preserve_lines=True)
            result_data = tool_result.data
    success = tool_result.success if tool_result is not None else bool(success)
    canonical_arguments = receipt_arguments if receipt_arguments is not None else args if isinstance(args, dict) else None
    evidence_arguments = canonical_arguments if canonical_arguments is not None else args
    effect, effect_verified = self._subagent_tool_evidence(canonical, evidence_arguments, result_text, success=success)
    acceptance, accepted = self._acceptance_for_tool(canonical, evidence_arguments, result_text, success=success)
    verification = {"status": "unavailable", "method": "none", "observed": None}
    if command is not None:
        verification = {
            "status": "verified" if command.exit_code == 0 else "failed" if command.exit_code is not None else "unavailable",
            "method": "command_exit_status", "observed": {"exit_code": command.exit_code},
        }
    elif canonical == "write_file" and success:
        verification = {
            "status": "verified" if effect_verified else "failed",
            "method": "file_content_match", "observed": effect,
        }
    elif getattr(expected_spec, "side_effects", None) == "read_only":
        verification = {"status": "not_applicable", "method": "read_only_tool", "observed": None}

    def safe_metadata(value, budget=4000):
        def clean(item, remaining, depth=0):
            if remaining <= 0:
                return "<truncated>", 0
            if isinstance(item, dict) and depth < 5:
                result = {}
                for key, nested in list(item.items())[:40]:
                    key = str(key)[:100]
                    if re.search(r"(?i)(api.?key|credential|password|secret|token|authorization|request.?body|^body$)", key):
                        result[key] = "<redacted>"
                        remaining -= len(key) + 12
                    else:
                        result[key], remaining = clean(nested, remaining - len(key), depth + 1)
                    if remaining <= 0:
                        break
                return result, remaining
            if isinstance(item, list) and depth < 5:
                result = []
                for nested in item[:40]:
                    value, remaining = clean(nested, remaining, depth + 1)
                    result.append(value)
                    if remaining <= 0:
                        break
                return result, remaining
            if isinstance(item, str):
                value = self._fix_safe_text(item, min(500, remaining), preserve_lines=True)
                return value, remaining - len(value)
            if item is None or type(item) in {bool, int, float}:
                return item, remaining - 16
            value = self._fix_safe_text(item, min(200, remaining))
            return value, remaining - len(value)
        return clean(value, budget)[0]

    receipt = ExecutionReceipt(
        receipt_id=f"receipt_{uuid.uuid4().hex}",
        operation_id=self._operation_id(operation_scope, canonical,
                                        canonical_arguments if canonical_arguments is not None else args),
        action=canonical,
        args=self._fix_safe_text(self._operation_args(canonical, args), 1000),
        args_sha256=(hashlib.sha256(self._operation_args(canonical, canonical_arguments).encode("utf-8")).hexdigest()
                     if canonical_arguments is not None else None),
        authorized=authorized,
        started_at=started_at,
        completed_at=utc_now(),
        success=tool_result.success if tool_result is not None else bool(success),
        result=result_text,
        failure=failure if not success else None,
        exit_code=command.exit_code if command is not None else None,
        verified_effect=effect if effect_verified else None,
        acceptance=acceptance if success and (accepted or effect_verified) else None,
        tool_error=({"code": tool_error.code, "message": self._fix_safe_text(tool_error.message, 1000),
                     "retriable": tool_error.retriable, "metadata": safe_metadata(tool_error.metadata)} if tool_error else None),
        result_data=result_data,
        call_id=call_id,
        call_id_source=call_id_source,
        arguments=canonical_arguments,
        tool_result=tool_result,
        user_id=getattr(task_manager, "user_id", None) or getattr(getattr(self, "memory_bank", None), "user_id", None),
        workspace_id=getattr(task_manager, "workspace_id", None) or getattr(getattr(self, "memory_bank", None), "workspace_id", None),
        session_id=getattr(task_manager, "session_id", None) or getattr(getattr(self, "memory_bank", None), "session_id", None),
        plan_id=getattr(getattr(task_manager, "working_plan", None), "id", None),
        task_id=task_id,
        tool_version=getattr(expected_spec, "version", None),
        tool_source=getattr(expected_spec, "source", None),
        side_effects=getattr(expected_spec, "side_effects", "unknown"),
        verification=verification,
    )
    if getattr(self, "_conversation_turn_id", None) is not None and self._conversation_turn_id.get():
        from openkyrozen.agent.conversation import tool_result_to_dict
        outcome = tool_result or ToolResult(
            data=receipt.result_data, text=receipt.result,
            error=(ToolError(receipt.failure or "execution_failed", receipt.result or "Tool failed")
                   if not receipt.success else None),
        )
        self._record_conversation_entry("tool.result", {
            "call_id": receipt.call_id, "name": receipt.action,
            "arguments": receipt.arguments if receipt.arguments is not None else {"legacy": receipt.args},
            "output": tool_result_to_dict(outcome), "is_error": not outcome.success,
            "receipt_id": receipt.receipt_id,
            "verification": receipt.verification,
        }, call_id=receipt.call_id, task_id=receipt.task_id)
    return receipt


def _run_tool(self, action: str, args: str, *, return_success: bool = False,
              return_receipt: bool = False, operation_scope: str = "",
              _typed_arguments: dict | None = None, _expected_spec=None,
              _call_id: str | None = None, _call_id_source: str = "legacy",
              _task_id: str | None = None, _pre_execution_check=None,
              _on_dispatch=None) -> str | tuple[str, bool] | ExecutionReceipt:
    started_at = utc_now()
    action = self.TOOL_ALIASES.get(action, action)
    receipt_args = _typed_arguments if _typed_arguments is not None else args
    approved_spec = _expected_spec or self.tool_registry.specs.get(action)
    receipt_arguments = _typed_arguments if _typed_arguments is not None else args if isinstance(args, dict) else None
    legacy_receipt_source = args
    parsed_arguments = None
    if isinstance(args, str) and args.lstrip().startswith("{"):
        try:
            parsed_arguments = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            try:
                parsed_arguments = ast.literal_eval(args)
            except (ValueError, SyntaxError):
                parsed_arguments = None
        if isinstance(parsed_arguments, dict):
            receipt_arguments = parsed_arguments
    if _typed_arguments is not None:
        args = _typed_arguments
    elif isinstance(args, dict):
        if action in ("run_cmd", "execute_terminal_command"):
            args = args.get("cmd") or args.get("command") or ""
        elif action == "write_file":
            args = f"{args.get('file_path') or args.get('path') or ''}|{args.get('content') or args.get('text') or ''}"
        else:
            args = json.dumps(args)
    elif isinstance(parsed_arguments, dict):
        if action in ("run_cmd", "execute_terminal_command"):
            args = parsed_arguments.get("cmd") or parsed_arguments.get("command") or ""
        elif action == "write_file":
            args = f"{parsed_arguments.get('file_path') or parsed_arguments.get('path') or ''}|{parsed_arguments.get('content') or parsed_arguments.get('text') or ''}"
        else:
            args = json.dumps(parsed_arguments)
    if isinstance(args, dict):
        receipt_arguments = args
        if not isinstance(receipt_args, dict):
            receipt_args = args
    elif receipt_arguments is None:
        legacy_arguments = _legacy_object_arguments(legacy_receipt_source, approved_spec)
        receipt_arguments = legacy_arguments or None
    self._publish_runtime_event(
        "tool.call", {"tool_name": action,
                      "argument_keys": sorted(receipt_args)[:64] if isinstance(receipt_args, dict) else []},
        call_id=_call_id, task_id=_task_id,
    )
    def finish(outcome: ToolResult, authorized: bool = False, raw_result: Any = None):
        text = render_tool_result(outcome)
        if return_receipt:
            return self._make_execution_receipt(
                action=action, args=receipt_args, receipt_arguments=receipt_arguments,
                authorized=authorized, started_at=started_at,
                success=outcome.success, result=text, operation_scope=operation_scope,
                failure=outcome.error.code if outcome.error else None, raw_result=raw_result,
                tool_error=outcome.error, result_data=outcome.data, tool_result=outcome,
                expected_spec=approved_spec, call_id=_call_id, call_id_source=_call_id_source,
                task_id=_task_id,
            )
        return (text, outcome.success) if return_success else text
    if action not in self.AVAILABLE_TOOLS:
        outcome = tool_failure("invalid_arguments", f"Error: unknown tool '{action}'")
        self._notify_tool_execute(action, args, outcome)
        return finish(outcome)
    interaction_state = self._interaction_controller.state()
    executing_plan = interaction_state.get("executing_plan")
    if _awaiting_revision_mutation(interaction_state, action, args, approved_spec, self):
        outcome = tool_failure("permission_denied", "Error: strategic plan revision is awaiting approval; mutation is paused.")
        self._notify_tool_execute(action, args, outcome)
        return finish(outcome)
    if executing_plan and action != "cancel_subagent":
        denial = _strategic_plan_denial(
            executing_plan, action, args, self.tool_registry.specs.get(action), self._get_workspace_root(),
        )
        if denial:
            outcome = tool_failure("permission_denied", f"Error: {denial}.")
            self._notify_tool_execute(action, args, outcome)
            return finish(outcome)
    from openkyrozen.security.permission_gate import git_read_arguments_allowed
    if not git_read_arguments_allowed(action, args):
        outcome = tool_failure("invalid_arguments", "Error: read-only Git tools reject output-writing and external-execution options.")
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
    if authorized:
        try:
            authorized = (self._execution_capability_token.allows(required_capability)
                          and required_capability in self._effective_execution_capabilities())
        except AgentConfigError:
            authorized = False
    if authorized and callable(_pre_execution_check) and not _pre_execution_check():
        outcome = tool_failure("invalid_arguments", "Error: task binding is no longer active.")
        self._notify_tool_execute(action, args, outcome)
        return finish(outcome)
    request_var = getattr(self, "_approval_request", None)
    request = request_var.get() if request_var is not None else None
    if authorized and self.tool_registry.specs.get(action) is not approved_spec:
        authorized = False
    if authorized and request is not None:
        approved_spec = self._approval_tool_spec.get()
        current_spec = self.tool_registry.specs.get(action)
        authorized = (
            current_spec is approved_spec
            and request.tool_name == action
            and request.arguments == _typed_arguments
            and request.workspace_id == self.memory_bank.workspace_id
            and request.session_id == self.current_session.session_id
            and self._execution_capability_token.allows(current_spec.capability)
        )
    if authorized and executing_plan and action != "cancel_subagent":
        current_state = self._interaction_controller.state()
        current_plan = current_state.get("executing_plan")
        if (not current_plan or current_plan.get("plan_id") != executing_plan.get("plan_id")
                or current_plan.get("fingerprint") != executing_plan.get("fingerprint")):
            denial = "accepted plan changed while approval was pending"
        elif _awaiting_revision_mutation(current_state, action, args, self.tool_registry.specs.get(action), self):
            denial = "strategic plan revision is awaiting approval; mutation is paused"
        else:
            denial = _strategic_plan_denial(
                current_plan, action, args, self.tool_registry.specs.get(action), self._get_workspace_root(),
            )
        if denial:
            outcome = tool_failure("permission_denied", f"Error: {denial}.")
            self._notify_tool_execute(action, args, outcome)
            return finish(outcome)
    if not authorized:
        outcome = tool_failure("approval_denied", f"Error: {action} requires confirmation. Approve it interactively or set KYROZEN_APPROVAL_MODE=never for an explicitly automated CLI.")
        self._notify_tool_execute(action, args, outcome)
        return finish(outcome)
    start = time.time()
    raw_result = None
    try:
        execution_args = args
        if _typed_arguments is not None:
            if approved_spec is not _expected_spec or self._execution_capability_token.allows(approved_spec.capability) is not True:
                outcome = tool_failure("permission_denied", "Error: tool authority or registration changed before execution.")
                self._notify_tool_execute(action, args, outcome)
                return finish(outcome)
            execution_args = _typed_arguments
        with self._delegation_tool_access(action, execution_args):
            if callable(_on_dispatch):
                _on_dispatch()
            self._publish_runtime_event(
                "tool.execution_started", {"tool_name": action}, call_id=_call_id, task_id=_task_id,
            )
            if action in {"run_cmd", "execute_terminal_command"}:
                raw_result = self.run_command(execution_args)
                outcome = command_tool_result(raw_result)
            else:
                outcome = self.tool_registry._invoke_executor(approved_spec, execution_args)
    except KeyboardInterrupt:
        outcome = tool_failure("cancelled", "Tool execution interrupted by user (Ctrl+C).")
    except Exception as exc:
        outcome = tool_failure("execution_error", f"Error: {type(exc).__name__}: {exc}")
    elapsed = time.time() - start
    text = render_tool_result(outcome)
    try:
        self._track_tool_performance(action, outcome.success, elapsed)
    except Exception:
        pass
    self._notify_tool_execute(action, args, outcome)
    if outcome.success and action in {"git_pull", "git_checkout", "git_reset", "git_clone"} and self._project_graph is not None:
        try:
            self._project_graph.refresh_async()
        except Exception:
            pass
    return finish(outcome, authorized=True, raw_result=raw_result)


def _execute_turn_action(self, action: str, args: Any, *, operation_scope: str,
                         successful_operations: set[str], call_id: str | None = None,
                         call_id_source: str = "synthetic") -> ExecutionReceipt:
    """Execute one model action, refusing a duplicate successful mutation."""
    canonical = self._operation_action(action)
    started_at = utc_now()
    call_id = call_id or f"synthetic_{uuid.uuid4().hex}"
    expected_spec = self.tool_registry.specs.get(canonical)
    def emit_rejected_call():
        self._publish_runtime_event(
            "tool.call", {"tool_name": canonical,
                          "argument_keys": sorted(args)[:64] if isinstance(args, dict) else []},
            call_id=call_id,
        )

    native_arguments = isinstance(args, dict) and call_id_source == "native"
    if isinstance(args, dict) and not native_arguments:
        emit_rejected_call()
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
            tool_result=outcome, call_id=call_id, call_id_source=call_id_source,
            expected_spec=expected_spec,
        )
    structured = args if native_arguments else None
    if not native_arguments and isinstance(args, str) and args.lstrip().startswith("{"):
        try:
            parsed = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        structured = parsed if isinstance(parsed, dict) else None
    if structured is None:
        structured = _legacy_object_arguments(args, expected_spec)
    interaction_state = self._interaction_controller.state()
    spec = expected_spec
    typed_arguments = None
    if native_arguments:
        schema_error = (self.tool_registry._argument_error(spec.input_schema, args)
                        if spec is not None else "unknown tool")
        if schema_error is not None:
            emit_rejected_call()
            result = f"Error: invalid arguments for `{action}`: {schema_error}."
            outcome = tool_failure("invalid_arguments", result)
            self._notify_tool_execute(action, args, outcome)
            return self._make_execution_receipt(
                action=canonical, args=args, authorized=False, started_at=started_at, success=False,
                result=result, operation_scope=operation_scope, failure=outcome.error.code,
                tool_error=outcome.error, tool_result=outcome, call_id=call_id,
                call_id_source=call_id_source, expected_spec=expected_spec,
            )
        typed_arguments = args
    elif spec is not None and spec.source not in {"builtin", "legacy"}:
        schema_error = (self.tool_registry._argument_error(spec.input_schema, structured)
                        if isinstance(structured, dict) else "expected a JSON object")
        if schema_error is not None:
            emit_rejected_call()
            result = f"Error: invalid arguments for `{action}`: {schema_error}."
            outcome = tool_failure("invalid_arguments", result)
            self._notify_tool_execute(action, args, outcome)
            return self._make_execution_receipt(
                action=canonical, args=args, authorized=False, started_at=started_at, success=False,
                result=result, operation_scope=operation_scope, failure=outcome.error.code,
                tool_error=outcome.error, tool_result=outcome, call_id=call_id,
                call_id_source=call_id_source, expected_spec=expected_spec,
            )
        typed_arguments = structured
    if (not native_arguments and interaction_state.get("executing_plan") and spec
            and spec.source != "legacy" and spec.legacy_fields):
        parts = args.split("|", len(spec.legacy_fields) - 1)
        if len(parts) == len(spec.legacy_fields):
            candidate = dict(zip(spec.legacy_fields, parts))
            try:
                if (spec.legacy_arguments(candidate) == args
                        and self.tool_registry._argument_error(spec.input_schema, candidate) is None):
                    typed_arguments = candidate
            except (TypeError, ValueError):
                pass
    if canonical == "update_plan" and isinstance(structured, dict):
        try:
            if self.tool_registry._argument_error(expected_spec.input_schema, structured) is None:
                typed_arguments = structured
        except (AttributeError, TypeError, ValueError):
            pass
    operation_arguments = typed_arguments or (structured if isinstance(structured, dict) and structured else args)
    operation_id = self._operation_id(operation_scope, canonical, operation_arguments)
    is_mutation = self._is_state_changing_action(canonical, args)
    if _awaiting_revision_mutation(interaction_state, canonical, args, expected_spec, self):
        emit_rejected_call()
        result = "Error: strategic plan revision is awaiting approval; mutation is paused."
        outcome = tool_failure("permission_denied", result)
        self._notify_tool_execute(canonical, args, outcome)
        return self._make_execution_receipt(
            action=canonical, args=args, authorized=False, started_at=started_at, success=False,
            result=result, operation_scope=operation_scope, failure=outcome.error.code,
            tool_error=outcome.error, tool_result=outcome, call_id=call_id,
            call_id_source=call_id_source, expected_spec=expected_spec,
        )
    active_plan = interaction_state.get("executing_plan")
    if is_mutation and active_plan and canonical not in {"cancel_subagent", "update_plan"}:
        structured = typed_arguments or structured
        match_args = (json.dumps(structured, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                      if isinstance(structured, dict) and structured else args)
        current_task = next((task for task in self.tasks.tasks
            if self.tasks.working_plan and task.get("id") in self.tasks.working_plan.task_ids
            and task.get("status") in {"pending", "running"}
            and self.tasks._dependencies_succeeded(task, self.tasks.tasks)), {})
        checkpoint = current_task.get("checkpoint") or {}
        scoped_file_mutation = (
            not checkpoint.get("action")
            and canonical in {"write_file", "edit_file", "apply_patch"}
            and _file_mutation_matches_objective(current_task, structured)
            and _strategic_plan_denial(active_plan, canonical, structured, expected_spec,
                                       self._get_workspace_root()) is None
        )
        allowed, reason = ((True, "") if scoped_file_mutation else
                           self.tasks.mutation_matches_current_task(canonical, match_args))
        if not allowed:
            emit_rejected_call()
            result = f"Error: action does not match the accepted plan; {reason}."
            outcome = tool_failure("permission_denied", result)
            self._notify_tool_execute(canonical, args, outcome)
            return self._make_execution_receipt(
                action=canonical, args=args,
                receipt_arguments=operation_arguments if isinstance(operation_arguments, dict) else None,
                authorized=False, started_at=started_at,
                success=False, result=result, operation_scope=operation_scope,
                failure=outcome.error.code, tool_error=outcome.error,
                tool_result=outcome, call_id=call_id, call_id_source=call_id_source,
                expected_spec=expected_spec,
            )
    if (canonical != "run_cmd" and self._is_state_changing_action(canonical, args)
            and operation_id in successful_operations):
        emit_rejected_call()
        outcome = tool_failure("conflict", "Error: duplicate successful state-changing action refused for this turn.")
        result = render_tool_result(outcome)
        self._notify_tool_execute(canonical, args, outcome)
        return self._make_execution_receipt(
            action=canonical, args=args,
            receipt_arguments=operation_arguments if isinstance(operation_arguments, dict) else None,
            authorized=True, started_at=started_at, success=False,
            result=result, operation_scope=operation_scope, failure=outcome.error.code, tool_error=outcome.error,
            tool_result=outcome, call_id=call_id, call_id_source=call_id_source,
            expected_spec=expected_spec,
        )
    receipt = self._run_tool(canonical, args, return_receipt=True, operation_scope=operation_scope,
                             _typed_arguments=typed_arguments,
                             _expected_spec=spec if typed_arguments is not None else None,
                             _call_id=call_id, _call_id_source=call_id_source)
    assert isinstance(receipt, ExecutionReceipt)
    if (receipt.success and receipt.action != "run_cmd"
            and self._is_state_changing_action(receipt.action, args)):
        successful_operations.add(receipt.operation_id)
    return receipt


def _execute_tool_batch(self, calls: list[dict[str, Any]], *, operation_scope: str,
                        successful_operations: set[str], call_id_source: str = "synthetic",
                        on_receipt) -> None:
    """Run consecutive catalog-approved reads concurrently and deliver in call order."""
    def is_parallel_read(call):
        action = self._operation_action(call.get("action", ""))
        spec = self.tool_registry.specs.get(action)
        return bool(spec and spec.parallel_safe and spec.side_effects == "read_only"
                    and spec.capability == "read" and spec.risk == "normal"
                    and not call.get("task_id"))

    def execute(call):
        return self._execute_turn_action(
            call.get("action", ""), call.get("args", ""), operation_scope=operation_scope,
            successful_operations=successful_operations, call_id=call.get("call_id"),
            call_id_source=call.get("call_id_source", call_id_source),
        )

    index = 0
    while index < len(calls):
        if not is_parallel_read(calls[index]):
            call = calls[index]
            on_receipt(call, execute(call))
            index += 1
            continue

        end = index + 1
        while end < len(calls) and is_parallel_read(calls[end]):
            end += 1
        group = calls[index:end]
        if len(group) == 1:
            on_receipt(group[0], execute(group[0]))
        else:
            with ThreadPoolExecutor(max_workers=min(4, len(group))) as executor:
                futures = [executor.submit(copy_context().run, execute, call) for call in group]
                for call, future in zip(group, futures):
                    on_receipt(call, future.result())
        index = end


def _record_turn_receipt(self, receipt: ExecutionReceipt, *, allow_legacy_task_matching: bool = False) -> dict[str, Any]:
    """Persist a receipt and reconcile the current planned task from verified evidence."""
    from openkyrozen.agent.delegation import TOOLS
    active_task = self.tasks.active_working_task(receipt.task_id) if receipt.task_id else None
    explicit_task_id = receipt.task_id if active_task is not None else None
    receipt_projection = receipt.as_dict()
    task_acceptance = receipt.acceptance
    if explicit_task_id and receipt.action == "run_cmd" and task_acceptance:
        command = (receipt.arguments or {}).get("command", "")
        criteria = next((item.get("acceptance", []) for item in self.tasks.tasks
                         if item.get("id") == explicit_task_id), [])
        declared = [" ".join(item.split()).casefold() for item in criteria if isinstance(item, str)]
        if not command or len(declared) != 1 or " ".join(command.split()).casefold() != declared[0]:
            task_acceptance = None
        else:
            task_acceptance = receipt_projection["acceptance"]
    should_reconcile = (
        bool(explicit_task_id) or receipt.call_id_source == "legacy"
        or (allow_legacy_task_matching and receipt.call_id_source == "synthetic")
    )
    legacy_contract = any(
        task.get("status") in {"pending", "running"}
        and self.tasks._dependencies_succeeded(task, self.tasks.tasks)
        and (task.get("checkpoint") or {}).get("action") == receipt.action
        and isinstance((task.get("checkpoint") or {}).get("args"), str)
        and not task["checkpoint"]["args"].lstrip().startswith("{")
        for task in self.tasks.tasks
    )
    matching_args = (receipt.args if legacy_contract else
                     json.dumps(receipt.arguments, ensure_ascii=False, allow_nan=False,
                                sort_keys=True, separators=(",", ":"))
                     if receipt.arguments is not None else receipt.args)
    evidence = {} if receipt.action in TOOLS or not should_reconcile else self.tasks.record_evidence(
        task_id=explicit_task_id, action=receipt.action, args=matching_args,
        args_projection=receipt_projection["args"],
        args_fingerprint=None if legacy_contract else receipt.args_sha256,
        result=receipt_projection["result"], success=receipt.success,
        acceptance=task_acceptance, receipt_id=receipt.receipt_id,
        verification_status=receipt.verification.get("status"),
        verification_result=(receipt.tool_result.text if receipt.action == "read_file"
                             and receipt.tool_result is not None else None),
    )
    task_id = evidence.get("task_id")
    effective_acceptance = evidence.get("acceptance") or (
        receipt_projection["acceptance"] if receipt.task_id is None else None
    )
    if task_id:
        task = next((item for item in self.tasks.tasks if item["id"] == task_id), None)
        checkpoint = (task or {}).get("checkpoint") or {}
        if task and not checkpoint.get("action") and not checkpoint.get("strategic_plan"):
            self.tasks.update_checkpoint(task_id, {"action": receipt.action, "args": receipt_projection["args"]})
    if task_id and receipt.success and effective_acceptance:
        task = next((item for item in self.tasks.tasks if item["id"] == task_id), None)
        strategic = (task or {}).get("checkpoint", {}).get("strategic_plan", {})
        required = set(strategic.get("acceptance_ids", []))
        covered = {criterion_id for item in (task or {}).get("evidence", [])
                   for criterion_id in item.get("acceptance_ids", [])}
        if not required or required <= covered:
            index = next(index for index, item in enumerate(self.tasks.tasks) if item["id"] == task_id)
            self.tasks.set_status(index, "succeeded")
    receipt_payload = receipt_projection
    if receipt.task_id is not None and receipt.task_id != task_id:
        receipt_payload["requested_task_id"] = receipt.task_id
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
        "call_id": receipt.call_id, "call_id_source": receipt.call_id_source,
        "action": receipt.action, "args": receipt_projection["args"], "result": receipt_projection["result"],
        "success": receipt.success, "authorized": receipt.authorized,
        "acceptance": effective_acceptance, "failure": receipt_projection["failure"],
        "tool_error": receipt_projection["tool_error"],
        "task_id": task_id, "requested_task_id": receipt.task_id,
        "result_data": receipt_projection["result_data"], "verification": receipt_projection["verification"],
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
