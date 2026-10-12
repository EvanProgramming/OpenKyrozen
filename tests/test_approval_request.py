import unittest

from openkyrozen.security.approvals import ApprovalRequest


class ApprovalRequestTests(unittest.TestCase):
    def test_execute_call_preserves_inherited_surface_capability_bound(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.providers.models import ToolCall
        from openkyrozen.tools import ToolAdapters, ToolSpec
        from openkyrozen.tools.models import ToolResult

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
            "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_CLI_CAPABILITIES": "full",
            "KYROZEN_APPROVAL_MODE": "never",
        }):
            app = build_application(surface="cli", memory=build_memory(Path(directory) / "state.sqlite3"),
                                    tools=ToolAdapters(directory))
            calls = []
            spec = ToolSpec(
                "bound_probe", lambda args: calls.append(args) or ToolResult(text="ran"),
                description="Capability inheritance probe.",
                input_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
                capability="write", side_effects="mutation",
            )
            try:
                app.runtime.tool_registry.register_spec(spec)
                bound = app.runtime._surface_capability_override.set(frozenset({"read"}))
                try:
                    receipt = app.runtime.execute_call(
                        app.runtime.current_session, ToolCall("bound-call", "bound_probe", {}),
                    )
                finally:
                    app.runtime._surface_capability_override.reset(bound)
                self.assertFalse(receipt.success)
                self.assertEqual(receipt.failure, "permission_denied")
                self.assertEqual(calls, [])
            finally:
                app.close()

    def test_ask_permission_mode_keeps_destructive_capability_for_explicit_approval(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.tools import ToolAdapters

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_TUI_CAPABILITIES": "full"}
        ):
            app = build_application(
                surface="tui", memory=build_memory(Path(directory) / "state.sqlite3"),
                tools=ToolAdapters(directory),
            )
            try:
                app.runtime.set_permission_mode("ask")
                self.assertIn("destructive", app.runtime._effective_execution_capabilities())
            finally:
                app.close()

    def test_request_is_frozen_and_snapshots_arguments(self):
        arguments = {"path": "src/a.py", "options": ["--write"]}
        request = ApprovalRequest(
            request_id="request-1", call_id="call-1", tool_name="write_file",
            arguments=arguments, workspace_id="workspace-1", session_id="session-1",
            tool_version="1.0.0", tool_source="builtin", capability="write",
            risk="normal", side_effects="mutation",
        )
        arguments["options"].append("--force")
        self.assertEqual(request.arguments["options"], ["--write"])
        self.assertEqual(len(request.fingerprint), 64)
        with self.assertRaises((AttributeError, TypeError)):
            request.tool_name = "run_cmd"

    def test_fingerprint_binds_call_identity_arguments_and_scope(self):
        values = dict(
            call_id="call-1", tool_name="write_file", arguments={"path": "a", "content": "x"},
            workspace_id="workspace-1", session_id="session-1", tool_version="1.0.0",
            tool_source="builtin", capability="write", risk="normal", side_effects="mutation",
        )
        first = ApprovalRequest(request_id="request-1", **values)
        self.assertEqual(ApprovalRequest(request_id="request-2", **values).fingerprint, first.fingerprint)
        self.assertNotEqual(ApprovalRequest(request_id="request-3", **(values | {"arguments": {"path": "b", "content": "x"}})).fingerprint, first.fingerprint)
        self.assertNotEqual(ApprovalRequest(request_id="request-4", **(values | {"workspace_id": "workspace-2"})).fingerprint, first.fingerprint)
        self.assertNotEqual(ApprovalRequest(request_id="request-5", **(values | {"call_id": "call-2"})).fingerprint, first.fingerprint)

    def test_non_json_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            ApprovalRequest(
                request_id="request-1", call_id="call-1", tool_name="write_file",
                arguments={"bad": object()}, workspace_id="workspace-1", session_id="session-1",
                tool_version="1.0.0", tool_source="builtin", capability="write",
                risk="normal", side_effects="mutation",
            )

    def test_legacy_approval_callbacks_are_adapted_once_and_must_return_true(self):
        from types import SimpleNamespace
        from openkyrozen.security.permissions import _call_approval_callback
        request = ApprovalRequest(
            call_id="call-1", tool_name="write_file", arguments={"path": "a"},
            workspace_id="w", session_id="s", tool_version="1", tool_source="builtin",
            capability="write", risk="normal", side_effects="mutation",
        )
        calls = []
        callback = lambda action, args: calls.append((action, args)) or 1
        self.assertFalse(_call_approval_callback(SimpleNamespace(), callback, "write_file", {"path": "a"}, request=request))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "write_file")
        self.assertTrue(_call_approval_callback(SimpleNamespace(), lambda value: value is request,
                                                 "write_file", {}, request=request))
        self.assertFalse(_call_approval_callback(SimpleNamespace(), lambda _value: (_ for _ in ()).throw(RuntimeError()),
                                                  "write_file", {}, request=request))

    def test_optional_two_argument_approval_callback_uses_legacy_arguments(self):
        from types import SimpleNamespace
        from openkyrozen.security.permissions import _call_approval_callback
        request = ApprovalRequest(
            call_id="call-legacy", tool_name="write_file", arguments={"path": "a"},
            workspace_id="w", session_id="s", tool_version="1", tool_source="builtin",
            capability="write", risk="normal", side_effects="mutation",
        )
        seen = []

        def legacy(action, details=None):
            seen.append((action, details))
            return True

        self.assertTrue(_call_approval_callback(
            SimpleNamespace(), legacy, "write_file", '{"path": "a"}', request=request,
        ))
        self.assertEqual(seen, [("write_file", '{"path": "a"}')])

    def test_approval_display_redacts_sensitive_keys_and_inline_secrets(self):
        from openkyrozen.security.permissions import _redacted_approval_value
        self.assertEqual(_redacted_approval_value({
            "api_key": "private", "nested": {"authorization": "Bearer private"},
            "note": "token=private",
            "command": "curl -H 'Authorization: Bearer abc123' https://example.test",
        }), {
            "api_key": "<redacted>", "nested": {"authorization": "<redacted>"},
            "note": "token=<redacted>",
            "command": "curl -H 'Authorization: Bearer <redacted>' https://example.test",
        })

    def test_runtime_requested_capabilities_cannot_widen_surface_token(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.tools import ToolAdapters, ToolSpec
        from openkyrozen.tools.models import ToolResult

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_WEB_CAPABILITIES": "workspace"}
        ):
            adapters = ToolAdapters(directory)
            app = build_application(surface="web", memory=build_memory(Path(directory) / "state.sqlite3"), tools=adapters)
            calls = []
            try:
                app.runtime.tool_registry.register_spec(ToolSpec(
                    "authority_probe", lambda _args: calls.append("ran") or ToolResult(text="ran"),
                    description="Test authority.", input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    capability="dynamic", side_effects="none",
                ))
                receipt = app.runtime.execute(
                    app.runtime.current_session, "authority_probe", {}, capabilities={"dynamic"},
                )
                self.assertFalse(receipt.success)
                self.assertEqual(calls, [])
            finally:
                app.close()

    def test_empty_execute_call_capabilities_deny_read_tool(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.providers.models import ToolCall
        from openkyrozen.tools import ToolAdapters, ToolSpec
        from openkyrozen.tools.models import ToolResult

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_WEB_CAPABILITIES": "full"}
        ):
            app = build_application(
                surface="web", memory=build_memory(Path(directory) / "state.sqlite3"),
                tools=ToolAdapters(directory),
            )
            calls = []
            try:
                app.runtime.tool_registry.register_spec(ToolSpec(
                    "empty_authority_probe", lambda _args: calls.append("ran") or ToolResult(text="ran"),
                    description="Test deny-all requested capabilities.",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    capability="read", side_effects="none",
                ))
                receipt = app.runtime.execute_call(
                    app.runtime.current_session,
                    ToolCall("empty-authority", "empty_authority_probe", {}),
                    capabilities=set(),
                )
                self.assertFalse(receipt.success)
                self.assertEqual(calls, [])
            finally:
                app.close()

    def test_explicit_mcp_surface_bound_preserves_configured_dangerous_opt_in(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.tools import ToolAdapters

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_WEB_CAPABILITIES": "workspace"}
        ):
            app = build_application(
                surface="web", memory=build_memory(Path(directory) / "state.sqlite3"),
                tools=ToolAdapters(directory),
            )
            try:
                from openkyrozen.security.capabilities import issue_capability_token
                app.runtime._execution_capability_token = issue_capability_token(
                    "test", {"read", "write", "shell", "network", "git", "browser", "destructive", "dynamic"},
                )
                self.assertNotIn("destructive", app.runtime._effective_execution_capabilities())
                self.assertEqual(app.runtime._effective_execution_capabilities(set()), frozenset())
                surface = app.runtime._surface_capability_override.set(
                    frozenset({"read", "write", "shell", "network", "git", "browser", "destructive", "dynamic"})
                )
                try:
                    self.assertIn("destructive", app.runtime._effective_execution_capabilities("full"))
                finally:
                    app.runtime._surface_capability_override.reset(surface)
                empty_surface = app.runtime._surface_capability_override.set(frozenset())
                try:
                    self.assertEqual(app.runtime._effective_execution_capabilities("full"), frozenset())
                finally:
                    app.runtime._surface_capability_override.reset(empty_surface)
                from openkyrozen.tools.models import ToolResult
                spec = app.runtime.tool_registry.get_spec("git_reset")
                calls = []
                app.runtime.tool_registry.register_spec(
                    spec.replace(executor=lambda args: calls.append(args) or ToolResult(text="executed")),
                    replace=True,
                )
                requested = {"read", "write", "shell", "network", "git", "browser", "destructive", "dynamic"}
                denied = app.runtime.execute(app.runtime.current_session, "git_reset", "--hard HEAD", capabilities=requested)
                self.assertFalse(denied.success)
                self.assertEqual(calls, [])
                allowed = app.runtime.execute(
                    app.runtime.current_session, "git_reset", "--hard HEAD", capabilities=requested,
                    _surface_capability_bound=requested,
                )
                self.assertTrue(allowed.success, allowed.result)
                self.assertEqual(calls, ["--hard HEAD"])
            finally:
                app.close()

    def test_execute_call_approval_is_bound_to_the_structured_operation_and_spec(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.providers.models import ToolCall
        from openkyrozen.tools import ToolAdapters, ToolSpec
        from openkyrozen.tools.models import ToolResult

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_CLI_CAPABILITIES": "full"}
        ):
            app = build_application(surface="cli", memory=build_memory(Path(directory) / "state.sqlite3"), tools=ToolAdapters(directory))
            calls = []
            spec = ToolSpec(
                "approval_probe", lambda value: calls.append(value) or ToolResult(text="ran"),
                description="Test approval binding.",
                input_schema={"type": "object", "properties": {"args": {"type": "string"}},
                              "required": ["args"], "additionalProperties": False},
                capability="write", side_effects="mutation", risk="high", legacy_fields=("args",),
            )
            try:
                app.runtime.tool_registry.register_spec(spec)
                long_value = "x" * 900
                seen = []
                call = ToolCall("call-1", "approval_probe", {"args": long_value})
                with patch.object(ToolSpec, "legacy_arguments", side_effect=AssertionError("legacy encoding used")):
                    receipt = app.runtime.execute_call(
                        app.runtime.current_session, call, approve=lambda request: seen.append(request) or True,
                    )
                self.assertTrue(receipt.success)
                self.assertEqual(seen[0].arguments, {"args": long_value})
                self.assertEqual(calls, [{"args": long_value}])

                calls.clear()
                def replace_during_approval(_request):
                    app.runtime.tool_registry.register_spec(spec.replace(executor=lambda value: calls.append("replacement") or ToolResult(text="ran")), replace=True)
                    return True
                denied = app.runtime.execute_call(
                    app.runtime.current_session, ToolCall("call-2", "approval_probe", {"args": "x"}),
                    approve=replace_during_approval,
                )
                self.assertFalse(denied.success)
                self.assertEqual(calls, [])

                mode_token = app.runtime._active_interaction_mode.set("agent")
                try:
                    def narrow_mode(_request):
                        app.runtime._active_interaction_mode.set("plan")
                        return True
                    narrowed = app.runtime.execute_call(
                        app.runtime.current_session, ToolCall("call-3", "approval_probe", {"args": "x"}),
                        approve=narrow_mode,
                    )
                    self.assertFalse(narrowed.success)
                    self.assertEqual(calls, [])
                finally:
                    app.runtime._active_interaction_mode.reset(mode_token)
            finally:
                app.close()

    def test_execute_call_rejects_arguments_mutated_while_approval_waits(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.providers.models import ToolCall
        from openkyrozen.tools import ToolAdapters, ToolSpec
        from openkyrozen.tools.models import ToolResult

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_CLI_CAPABILITIES": "full"}
        ):
            app = build_application(surface="cli", memory=build_memory(Path(directory) / "state.sqlite3"), tools=ToolAdapters(directory))
            calls = []
            spec = ToolSpec(
                "approval_mutation_probe", lambda value: calls.append(value) or ToolResult(text="ran"),
                description="Test argument mutation.",
                input_schema={"type": "object", "properties": {"args": {"type": "string"}},
                              "required": ["args"], "additionalProperties": False},
                capability="write", side_effects="mutation", risk="high", legacy_fields=("args",),
            )
            try:
                app.runtime.tool_registry.register_spec(spec)
                arguments = {"args": "approved"}
                call = ToolCall("call-3", "approval_mutation_probe", arguments)
                def mutate(_request):
                    call.arguments["args"] = "different"
                    return True
                receipt = app.runtime.execute_call(app.runtime.current_session, call, approve=mutate)
                self.assertFalse(receipt.success)
                self.assertEqual(calls, [])
            finally:
                app.close()

    def test_structured_file_arguments_preserve_pipe_characters(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.providers.models import ToolCall
        from openkyrozen.tools import ToolAdapters

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_CLI_CAPABILITIES": "full"}
        ):
            root = Path(directory) / "workspace"
            root.mkdir()
            app = build_application(surface="cli", memory=build_memory(Path(directory) / "state.sqlite3"), tools=ToolAdapters(root))
            try:
                app.runtime._set_workspace_root(root)
                receipt = app.runtime.execute_call(
                    app.runtime.current_session,
                    ToolCall("call-pipe", "write_file", {"path": "part|outside.txt", "content": "x"}),
                    approve=lambda request: True,
                )
                self.assertTrue(receipt.success, receipt.result)
                self.assertEqual((root / "part|outside.txt").read_text(encoding="utf-8"), "x")
            finally:
                app.close()

    def test_legacy_runtime_entry_rejects_registry_replacement_during_approval(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.tools import ToolAdapters
        from openkyrozen.tools.models import ToolResult

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_CLI_CAPABILITIES": "full"}
        ):
            app = build_application(
                surface="cli", memory=build_memory(Path(directory) / "state.sqlite3"),
                tools=ToolAdapters(directory),
            )
            executed = []
            try:
                original = app.runtime.tool_registry.get_spec("git_reset")
                replacement = original.replace(executor=lambda _args: executed.append("replacement") or ToolResult(text="ran"))

                def replace_while_approving(_action, _args):
                    app.runtime.tool_registry.register_spec(replacement, replace=True)
                    return True

                approval = app.runtime._approval_callback.set(replace_while_approving)
                try:
                    receipt = app.runtime._execute_turn_action(
                        "git_reset", "--hard HEAD", operation_scope="approval-replacement",
                        successful_operations=set(),
                    )
                finally:
                    app.runtime._approval_callback.reset(approval)
                self.assertFalse(receipt.success)
                self.assertEqual(executed, [])
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
