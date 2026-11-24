from __future__ import annotations

import hashlib
import ast
import json
import os
import re
import uuid
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from types import MethodType
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
_RETRYABLE_NETWORK_LOOKUPS = frozenset({
    "search_web", "read_webpage", "analyze_remote_repo", "github_status", "github_read",
})
_STRATEGIC_EFFECTS = {
    "none": "read_only", "read": "read_only", "read_only": "read_only",
    "mutation": "filesystem", "internal_write": "internal", "orchestration": "internal",
    "stateful": "internal", "external": "remote", "unknown": "unknown",
}


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


def _strategic_tool_denial(plan, spec):
    """Check the argument-independent bounds of an accepted plan."""
    bounds = plan.get("bounds") if isinstance(plan, dict) else None
    if not isinstance(bounds, dict):
        return "accepted plan has no enforceable bounds"
    capabilities = set(bounds.get("capabilities", ()))
    effects = set(bounds.get("effects", ()))
    if not spec or spec.capability not in capabilities:
        return "tool capability is outside the accepted plan"
    effect = _STRATEGIC_EFFECTS.get(spec.side_effects, spec.side_effects)
    if effect not in effects:
        return "tool effect is outside the accepted plan"
    if effect == "process":
        return "process effects cannot be confined to approved workspace paths"
    if spec.risk == "high" and bounds.get("risk_ceiling") != "high":
        return "tool risk exceeds the accepted plan"
    return None


def _strategic_plan_denial(plan, action, args, spec, workspace_root):
    """Return a denial when a tool exceeds the bounds the user accepted."""
    denial = _strategic_tool_denial(plan, spec)
    if denial:
        return denial
    bounds = plan["bounds"]
    effect = _STRATEGIC_EFFECTS.get(spec.side_effects, spec.side_effects)
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
    pending = state.get("pending_plan")
    return bool(
        action != "cancel_subagent" and pending
        and (state.get("executing_plan") or pending.get("revision_materiality") == "material")
        and (runtime._is_state_changing_action(action, args) or not spec
             or spec.capability in {"write", "shell", "destructive"}
             or spec.side_effects in {"filesystem", "process", "destructive"})
    )


def _builtin_interaction_control(runtime, action, spec) -> bool:
    from openkyrozen.agent.plan_tools import _propose_plan, _request_question
    from openkyrozen.tools.catalog import builtin_metadata

    handlers = {"propose_plan": _propose_plan, "request_question": _request_question}
    if action not in handlers or spec is None or spec.source != "builtin" or spec.name != action:
        return False
    handler = getattr(handlers[action], "__tool_result_executor__", handlers[action])
    if (getattr(spec.executor, "__self__", None) is not runtime
            or getattr(spec.executor, "__func__", None) is not handler):
        return False
    metadata = builtin_metadata(action)
    return (spec.input_schema == metadata["input_schema"] and spec.version == "1.0.0"
            and all(getattr(spec, field) == metadata[field] for field in (
                "capability", "side_effects", "risk", "parallel_safe", "aliases",
                "legacy_fields", "legacy_argument_aliases", "confirmation_surfaces",
            )))


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
                            task_plan_id: str | None = None,
                            receipt_arguments: dict[str, Any] | None = None,
                            _duplicate_successful_operation: bool = False,
                            _precommit_plan_validation: bool = False,
                            _plan_write_precondition: bool = False,
                            expected_spec=None, file_content_changed: bool = False) -> ExecutionReceipt:
    canonical = self._operation_action(action)
    from openkyrozen.agent.delegation import TOOLS
    current_session = getattr(self, "current_session", None)
    delegated_parent = getattr(current_session, "_delegation_parent", None)
    task_owner = self if task_plan_id is not None or canonical == "update_plan" else delegated_parent or self
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
    plan_read_result = None
    plan_read_method = None
    if canonical == "update_plan" and tool_result is not None:
        for method in ("working_plan_confirmation", "working_plan_evidence_check"):
            if tool_result.metadata.get(method) is True:
                plan_read_result = tool_result.data
                plan_read_method = method
                break
    git_commit = tool_result.metadata.get("git_commit") if canonical == "git_show" and tool_result is not None else None
    read_data = tool_result.data if canonical == "read_file" and tool_result is not None else None
    archived_read_content = None
    # Keep large outputs out of provider messages and the conversation journal.
    if tool_result is not None and canonical != "read_tool_artifact":
        from openkyrozen.agent.conversation import tool_result_to_dict
        full_output = json.dumps(tool_result_to_dict(tool_result), ensure_ascii=False,
                                 sort_keys=True, separators=(",", ":"))
        if len(full_output) > _TOOL_ARTIFACT_THRESHOLD:
            memory = getattr(self, "memory_bank", None)
            store = getattr(memory, "store", None)
            artifact_owner = getattr(current_session, "tasks", None) or task_manager
            user_id = getattr(artifact_owner, "user_id", None) or getattr(memory, "user_id", None)
            workspace_id = getattr(artifact_owner, "workspace_id", None) or getattr(memory, "workspace_id", None)
            session_id = getattr(artifact_owner, "session_id", None) or getattr(memory, "session_id", None)
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
            if (authorized and tool_result.success and getattr(expected_spec, "source", None) == "builtin"
                    and isinstance(read_data, dict) and isinstance(read_data.get("content"), str)
                    and read_data.get("truncated") is False):
                archived_read_content = read_data["content"]
            summary = {"artifact_id": artifact_id, "total_chars": len(full_output),
                       "preview": f"{preview}{suffix}"}
            projected_text = json.dumps(summary, ensure_ascii=False)
            if (canonical == "wait_subagents" and call_id_source == "native" and authorized and tool_result.success
                    and getattr(expected_spec, "source", None) == "builtin"):
                from openkyrozen.agent.delegation_runtime import _wait_result_preview, wait_subagents

                executor = expected_spec.executor
                original = tool_result.data
                agents = original.get("agents") if isinstance(original, dict) else None
                if (getattr(executor, "__self__", None) is self
                        and getattr(executor, "__func__", None) is wait_subagents.__tool_result_executor__
                        and isinstance(agents, list)):
                    projected_text = "The full delegation audit is retained in a tool artifact."
                    summary.update(agents=_wait_result_preview(agents), agent_count=len(agents),
                                   preview=projected_text)
            tool_result = ToolResult(
                data=summary, text=projected_text, error=bounded_error,
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
    elif git_commit and success:
        verification = {"status": "verified", "method": "git_commit_identity", "observed": {"commit": git_commit}}
    elif getattr(expected_spec, "side_effects", None) == "read_only":
        verification = {"status": "not_applicable", "method": "read_only_tool", "observed": None}
        if canonical == "read_file" and call_id_source == "native" and authorized and success:
            from openkyrozen.tools.manifest import typed_executor
            adapters = getattr(getattr(current_session, "workspace", None), "adapters", None)
            read = getattr(adapters, "read_file", None)
            if (expected_spec.source == "builtin" and read is not None and expected_spec.executor == typed_executor(read)
                    and isinstance(read_data, dict) and isinstance(read_data.get("content"), str)
                    and read_data.get("truncated") is False and read_data.get("start_line") == 1
                    and read_data.get("end_line") == read_data.get("total_lines")
                    and hashlib.sha256(read_data["content"].encode("utf-8")).hexdigest() == read_data.get("sha256")):
                verification["observed"] = {"complete_file": True}
    if file_content_changed and success:
        verification["file_content_changed"] = True
    if (canonical == "update_plan" and expected_spec is not None
            and (call_id_source == "native" or plan_read_method == "working_plan_evidence_check")):
        from openkyrozen.agent.plan_tools import _update_plan
        executor = expected_spec.executor
        trusted = (getattr(executor, "__self__", None) is self
                   and getattr(executor, "__func__", None) is _update_plan.__tool_result_executor__)
        turn_id = self._conversation_turn_id.get()
        if trusted and success and plan_read_result is not None:
            verification = {
                "status": "verified" if plan_read_method == "working_plan_confirmation" else "not_applicable",
                "method": plan_read_method,
                "observed": {"plan_id": plan_read_result["plan_id"], "revision": plan_read_result["revision"],
                             "turn_id": turn_id},
            }
        elif trusted and success and task_manager.working_plan is not None:
            plan = task_manager.working_plan
            verification = {"status": "verified", "method": "working_plan_commit", "observed": {
                "plan_id": plan.id, "revision": plan.revision, "turn_id": turn_id}}
        elif (trusted and tool_error is not None and tool_error.code == "invalid_arguments"
              and (_precommit_plan_validation or tool_error.metadata.get("plan_validation_no_effect") is True)):
            observed = {"committed": False, "turn_id": turn_id}
            if canonical_arguments is not None and "plan_id" in canonical_arguments:
                active_id = getattr(task_manager.working_plan, "id", None)
                observed.update(plan_id=active_id,
                                target_mismatch=not active_id or canonical_arguments["plan_id"] != active_id)
            verification = {"status": "not_applicable", "method": "plan_input_validation",
                            "observed": observed}
    if _duplicate_successful_operation:
        verification = {"status": "not_applicable", "method": "duplicate_success_refusal", "observed": None}
    if _plan_write_precondition:
        verification = {"status": "not_applicable", "method": "working_plan_write_precondition", "observed": None}

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
        session_id=(getattr(current_session, "session_id", None) if delegated_parent else None)
        or getattr(task_manager, "session_id", None)
        or getattr(getattr(self, "memory_bank", None), "session_id", None),
        plan_id=(verification["observed"]["plan_id"]
                 if plan_read_method is not None and verification.get("method") == plan_read_method
                 else task_plan_id or getattr(getattr(task_manager, "working_plan", None), "id", None)),
        task_id=task_id,
        tool_version=getattr(expected_spec, "version", None),
        tool_source=getattr(expected_spec, "source", None),
        side_effects=getattr(expected_spec, "side_effects", "unknown"),
        verification=verification,
        archived_read_content=archived_read_content,
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


def _write_changes_bytes(adapters, arguments):
    try:
        path = adapters._resolve_workspace_path(arguments["path"])
        if not path.is_file():
            return not path.exists()
        expected = arguments["content"].replace("\n", os.linesep).encode("utf-8")
        with path.open("rb") as existing:
            return existing.read(len(expected) + 1) != expected
    except (OSError, ValueError):
        return False


def _run_tool(self, action: str, args: str, *, return_success: bool = False,
              return_receipt: bool = False, operation_scope: str = "",
              _typed_arguments: dict | None = None, _expected_spec=None,
              _call_id: str | None = None, _call_id_source: str = "legacy",
              _task_id: str | None = None, _pre_execution_check=None,
              _task_plan_id: str | None = None,
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
    file_content_changed = False
    plan_write_precondition = False
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
                task_id=_task_id, task_plan_id=_task_plan_id, file_content_changed=file_content_changed,
                _plan_write_precondition=plan_write_precondition,
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
    if (executing_plan and action != "cancel_subagent"
            and not _builtin_interaction_control(self, action, approved_spec)):
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
    if (authorized and action != "cancel_subagent"
            and not _builtin_interaction_control(self, action, self.tool_registry.specs.get(action))):
        current_state = self._interaction_controller.state()
        current_plan = current_state.get("executing_plan")
        denial = None
        if _awaiting_revision_mutation(current_state, action, args, approved_spec, self):
            denial = "strategic plan revision is awaiting approval; mutation is paused"
        elif any((current_plan or {}).get(key) != (executing_plan or {}).get(key)
                 for key in ("plan_id", "fingerprint")):
            denial = "accepted plan changed while approval was pending"
        elif current_plan:
            denial = _strategic_plan_denial(
                current_plan, action, args, approved_spec, self._get_workspace_root(),
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
    attempt = 0
    try:
        execution_args = args
        if _typed_arguments is not None:
            if approved_spec is not _expected_spec or self._execution_capability_token.allows(approved_spec.capability) is not True:
                outcome = tool_failure("permission_denied", "Error: tool authority or registration changed before execution.")
                self._notify_tool_execute(action, args, outcome)
                return finish(outcome)
            execution_args = _typed_arguments
        retry_safe = bool(
            approved_spec
            and approved_spec.risk == "normal"
            and not self._is_state_changing_action(action, execution_args)
            and (
                (approved_spec.capability == "read"
                 and approved_spec.side_effects in {"none", "read", "read_only"})
                or (action in _RETRYABLE_NETWORK_LOOKUPS
                    and approved_spec.source == "builtin"
                    and approved_spec.capability == "network"
                    and approved_spec.side_effects == "remote")
            )
        )
        native_file_action = False
        if (_call_id_source == "native" and _typed_arguments is not None and approved_spec.source == "builtin"
                and action in {"write_file", "edit_file", "apply_patch"} and not self.execution_context.child_run_id
                and not executing_plan and not interaction_state.get("pending_plan")):
            from openkyrozen.tools.manifest import typed_executor
            adapters = self.current_session.workspace.adapters
            executor = approved_spec.executor
            native_file_action = (type(executor) is MethodType
                                  and executor == typed_executor(getattr(adapters, action, None)))
        while True:
            attempt += 1
            with self._delegation_tool_access(action, execution_args):
                if self.execution_context.cancelled is not None and self.execution_context.cancelled.is_set():
                    outcome = tool_failure("cancelled", "Tool call was cancelled before execution.")
                    break
                if callable(_pre_execution_check) and not _pre_execution_check():
                    outcome = tool_failure("cancelled", "Tool retry stopped because its task is no longer active.")
                    break
                if (self.tool_registry.specs.get(action) is not approved_spec
                        or not self._execution_capability_token.allows(approved_spec.capability)):
                    outcome = tool_failure("permission_denied", "Tool retry stopped because its authority changed.")
                    break
                if native_file_action and self.execution_context.native_plan_active:
                    selected = self.tasks.active_working_task(_task_id) if _task_id else None
                    if not selected or not _file_edit_has_contract(
                            self, selected, action, execution_args, adapters):
                        from openkyrozen.tasks.engine import can_bind_command_check, can_replace_complete_read_check
                        guidance = "Select a Working Plan task with typed verification before editing files."
                        if (selected and self._execution_capability_token.allows("shell")
                                and "run_cmd" in self._permitted_tool_names()):
                            if can_replace_complete_read_check(selected):
                                guidance += (" Use one update_plan operation, replace_command_check, with the selected "
                                             "task ID, a new ref and the exact test command. It starts the replacement "
                                             "and preserves dependent tasks.")
                            elif can_bind_command_check(selected):
                                guidance += (" Use update_plan with bind_command_check, the selected task ID and "
                                             "the exact command.")
                        plan_write_precondition = True
                        outcome = tool_failure("plan_verification_required", guidance, retriable=True)
                        break
                if callable(_on_dispatch):
                    _on_dispatch()
                self._publish_runtime_event(
                    "tool.execution_started", {"tool_name": action, "attempt": attempt},
                    call_id=_call_id, task_id=_task_id,
                )
                if action in {"run_cmd", "execute_terminal_command"}:
                    raw_result = self.run_command(execution_args)
                    outcome = command_tool_result(raw_result)
                else:
                    if native_file_action and action == "write_file" and self._execution_capability_token.allows("read"):
                        file_content_changed = _write_changes_bytes(adapters, execution_args)
                    outcome = self.tool_registry._invoke_executor(approved_spec, execution_args)
                    if native_file_action and action == "edit_file":
                        file_content_changed = execution_args["old_text"] != execution_args["new_text"]
                    if native_file_action and action == "apply_patch" and outcome.success:
                        file_content_changed = outcome.data["old_sha256"] != outcome.data["new_sha256"]
            if (attempt == 1 and retry_safe and outcome.error is not None
                    and outcome.error.retriable):
                continue
            break
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
    return finish(outcome, authorized=not plan_write_precondition, raw_result=raw_result)


def _file_edit_has_contract(runtime, task, action, arguments, adapters):
    checkpoint = task.get("checkpoint") or {}
    if task["status"] != "running" or not checkpoint.get("native_execution_selected"):
        return False
    if (checkpoint.get("action") == action and (checkpoint.get("declared_execution") or not task.get("evidence"))
            and checkpoint.get("args") == json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))):
        return True
    allowed = runtime._permitted_tool_names() & {"run_cmd", "read_file"}
    for criterion in task.get("acceptance", ()):
        if not isinstance(criterion, dict) or criterion.get("action") not in allowed:
            continue
        spec = runtime.tool_registry.specs.get(criterion["action"])
        if spec is None or not runtime._execution_capability_token.allows(spec.capability):
            continue
        try:
            check = json.loads(criterion["args"])
            if criterion["action"] == "run_cmd" and not check.get("background"):
                return True
            expect = criterion.get("expect") or {}
            if (criterion["action"] == "read_file" and set(expect) in ({"equals"}, {"contains"})
                    and adapters._resolve_workspace_path(check["path"]) == adapters._resolve_workspace_path(arguments["path"])):
                return True
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            continue
    return False


def _execute_turn_action(self, action: str, args: Any, *, operation_scope: str,
                         successful_operations: set[str], call_id: str | None = None,
                         call_id_source: str = "synthetic", task_id: str | None = None,
                         task_plan_id: str | None = None, native_plan_start=None) -> ExecutionReceipt:
    """Execute one model action, refusing a duplicate successful mutation."""
    task_id = (task_id or getattr(getattr(self, "current_session", None), "_delegation_task_id", None)
               if action != "update_plan" else None)
    canonical = self._operation_action(action)
    started_at = utc_now()
    call_id = call_id or f"synthetic_{uuid.uuid4().hex}"
    expected_spec = self.tool_registry.specs.get(canonical)
    if (native_plan_start is self.tasks.working_plan
            and call_id_source == "native" and canonical == "update_plan" and expected_spec is not None
            and expected_spec.source == "builtin" and isinstance(args, dict)):
        from openkyrozen.agent.plan_tools import _update_plan

        executor = expected_spec.executor
        if (type(executor) is MethodType and executor.__self__ is self
                and executor.__func__ is _update_plan.__tool_result_executor__):
            if native_plan_start is None:
                from openkyrozen.agent.engine import _requests_one_delegation

                operations = args.get("operations")
                if (set(args) == {"expected_revision", "operations"} and type(args["expected_revision"]) is int
                        and args["expected_revision"] == 0 and isinstance(operations, list) and len(operations) == 1
                        and isinstance(operation := operations[0], dict) and operation.get("op") == "add_read_check"
                        and isinstance(operation.get("ref"), str) and not self.execution_context.child_run_id
                        and self._active_interaction_mode.get() == "agent"
                        and _requests_one_delegation(self.execution_context.native_control_input)):
                    # A single native read-check declaration for a requested review selects only that read.
                    args = {**args, "operations": [operation, {"op": "start", "task": operation["ref"]}]}
            elif set(args) == {"description", "command"}:
                from openkyrozen.agent.engine import _can_start_command_followup

                predecessor = self.execution_context.native_completed_predecessor
                if (predecessor is not None and predecessor[:2] == (native_plan_start.id, native_plan_start.revision)
                        and _can_start_command_followup(self, native_plan_start)):
                    args = {"expected_revision": 0, "operations": [
                        {"op": "add_command_check", "ref": "followup", **args},
                        {"op": "start", "task": "followup"},
                    ]}
            elif (native_plan_start.status == "active"
                    and set(args) == {"expected_revision", "operations"} and type(args["expected_revision"]) is int
                    and args["expected_revision"] == native_plan_start.revision
                    and isinstance(operations := args["operations"], list) and operations
                    and isinstance(operation := operations[0], dict) and set(operation) == {"op", "task"}
                    and operation["op"] == "start" and operation["task"] in native_plan_start.task_ids
                    and all(isinstance(item, dict) and item.get("op") in {"add_read_check", "add_command_check"}
                            for item in operations[1:])):
                # The exact task and revision identify the ordinary plan after an omitted ID.
                interaction = self._interaction_controller.state()
                if (not interaction.get("executing_plan") and not interaction.get("pending_plan")
                        and not any((task.get("checkpoint") or {}).get("strategic_plan")
                                    for task in self.tasks.tasks if task["id"] in native_plan_start.task_ids)):
                    args = {**args, "plan_id": native_plan_start.id}
    if call_id_source == "native" and canonical == "discover_tools" and expected_spec is not None:
        from openkyrozen.agent.prompts import _discover_tools
        executor = expected_spec.executor
        if (expected_spec.source == "builtin" and type(executor) is MethodType
                and executor.__self__ is self and executor.__func__ is _discover_tools.__tool_result_executor__):
            task_id = task_plan_id = None
    def emit_rejected_call():
        self._publish_runtime_event(
            "tool.call", {"tool_name": canonical,
                          "argument_keys": sorted(args)[:64] if isinstance(args, dict) else []},
            call_id=call_id, task_id=task_id,
        )

    plan_delegation = (call_id_source == "native" and task_plan_id is not None
                       and canonical in {"spawn_agents", "wait_subagents"}
                       and expected_spec is not None and expected_spec.source == "builtin")
    inspection = (call_id_source == "native" and canonical in {
        "read_file", "list_dir", "list_tree", "search_files", "find_files",
    } and expected_spec is not None and expected_spec.source == "builtin"
        and expected_spec.capability == "read" and expected_spec.side_effects in {"read", "read_only"})
    selected_task_id = task_id
    if plan_delegation:
        task_id = None

    def local_binding_valid():
        plan = self.tasks.working_plan
        if plan is None or plan.id != task_plan_id:
            return False
        if plan_delegation:
            return plan.status == "active" or (canonical == "wait_subagents" and plan.status == "completed")
        active_task_id = self.tasks.execution_task_id
        if active_task_id == selected_task_id:
            return True
        # An earlier receipt in this response can finish the selected task.
        # Finish only the remaining inspections, without binding their evidence.
        return (inspection and active_task_id is None and plan.status in {"active", "completed"}
                and selected_task_id in plan.task_ids
                and any(task["id"] == selected_task_id and task["status"] == "succeeded"
                        for task in self.tasks.tasks))

    if task_plan_id is not None and not local_binding_valid():
        emit_rejected_call()
        outcome = tool_failure("invalid_arguments", "Error: explicit task selection is no longer active in this Working Plan.")
        return self._make_execution_receipt(
            action=canonical, args=args, authorized=False, started_at=started_at, success=False,
            result=outcome.text, operation_scope=operation_scope, tool_result=outcome,
            call_id=call_id, call_id_source=call_id_source, task_id=task_id,
            task_plan_id=task_plan_id, expected_spec=expected_spec,
        )
    if inspection and task_plan_id is not None and self.tasks.execution_task_id != selected_task_id:
        task_id = None
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
            task_id=task_id, task_plan_id=task_plan_id, expected_spec=expected_spec,
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
                call_id_source=call_id_source, task_id=task_id, task_plan_id=task_plan_id, expected_spec=expected_spec,
                _precommit_plan_validation=canonical == "update_plan",
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
                call_id_source=call_id_source, task_id=task_id, task_plan_id=task_plan_id, expected_spec=expected_spec,
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
            call_id_source=call_id_source, task_id=task_id, task_plan_id=task_plan_id, expected_spec=expected_spec,
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
            task_id=task_id, task_plan_id=task_plan_id, expected_spec=expected_spec,
            _duplicate_successful_operation=True,
        )
    receipt = self._run_tool(canonical, args, return_receipt=True, operation_scope=operation_scope,
                             _typed_arguments=typed_arguments,
                             _expected_spec=spec if typed_arguments is not None else None,
                             _call_id=call_id, _call_id_source=call_id_source, _task_id=task_id,
                             _task_plan_id=task_plan_id,
                             _pre_execution_check=local_binding_valid if task_plan_id is not None else None)
    assert isinstance(receipt, ExecutionReceipt)
    if (receipt.success and receipt.action != "run_cmd"
            and receipt.verification.get("method") != "working_plan_evidence_check"
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
            call_id_source=call.get("call_id_source", call_id_source), task_id=call.get("task_id"),
            task_plan_id=call.get("task_plan_id"), native_plan_start=call.get("native_plan_start"),
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
    current_session = getattr(self, "current_session", None)
    delegated_parent = getattr(current_session, "_delegation_parent", None)
    local_plan = getattr(self.tasks, "working_plan", None)
    local_scope = bool(local_plan and receipt.plan_id == local_plan.id
                       and receipt.user_id == self.tasks.user_id
                       and receipt.workspace_id == self.tasks.workspace_id
                       and receipt.session_id == self.tasks.session_id)
    task_manager = self.tasks if local_scope or not delegated_parent else delegated_parent.tasks
    reconcile_locally = not delegated_parent or local_scope
    active_task = task_manager.active_working_task(receipt.task_id) if receipt.task_id else None
    binding_scope = (getattr(getattr(task_manager, "working_plan", None), "id", None) == receipt.plan_id
                     and receipt.user_id == task_manager.user_id
                     and receipt.workspace_id == task_manager.workspace_id)
    explicit_task_id = receipt.task_id if active_task is not None and binding_scope and (
        reconcile_locally or self._valid_receipt_task_binding(current_session, receipt.task_id)
    ) else None
    receipt_projection = receipt.as_dict()
    task_acceptance = receipt.acceptance
    if receipt.verification.get("status") == "failed":
        task_acceptance = None
    if explicit_task_id and receipt.action == "run_cmd" and task_acceptance:
        command = (receipt.arguments or {}).get("command", "")
        criteria = next((item.get("acceptance", []) for item in task_manager.tasks
                         if item.get("id") == explicit_task_id), [])
        declared = [" ".join(item.split()).casefold() for item in criteria if isinstance(item, str)]
        if not command or len(declared) != 1 or " ".join(command.split()).casefold() != declared[0]:
            task_acceptance = None
        else:
            task_acceptance = receipt_projection["acceptance"]
    should_reconcile = receipt.verification.get("method") != "working_plan_write_precondition" and (
        bool(explicit_task_id) or receipt.call_id_source == "legacy"
        or (allow_legacy_task_matching and receipt.call_id_source == "synthetic")
    )
    matching_tasks = ((active_task,) if local_scope and explicit_task_id and receipt.action in {"read_file", "run_cmd"}
                      else task_manager.tasks)
    legacy_contract = any(
        task.get("status") in {"pending", "running"}
        and task_manager._dependencies_succeeded(task, task_manager.tasks)
        and (task.get("checkpoint") or {}).get("action") == receipt.action
        and isinstance((task.get("checkpoint") or {}).get("args"), str)
        and not task["checkpoint"]["args"].lstrip().startswith("{")
        for task in matching_tasks
    )
    matching_args = (receipt.args if legacy_contract else
                     json.dumps(receipt.arguments, ensure_ascii=False, allow_nan=False,
                                sort_keys=True, separators=(",", ":"))
                     if receipt.arguments is not None else receipt.args)
    verification = receipt.verification
    criterion_status = verification.get("status")
    read_content = None
    whole_read_args = None
    if receipt.action == "read_file":
        data = receipt.tool_result.data if receipt.tool_result is not None else None
        read_content = receipt._archived_read_content
        if (read_content is None and isinstance(data, dict) and isinstance(data.get("content"), str)
                and data.get("truncated") is False):
            read_content = data["content"]
        if read_content is not None:
            arguments = receipt.arguments or {}
            path = arguments.get("path")
            observed = verification.get("observed")
            if (local_scope and explicit_task_id and receipt.authorized and receipt.call_id_source == "native"
                    and receipt.tool_source == "builtin" and verification.get("method") == "read_only_tool"
                    and isinstance(observed, dict) and observed.get("complete_file") is True
                    and isinstance(path, str)
                    and arguments.keys() <= {"path", "start_line", "max_lines", "max_chars"}):
                whole_read_args = json.dumps({"path": path}, ensure_ascii=False, separators=(",", ":"))
        else:
            criterion_status = "unavailable"
    matching_fingerprint = None if legacy_contract else receipt.args_sha256
    native_command = (local_scope and explicit_task_id and receipt.authorized and receipt.call_id_source == "native"
                      and receipt.tool_source == "builtin" and receipt.action == "run_cmd"
                      and verification.get("method") == "command_exit_status")
    if native_command:
        checkpoint = active_task.get("checkpoint") or {}
        declared_criteria = active_task["acceptance"]
        arguments = receipt.arguments or {}
        cwd = arguments.get("cwd")
        # The builtin defaults to the workspace root. Match that ordinary
        # check without altering the original invocation or its receipt.
        if (not legacy_contract and not checkpoint.get("strategic_plan") and isinstance(cwd, str)
                and Path(cwd) in {Path("."), self._get_workspace_root()}):
            default_cwd_args = json.dumps({key: value for key, value in arguments.items() if key != "cwd"},
                                          ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
            if any(isinstance(criterion, dict) and criterion.get("id", "").startswith("command-ac-")
                   and criterion.get("action") == "run_cmd" and criterion.get("args") == default_cwd_args
                   for criterion in declared_criteria):
                matching_args = default_cwd_args
                matching_fingerprint = hashlib.sha256(default_cwd_args.encode("utf-8")).hexdigest()
    failed_command_exit_code = None
    if (native_command and not receipt.success and receipt.failure == "command_failed"
            and not (receipt.arguments or {}).get("background")):
        criteria = active_task["acceptance"]
        checkpoint = active_task.get("checkpoint") or {}
        if (any(isinstance(criterion, dict) and criterion.get("action") == "run_cmd"
                and criterion.get("args") == matching_args for criterion in criteria)
                and checkpoint.get("native_execution_selected") and not checkpoint.get("strategic_plan")
                and (not checkpoint.get("action") or (checkpoint["action"] == "run_cmd"
                    and checkpoint.get("args") == matching_args))):
            failed_command_exit_code = (verification.get("observed") or {}).get("exit_code")
    evidence = {} if not reconcile_locally or receipt.action in TOOLS or not should_reconcile else task_manager.record_evidence(
        task_id=explicit_task_id, action=receipt.action, args=matching_args,
        args_projection=receipt_projection["args"],
        args_fingerprint=matching_fingerprint,
        result=receipt_projection["result"], success=receipt.success,
        acceptance=task_acceptance, receipt_id=receipt.receipt_id,
        verification_status=criterion_status, verification_result=read_content,
        whole_read_args=whole_read_args, failed_command_exit_code=failed_command_exit_code,
    )
    task_id = evidence.get("task_id") if reconcile_locally else explicit_task_id
    effective_acceptance = evidence.get("acceptance") or (
        receipt_projection["acceptance"] if receipt.task_id is None else None
    )
    if task_id and reconcile_locally:
        task = next((item for item in task_manager.tasks if item["id"] == task_id), None)
        checkpoint = (task or {}).get("checkpoint") or {}
        if (task and not checkpoint.get("action") and not checkpoint.get("strategic_plan")
                and "failed_command_exit_code" not in evidence):
            task_manager.update_checkpoint(task_id, {"action": receipt.action, "args": receipt_projection["args"]})
    expected_exit_verified = (failed_command_exit_code is not None and bool(evidence.get("acceptance_ids"))
                              and not evidence.get("unmatched_reason"))
    if task_id and reconcile_locally and (receipt.success or expected_exit_verified) and effective_acceptance:
        task = next((item for item in task_manager.tasks if item["id"] == task_id), None)
        strategic = (task or {}).get("checkpoint", {}).get("strategic_plan", {})
        required = set(strategic.get("acceptance_ids", []))
        covered = {criterion_id for item in (task or {}).get("evidence", [])
                   for criterion_id in item.get("acceptance_ids", [])}
        if not required or required <= covered:
            index = next(index for index, item in enumerate(task_manager.tasks) if item["id"] == task_id)
            task_manager.set_status(index, "succeeded")
    receipt_payload = receipt_projection
    if receipt.task_id is not None and receipt.task_id != task_id:
        receipt_payload["requested_task_id"] = receipt.task_id
    if effective_acceptance and not receipt_payload.get("acceptance"):
        receipt_payload["acceptance"] = effective_acceptance
    if evidence.get("unmatched_reason"):
        receipt_payload["unmatched_reason"] = evidence["unmatched_reason"]
    self.tasks.store.append_event(
        "execution.receipt", {**receipt_payload, "task_id": task_id}, user_id=self.tasks.user_id,
        workspace_id=self.tasks.workspace_id, session_id=receipt.session_id or self.tasks.session_id, task_id=task_id,
    )
    result = {
        "receipt_id": receipt.receipt_id, "operation_id": receipt.operation_id,
        "call_id": receipt.call_id, "call_id_source": receipt.call_id_source,
        "user_id": receipt.user_id, "workspace_id": receipt.workspace_id, "session_id": receipt.session_id,
        "action": receipt.action, "args": receipt_projection["args"], "result": receipt_projection["result"],
        "success": receipt.success, "authorized": receipt.authorized,
        "acceptance": effective_acceptance, "failure": receipt_projection["failure"],
        "tool_error": receipt_projection["tool_error"],
        "task_id": task_id, "requested_task_id": receipt.task_id,
        "plan_id": receipt.plan_id, "side_effects": receipt.side_effects,
        "result_data": receipt_projection["result_data"], "verification": receipt_projection["verification"],
    }
    if evidence.get("unmatched_reason"):
        result["unmatched_reason"] = evidence["unmatched_reason"]
    if expected_exit_verified:
        result["expected_exit_verified"] = True
    self._emit_stream_event({"event": "tool_receipt", "tool_receipt": result})
    self._emit_stream_event({"event": "tasks", "tasks": [
        {"id": item["id"], "description": item["description"], "status": item["status"]}
        for item in self.tasks.tasks
    ], "working_plan": self.tasks.working_plan_snapshot()})
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
