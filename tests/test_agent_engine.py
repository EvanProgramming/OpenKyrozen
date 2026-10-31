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
                if not isinstance(messages[-1], ToolReply) or messages[-1].call_id != "read-1":
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
        }):
            root = Path(directory)
            (root / "note.txt").write_text("verified text", encoding="utf-8")
            app = build_application(
                surface="cli", memory=build_memory(root / "state.sqlite3", user_id="alice", index=None),
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

        self.assertIn("verified text", answer)
        retrieve_memory.assert_called_once()
        self.assertEqual(provider.calls_attempted, 3)
        self.assertEqual(len(provider.requests), 3)
        self.assertTrue(all(any(packet in item.get("content", "") for item in request[0]
                                if isinstance(item, dict)) for request in provider.requests))
        self.assertEqual(provider.requests[1][0][-1].call_id, "plan-1")
        self.assertEqual(provider.requests[2][0][-1].call_id, "read-1")
        self.assertEqual(app.runtime.tasks.working_plan.complexity, "complex")
        self.assertEqual(app.runtime.tasks.tasks[0]["description"], "Read note.txt")


if __name__ == "__main__":
    unittest.main()
