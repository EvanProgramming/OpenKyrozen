from contextlib import ExitStack, closing
import contextvars
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openkyrozen.agent.engine import AgentEngine
from openkyrozen.agent.executor import _execute_tool_batch
from openkyrozen.agent.turn import TurnContext
from openkyrozen.providers.models import (
    FinishReason, ModelResponse, ProviderCapabilities, ToolCall, ToolReply,
    provider_continuation,
)
from openkyrozen.tools.models import ToolResult
from openkyrozen.providers import ProviderConfig
from openkyrozen.providers.base import LLMProvider
from openkyrozen.providers.models import validate_replied_tool_definitions, validate_tool_history


class AgentEngineTests(unittest.TestCase):
    def test_turn_learning_is_queued_only_after_completion(self):
        events = []
        provider = SimpleNamespace(get_capabilities=lambda _model: ProviderCapabilities(native_tools=True))
        runtime = SimpleNamespace(
            execution_context=SimpleNamespace(provider=provider), llm_provider=None, DEEPSEEK_MODEL="model",
        )
        runtime._prepare_turn = lambda turn: setattr(turn, "interaction_mode", "agent")
        runtime._complete_turn = lambda _turn: events.append("complete") or "finished"
        runtime._schedule_post_turn_learning = lambda task: events.append(("queued", task))
        engine = AgentEngine(runtime)
        engine._run_native = lambda _turn: events.append("work")

        result = engine.run(TurnContext("Use Orion"))

        self.assertEqual(result, "finished")
        self.assertEqual(events, ["work", "complete", ("queued", "Use Orion")])

    def test_delegated_child_turn_does_not_queue_parent_learning(self):
        scheduled = Mock()
        runtime = SimpleNamespace(
            execution_context=SimpleNamespace(child_run_id="child"),
            _schedule_post_turn_learning=scheduled,
        )
        self.assertEqual(AgentEngine(runtime)._finish_turn(TurnContext("inspect"), "done"), "done")
        scheduled.assert_not_called()

    def test_learning_queue_failure_does_not_fail_completed_turn(self):
        provider = SimpleNamespace(get_capabilities=lambda _model: ProviderCapabilities(native_tools=True))
        events = []
        runtime = SimpleNamespace(
            execution_context=SimpleNamespace(provider=provider), llm_provider=None, DEEPSEEK_MODEL="model",
            _record_learning_event=lambda *args: events.append(args),
        )
        runtime._prepare_turn = lambda turn: setattr(turn, "interaction_mode", "agent")
        runtime._complete_turn = lambda _turn: "finished"
        runtime._schedule_post_turn_learning = Mock(side_effect=RuntimeError("closed"))
        engine = AgentEngine(runtime)
        engine._run_native = lambda _turn: None

        self.assertEqual(engine.run(TurnContext("Use Orion")), "finished")
        self.assertEqual(events, [("learning.dispatch_failed", {
            "trigger": "post-turn", "reason": "RuntimeError",
        })])

    def test_turn_context_keeps_legacy_positional_inspection_argument(self):
        turn = TurnContext("Plan the change", False, None, None, True)
        self.assertTrue(turn.inspection_complete)
        self.assertEqual(turn.memory_packet, "")

    def bind_tool_batch(self, runtime, specs=None):
        runtime.tool_registry = SimpleNamespace(specs=specs or {})
        runtime._operation_action = lambda name: name
        runtime._execute_tool_batch = lambda calls, **kwargs: _execute_tool_batch(runtime, calls, **kwargs)

    def complex_native_runtime(self):
        provider = SimpleNamespace(get_capabilities=lambda _model: ProviderCapabilities(native_tools=True))
        runtime = SimpleNamespace(
            execution_context=SimpleNamespace(provider=provider, discovered_tools=frozenset()),
            llm_provider=None, DEEPSEEK_MODEL="model", MAX_STEPS_PER_TURN=2,
            _record_foreground_model=contextvars.ContextVar("record", default=False),
            _conversation_turn_id=contextvars.ContextVar("turn", default=None),
            _last_response_is_native=contextvars.ContextVar("native", default=False),
            _last_prompt_tokens=0, _last_completion_tokens=0,
            tasks=SimpleNamespace(working_plan=None),
        )
        runtime._prepare_turn = lambda turn: (
            setattr(turn, "interaction_mode", "agent"), setattr(turn, "complexity", "complex"),
            setattr(turn, "learning_run", {"run_id": "run"}), setattr(turn, "learned_context", ""),
        )
        runtime._prepare_context_for_call = lambda *_args: None
        runtime._build_messages = lambda *args, **kwargs: []
        runtime._permitted_tool_names = lambda: {"read_file", "update_plan"}
        runtime._prompt_profile = lambda: "compact"
        runtime._get_model_response = Mock(return_value=ModelResponse(
            tool_calls=(ToolCall("read-1", "read_file", {"path": "note.txt"}),),
            finish_reason=FinishReason.TOOL_REQUEST,
        ))
        runtime._execute_tool_batch = Mock()
        runtime._deterministic_tool_summary = lambda records: "no tools"
        runtime._complete_turn = lambda turn: turn.protocol_error_message or turn.final_answer
        return runtime

    def test_complex_native_turn_cannot_run_tools_before_a_working_plan(self):
        runtime = self.complex_native_runtime()

        result = AgentEngine(runtime).run(TurnContext("Complex task"))

        runtime._execute_tool_batch.assert_not_called()
        self.assertIn("plan", result.lower())
        self.assertEqual(runtime._get_model_response.call_args.kwargs["tool_names"], {"update_plan"})

    def test_complex_native_turn_blocks_when_planning_is_not_permitted(self):
        runtime = self.complex_native_runtime()
        runtime._permitted_tool_names = lambda: {"read_file"}
        result = AgentEngine(runtime).run(TurnContext("Complex task"))
        self.assertIn("planning is not permitted", result)
        runtime._get_model_response.assert_not_called()
        runtime._execute_tool_batch.assert_not_called()

    def test_complex_native_turn_rejects_plan_and_other_tool_in_same_batch(self):
        runtime = self.complex_native_runtime()
        runtime._get_model_response.return_value = ModelResponse(tool_calls=(
            ToolCall("plan-1", "update_plan", {"complexity": "complex", "tasks": []}),
            ToolCall("read-1", "read_file", {"path": "note.txt"}),
        ), finish_reason=FinishReason.TOOL_REQUEST)
        result = AgentEngine(runtime).run(TurnContext("Complex task"))
        self.assertIn("plan", result.lower())
        runtime._execute_tool_batch.assert_not_called()

    def test_complex_native_turn_cannot_continue_after_failed_plan_update(self):
        runtime = self.complex_native_runtime()
        runtime._get_model_response.side_effect = [
            ModelResponse(tool_calls=(ToolCall("plan-1", "update_plan", {}),),
                          finish_reason=FinishReason.TOOL_REQUEST),
            ModelResponse(tool_calls=(ToolCall("read-1", "read_file", {"path": "note.txt"}),),
                          finish_reason=FinishReason.TOOL_REQUEST),
        ]
        result = AgentEngine(runtime).run(TurnContext("Complex task"))
        self.assertIn("plan", result.lower())
        self.assertEqual(runtime._execute_tool_batch.call_count, 1)
        self.assertEqual(runtime._execute_tool_batch.call_args.args[0][0]["action"], "update_plan")

    def test_complex_native_turn_retries_final_answer_without_plan_then_stops(self):
        runtime = self.complex_native_runtime()
        runtime._get_model_response.return_value = ModelResponse(text="Done", finish_reason=FinishReason.FINAL)
        result = AgentEngine(runtime).run(TurnContext("Complex task"))
        self.assertIn("not established", result)
        self.assertEqual(runtime._get_model_response.call_count, 3)
        runtime._execute_tool_batch.assert_not_called()

    def test_complex_native_turn_runs_other_tools_after_plan_succeeds(self):
        runtime = self.complex_native_runtime()
        runtime._get_model_response.side_effect = [
            ModelResponse(tool_calls=(ToolCall("plan-1", "update_plan", {"complexity": "complex", "tasks": []}),),
                          finish_reason=FinishReason.TOOL_REQUEST),
            ModelResponse(tool_calls=(ToolCall("read-1", "read_file", {"path": "note.txt"}),),
                          finish_reason=FinishReason.TOOL_REQUEST),
            ModelResponse(text="Done", finish_reason=FinishReason.FINAL),
        ]
        def execute(calls, **kwargs):
            if calls[0]["action"] == "update_plan":
                runtime.tasks.working_plan = SimpleNamespace(id="plan-1", status="active")
        runtime._execute_tool_batch.side_effect = execute
        self.assertEqual(AgentEngine(runtime).run(TurnContext("Complex task")), "Done")
        self.assertEqual([call.args[0][0]["action"] for call in runtime._execute_tool_batch.call_args_list],
                         ["update_plan", "read_file"])
        self.assertEqual(runtime._get_model_response.call_args_list[0].kwargs["tool_names"], {"update_plan"})
        self.assertIn("read_file", runtime._get_model_response.call_args_list[1].kwargs["tool_names"])

    def test_complex_native_turn_can_revise_plan_in_compact_mode(self):
        runtime = self.complex_native_runtime()
        runtime._permitted_tool_names = lambda: {"read_file", "discover_tools", "update_plan"}
        runtime._get_model_response.side_effect = [
            ModelResponse(tool_calls=(ToolCall("plan-1", "update_plan", {"complexity": "complex", "tasks": []}),),
                          finish_reason=FinishReason.TOOL_REQUEST),
            ModelResponse(tool_calls=(ToolCall("plan-2", "update_plan", {"expected_revision": 1, "operations": []}),),
                          finish_reason=FinishReason.TOOL_REQUEST),
            ModelResponse(text="Done", finish_reason=FinishReason.FINAL),
        ]
        def execute(calls, **kwargs):
            runtime.tasks.working_plan = SimpleNamespace(id="plan-1", status="active")
        runtime._execute_tool_batch.side_effect = execute
        self.assertEqual(AgentEngine(runtime).run(TurnContext("Complex task")), "Done")
        self.assertIn("update_plan", runtime._get_model_response.call_args_list[1].kwargs["tool_names"])

    def test_complex_native_turn_does_not_retain_revoked_plan_permission(self):
        runtime = self.complex_native_runtime()
        allowed = {"read_file", "discover_tools", "update_plan"}
        runtime._permitted_tool_names = lambda: allowed.copy()
        runtime._get_model_response.side_effect = [
            ModelResponse(tool_calls=(ToolCall("plan-1", "update_plan", {}),), finish_reason=FinishReason.TOOL_REQUEST),
            ModelResponse(tool_calls=(ToolCall("plan-2", "update_plan", {}),), finish_reason=FinishReason.TOOL_REQUEST),
        ]
        def execute(calls, **kwargs):
            runtime.tasks.working_plan = SimpleNamespace(id="plan-1", status="active")
            allowed.remove("update_plan")
        runtime._execute_tool_batch.side_effect = execute
        result = AgentEngine(runtime).run(TurnContext("Complex task"))
        self.assertIn("unknown tool", result)
        self.assertEqual(runtime._execute_tool_batch.call_count, 1)

    def test_complex_native_turn_can_finish_after_its_plan_completes(self):
        runtime = self.complex_native_runtime()
        runtime.tasks.working_plan = SimpleNamespace(id="plan-1", status="active")
        runtime._get_model_response.side_effect = [
            ModelResponse(tool_calls=(ToolCall("read-1", "read_file", {"path": "note.txt"}),),
                          finish_reason=FinishReason.TOOL_REQUEST),
            ModelResponse(text="Done", finish_reason=FinishReason.FINAL),
        ]
        runtime._execute_tool_batch.side_effect = lambda *args, **kwargs: setattr(
            runtime.tasks.working_plan, "status", "completed")
        self.assertEqual(AgentEngine(runtime).run(TurnContext("Complex task")), "Done")

    def test_compact_native_artifact_reader_respects_permission(self):
        permitted = {"discover_tools", "read_tool_artifact"}
        runtime = SimpleNamespace(
            execution_context=SimpleNamespace(discovered_tools=frozenset()),
            _prompt_profile=lambda: "compact", _permitted_tool_names=lambda: permitted,
        )
        engine = AgentEngine(runtime)
        self.assertIn("read_tool_artifact", engine._native_tool_names())
        permitted.remove("read_tool_artifact")
        self.assertNotIn("read_tool_artifact", engine._native_tool_names())

    def test_compact_native_falls_back_to_permitted_inventory_without_discovery_access(self):
        runtime = SimpleNamespace(
            execution_context=SimpleNamespace(discovered_tools=frozenset()),
            _prompt_profile=lambda: "compact",
            _permitted_tool_names=lambda: {"github_read", "browser_open"},
        )

        self.assertEqual(AgentEngine(runtime)._native_tool_names(), {"github_read", "browser_open"})

    def test_native_history_keeps_past_schema_but_rejects_revoked_new_calls(self):
        provider = SimpleNamespace(name="native", _continuation_id="native-session")
        provider.get_capabilities = lambda model: ProviderCapabilities(native_tools=True)
        responses = [
            ModelResponse(
                tool_calls=(ToolCall("write-1", "write_file", {"path": "note.txt", "content": "saved"}),),
                finish_reason=FinishReason.TOOL_REQUEST,
                continuation=provider_continuation(provider, "model-1", "fake", {"id": "response-1"}),
            ),
            ModelResponse(
                tool_calls=(ToolCall("write-2", "write_file", {"path": "other.txt", "content": "blocked"}),),
                finish_reason=FinishReason.TOOL_REQUEST,
                continuation=provider_continuation(provider, "model-1", "fake", {"id": "response-2"}),
            ),
        ]
        runtime = SimpleNamespace(
            execution_context=SimpleNamespace(provider=provider, discovered_tools=frozenset()), llm_provider=None,
            DEEPSEEK_MODEL="model-1", MAX_STEPS_PER_TURN=3,
            _record_foreground_model=contextvars.ContextVar("record", default=False),
            _conversation_turn_id=contextvars.ContextVar("turn_id", default=None),
            _last_response_is_native=contextvars.ContextVar("native", default=False),
            _last_prompt_tokens=0, _last_completion_tokens=0,
        )
        runtime._prepare_turn = lambda turn: (
            setattr(turn, "interaction_mode", "agent"), setattr(turn, "complexity", "simple"),
            setattr(turn, "learning_run", {"run_id": "run-1"}),
            setattr(turn, "learned_context", ""),
        )
        runtime._prepare_context_for_call = lambda *_args: None
        runtime._build_messages = lambda *args, **kwargs: [{"role": "user", "content": "Save this note"}]
        current_tools = {"write_file"}
        runtime._permitted_tool_names = lambda: current_tools.copy()
        runtime._prompt_profile = lambda: "classic"
        runtime._get_model_response = Mock(side_effect=responses)

        def execute(action, arguments, **kwargs):
            current_tools.clear()
            return SimpleNamespace(
                action=action, tool_result=ToolResult(text="saved"), result_data={}, result="saved",
            )

        runtime._execute_turn_action = Mock(side_effect=execute)
        self.bind_tool_batch(runtime)
        runtime._record_turn_receipt = Mock(return_value={"success": True})
        runtime._tool_result_for_prompt = Mock(return_value="saved")
        runtime._render_tool_result = Mock()
        runtime._update_tasks_panel = Mock()
        runtime._safe_fstring = str
        runtime._deterministic_tool_summary = lambda records: "Saved note."
        runtime.tasks = SimpleNamespace(tasks=[])
        runtime._complete_turn = lambda turn: turn.protocol_error_message

        result = AgentEngine(runtime).run(TurnContext("Save this note"))

        self.assertEqual(result, "The provider returned an unknown tool call; no tool was run.")
        self.assertEqual(runtime._execute_turn_action.call_count, 1)
        second_request = runtime._get_model_response.call_args_list[1].kwargs
        self.assertEqual(second_request["tool_names"], set())
        self.assertEqual(second_request["history_tool_names"], {"write_file"})

    def test_compact_native_discovery_adds_only_permitted_schemas_for_next_request(self):
        provider = SimpleNamespace(name="native", _continuation_id="native-session")
        provider.get_capabilities = lambda model: ProviderCapabilities(native_tools=True)
        responses = [
            ModelResponse(
                tool_calls=(ToolCall("discover-1", "discover_tools", {"names": ["git_log"]}),),
                finish_reason=FinishReason.TOOL_REQUEST,
                continuation=provider_continuation(provider, "model-1", "fake", {"id": "response-1"}),
            ),
            ModelResponse(
                tool_calls=(ToolCall("git-1", "git_log", {"options": ["-1"]}),),
                finish_reason=FinishReason.TOOL_REQUEST,
                continuation=provider_continuation(provider, "model-1", "fake", {"id": "response-2"}),
            ),
            ModelResponse(text="History checked.", finish_reason=FinishReason.FINAL),
        ]
        runtime = SimpleNamespace(
            execution_context=SimpleNamespace(provider=provider, discovered_tools=set()), llm_provider=None,
            DEEPSEEK_MODEL="model-1", MAX_STEPS_PER_TURN=3,
            _record_foreground_model=contextvars.ContextVar("record", default=False),
            _conversation_turn_id=contextvars.ContextVar("turn_id", default=None),
            _last_response_is_native=contextvars.ContextVar("native", default=False),
            _last_prompt_tokens=0, _last_completion_tokens=0,
        )
        runtime._prepare_turn = lambda turn: (
            setattr(turn, "interaction_mode", "agent"), setattr(turn, "complexity", "simple"),
            setattr(turn, "learning_run", {"run_id": "run-1"}),
            setattr(turn, "learned_context", ""),
        )
        runtime._prepare_context_for_call = lambda *_args: None
        runtime._build_messages = lambda *args, **kwargs: [{"role": "user", "content": "Review Git history"}]
        permitted = {"read_file", "list_dir", "discover_tools", "git_log", "github_read", "browser_open", "graph_query"}
        runtime._permitted_tool_names = lambda: permitted
        runtime._prompt_profile = lambda: "compact"
        runtime._get_model_response = Mock(side_effect=responses)

        def execute(action, arguments, **kwargs):
            if action == "discover_tools":
                runtime.execution_context.discovered_tools.update(arguments["names"])
            return SimpleNamespace(
                action=action, tool_result=ToolResult(text="discovered"), result_data={}, result="discovered",
            )

        runtime._execute_turn_action = Mock(side_effect=execute)
        self.bind_tool_batch(runtime)
        runtime._record_turn_receipt = Mock(return_value={"success": True})
        runtime._tool_result_for_prompt = Mock(return_value="discovered")
        runtime._render_tool_result = Mock()
        runtime._update_tasks_panel = Mock()
        runtime._safe_fstring = str
        runtime._deterministic_tool_summary = lambda records: "tool summary"
        runtime.tasks = SimpleNamespace(tasks=[])
        runtime.current_session = SimpleNamespace()
        runtime.memory_bank = SimpleNamespace(
            user_id="user", workspace_id="workspace", session_id="session",
            store=SimpleNamespace(artifacts=SimpleNamespace(get_or_create=lambda *args, **kwargs: "artifact-test")),
        )
        runtime._complete_turn = Mock(side_effect=lambda turn: turn.final_answer)

        result = AgentEngine(runtime).run(TurnContext("Review Git history"))

        self.assertEqual(result, "History checked.")
        calls = runtime._get_model_response.call_args_list
        self.assertEqual(calls[0].kwargs["tool_names"], {"read_file", "list_dir", "discover_tools"})
        self.assertEqual(calls[1].kwargs["tool_names"], {"read_file", "list_dir", "discover_tools", "git_log"})
        self.assertNotIn("github_read", calls[1].kwargs["tool_names"])
        self.assertNotIn("browser_open", calls[1].kwargs["tool_names"])
        self.assertNotIn("graph_query", calls[1].kwargs["tool_names"])

    def test_native_loop_stops_at_engine_step_limit(self):
        provider = SimpleNamespace(name="native", _continuation_id="native-session")
        provider.get_capabilities = lambda model: ProviderCapabilities(native_tools=True)
        response = ModelResponse(
            tool_calls=(ToolCall("call-1", "read_file", {"path": "README.md"}),),
            finish_reason=FinishReason.TOOL_REQUEST,
            continuation=provider_continuation(provider, "model-1", "fake", {"id": "response-1"}),
        )
        runtime = SimpleNamespace(
            execution_context=SimpleNamespace(provider=provider, max_steps=0), llm_provider=None, DEEPSEEK_MODEL="model-1",
            MAX_STEPS_PER_TURN=2, _record_foreground_model=contextvars.ContextVar("record", default=False),
            _conversation_turn_id=contextvars.ContextVar("turn_id", default=None),
            _last_response_is_native=contextvars.ContextVar("native", default=False),
            _last_prompt_tokens=0, _last_completion_tokens=0,
        )
        runtime._prepare_turn = lambda turn: (
            setattr(turn, "interaction_mode", "agent"), setattr(turn, "complexity", "simple"),
            setattr(turn, "learning_run", {"run_id": "run-1"}),
            setattr(turn, "learned_context", ""),
        )
        runtime._prepare_context_for_call = lambda *_args: None
        runtime._build_messages = lambda *args, **kwargs: [{"role": "user", "content": "Read README"}]
        runtime._permitted_tool_names = lambda: {"read_file"}
        runtime._prompt_profile = lambda: "classic"
        runtime._get_model_response = Mock(return_value=response)
        runtime._execute_turn_action = Mock(side_effect=AssertionError("step limit was bypassed"))
        runtime._deterministic_tool_summary = lambda records: "No tools were run."
        runtime._complete_turn = lambda turn: turn.protocol_error_message

        result = AgentEngine(runtime).run(TurnContext("Read README"))

        self.assertEqual(result, "The tool-call step limit was reached; remaining work was not run.")
        runtime._execute_turn_action.assert_not_called()

    def test_native_loop_rejects_unknown_tool_without_execution(self):
        provider = SimpleNamespace(name="native", _continuation_id="native-session")
        provider.get_capabilities = lambda model: ProviderCapabilities(native_tools=True)
        response = ModelResponse(
            tool_calls=(ToolCall("call-1", "missing_tool", {}),),
            finish_reason=FinishReason.TOOL_REQUEST,
            continuation=provider_continuation(provider, "model-1", "fake", {"id": "response-1"}),
        )
        runtime = SimpleNamespace(
            execution_context=SimpleNamespace(provider=provider), llm_provider=None, DEEPSEEK_MODEL="model-1",
            MAX_STEPS_PER_TURN=2, _record_foreground_model=contextvars.ContextVar("record", default=False),
            _conversation_turn_id=contextvars.ContextVar("turn_id", default=None),
            _last_response_is_native=contextvars.ContextVar("native", default=False),
            _last_prompt_tokens=0, _last_completion_tokens=0,
        )
        runtime._prepare_turn = lambda turn: (
            setattr(turn, "interaction_mode", "agent"), setattr(turn, "complexity", "simple"),
            setattr(turn, "learning_run", {"run_id": "run-1"}),
            setattr(turn, "learned_context", ""),
        )
        runtime._prepare_context_for_call = lambda *_args: None
        runtime._build_messages = lambda *args, **kwargs: [{"role": "user", "content": "Use missing tool"}]
        runtime._permitted_tool_names = lambda: {"read_file"}
        runtime._prompt_profile = lambda: "classic"
        runtime._get_model_response = Mock(return_value=response)
        runtime._execute_turn_action = Mock(side_effect=AssertionError("unknown tool was executed"))
        runtime._deterministic_tool_summary = lambda records: "No tools were run."
        runtime._complete_turn = lambda turn: turn.protocol_error_message

        result = AgentEngine(runtime).run(TurnContext("Use missing tool"))

        self.assertEqual(result, "The provider returned an unknown tool call; no tool was run.")
        runtime._execute_turn_action.assert_not_called()

    def test_native_turn_uses_typed_calls_and_correlated_results_without_legacy_loop(self):
        provider = SimpleNamespace(name="native", _continuation_id="native-session")
        provider.get_capabilities = lambda model: ProviderCapabilities(native_tools=True)
        call_response = ModelResponse(
            text="", tool_calls=(
                ToolCall("call-1", "read_file", {"path": "README.md"}),
                ToolCall("call-2", "read_file", {"path": "CONTRIBUTING.md"}),
            ),
            finish_reason=FinishReason.TOOL_REQUEST,
            continuation=provider_continuation(provider, "model-1", "fake", {"id": "response-1"}),
        )
        final_response = ModelResponse(text="Read the file successfully.", finish_reason=FinishReason.FINAL)
        runtime = SimpleNamespace(
            execution_context=SimpleNamespace(provider=provider), llm_provider=None, DEEPSEEK_MODEL="model-1",
            MAX_STEPS_PER_TURN=3, _record_foreground_model=contextvars.ContextVar("record", default=False),
            _conversation_turn_id=contextvars.ContextVar("turn_id", default=None),
            _last_response_is_native=contextvars.ContextVar("native", default=False),
            _last_prompt_tokens=2, _last_completion_tokens=1,
        )
        runtime._prepare_turn = lambda turn: (
            setattr(turn, "interaction_mode", "agent"), setattr(turn, "complexity", "simple"),
            setattr(turn, "learning_run", {"run_id": "run-1"}),
            setattr(turn, "learned_context", ""),
        )
        runtime._prepare_context_for_call = lambda *_args: None
        runtime._build_messages = lambda *args, **kwargs: [{"role": "user", "content": "Read README"}]
        runtime._permitted_tool_names = lambda: {"read_file"}
        runtime._prompt_profile = lambda: "classic"
        runtime._get_model_response = Mock(side_effect=[call_response, final_response])
        runtime._execute_turn_action = Mock(side_effect=lambda action, _args, **kwargs: SimpleNamespace(
            action=action, call_id=kwargs["call_id"],
            tool_result=ToolResult(data={"text": "contents"}, text="contents"),
            result_data={"text": "contents"}, result="contents",
        ))
        self.bind_tool_batch(runtime, specs={"read_file": SimpleNamespace(
            parallel_safe=True, side_effects="read_only", capability="read", risk="normal",
        )})
        runtime._record_turn_receipt = Mock(return_value={"success": True})
        runtime._tool_result_for_prompt = Mock(return_value="contents")
        runtime._render_tool_result = Mock()
        runtime._update_tasks_panel = Mock()
        runtime._safe_fstring = str
        runtime._deterministic_tool_summary = lambda records: "tool summary"
        runtime.tasks = SimpleNamespace(tasks=[])
        runtime._complete_turn = Mock(side_effect=lambda turn: turn.final_answer)
        runtime._initial_turn_response = Mock(side_effect=AssertionError("legacy protocol used"))
        runtime._execute_action_rounds = Mock(side_effect=AssertionError("legacy loop used"))

        turn = TurnContext("Read README")
        result = AgentEngine(runtime).run(turn)

        self.assertEqual(result, "Read the file successfully.")
        self.assertEqual(runtime._get_model_response.call_args_list[0].kwargs["tool_names"], {"read_file"})
        self.assertEqual(runtime._execute_turn_action.call_count, 2)
        self.assertEqual({call.kwargs["call_id"] for call in runtime._execute_turn_action.call_args_list},
                         {"call-1", "call-2"})
        self.assertTrue(all(call.kwargs["call_id_source"] == "native"
                            for call in runtime._execute_turn_action.call_args_list))
        messages = runtime._get_model_response.call_args_list[1].args[0]
        self.assertIs(messages[-3], call_response)
        self.assertEqual([item.call_id for item in messages[-2:]], ["call-1", "call-2"])
        self.assertTrue(all(isinstance(item, ToolReply) for item in messages[-2:]))
        runtime._initial_turn_response.assert_not_called()
        runtime._execute_action_rounds.assert_not_called()

    def test_archived_failed_result_keeps_error_identity_and_scoped_full_output(self):
        import json
        from openkyrozen.persistence.store import EventStore

        with tempfile.TemporaryDirectory() as directory, ExitStack() as sqlite_owners:
            store = sqlite_owners.enter_context(closing(EventStore(Path(directory) / "state.sqlite3")))
            memory = SimpleNamespace(store=store, user_id="alice", workspace_id="project", session_id="session")
            runtime = SimpleNamespace(memory_bank=memory, current_session=SimpleNamespace(), tasks=None)
            original = {"data": None, "text": "failed detail", "error": {"code": "denied"}, "metadata": {}}
            messages = [ToolReply("call-1", "read_file", original, True)]
            AgentEngine(runtime)._archive_replies(messages, [0])
            reply = messages[0]
            self.assertTrue(reply.is_error)
            self.assertEqual((reply.call_id, reply.name), ("call-1", "read_file"))
            artifact_id = reply.output["data"]["artifact_id"]
            page = store.artifacts.read_page(artifact_id, 0, 1000, user_id="alice",
                                             workspace_id="project", session_id="session")
            self.assertEqual(json.loads(page["content"]), original)
            self.assertIsNone(store.artifacts.read_page(artifact_id, 0, 1000, user_id="alice",
                                                       workspace_id="other", session_id="session"))
            reopened = sqlite_owners.enter_context(closing(EventStore(Path(directory) / "state.sqlite3")))
            second = [ToolReply("call-1", "read_file", original, True)]
            runtime.memory_bank.store = reopened
            AgentEngine(runtime)._archive_replies(second, [0])
            self.assertEqual(second[0].output["data"]["artifact_id"], artifact_id)

    def test_runtime_native_provider_executes_and_continues_without_text_parsing(self):
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.tools import ToolAdapters

        class FixtureProvider(LLMProvider):
            def __init__(self):
                super().__init__(ProviderConfig(
                    provider="custom", model_simple="fixture", model_complex="fixture",
                    base_url="https://provider.invalid/v1",
                ))
                self.requests = []
                self.calls_attempted = 0

            def get_capabilities(self, model=None):
                return ProviderCapabilities(native_tools=True)

            def chat(self, messages, model=None):
                raise AssertionError("native calls must not use the text provider API")

            def chat_response(self, messages, model=None, *, tools=None):
                self.calls_attempted += 1
                from openkyrozen.providers.models import provider_identity
                validate_tool_history(messages, provider_id=provider_identity(self), model=model, native=True)
                validate_replied_tool_definitions(messages, tools)
                self.requests.append((list(messages), tools))
                system_prompt = messages[0]["content"]
                if any(marker in system_prompt for marker in ("Action:", "TaskList:", "TaskDone:", "DefineTool:")):
                    raise AssertionError("native prompt includes legacy control syntax")
                if len(self.requests) == 1:
                    self.assert_tool_schema(tools, "update_plan")
                    if {item["name"] for item in tools} != {"update_plan"}:
                        raise AssertionError("complex direct turn offered other tools before planning")
                    return ModelResponse(
                        tool_calls=(ToolCall("plan-1", "update_plan", {
                            "complexity": "complex",
                            "tasks": [{"description": "Read note.txt", "acceptance": ["contents verified"]}],
                        }),),
                        finish_reason=FinishReason.TOOL_REQUEST,
                        continuation=provider_continuation(self, model, "fixture", {"id": "response-1"}),
                        metadata={"provider": self.name, "model": model},
                    )
                if len(self.requests) == 2:
                    self.assert_tool_schema(tools, "update_plan")
                    if not isinstance(messages[-1], ToolReply) or messages[-1].call_id != "plan-1":
                        raise AssertionError("native plan result was not correlated to its call")
                    return ModelResponse(
                        tool_calls=(ToolCall("read-1", "read_file", {"path": "note.txt"}),),
                        finish_reason=FinishReason.TOOL_REQUEST,
                        continuation=provider_continuation(self, model, "fixture", {"id": "response-2"}),
                        metadata={"provider": self.name, "model": model},
                    )
                if len(self.requests) == 3 and (not isinstance(messages[-1], ToolReply) or messages[-1].call_id != "read-1"):
                    raise AssertionError("native read result was not correlated to its call")
                return ModelResponse(
                    text="The file contains verified text.", finish_reason=FinishReason.FINAL,
                    metadata={"provider": self.name, "model": model},
                )

            @staticmethod
            def assert_tool_schema(tools, name):
                if not any(item["name"] == name for item in tools or ()):
                    raise AssertionError(f"missing structured schema for {name}")

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_APPROVAL_MODE": "never",
            "KYROZEN_WORKSPACE_ROOT": directory,
        }), ExitStack() as sqlite_owners:
            root = Path(directory)
            (root / "note.txt").write_text("verified text", encoding="utf-8")
            app = build_application(
                surface="cli", memory=sqlite_owners.enter_context(closing(build_memory(root / "state.sqlite3", user_id="alice", index=None))),
                tools=ToolAdapters(root), user_id="alice",
            )
            self.addCleanup(app.close)
            runtime = app.runtime
            provider = FixtureProvider()
            runtime.llm_provider = provider
            runtime._default_turn.provider = provider
            runtime.set_interaction_mode("agent")
            packet = "<memory_context>frozen packet</memory_context>"
            prepare_turn = runtime._prepare_turn
            def prepare_complex(turn):
                prepare_turn(turn)
                turn.routing_decision = None
                turn.complexity = "complex"
            with patch.object(runtime, "_schedule_post_turn_learning"), \
                    patch.object(runtime, "_prepare_turn", side_effect=prepare_complex), \
                    patch.object(runtime, "_build_memory_context", return_value=packet) as retrieve_memory, \
                    patch.object(runtime, "_collect_tool_calls", side_effect=AssertionError("legacy parser used")), \
                    patch.object(runtime, "_collect_unwrapped_tool_calls", side_effect=AssertionError("legacy parser used")):
                answer = runtime.chat(runtime.current_session, "Read note.txt")

            older = next(item for item in provider.requests[2][0]
                         if isinstance(item, ToolReply) and item.call_id == "plan-1")
            original = provider.requests[1][0][-1].output
            self.assertNotEqual(older.output, original)
            artifact_id = older.output["data"]["artifact_id"]
            page = runtime.tool_registry.get_spec("read_tool_artifact").executor({"artifact_id": artifact_id})
            import json
            self.assertEqual(json.loads(page.data["content"]), original)
            self.assertIsNone(runtime.memory_bank.store.artifacts.read_page(
                artifact_id, 0, 1000, user_id="another-user", workspace_id=runtime.tasks.workspace_id,
                session_id=runtime.tasks.session_id))
            with patch.object(runtime, "_schedule_post_turn_learning"), \
                    patch.object(runtime, "_prepare_turn", side_effect=prepare_complex), \
                    patch.object(runtime, "_build_memory_context", return_value=packet):
                next_answer = runtime.chat(runtime.current_session, "Continue")
            self.assertIn("verified text", next_answer)
            historical = [item for item in provider.requests[3][0] if isinstance(item, ToolReply)]
            self.assertTrue(historical)
            self.assertTrue(all("artifact_id" in item.output["data"] for item in historical))
            self.assertEqual(historical[0].output["data"]["artifact_id"], artifact_id)
            with patch.object(runtime.memory_bank.store.artifacts, "get_or_create", side_effect=OSError("offline")), \
                    patch.object(runtime, "_schedule_post_turn_learning"):
                failure = runtime.chat(runtime.current_session, "Continue again")
            self.assertIn("Could not retain earlier tool results", failure)
            self.assertEqual(provider.calls_attempted, 4)

        self.assertIn("verified text", answer)
        retrieve_memory.assert_called_once()
        self.assertEqual(provider.calls_attempted, 4)
        self.assertEqual(len(provider.requests), 4)
        self.assertTrue(all(any(packet in item.get("content", "") for item in request[0]
                                if isinstance(item, dict)) for request in provider.requests))
        self.assertEqual(provider.requests[1][0][-1].call_id, "plan-1")
        self.assertEqual(provider.requests[2][0][-1].call_id, "read-1")
        self.assertEqual(app.runtime.tasks.working_plan.complexity, "complex")
        self.assertEqual(app.runtime.tasks.tasks[0]["description"], "Read note.txt")


class NativeLifecycleRegressionTests(unittest.TestCase):
    def runtime(self, *, complexity="simple", child=False):
        import time
        from types import MethodType
        from openkyrozen.agent.completion import _complete_turn

        runtime = AgentEngineTests().complex_native_runtime()
        runtime.execution_context.child_run_id = "child-1" if child else None
        runtime.tasks.tasks = []
        runtime._turn_failed = contextvars.ContextVar("failed", default=False)
        runtime._turn_completion_status = contextvars.ContextVar("completion", default="completed")
        runtime.subagent_manager = SimpleNamespace(coordinator=None)
        runtime._clean_final_response = str
        runtime._advance_fix_workflow = lambda state, _request, answer, *_args: (state, answer)
        runtime._finish_learning_run = lambda _run, _receipts, _task, answer, *_args: answer
        runtime._turn_cost_log = []
        runtime._complete_turn = MethodType(_complete_turn, runtime)
        def prepare(turn):
            turn.interaction_mode, turn.complexity = "agent", complexity
            turn.learning_run, turn.turn_start = {"run_id": "fixture"}, time.time()
        runtime._prepare_turn = prepare
        return runtime

    def test_unsuccessful_native_finishes_cannot_complete_the_turn(self):
        cases = [(FinishReason.BLOCKED, "blocked"), (FinishReason.ERROR, "incomplete"),
                 (FinishReason.LENGTH, "incomplete"), (FinishReason.UNKNOWN, "incomplete")]
        for reason, expected_status in cases:
            with self.subTest(reason=reason):
                runtime = self.runtime()
                runtime._get_model_response.return_value = ModelResponse(
                    text="A useful partial explanation.", finish_reason=reason)
                turn = TurnContext("Explain the result")
                answer = AgentEngine(runtime).run(turn)
                self.assertEqual(turn.runtime_status, expected_status)
                self.assertIn("partial explanation", answer)
                self.assertTrue(turn.protocol_error_message)

    def test_unsuccessful_native_finish_never_dispatches_its_tool_calls(self):
        runtime = self.runtime()
        runtime._get_model_response.return_value = ModelResponse(
            text="Provider failed while generating.",
            tool_calls=(ToolCall("must-not-run", "read_file", {"path": "note.txt"}),),
            finish_reason=FinishReason.ERROR)
        AgentEngine(runtime).run(TurnContext("Read a note"))
        runtime._execute_tool_batch.assert_not_called()

    def test_native_cancellation_reaches_the_runtime_cancellation_handler(self):
        from openkyrozen.providers.errors import ProviderError, ProviderErrorKind
        for response in (ModelResponse(text="Cancelled by provider.", finish_reason=FinishReason.CANCELLED),
                         ProviderError(ProviderErrorKind.CANCELLED, terminal=True)):
            with self.subTest(response=type(response).__name__):
                runtime = self.runtime()
                if isinstance(response, Exception):
                    runtime._get_model_response.side_effect = response
                else:
                    runtime._get_model_response.return_value = response
                with self.assertRaises(ProviderError) as caught:
                    AgentEngine(runtime).run(TurnContext("Read a note"))
                self.assertEqual(caught.exception.kind, ProviderErrorKind.CANCELLED)

    def test_default_native_inventory_defers_specialized_tools_in_classic_profile(self):
        runtime = self.runtime()
        runtime._prompt_profile = lambda: "classic"
        runtime._permitted_tool_names = lambda: {"read_file", "discover_tools", "github_read", "browser_open", "graph_query"}
        self.assertEqual(AgentEngine(runtime)._native_tool_names(), {"read_file", "discover_tools"})
        runtime.execution_context.discovered_tools = frozenset({"github_read"})
        self.assertEqual(AgentEngine(runtime)._native_tool_names(), {"read_file", "discover_tools", "github_read"})

    def test_complex_child_cannot_execute_before_establishing_its_working_plan(self):
        runtime = self.runtime(complexity="complex", child=True)
        answer = AgentEngine(runtime).run(TurnContext("Investigate and fix the repository"))
        runtime._execute_tool_batch.assert_not_called()
        self.assertIn("plan", answer.lower())

    def test_complex_child_completion_reports_its_outstanding_required_tasks(self):
        runtime = self.runtime(complexity="complex", child=True)
        runtime.tasks.working_plan = SimpleNamespace(id="child-plan", status="active", task_ids=["child-task"])
        runtime.tasks.tasks = [{"id": "child-task", "status": "pending", "description": "Verify the change"}]
        runtime._get_model_response.return_value = ModelResponse(text="The investigation is complete.", finish_reason=FinishReason.FINAL)
        turn = TurnContext("Investigate and fix the repository")
        answer = AgentEngine(runtime).run(turn)
        self.assertEqual(turn.runtime_status, "incomplete")
        self.assertIn("Verify the change", answer)

    def test_failed_effect_verification_marks_final_status_incomplete(self):
        runtime = self.runtime()
        turn = TurnContext("Write a note")
        runtime._prepare_turn(turn)
        turn.final_answer, turn.response_text = "The file was written successfully.", ""
        turn.tool_calls = []
        turn.tool_records = [{"action": "write_file", "success": True,
                              "verification": {"status": "failed", "method": "file_content_match"}}]
        answer = runtime._complete_turn(turn)
        self.assertEqual(turn.runtime_status, "incomplete")
        self.assertIn("not verified", answer)
        self.assertNotIn("file was written successfully", answer.lower())

    def test_failed_write_without_effect_verification_cannot_keep_success_claim(self):
        for stage in (None, "verified"):
            for retried in (False, True):
                with self.subTest(stage=stage, retried=retried):
                    runtime = self.runtime()
                    turn = TurnContext("Write a note")
                    runtime._prepare_turn(turn)
                    turn.fix_workflow = {"stage": stage} if stage else None
                    turn.final_answer, turn.response_text = "The file was written successfully.", ""
                    turn.tool_calls = []
                    first = {"action": "write_file", "success": False, "operation_id": "same-write",
                        "args": "note.txt|requested", "task_id": "task", "plan_id": "plan",
                        "verification": {"status": "not_applicable"}}
                    turn.tool_records = [first]
                    if retried:
                        turn.tool_records.append({**first, "success": True,
                            "verification": {"status": "verified", "method": "file_content_match"}})
                    answer = runtime._complete_turn(turn)
                    self.assertEqual(turn.runtime_status, "completed" if retried else "incomplete")
                    if retried:
                        self.assertIn("file was written successfully", answer)
                    else:
                        self.assertNotIn("file was written successfully", answer)
                        self.assertIn("write_file", answer)

    def test_typed_remote_effect_failure_cannot_keep_success_claim(self):
        runtime = self.runtime()
        turn = TurnContext("Publish the change")
        runtime._prepare_turn(turn)
        turn.fix_workflow = {"stage": "verified"}
        turn.final_answer, turn.response_text = "The remote change was published successfully.", ""
        turn.tool_calls = []
        turn.tool_records = [{"action": "git_push", "side_effects": "remote", "success": False,
            "verification": {"status": "not_applicable"}}]
        answer = runtime._complete_turn(turn)
        self.assertEqual(turn.runtime_status, "incomplete")
        self.assertNotIn("published successfully", answer)
        self.assertIn("git_push", answer)
        self.assertIn("not verified", answer)

    def test_only_trusted_duplicate_refusal_can_preserve_prior_verified_claim(self):
        for kind in ("duplicate", "conflict", "permission_denied", "failed_verification", "different_task"):
            with self.subTest(kind=kind):
                runtime = self.runtime()
                turn = TurnContext("Write a note")
                runtime._prepare_turn(turn)
                turn.fix_workflow = {"stage": "verified"}
                turn.final_answer, turn.response_text = "The file was written successfully.", ""
                turn.tool_calls = []
                first = {"action": "write_file", "success": True, "operation_id": "write", "args": "note.txt|requested",
                    "task_id": "task", "plan_id": "plan", "verification": {"status": "verified", "method": "file_content_match"}}
                later = {**first, "success": False, "failure": "conflict",
                    "verification": {"status": "not_applicable", "method": "duplicate_success_refusal"}}
                if kind == "conflict": later["verification"] = {"status": "unavailable", "method": "none"}
                elif kind == "permission_denied": later.update(failure="permission_denied", verification={"status": "unavailable"})
                elif kind == "failed_verification": later["verification"] = {"status": "failed", "method": "file_content_match"}
                elif kind == "different_task": later["task_id"] = "other-task"
                turn.tool_records = [first, later]
                answer = runtime._complete_turn(turn)
                if kind == "duplicate":
                    self.assertIn("file was written successfully", answer)
                else:
                    self.assertNotIn("file was written successfully", answer)
                    self.assertEqual(turn.runtime_status, "blocked" if kind == "permission_denied" else "incomplete")

    def test_trusted_duplicate_refusal_completes_without_a_fix_workflow(self):
        runtime = self.runtime()
        turn = TurnContext("Write a note")
        runtime._prepare_turn(turn)
        turn.final_answer, turn.response_text = "The file was written successfully.", ""
        turn.tool_calls = []
        first = {"action": "write_file", "success": True, "operation_id": "write", "args": "note.txt|requested",
            "task_id": "task", "plan_id": "plan", "verification": {"status": "verified", "method": "file_content_match"}}
        turn.tool_records = [first, {**first, "success": False, "failure": "conflict",
            "verification": {"status": "not_applicable", "method": "duplicate_success_refusal"}}]
        answer = runtime._complete_turn(turn)
        self.assertEqual(turn.runtime_status, "completed")
        self.assertEqual(answer, "The file was written successfully.")

    def test_delegated_failure_reply_remains_valid_worker_or_review_json(self):
        import json
        from openkyrozen.agent.delegation import REPORT_LISTS, report
        for review in (False, True):
            with self.subTest(review=review):
                runtime = self.runtime(complexity="complex", child=True)
                runtime.execution_context.coordinator = object()
                runtime.tasks.working_plan = SimpleNamespace(id="child-plan", status="active", task_ids=["child-task"])
                runtime.tasks.tasks = [{"id": "child-task", "status": "pending", "description": "Verify the change"}]
                turn = TurnContext("Write a note")
                runtime._prepare_turn(turn)
                payload = ({"verdict": "verified", "findings": [], "evidence": []} if review else
                           {**{key: [] for key in REPORT_LISTS}, "status": "completed"})
                payload["summary"] = "The file was written successfully."
                raw = json.dumps(payload)
                turn.final_answer = "```json\n" + raw + "\n```" if review else raw
                turn.response_text, turn.tool_calls = "", []
                turn.tool_records = [{"action": "write_file", "success": False, "failure": "permission_denied",
                    "verification": {"status": "unavailable"}, "tool_error": {"code": "permission_denied"}}]
                answer = runtime._complete_turn(turn)
                parsed = report(answer, review=review)
                self.assertEqual(json.loads(answer), parsed)
                self.assertEqual(parsed["verdict"] if review else parsed["status"], "unverifiable" if review else "blocked")
                self.assertTrue(any("write_file" in finding for finding in parsed["findings"]))
                self.assertNotIn("written successfully", answer)
                self.assertEqual(turn.runtime_status, "blocked")

    def test_complex_child_plan_status_keeps_valid_worker_json_and_cannot_complete_active_plan(self):
        import json
        from openkyrozen.agent.delegation import REPORT_LISTS, report
        for plan_status, task_status, expected_runtime, expected_report in (
                ("completed", "succeeded", "completed", "completed"),
                ("active", "pending", "incomplete", "partial"),
                ("active", "blocked", "blocked", "blocked")):
            with self.subTest(plan_status=plan_status, task_status=task_status):
                runtime = self.runtime(complexity="complex", child=True)
                runtime.execution_context.coordinator = object()
                runtime.tasks.working_plan = SimpleNamespace(id="child-plan", status=plan_status, task_ids=["child-task"])
                runtime.tasks.tasks = [{"id": "child-task", "status": task_status, "description": "Verify the change"}]
                turn = TurnContext("Investigate the result")
                runtime._prepare_turn(turn)
                payload = {**{key: [] for key in REPORT_LISTS}, "status": "completed", "summary": "The requested result."}
                turn.final_answer = json.dumps(payload)
                turn.response_text, turn.tool_calls, turn.tool_records = "", [], []
                answer = runtime._complete_turn(turn)
                parsed = report(answer)
                self.assertEqual(json.loads(answer), parsed)
                self.assertEqual(turn.runtime_status, expected_runtime)
                self.assertEqual(parsed["status"], expected_report)
                if expected_report == "completed":
                    self.assertEqual(parsed, payload)
                else:
                    self.assertTrue(any("Plan status:" in item for item in parsed["uncertainties"]))

    def test_native_final_claim_is_replaced_after_actual_failed_write_verification(self):
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.tools import ToolAdapters
        from openkyrozen.tools.manifest import ToolSpec
        class Provider(LLMProvider):
            calls = 0
            def get_capabilities(self, model=None): return ProviderCapabilities(native_tools=True)
            def chat(self, messages, model=None): raise AssertionError("legacy provider boundary used")
            def chat_response(self, messages, model=None, *, tools=None):
                self.calls += 1
                if self.calls == 1:
                    return ModelResponse(tool_calls=(ToolCall("failed-write", "write_file", {
                        "path": "note.txt", "content": "requested content"}),), finish_reason=FinishReason.TOOL_REQUEST,
                        continuation=provider_continuation(self, model, "fixture", {"call": "failed-write"}))
                return ModelResponse(text="The file was written successfully.", finish_reason=FinishReason.FINAL)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_APPROVAL_MODE": "never",
        }), ExitStack() as sqlite_owners:
            root = Path(directory)
            (root / "note.txt").write_text("unchanged old content")
            app = build_application(surface="cli", memory=sqlite_owners.enter_context(closing(build_memory(root / "state.sqlite3", index=None))), tools=ToolAdapters(root))
            self.addCleanup(app.close)
            runtime = app.runtime
            runtime._set_workspace_root(root)
            runtime.set_interaction_mode("agent")
            config = ProviderConfig(provider="custom", model_simple="fixture", model_complex="fixture")
            runtime._provider_config = config
            provider = Provider(config)
            runtime.llm_provider = runtime._default_turn.provider = provider
            spec = runtime.tool_registry.get_spec("write_file")
            runtime.tool_registry.register_spec(ToolSpec("write_file", lambda _arguments: ToolResult(text="Tool reported success"),
                description=spec.description, input_schema=spec.input_schema, capability=spec.capability,
                side_effects=spec.side_effects), replace=True)
            events = []
            with patch.object(runtime, "_schedule_post_turn_learning"), patch.object(runtime, "_finish_learning_run",
                    side_effect=lambda _run, _receipts, _task, answer, *_args: answer):
                answer = runtime.chat(runtime.current_session, "Write note.txt", on_runtime_event=events.append)
            receipts = runtime.memory_bank.store.list_events("execution.receipt", user_id=runtime.tasks.user_id,
                workspace_id=runtime.tasks.workspace_id, session_id=runtime.tasks.session_id)
            receipt = next(event["payload"] for event in receipts if event["payload"].get("call_id") == "failed-write")
            self.assertTrue(receipt["success"])
            self.assertEqual(receipt["verification"]["status"], "failed")
            self.assertEqual((root / "note.txt").read_text(), "unchanged old content")
            self.assertEqual(provider.calls, 2)
            self.assertNotIn("file was written successfully", answer.lower())
            self.assertIn("write_file", answer)
            self.assertIn("not verified", answer)
            terminal = next(event for event in events if event.kind == "turn.completed")
            self.assertEqual(terminal.payload["status"], "incomplete")
            # CLI/TUI/Web consume the returned native reply through these literal projections.
            self.assertEqual(runtime._clean_final_response(answer), answer)
            self.assertEqual(runtime._split_reply(answer)[1], answer)

    def test_failed_effect_summary_preserves_provider_block_reason(self):
        runtime = self.runtime()
        turn = TurnContext("Write a note")
        runtime._prepare_turn(turn)
        turn.runtime_status = "blocked"
        turn.protocol_error_message = "The provider blocked this response; no further tools were run."
        turn.final_answer, turn.response_text = "The file was written successfully.", ""
        turn.tool_calls = []
        turn.tool_records = [{"action": "write_file", "success": True,
            "verification": {"status": "failed", "method": "file_content_match"}}]
        answer = runtime._complete_turn(turn)
        self.assertEqual(turn.runtime_status, "blocked")
        self.assertIn("provider blocked", answer)
        self.assertIn("not verified", answer)
        self.assertNotIn("written successfully", answer)

    def test_verified_reproduction_failure_preserves_literal_final_reply(self):
        runtime = self.runtime()
        turn = TurnContext("Fix the page")
        runtime._prepare_turn(turn)
        turn.fix_workflow = {"stage": "verified"}
        turn.final_answer = "The verified repair passed. Action: read_file is a literal example."
        turn.response_text, turn.tool_calls = "", []
        turn.tool_records = [{"action": "run_cmd", "success": False,
            "verification": {"status": "failed", "method": "command_exit_status"}}]
        answer = runtime._complete_turn(turn)
        self.assertEqual(turn.runtime_status, "completed")
        self.assertEqual(answer, turn.final_answer)
        self.assertIn("Action: read_file is a literal example.", answer)

    def test_verified_fix_cannot_hide_latest_failed_file_verification(self):
        for stage in ("verified", "explained"):
            with self.subTest(stage=stage):
                runtime = self.runtime()
                turn = TurnContext("Fix the page")
                runtime._prepare_turn(turn)
                turn.fix_workflow = {"stage": stage}
                turn.final_answer, turn.response_text = "The repair was completed successfully.", ""
                turn.tool_calls = []
                turn.tool_records = [{"action": "write_file", "success": True, "operation_id": "write-1",
                    "args": "page.txt|expected", "task_id": "task-1", "plan_id": "plan-1",
                    "verification": {"status": "failed", "method": "file_content_match"}}]
                answer = runtime._complete_turn(turn)
                self.assertEqual(turn.runtime_status, "incomplete", answer)
                self.assertIn("not verified", answer)

    def test_only_later_verified_retry_of_same_bound_operation_resolves_file_failure(self):
        first = {"action": "write_file", "success": True, "operation_id": "write-1", "args": "page.txt|expected",
                 "task_id": "task-1", "plan_id": "plan-1", "verification": {"status": "failed", "method": "file_content_match"}}
        for changes, expected in (({}, "completed"), ({"operation_id": "different"}, "incomplete"),
                                  ({"task_id": "other-task"}, "incomplete")):
            with self.subTest(changes=changes):
                runtime = self.runtime()
                turn = TurnContext("Fix the page")
                runtime._prepare_turn(turn)
                turn.fix_workflow = {"stage": "verified"}
                turn.final_answer, turn.response_text = "The repair was independently verified.", ""
                turn.tool_calls = []
                turn.tool_records = [first, {**first, **changes, "verification": {"status": "verified", "method": "file_content_match"}}]
                answer = runtime._complete_turn(turn)
                self.assertEqual(turn.runtime_status, expected)
                if expected == "completed":
                    self.assertIn("independently verified", answer)
                else:
                    self.assertNotIn("independently verified", answer)

    def test_learning_bookkeeping_failure_preserves_the_completed_answer(self):
        runtime = self.runtime()
        runtime._record_learning_event = Mock()
        runtime._finish_learning_run = Mock(side_effect=RuntimeError("database unavailable"))
        runtime._get_model_response.return_value = ModelResponse(text="The verified direct answer.", finish_reason=FinishReason.FINAL)
        turn = TurnContext("Explain the result")
        answer = AgentEngine(runtime).run(turn)
        self.assertEqual(answer, "The verified direct answer.")
        self.assertEqual(turn.runtime_status, "completed")

    def test_deterministic_summary_does_not_call_failed_verification_completed(self):
        from openkyrozen.agent.protocol import _deterministic_tool_summary
        runtime = self.runtime()
        answer = _deterministic_tool_summary(runtime, [{
            "action": "write_file", "success": True, "result": "File content differs from requested content.",
            "verification": {"status": "failed", "method": "file_content_match"},
        }])
        self.assertIn("Could not complete", answer)
        self.assertNotIn("- Completed", answer)

    def test_plan_edits_cannot_share_a_batch_with_calls_using_the_old_task_selection(self):
        runtime = self.runtime(complexity="complex")
        runtime.tasks.working_plan = SimpleNamespace(id="plan", status="active", task_ids=[])
        runtime._permitted_tool_names = lambda: {"read_file", "update_plan"}
        runtime._get_model_response.side_effect = [ModelResponse(tool_calls=(
            ToolCall("select", "update_plan", {"expected_revision": 1, "operations": [{"op": "start", "task": "new"}]}),
            ToolCall("read", "read_file", {"path": "note.txt"}),
        ), finish_reason=FinishReason.TOOL_REQUEST), ModelResponse(text="Done", finish_reason=FinishReason.FINAL)]
        turn = TurnContext("Inspect the task")
        answer = AgentEngine(runtime).run(turn)
        runtime._execute_tool_batch.assert_not_called()
        self.assertIn("separate", answer.lower())
        self.assertEqual(turn.runtime_status, "incomplete")
        self.assertEqual(runtime._get_model_response.call_count, 2)

    def test_native_mixed_plan_validation_can_continue_to_explicit_verified_work(self):
        self._mixed_plan_validation_workflow(deny_scope=False)

    def test_native_mixed_plan_validation_keeps_subsequent_scope_denial_incomplete(self):
        self._mixed_plan_validation_workflow(deny_scope=True)

    def _mixed_plan_validation_workflow(self, *, deny_scope):
        import sys
        import time
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.providers.models import provider_identity
        from openkyrozen.tools import ToolAdapters

        case = self
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_APPROVAL_MODE": "never",
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        }), ExitStack() as owners:
            root = Path(directory) / "workspace"
            root.mkdir()
            note = root / "note.txt"
            note.write_text("before", encoding="utf-8")
            command = "python -m unittest test_note"
            (root / "test_note.py").write_text(
                "import unittest\nfrom pathlib import Path\nclass NoteTest(unittest.TestCase):\n"
                "    def test_update(self):\n        p = Path('note.txt')\n        p.write_text('verified')\n"
                "        self.assertEqual(p.read_text(), 'verified')\n", encoding="utf-8")
            memory = owners.enter_context(closing(build_memory(root / "state.sqlite3", index=None)))
            app = build_application(surface="cli", memory=memory, tools=ToolAdapters(root))
            owners.callback(app.close)
            runtime = app.runtime
            runtime._set_workspace_root(root)
            runtime.tasks.edit_plan(complexity="complex", expected_revision=0, operations=[{
                "op": "add", "ref": "verify", "task": {
                    "description": "Update and verify note.txt", "acceptance": [command],
                    "execution": {"action": "run_cmd", "arguments": {"command": command}},
                },
            }], reason="Explicit test objective")
            task = runtime.tasks.tasks[0]
            selection = {"plan_id": runtime.tasks.working_plan.id, "expected_revision": 1, "operations": [
                {"op": "start", "task": task["id"]}, {"op": "suggest_complete", "task": task["id"]},
            ]}

            class PlanningProvider(LLMProvider):
                requests = 0

                def get_capabilities(self, model=None):
                    return ProviderCapabilities(native_tools=True)

                def chat(self, messages, model=None):
                    raise AssertionError("legacy provider boundary used")

                def chat_response(self, messages, model=None, *, tools=None):
                    self.requests += 1
                    validate_tool_history(messages, provider_id=provider_identity(self), model=model, native=True)
                    validate_replied_tool_definitions(messages, tools)
                    if self.requests == 1:
                        calls = (ToolCall("rejected-plan", "update_plan", selection),
                                 ToolCall("rejected-command", "run_cmd", {"command": command}))
                    elif self.requests == 2:
                        case.assertEqual(note.read_text(), "before")
                        case.assertEqual(runtime.tasks.working_plan.revision, 1)
                        case.assertEqual(task["status"], "pending")
                        case.assertIsNone(runtime.tasks.execution_task_id)
                        replies = messages[-2:]
                        case.assertEqual([item.call_id for item in replies], ["rejected-plan", "rejected-command"])
                        for item in replies:
                            case.assertIsInstance(item, ToolReply)
                            case.assertTrue(item.is_error)
                            case.assertEqual(item.output["error"]["code"], "invalid_arguments")
                            case.assertTrue(item.output["error"]["retriable"])
                        calls = (ToolCall("select", "update_plan", selection),)
                    elif self.requests == 3:
                        case.assertEqual(note.read_text(), "before")
                        case.assertEqual(runtime.tasks.working_plan.revision, 2)
                        case.assertEqual(runtime.tasks.execution_task_id, task["id"])
                        calls = ((ToolCall("denied", "write_file", {"path": "../outside.txt", "content": "forbidden"})
                                  if deny_scope else ToolCall("verify", "run_cmd", {"command": command})),)
                    else:
                        case.assertEqual(self.requests, 4)
                        case.assertEqual(note.read_text(), "before" if deny_scope else "verified")
                        case.assertFalse((root.parent / "outside.txt").exists())
                        return ModelResponse(text="The requested change was verified.", finish_reason=FinishReason.FINAL)
                    return ModelResponse(tool_calls=calls, finish_reason=FinishReason.TOOL_REQUEST,
                        continuation=provider_continuation(self, model, "fixture", {"step": self.requests}))

            provider = PlanningProvider(ProviderConfig(provider="custom", model_simple="fixture"))
            runtime.llm_provider = runtime._default_turn.provider = provider
            runtime.set_interaction_mode("agent")
            def prepare(turn):
                turn.interaction_mode, turn.complexity = "agent", "complex"
                turn.learning_run, turn.turn_start = {"run_id": "fixture"}, time.time()
                turn.learned_context = ""
            events, runtime_events = [], []
            with patch.object(runtime, "_prepare_turn", side_effect=prepare), \
                    patch.object(runtime, "_finish_learning_run", side_effect=lambda _run, _records, _task, answer, *_args: answer), \
                    patch.object(runtime, "_schedule_post_turn_learning"), \
                    patch.object(runtime, "_collect_tool_calls", side_effect=AssertionError("legacy parser used")), \
                    patch.object(runtime, "_collect_unwrapped_tool_calls", side_effect=AssertionError("legacy parser used")):
                answer = runtime.chat(runtime.current_session, "Update and verify note.txt",
                                      on_event=events.append, on_runtime_event=runtime_events.append)
            self.assertEqual(provider.requests, 4, answer)
            completed = [event for event in runtime_events if event.kind == "turn.completed"]
            self.assertEqual([event.payload["status"] for event in completed],
                             ["incomplete" if deny_scope else "completed"], answer)
            self.assertEqual(runtime.tasks.tasks[0]["status"], "running" if deny_scope else "succeeded")
            self.assertEqual(runtime.tasks.working_plan.status, "active" if deny_scope else "completed")
            receipts = memory.store.list_events("execution.receipt", user_id=runtime.tasks.user_id,
                workspace_id=runtime.tasks.workspace_id, session_id=runtime.tasks.session_id)
            self.assertEqual(len(receipts), 2)
            by_call = {event["payload"]["call_id"]: event["payload"] for event in receipts}
            self.assertEqual(set(by_call), {"select", "denied" if deny_scope else "verify"})
            self.assertIsNone(by_call["select"]["task_id"])
            outcome = by_call["denied" if deny_scope else "verify"]
            if deny_scope:
                self.assertIsNone(outcome["task_id"])
                self.assertFalse(outcome["success"])
                self.assertIn("outside the active workspace", outcome["result"])
                self.assertFalse((root.parent / "outside.txt").exists())
            else:
                self.assertEqual(outcome["task_id"], task["id"])
                self.assertEqual(outcome["verification"]["status"], "verified")
            entries = runtime.conversation_history.entries()
            rejected = [entry for entry in entries if entry.kind == "tool.result"
                        and entry.call_id in {"rejected-plan", "rejected-command"}]
            self.assertEqual(len(rejected), 2)
            self.assertTrue(all(entry.task_id is None and entry.payload["is_error"] for entry in rejected))
            history = runtime.conversation_history.provider_messages(
                provider_id=provider_identity(provider), model=runtime.DEEPSEEK_MODEL, native=True)
            validate_tool_history(history, provider_id=provider_identity(provider), model=runtime.DEEPSEEK_MODEL, native=True)
            self.assertFalse(any(event.get("event") in {"protocol_recovery", "protocol.recovery"} for event in events))
            self.assertFalse(any(event.kind in {"protocol_recovery", "protocol.recovery"} for event in runtime_events))

    def test_repeated_mixed_plan_validation_consumes_the_existing_step_budget(self):
        runtime = self.runtime(complexity="complex")
        runtime.tasks.working_plan = SimpleNamespace(id="plan", status="active", task_ids=[], revision=1)
        requests = []
        def respond(messages, **_kwargs):
            requests.append(list(messages))
            index = len(requests)
            return ModelResponse(tool_calls=(
                ToolCall(f"plan-{index}", "update_plan", {"expected_revision": 1, "operations": []}),
                ToolCall(f"read-{index}", "read_file", {"path": "note.txt"}),
            ), finish_reason=FinishReason.TOOL_REQUEST)
        runtime._get_model_response.side_effect = respond
        turn = TurnContext("Inspect the task")
        with patch.object(AgentEngine, "_archive_replies"):
            answer = AgentEngine(runtime).run(turn)
        self.assertEqual(len(requests), 3)
        self.assertEqual([item.call_id for item in requests[1] if isinstance(item, ToolReply)], ["plan-1", "read-1"])
        self.assertEqual([item.call_id for item in requests[2] if isinstance(item, ToolReply)],
                         ["plan-1", "read-1", "plan-2", "read-2"])
        self.assertIn("step limit", answer)
        self.assertEqual(turn.runtime_status, "incomplete")
        self.assertEqual(runtime.tasks.working_plan.revision, 1)
        self.assertEqual(turn.tool_calls, [])
        self.assertEqual(turn.tool_records, [])
        runtime._execute_tool_batch.assert_not_called()

    def test_cancellation_after_mixed_plan_feedback_prevents_the_next_request(self):
        import threading
        from openkyrozen.providers.errors import ProviderError, ProviderErrorKind
        runtime = self.runtime(complexity="complex")
        runtime.tasks.working_plan = SimpleNamespace(id="plan", status="active", task_ids=[])
        runtime.execution_context.cancelled = threading.Event()
        runtime._conversation_turn_id = contextvars.ContextVar("validation-turn", default="turn")
        runtime._last_model_request_id = contextvars.ContextVar("validation-request", default="request")
        journal = []
        def record(kind, payload, **correlation):
            journal.append((kind, payload, correlation))
            if kind == "tool.result":
                runtime.execution_context.cancelled.set()
        runtime._record_conversation_entry = record
        runtime._get_model_response.return_value = ModelResponse(tool_calls=(
            ToolCall("plan", "update_plan", {"expected_revision": 1, "operations": []}),
            ToolCall("read", "read_file", {"path": "note.txt"}),
        ), finish_reason=FinishReason.TOOL_REQUEST)
        with self.assertRaises(ProviderError) as caught:
            AgentEngine(runtime).run(TurnContext("Inspect the task"))
        self.assertEqual(caught.exception.kind, ProviderErrorKind.CANCELLED)
        runtime._get_model_response.assert_called_once()
        runtime._execute_tool_batch.assert_not_called()
        self.assertEqual([payload["call_id"] for kind, payload, _ in journal if kind == "tool.result"], ["plan", "read"])

    def test_mixed_plan_feedback_does_not_relax_unknown_tools_or_interactive_controls(self):
        for name, permitted, expected in (
            ("forbidden", {"update_plan", "read_file"}, "unknown tool"),
            ("request_question", {"update_plan", "request_question"}, "separate"),
        ):
            with self.subTest(name=name):
                runtime = self.runtime(complexity="complex")
                runtime.tasks.working_plan = SimpleNamespace(id="plan", status="active", task_ids=[])
                runtime._permitted_tool_names = lambda: permitted
                runtime._get_model_response.return_value = ModelResponse(tool_calls=(
                    ToolCall("plan", "update_plan", {"expected_revision": 1, "operations": []}),
                    ToolCall("other", name, {}),
                ), finish_reason=FinishReason.TOOL_REQUEST)
                answer = AgentEngine(runtime).run(TurnContext("Inspect the task"))
                self.assertIn(expected, answer)
                runtime._get_model_response.assert_called_once()
                runtime._execute_tool_batch.assert_not_called()

    def test_native_plan_start_and_completion_suggestion_bind_verified_receipts(self):
        import time
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.tools import ToolAdapters
        command = "python -m unittest test_note"
        class PlanningProvider(LLMProvider):
            def get_capabilities(self, model=None):
                return ProviderCapabilities(native_tools=True)
            def chat(self, messages, model=None):
                raise AssertionError("legacy boundary used")
            def chat_response(self, messages, model=None, *, tools=None):
                previous = [message for message in messages if isinstance(message, ToolReply)]
                if not previous:
                    call = ToolCall("plan", "update_plan", {"complexity": "complex", "expected_revision": 0, "operations": [
                        {"op": "add", "ref": "verify", "task": {"description": "Verify note.txt contents", "acceptance": [command],
                         "execution": {"action": "run_cmd", "arguments": {"command": command}}}},
                        {"op": "start", "task": "verify"}, {"op": "suggest_complete", "task": "verify"},
                    ]})
                elif not any(message.call_id == "verify-command" for message in previous):
                    call = ToolCall("verify-command", "run_cmd", {"command": command})
                else:
                    return ModelResponse(text="The requested file contents passed the check.", finish_reason=FinishReason.FINAL)
                return ModelResponse(tool_calls=(call,), finish_reason=FinishReason.TOOL_REQUEST,
                                     continuation=provider_continuation(self, model, "fixture", {"call": call.id}))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_APPROVAL_MODE": "never",
            "PATH": str(Path(__import__("sys").executable).parent) + os.pathsep + os.environ["PATH"],
        }), ExitStack() as sqlite_owners:
            root = Path(directory)
            (root / "note.txt").write_text("verified content", encoding="utf-8")
            (root / "test_note.py").write_text(
                "import unittest\nfrom pathlib import Path\nclass NoteTest(unittest.TestCase):\n"
                "    def test_content(self):\n        self.assertEqual(Path('note.txt').read_text(), 'verified content')\n",
                encoding="utf-8")
            app = build_application(surface="cli", memory=sqlite_owners.enter_context(closing(build_memory(root / "state.sqlite3", index=None))), tools=ToolAdapters(root))
            self.addCleanup(app.close)
            runtime = app.runtime
            provider = PlanningProvider(ProviderConfig(provider="custom", model_simple="fixture"))
            runtime._set_workspace_root(root)
            runtime.llm_provider = runtime._default_turn.provider = provider
            runtime.set_interaction_mode("agent")
            def prepare(turn):
                turn.interaction_mode, turn.complexity = "agent", "complex"
                turn.learning_run, turn.turn_start = {"run_id": "fixture"}, time.time()
                turn.learned_context = ""
            with patch.object(runtime, "_prepare_turn", side_effect=prepare), \
                    patch.object(runtime, "_finish_learning_run", side_effect=lambda _run, _receipts, _task, answer, *_args: answer), \
                    patch.object(runtime, "_schedule_post_turn_learning"):
                answer = runtime.chat(runtime.current_session, "Verify the requested file contents")
            task = runtime.tasks.tasks[0]
            self.assertEqual(task["status"], "succeeded", answer)
            self.assertEqual(runtime.tasks.working_plan.status, "completed")
            self.assertTrue(task["evidence"])
            receipts = runtime.memory_bank.store.list_events("execution.receipt", user_id=runtime.tasks.user_id,
                workspace_id=runtime.tasks.workspace_id, session_id=runtime.tasks.session_id)
            verification = next(event["payload"] for event in receipts if event["payload"]["call_id"] == "verify-command")
            self.assertEqual(verification["task_id"], task["id"])
            self.assertEqual(verification["verification"]["status"], "verified")
            self.assertIsNone(next(event["payload"] for event in receipts if event["payload"]["call_id"] == "plan")["task_id"])

    def test_native_verified_write_receipts_stay_local_without_certifying_prose_acceptance(self):
        import copy
        import time
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.tools import ToolAdapters
        for delegated in (False, True):
            with self.subTest(delegated=delegated), tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_APPROVAL_MODE": "never",
            }), ExitStack() as sqlite_owners:
                root = Path(directory)
                app = build_application(surface="cli", memory=sqlite_owners.enter_context(closing(build_memory(root / "state.sqlite3", index=None))), tools=ToolAdapters(root))
                self.addCleanup(app.close)
                runtime = app.runtime
                runtime._set_workspace_root(root)
                parent = runtime.current_session
                parent.tasks.update_plan(complexity="complex", tasks=[{
                    "description": "Independently verify the parent objective", "acceptance": ["Independent review passed"],
                }])
                target = runtime.open_session("child-session") if delegated else parent
                if delegated:
                    target._delegation_parent, target._delegation_task_id = parent, parent.tasks.tasks[0]["id"]
                else:
                    parent.tasks.clear()
                target.interaction.set_mode("agent")
                class WriteProvider(LLMProvider):
                    def get_capabilities(self, model=None):
                        return ProviderCapabilities(native_tools=True)
                    def chat(self, messages, model=None):
                        raise AssertionError("legacy boundary used")
                    def chat_response(self, messages, model=None, *, tools=None):
                        replies = [item for item in messages if isinstance(item, ToolReply)]
                        arguments = {"path": "child.txt", "content": "precisely verified content"}
                        if not replies:
                            call = ToolCall("plan", "update_plan", {"complexity": "complex", "expected_revision": 0, "operations": [
                                {"op": "add", "ref": "write", "task": {"description": "Write the exact requested child.txt content",
                                 "acceptance": ["Exact requested contents were written"],
                                 "execution": {"action": "write_file", "arguments": arguments}}},
                                {"op": "start", "task": "write"}, {"op": "suggest_complete", "task": "write"},
                            ]})
                        elif not any(item.call_id == "write" for item in replies):
                            call = ToolCall("write", "write_file", arguments)
                        else:
                            return ModelResponse(text="The requested file contents were verified.", finish_reason=FinishReason.FINAL)
                        return ModelResponse(tool_calls=(call,), finish_reason=FinishReason.TOOL_REQUEST,
                            continuation=provider_continuation(self, model, "fixture", {"call": call.id}))
                provider = WriteProvider(ProviderConfig(provider="custom", model_simple="fixture"))
                context = copy.copy(runtime.execution_context)
                context.provider = provider
                context.child_run_id = "child-run" if delegated else None
                token = runtime._turn_context.set(context)
                self.addCleanup(runtime._turn_context.reset, token)
                def prepare(turn):
                    turn.interaction_mode, turn.complexity = "agent", "complex"
                    turn.learning_run, turn.turn_start = {"run_id": "fixture"}, time.time()
                    turn.learned_context = ""
                with patch.object(runtime, "_prepare_turn", side_effect=prepare), \
                        patch.object(runtime, "_finish_learning_run", side_effect=lambda _run, _receipts, _task, answer, *_args: answer), \
                        patch.object(runtime, "_schedule_post_turn_learning"):
                    answer = runtime.chat(target, "Write and verify the exact requested content")
                self.assertEqual((root / "child.txt").read_text(), "precisely verified content")
                self.assertEqual(target.tasks.tasks[0]["status"], "running", answer)
                self.assertEqual(target.tasks.working_plan.status, "active")
                self.assertTrue(target.tasks.tasks[0]["evidence"])
                self.assertIsNone(target.tasks.tasks[0]["evidence"][-1].get("acceptance"))
                receipts = target.memory.store.list_events("execution.receipt", user_id=target.tasks.user_id,
                    workspace_id=target.tasks.workspace_id, session_id=target.session_id)
                write = next(event["payload"] for event in receipts if event["payload"]["call_id"] == "write")
                self.assertEqual((write["plan_id"], write["task_id"]), (target.tasks.working_plan.id, target.tasks.tasks[0]["id"]))
                self.assertEqual(write["verification"]["status"], "verified")
                if delegated:
                    self.assertEqual(parent.tasks.tasks[0]["status"], "pending")
                    self.assertEqual(parent.tasks.tasks[0]["evidence"], [])

    def test_native_foreground_stream_delivers_text_before_generation_finishes(self):
        import time
        import threading
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.tools import ToolAdapters
        from openkyrozen.providers.models import TextDelta, ResponseComplete
        events = []
        delta_delivered = threading.Event()
        def observe(event):
            events.append(event)
            if event.kind == "model.text_delta":
                delta_delivered.set()
        class StreamingProvider(LLMProvider):
            def get_capabilities(self, model=None):
                return ProviderCapabilities(native_tools=True, text_streaming=True, streaming_tool_calls=True)
            def chat(self, messages, model=None):
                raise AssertionError("text provider boundary used")
            def chat_response(self, messages, model=None, *, tools=None):
                raise AssertionError("native foreground response was buffered")
            def chat_response_stream(self, messages, model=None, *, tools=None):
                yield TextDelta("First useful text. ")
                if not delta_delivered.wait(timeout=1):
                    raise AssertionError("first text was withheld until completion")
                yield ResponseComplete(ModelResponse(text="First useful text. Finished.", finish_reason=FinishReason.FINAL))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"KYROZEN_DISABLE_VECTOR_INDEX": "1"}), ExitStack() as sqlite_owners:
            root = Path(directory)
            app = build_application(surface="cli", memory=sqlite_owners.enter_context(closing(build_memory(root / "state.sqlite3", index=None))), tools=ToolAdapters(root))
            self.addCleanup(app.close)
            runtime = app.runtime
            provider = StreamingProvider(ProviderConfig(provider="custom", model_simple="fixture"))
            runtime.llm_provider = runtime._default_turn.provider = provider
            runtime.set_interaction_mode("agent")
            def prepare(turn):
                turn.interaction_mode, turn.complexity = "agent", "simple"
                turn.learning_run, turn.turn_start = {"run_id": "fixture"}, time.time()
                turn.learned_context = ""
            with patch.object(runtime, "_prepare_turn", side_effect=prepare), \
                    patch.object(runtime, "_finish_learning_run", side_effect=lambda _run, _receipts, _task, answer, *_args: answer), \
                    patch.object(runtime, "_schedule_post_turn_learning"):
                answer = runtime.chat(runtime.current_session, "Explain the result", on_runtime_event=observe)
            self.assertEqual(answer, "First useful text. Finished.")
            self.assertTrue(any(event.kind == "model.text_delta" for event in events))
            self.assertLess(next(event.sequence for event in events if event.kind == "model.text_delta"),
                            next(event.sequence for event in events if event.kind == "model.completed"))


if __name__ == "__main__":
    unittest.main()
