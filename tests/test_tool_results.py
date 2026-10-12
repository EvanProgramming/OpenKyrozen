import dataclasses
import json
import unittest

from openkyrozen.tools import models


ToolError = getattr(models, "ToolError", None)
ToolResult = getattr(models, "ToolResult", None)
render_tool_result = getattr(models, "render_tool_result", None)


class ToolResultContractTests(unittest.TestCase):
    def require_api(self):
        self.assertTrue(all((ToolError, ToolResult, render_tool_result)), "typed tool result API is missing")
        if not all((ToolError, ToolResult, render_tool_result)):
            self.skipTest("typed tool result API is not implemented yet")

    def test_success_depends_on_typed_error_not_display_text(self):
        self.require_api()
        success = ToolResult(data={"contents": "Error: this is a valid file"}, text="Error: this is a valid file")
        failure = ToolResult(data={"partial": True}, text="Completed", error=ToolError("not_found", "Missing file"))
        self.assertTrue(success.success)
        self.assertFalse(failure.success)

    def test_browser_close_forwards_structured_session_id(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from openkyrozen.tools.browser import browser_close
        close = Mock(return_value=ToolResult(text="closed"))
        with patch("openkyrozen.tools.browser.invoke_tool_result", side_effect=lambda fn, arg: fn(arg)):
            self.assertEqual(browser_close(SimpleNamespace(_BROWSER=SimpleNamespace(close=close)),
                                           {"session_id": "browser_123"}), "closed")
        close.assert_called_once_with({"session_id": "browser_123"})

    def test_contracts_are_frozen_and_return_defensive_json_copies(self):
        self.require_api()
        source = {"nested": [1]}
        result = ToolResult(data=source, metadata={"audit": {"tags": ["safe"]}})
        source["nested"].append(2)
        observed = result.data
        observed["nested"].append(3)
        observed_meta = result.metadata
        observed_meta["audit"]["tags"].append("changed")
        self.assertEqual(result.data, {"nested": [1]})
        self.assertEqual(result.metadata, {"audit": {"tags": ["safe"]}})
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.text = "mutated"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.error = ToolError("x", "y")

    def test_invalid_code_message_and_non_json_values_are_rejected(self):
        self.require_api()
        for kwargs in ({"code": "", "message": "bad"}, {"code": "execution_error", "message": "  "}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ToolError(**kwargs)
        with self.assertRaises((TypeError, ValueError)):
            ToolResult(data={"bad": object()})
        with self.assertRaises((TypeError, ValueError)):
            ToolResult(metadata={"bad": float("nan")})
        with self.assertRaises(TypeError):
            ToolResult(text=123)

    def test_renderer_preserves_text_and_renders_structured_fallback(self):
        self.require_api()
        self.assertEqual(render_tool_result(ToolResult(text="Error: valid text", error=None)), "Error: valid text")
        self.assertEqual(render_tool_result(ToolResult(error=ToolError("not_found", "Missing"))), "Error: Missing")
        self.assertEqual(render_tool_result(ToolResult(data={"count": 2})), '{"count": 2}')

class TypedRegistryIntegrationTests(unittest.TestCase):
    def test_registry_returns_typed_outcomes_and_rejects_untyped_values_once(self):
        from openkyrozen.security.capabilities import issue_capability_token
        from openkyrozen.tools import ToolRegistry, ToolSpec

        def make(executor):
            return ToolSpec("sample", executor, description="Sample.",
                            input_schema={"type": "object", "properties": {"args": {"type": "string"}},
                                          "additionalProperties": False}, capability="read", side_effects="read")

        registry = ToolRegistry()
        registry.register_spec(make(lambda _args: ToolResult(text="Error: valid content")))
        token = issue_capability_token("test", {"read"})
        result = registry.invoke_result("sample", {"args":""}, token)
        self.assertTrue(result.success)
        self.assertEqual(registry.invoke("sample", {"args":""}, token), "Error: valid content")

        calls = []
        registry.register_spec(make(lambda _args: calls.append("called") or "looks successful"), replace=True)
        result = registry.invoke_result("sample", {"args":""}, token)
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "contract_error")
        self.assertEqual(calls, ["called"])

    def test_runtime_does_not_classify_successful_error_prefixed_file_content(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.tools import ToolAdapters

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"KYROZEN_DISABLE_VECTOR_INDEX": "1"}):
            root = Path(directory) / "workspace"
            root.mkdir()
            (root / "note.txt").write_text("Error: this is the file's content", encoding="utf-8")
            app = build_application(memory=build_memory(Path(directory) / "state.sqlite3"), tools=ToolAdapters(root))
            try:
                app.runtime._set_workspace_root(root)
                receipt = app.runtime.execute(app.runtime.current_session, "read_file", "note.txt", capabilities={"read"})
                self.assertTrue(receipt.success)
                self.assertIsNone(receipt.failure)
                self.assertEqual(receipt.result, "Error: this is the file's content")
            finally:
                app.close()

    def test_runtime_uses_command_status_even_when_failure_text_has_no_error_marker(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.tools import ToolAdapters
        from openkyrozen.tools.models import CommandResult

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"KYROZEN_DISABLE_VECTOR_INDEX": "1"}):
            app = build_application(memory=build_memory(Path(directory) / "state.sqlite3"), tools=ToolAdapters(directory))
            try:
                with patch.object(app.runtime, "run_command", return_value=CommandResult("permission denied", False, 1)):
                    result, success = app.runtime._run_tool("run_cmd", "false", return_success=True)
                self.assertEqual(result, "permission denied")
                self.assertFalse(success)
            finally:
                app.close()

class TypedObserverTests(unittest.TestCase):
    def test_plugin_status_comes_from_typed_outcome_not_rendered_text(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from openkyrozen.agent.executor import _notify_tool_execute
        recorded = Mock()
        runtime = SimpleNamespace(
            _plugin_runtime_for_surface=lambda: SimpleNamespace(tool_execute=recorded),
            memory_bank=SimpleNamespace(user_id="u", workspace_id="w", session_id="s"),
        )
        with patch("openkyrozen.agent.executor.render_tool_result", return_value="Error: this text is successful content"):
            _notify_tool_execute(runtime, "read_file", "x", ToolResult(text="normal"))
        self.assertTrue(recorded.call_args.kwargs["success"])
        self.assertIsNone(recorded.call_args.kwargs["error"])
        self.assertEqual(recorded.call_args.kwargs["result"], "Error: this text is successful content")

    def test_learning_stats_use_explicit_outcome_boolean(self):
        from types import SimpleNamespace
        from openkyrozen.learning.failures import _track_tool_performance
        events = []
        runtime = SimpleNamespace(
            _tool_stats={},
            _record_learning_event=lambda name, payload: events.append((name, payload)),
        )
        _track_tool_performance(runtime, "read_file", True, 0.2)
        _track_tool_performance(runtime, "write_file", False, 0.3)
        self.assertEqual(runtime._tool_stats["read_file"]["successes"], 1)
        self.assertEqual(runtime._tool_stats["write_file"]["successes"], 0)
        self.assertEqual([event[1]["success"] for event in events], [True, False])


class ReviewFindingRegressions(unittest.TestCase):
    def test_receipt_error_metadata_is_redacted_and_bounded(self):
        from types import SimpleNamespace
        from openkyrozen.agent.executor import _make_execution_receipt
        runtime = SimpleNamespace(
            _operation_action=lambda action: action,
            _operation_args=lambda _action, args: str(args),
            _operation_id=lambda *_args: "operation",
            _fix_safe_text=lambda value, limit=500, **_kwargs: str(value)[:limit],
            _subagent_tool_evidence=lambda *_args, **_kwargs: (None, False),
            _acceptance_for_tool=lambda *_args, **_kwargs: (None, False),
        )
        receipt = _make_execution_receipt(
            runtime, action="read_file", args="x", authorized=True, started_at="now", success=False,
            result="failed", operation_scope="test",
            tool_error=ToolError("execution_error", "failed", metadata={
                "api_key": "secret-value", "request_body": "private request", "details": "x" * 10000,
            }),
        ).as_dict()
        metadata = receipt["tool_error"]["metadata"]
        self.assertEqual(metadata["api_key"], "<redacted>")
        self.assertEqual(metadata["request_body"], "<redacted>")
        self.assertLessEqual(len(json.dumps(metadata)), 4100)
        self.assertNotIn("secret-value", json.dumps(receipt))

    def test_filesystem_and_git_failures_have_specific_codes(self):
        import tempfile
        import subprocess
        from unittest.mock import patch
        from openkyrozen.security.capabilities import issue_capability_token
        from openkyrozen.tools import ToolAdapters
        with tempfile.TemporaryDirectory() as directory:
            tools = ToolAdapters(directory).tool_registry
            token = issue_capability_token("test", {"read", "git"})
            missing = tools.invoke_result("list_dir", {"path":"missing"}, token)
            self.assertEqual(missing.error.code, "not_found")
            with patch("openkyrozen.tools.git.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 60)):
                timed_out = tools.invoke_result("git_clone", {"url":"https://example.invalid/repo"}, token)
            self.assertEqual(timed_out.error.code, "timeout")

    def test_malformed_delegation_json_is_invalid_arguments(self):
        from types import SimpleNamespace
        from openkyrozen.tools.models import invoke_tool_result
        from openkyrozen.agent.delegation_runtime import spawn_agents
        runtime = SimpleNamespace(execution_context=SimpleNamespace(child_run_id=""))
        result = invoke_tool_result(spawn_agents, runtime, "{")
        self.assertEqual(result.error.code, "invalid_arguments")

    def test_graph_tool_propagates_workspace_failure_status(self):
        import tempfile
        from pathlib import Path
        from openkyrozen.security.capabilities import issue_capability_token
        from openkyrozen.tools import ToolAdapters
        from openkyrozen.workspace.graph import ProjectGraph
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = ToolAdapters(root)
            graph = ProjectGraph(root, root / "state", "scope", runner=lambda *_args, **_kwargs: None)
            tools._PROJECT_GRAPH = graph
            result = tools.tool_registry.invoke_result("graph_query", {"question":"question"}, issue_capability_token("test", {"read"}))
            self.assertEqual(result.error.code, "not_found")
