"""Offline tests for schema-backed native provider requests and replay history."""
import unittest
import json
import sys
from contextlib import contextmanager, nullcontext
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import Mock, patch

from openkyrozen.providers.models import (
    FinishReason, ModelResponse, ProviderContinuation, ProviderContractError,
    ToolCall, ToolReply, validate_tool_history,
)


@contextmanager
def _google_schema_sdk():
    try:
        import google.genai  # noqa: F401
    except ImportError:
        class Schema:
            @staticmethod
            def model_validate(value):
                return value

        package = ModuleType("google")
        package.__path__ = []
        genai = ModuleType("google.genai")
        genai.types = NS(Schema=Schema)
        package.genai = genai
        with patch.dict(sys.modules, {"google": package, "google.genai": genai}):
            yield
    else:
        with nullcontext():
            yield


class NativeHistoryTests(unittest.TestCase):
    def test_reply_and_continuation_are_defensive_json_values(self):
        output = {"data": {"text": "雪|\"quoted\""}, "error": None}
        payload = {"assistant": [{"type": "function_call", "id": "item"}]}
        reply = ToolReply("call-1", "read_file", output)
        continuation = ProviderContinuation("provider-instance", "model", "responses", payload)
        output["data"]["text"] = "mutated"
        payload["assistant"].clear()
        self.assertEqual(reply.output["data"]["text"], "雪|\"quoted\"")
        self.assertEqual(continuation.payload["assistant"][0]["id"], "item")
        with self.assertRaises(ProviderContractError):
            ToolReply("call-1", "read_file", {"bad": object()})

    def test_native_call_requires_exact_contiguous_matching_replies(self):
        continuation = ProviderContinuation("provider-instance", "model", "fixture", {"assistant": []})
        response = ModelResponse(tool_calls=(ToolCall("one", "read", {}), ToolCall("two", "write", {})),
                                 finish_reason=FinishReason.TOOL_REQUEST, continuation=continuation)
        valid = [response, ToolReply("one", "read", {}), ToolReply("two", "write", {})]
        validate_tool_history(valid, provider_id="provider-instance", model="model")
        cases = [
            [response],
            [response, ToolReply("two", "write", {}), ToolReply("one", "read", {})],
            [response, ToolReply("one", "wrong", {}), ToolReply("two", "write", {})],
            [response, ToolReply("one", "read", {}), {"role": "user", "content": "interleaved"},
             ToolReply("two", "write", {})],
            [ToolReply("one", "read", {})],
            [response, ToolReply("one", "read", {}), ToolReply("one", "read", {})],
        ]
        for history in cases:
            with self.subTest(history=history), self.assertRaises(ProviderContractError):
                validate_tool_history(history, provider_id="provider-instance", model="model")

    def test_continuation_is_rejected_for_another_provider_or_model(self):
        continuation = ProviderContinuation("provider-instance", "model", "fixture", {"assistant": []})
        response = ModelResponse(text="answer", continuation=continuation)
        for provider_id, model in (("other", "model"), ("provider-instance", "other-model")):
            with self.subTest(provider_id=provider_id, model=model), self.assertRaises(ProviderContractError):
                validate_tool_history([response], provider_id=provider_id, model=model)


class NativeRequestTests(unittest.TestCase):
    def test_openai_chat_sends_registry_schemas_and_correlated_results(self):
        from openkyrozen.providers.openai import OpenAICompatProvider
        provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
        provider.config = NS(provider="custom", model_simple="model")
        call = NS(id="call", type="function", function=NS(
            name="read_file", arguments='{"path":"snow | \\"quoted\\""}'))
        message = NS(role="assistant", content=None, tool_calls=[call])
        choice = NS(finish_reason="tool_calls", message=message)
        response = NS(id="r", model="model", usage=None, choices=[choice])
        send = Mock(return_value=response)
        provider._client = NS(chat=NS(completions=NS(create=send)))
        schema = {"type": "object", "properties": {"path": {"type": "string"},
                  "note": {"type": "string", "description": "Optional note"}},
                  "required": ["path"], "additionalProperties": False}
        with patch("openkyrozen.providers.usage._track_cost"):
            result = provider.chat_response([{"role": "user", "content": "inspect"}], tools=[
                {"name": "read_file", "description": "Read a file", "parameters": schema},
            ])
        self.assertEqual(send.call_args.kwargs["tools"][0]["function"]["parameters"], schema)
        self.assertNotIn("strict", send.call_args.kwargs["tools"][0]["function"])
        self.assertEqual(result.tool_calls[0].arguments, {"path": 'snow | "quoted"'})
        self.assertIsNotNone(result.continuation)

    def test_custom_gemini_gateway_receives_correlated_openai_tool_history(self):
        """Regression for #225; logs assertions about shape, never message text or credentials."""
        from openkyrozen.providers.openai import OpenAICompatProvider
        provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
        provider.config = NS(provider="custom", model_simple="gemini-3-flash", base_url="https://gateway.invalid/v1")
        provider._continuation_id = "custom-gateway-instance"
        first_message = NS(role="assistant", content=None, tool_calls=[NS(
            id="search-call", type="function", function=NS(name="search_web", arguments='{"query":"redacted"}'))])
        first = NS(id="response-1", model="gemini-3-flash", usage=None,
                   choices=[NS(finish_reason="tool_calls", message=first_message)])
        final = NS(id="response-2", model="gemini-3-flash", usage=None,
                   choices=[NS(finish_reason="stop", message=NS(role="assistant", content="continued"))])
        send = Mock(side_effect=[first, final])
        provider._client = NS(chat=NS(completions=NS(create=send)))
        tools = [{"name": "search_web", "description": "Search", "parameters": {
            "type": "object", "properties": {"query": {"type": "string"}},
            "required": ["query"], "additionalProperties": False}}]
        with patch("openkyrozen.providers.usage._track_cost"):
            response = provider.chat_response([{"role": "user", "content": "search request"}],
                                             "gemini-3-flash", tools=tools)
            response = provider.chat_response([response, ToolReply("search-call", "search_web",
                {"data": {"results": ["redacted"]}})], "gemini-3-flash", tools=tools)
        self.assertEqual(response.text, "continued")
        sent = send.call_args_list[1].kwargs["messages"]
        self.assertEqual([item["role"] for item in sent], ["assistant", "tool"])
        self.assertEqual(sent[0]["tool_calls"][0]["id"], "search-call")
        self.assertEqual(sent[1]["tool_call_id"], "search-call")

    def test_runtime_passes_only_requested_capability_permitted_registry_schemas(self):
        import tempfile
        from pathlib import Path
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.providers.base import LLMProvider
        from openkyrozen.providers.config import ProviderConfig

        class CaptureProvider(LLMProvider):
            def chat(self, messages, model=None): return "legacy", None
            def chat_response(self, messages, model=None, *, tools=None):
                self.tools = tools
                return ModelResponse(text="ready")

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"KYROZEN_DISABLE_VECTOR_INDEX": "1"}):
            app = build_application(memory=build_memory(Path(directory) / "state.sqlite3"))
            try:
                provider = CaptureProvider(ProviderConfig(provider="custom", model_simple="model"))
                app.runtime.llm_provider = provider
                result = app.runtime._get_model_response([], tool_names=["read_file", "unknown"])
                self.assertEqual(result.text, "ready")
                self.assertEqual([tool["name"] for tool in provider.tools], ["read_file"])
                self.assertEqual(set(provider.tools[0]), {"name", "description", "parameters"})
                with patch.object(app.runtime, "_effective_execution_capabilities", return_value=frozenset({"read"})):
                    app.runtime._get_model_response(
                        [], tool_names=["read_file"], history_tool_names={"write_file"},
                    )
                self.assertEqual([tool["name"] for tool in provider.tools], ["read_file", "write_file"])
            finally:
                app.close()

    def test_fallback_pins_tool_continuation_to_the_responding_provider(self):
        from openkyrozen.providers.base import LLMProvider
        from openkyrozen.providers.config import ProviderConfig
        from openkyrozen.providers.fallback import FallbackProvider
        from openkyrozen.providers.models import with_provider_continuation

        class Probe(LLMProvider):
            def __init__(self, provider, response=None):
                super().__init__(ProviderConfig(provider=provider, model_simple=f"{provider}-model"))
                self.responses = response
                self.calls = []
            def chat(self, messages, model=None): return "", None
            def chat_response(self, messages, model=None, *, tools=None):
                self.calls.append(model)
                if self.responses is not None:
                    return self.responses
                if any(isinstance(message, ToolReply) for message in messages):
                    return ModelResponse(text="continued")
                pending = ModelResponse(tool_calls=(ToolCall("pinned-call", "read_file", {"path": "a"}),),
                    finish_reason=FinishReason.TOOL_REQUEST)
                return with_provider_continuation(pending, self, model, "probe", {"assistant": {"opaque": True}})

        primary, fallback = Probe("deepseek"), Probe("openai", ModelResponse(text="wrong provider"))
        wrapper = FallbackProvider.__new__(FallbackProvider)
        wrapper._primary, wrapper._fallbacks = primary, [fallback]
        tools = [{"name": "read_file", "description": "Read", "parameters": {"type": "object"}}]
        first = wrapper.chat_response([], "deepseek-model", tools=tools)
        reply = ToolReply(first.tool_calls[0].id, first.tool_calls[0].name, {"result": "ok"})
        final = wrapper.chat_response([first, reply], "deepseek-model", tools=tools)
        self.assertEqual(final.text, "continued")
        self.assertEqual(primary.calls, ["deepseek-model", "deepseek-model"])
        self.assertEqual(fallback.calls, [])

    def test_fallback_pins_final_continuation_without_tool_calls(self):
        from openkyrozen.providers.base import LLMProvider
        from openkyrozen.providers.config import ProviderConfig
        from openkyrozen.providers.fallback import FallbackProvider
        from openkyrozen.providers.models import with_provider_continuation

        class Probe(LLMProvider):
            def __init__(self, provider, response=None):
                super().__init__(ProviderConfig(provider=provider, model_simple=f"{provider}-model"))
                self.response = response
                self.calls = []
            def chat(self, messages, model=None): return "", None
            def chat_response(self, messages, model=None, *, tools=None):
                self.calls.append(model)
                return self.response or ModelResponse(text="reply")

        primary, fallback = Probe("deepseek"), Probe("openai")
        first = with_provider_continuation(
            ModelResponse(text="fallback answer"), fallback, "openai-model", "probe", {"opaque": True},
        )
        wrapper = FallbackProvider.__new__(FallbackProvider)
        wrapper._primary, wrapper._fallbacks = primary, [fallback]
        wrapper.chat_response([first, {"role": "user", "content": "next turn"}], "deepseek-model")
        self.assertEqual(primary.calls, [])
        self.assertEqual(fallback.calls, ["openai-model"])

    def test_native_request_executes_through_runtime_without_action_parsing(self):
        import tempfile
        from pathlib import Path
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.tools import ToolAdapters
        from openkyrozen.providers.openai import OpenAICompatProvider

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"KYROZEN_DISABLE_VECTOR_INDEX": "1"}):
            workspace = Path(directory)
            (workspace / "note.txt").write_text("native result | 雪", encoding="utf-8")
            adapters = ToolAdapters(str(workspace))
            app = build_application(memory=build_memory(workspace / "state.sqlite3"), tools=adapters)
            runtime = app.runtime
            from openkyrozen.agent.compaction import ContextState
            runtime._active_context_state.set(ContextState("gemini-3-flash"))
            runtime.current_session.workspace.adapters.set_workspace_root(str(workspace))
            provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
            provider.config = NS(provider="custom", model_simple="gemini-3-flash")
            provider._continuation_id = "runtime-provider"
            first_message = NS(role="assistant", content=None, tool_calls=[NS(
                id="runtime-read", type="function", function=NS(name="read_file", arguments='{"path":"note.txt"}'))])
            first = NS(id="response-1", model="gemini-3-flash", usage=None,
                       choices=[NS(finish_reason="tool_calls", message=first_message)])
            final = NS(id="response-2", model="gemini-3-flash", usage=None,
                       choices=[NS(finish_reason="stop", message=NS(role="assistant", content="finished"))])
            send = Mock(side_effect=[first, final])
            provider._client = NS(chat=NS(completions=NS(create=send)))
            runtime.llm_provider = provider
            try:
                with patch("openkyrozen.providers.usage._track_cost"), \
                     patch.object(runtime, "_parse_model_response", side_effect=AssertionError("text parser called")), \
                     patch.object(runtime, "_observe_model_response", side_effect=AssertionError("text parser called")):
                    typed = runtime._get_model_response([{"role": "user", "content": "read note.txt"}],
                                                        "gemini-3-flash", tool_names=["read_file"])
                    receipt = runtime.execute_call(runtime.current_session, typed.tool_calls[0],
                        capabilities={"read"}, approve=lambda request: True)
                    self.assertTrue(receipt.success, receipt.as_dict())
                    reply = ToolReply(typed.tool_calls[0].id, typed.tool_calls[0].name,
                        {"success": receipt.success, "result": receipt.result, "error": receipt.tool_error},
                        is_error=not receipt.success)
                    finished = runtime._get_model_response(
                        [{"role": "user", "content": "read note.txt"}, typed, reply],
                        "gemini-3-flash", tool_names=["read_file"])
                self.assertEqual(finished.text, "finished")
                sent = send.call_args_list[1].kwargs["messages"]
                self.assertEqual(sent[-1]["tool_call_id"], "runtime-read")
                self.assertIn("native result | 雪", json.loads(sent[-1]["content"])["result"]["result"])
            finally:
                app.close()

    def test_legacy_openai_function_call_uses_function_reply_protocol(self):
        from openkyrozen.providers.openai import OpenAICompatProvider
        provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
        provider.config = NS(provider="custom", model_simple="model")
        first = NS(id="r1", model="model", usage=None, choices=[NS(finish_reason="function_call",
            message=NS(role="assistant", content=None, function_call=NS(name="lookup", arguments='{}')))])
        final = NS(id="r2", model="model", usage=None,
            choices=[NS(finish_reason="stop", message=NS(role="assistant", content="done"))])
        send = Mock(side_effect=[first, final])
        provider._client = NS(chat=NS(completions=NS(create=send)))
        tools = [{"name": "lookup", "description": "Lookup", "parameters": {"type": "object"}}]
        with patch("openkyrozen.providers.usage._track_cost"):
            response = provider.chat_response([], tools=tools)
            provider.chat_response([response, ToolReply(response.tool_calls[0].id, "lookup", {"ok": True})], tools=tools)
        self.assertEqual(send.call_args_list[1].kwargs["messages"][-1], {
            "role": "function", "name": "lookup", "content": '{"is_error": false, "result": {"ok": true}}'})

    def test_openai_continuation_strips_output_only_message_fields(self):
        from openkyrozen.providers.openai import OpenAICompatProvider
        provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
        provider.config = NS(provider="custom", model_simple="model")
        message = NS(role="assistant", content=None, annotations=[{"type": "url_citation"}],
                   audio=NS(id="audio-id", data="not-replayable", transcript="spoken"),
                   tool_calls=[NS(id="c", type="function", function=NS(name="lookup", arguments="{}"))])
        first = NS(id="r1", model="model", usage=None,
            choices=[NS(finish_reason="tool_calls", message=message)])
        final = NS(id="r2", model="model", usage=None,
            choices=[NS(finish_reason="stop", message=NS(role="assistant", content="done"))])
        send = Mock(side_effect=[first, final])
        provider._client = NS(chat=NS(completions=NS(create=send)))
        tools = [{"name": "lookup", "description": "Lookup", "parameters": {"type": "object"}}]
        with patch("openkyrozen.providers.usage._track_cost"):
            response = provider.chat_response([], tools=tools)
            provider.chat_response([response, ToolReply("c", "lookup", {"ok": True})], tools=tools)
        replay = send.call_args_list[1].kwargs["messages"][0]
        self.assertNotIn("annotations", replay)
        self.assertEqual(replay["audio"], {"id": "audio-id"})

    def test_bedrock_tool_error_status_is_limited_to_supported_model_families(self):
        from openkyrozen.providers.bedrock import BedrockProvider
        from openkyrozen.providers.models import with_provider_continuation
        provider = BedrockProvider.__new__(BedrockProvider)
        response = with_provider_continuation(ModelResponse(tool_calls=(ToolCall("c", "lookup", {}),),
            finish_reason=FinishReason.TOOL_REQUEST), provider, "anthropic.claude-3-sonnet", "bedrock-converse",
            {"content": [{"toolUse": {"toolUseId": "c", "name": "lookup", "input": {}}}]})
        reply = ToolReply("c", "lookup", {"error": {"code": "execution_failed"}}, is_error=True)
        for model, expected in (("anthropic.claude-3-sonnet", "error"),
                                ("anthropic.claude-sonnet-4", "error"),
                                ("account.custom-model", None)):
            with self.subTest(model=model):
                conversation, _ = BedrockProvider._request([response, reply], model, provider)
                result = conversation[-1]["content"][0]["toolResult"]
                self.assertEqual(result.get("status"), expected)

    def test_context_accounting_accepts_typed_native_history(self):
        from openkyrozen.agent.compaction import ContextState
        from openkyrozen.providers.context import context_accounting_messages
        response = ModelResponse(text="", tool_calls=(ToolCall("c", "read_file", {"path": "note.txt"}),),
                                 finish_reason=FinishReason.TOOL_REQUEST)
        messages = context_accounting_messages([
            {"role": "user", "content": "read note.txt"}, response,
            ToolReply("c", "read_file", {"data": "contents"}),
        ])
        state = ContextState("test-model")
        state.note_provider_usage(messages, 42)
        self.assertEqual(state.status["input_tokens"], 42)
        self.assertEqual(len(state.status["breakdown"]), 5)

    def test_google_omits_synthetic_call_id_from_function_response(self):
        from openkyrozen.providers.google import GoogleProvider
        from openkyrozen.providers.models import ProviderContinuation
        response = ModelResponse(tool_calls=(ToolCall("synthetic", "lookup", {}),),
            finish_reason=FinishReason.TOOL_REQUEST, metadata={"synthesized_call_ids": ["synthetic"]},
            continuation=ProviderContinuation("p", "m", "google-genai", {"parts": [{"function_call": {"name": "lookup"}}]}))
        contents, _ = GoogleProvider._contents([response, ToolReply("synthetic", "lookup", {"ok": True})], "m", None)
        function_response = contents[-1]["parts"][0]["function_response"]
        self.assertNotIn("id", function_response)
        self.assertEqual(function_response["name"], "lookup")

    def test_native_history_mismatch_is_rejected_before_transport(self):
        from openkyrozen.providers.openai import OpenAICompatProvider
        provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
        provider.config = NS(provider="custom", model_simple="model")
        send = Mock()
        provider._client = NS(chat=NS(completions=NS(create=send)))
        call = ToolCall("call", "read_file", {})
        continuation = ProviderContinuation("provider-instance", "model", "openai-chat", {"assistant": {}})
        response = ModelResponse(tool_calls=(call,), finish_reason=FinishReason.TOOL_REQUEST,
                                 continuation=continuation)
        with self.assertRaises(ProviderContractError):
            provider.chat_response([response, ToolReply("orphan", "read_file", {})], tools=[])
        send.assert_not_called()

    def test_every_adapter_replays_native_history_with_correlated_error_results(self):
        from openkyrozen.providers.config import ProviderConfig
        from openkyrozen.providers.openai import OpenAICompatProvider, OpenAIResponsesProvider
        from openkyrozen.providers.azure import AzureOpenAIProvider
        from openkyrozen.providers.anthropic import AnthropicProvider
        from openkyrozen.providers.google import GoogleProvider, VertexProvider
        from openkyrozen.providers.bedrock import BedrockProvider
        from openkyrozen.providers.ollama import OllamaNativeProvider
        from openkyrozen.providers.perplexity import PerplexityProvider

        schema = {"type": "object", "properties": {"path": {"type": "string"}},
                  "required": ["path"], "additionalProperties": False}
        tools = [{"name": "read_file", "description": "Read a file", "parameters": schema}]
        call = ToolCall("native-call", "read_file", {"path": "a|雪"})
        reply = ToolReply(call.id, call.name, {"error": {"code": "missing_resource"}}, is_error=True)
        cases = [
            (OpenAICompatProvider, "deepseek", "chat", {
                "id": "r1", "model": "m", "usage": None,
                "choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                    "tool_calls": [{"id": call.id, "type": "function", "function": {"name": call.name,
                    "arguments": '{"path":"a|雪"}'}}]}}]},
             {"id": "r2", "model": "m", "usage": None,
              "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}]}),
            (AzureOpenAIProvider, "azure_openai", "chat", {
                "id": "r1", "model": "m", "usage": None,
                "choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                    "tool_calls": [{"id": call.id, "type": "function", "function": {"name": call.name,
                    "arguments": '{"path":"a|雪"}'}}]}}]},
             {"id": "r2", "model": "m", "usage": None,
              "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}]}),
            (OpenAIResponsesProvider, "openai", "responses", {
                "id": "r1", "model": "m", "status": "completed", "output_text": "", "usage": None,
                "output": [{"id": "item", "type": "function_call", "call_id": call.id, "name": call.name,
                            "arguments": '{"path":"a|雪"}'}]},
             {"id": "r2", "model": "m", "status": "completed", "output_text": "done", "usage": None, "output": []}),
            (PerplexityProvider, "perplexity", "responses", {
                "id": "r1", "model": "m", "status": "completed", "output_text": "", "usage": None,
                "output": [{"id": "item", "type": "function_call", "call_id": call.id, "name": call.name,
                            "arguments": '{"path":"a|雪"}'}]},
             {"id": "r2", "model": "m", "status": "completed", "output_text": "done", "usage": None, "output": []}),
            (AnthropicProvider, "anthropic", "messages", {
                "id": "r1", "model": "m", "stop_reason": "tool_use", "usage": None,
                "content": [{"type": "tool_use", "id": call.id, "name": call.name, "input": {"path": "a|雪"}}]},
             {"id": "r2", "model": "m", "stop_reason": "end_turn", "usage": None,
              "content": [{"type": "text", "text": "done"}]}),
            (GoogleProvider, "google", "google", {
                "response_id": "r1", "model_version": "m", "usage_metadata": None,
                "candidates": [{"finish_reason": "STOP", "content": {"role": "model", "parts": [
                    {"function_call": {"id": call.id, "name": call.name, "args": {"path": "a|雪"}},
                     "thought_signature": "opaque-signature"}]}}]},
             {"response_id": "r2", "model_version": "m", "usage_metadata": None,
              "candidates": [{"finish_reason": "STOP", "content": {"role": "model", "parts": [{"text": "done"}]}}]}),
            (VertexProvider, "vertex", "google", {
                "response_id": "r1", "model_version": "m", "usage_metadata": None,
                "candidates": [{"finish_reason": "STOP", "content": {"role": "model", "parts": [
                    {"function_call": {"id": call.id, "name": call.name, "args": {"path": "a|雪"}},
                     "thought_signature": "opaque-signature"}]}}]},
             {"response_id": "r2", "model_version": "m", "usage_metadata": None,
              "candidates": [{"finish_reason": "STOP", "content": {"role": "model", "parts": [{"text": "done"}]}}]}),
            (BedrockProvider, "bedrock", "bedrock", {
                "output": {"message": {"content": [{"toolUse": {"toolUseId": call.id, "name": call.name,
                    "input": {"path": "a|雪"}}}]}}, "stopReason": "tool_use"},
             {"output": {"message": {"content": [{"text": "done"}]}, "stopReason": "end_turn"}}),
            (OllamaNativeProvider, "ollama", "ollama", {
                "message": {"role": "assistant", "content": "", "tool_calls": [{"id": call.id,
                    "function": {"name": call.name, "arguments": {"path": "a|雪"}}}]}, "done_reason": "stop"},
             {"message": {"role": "assistant", "content": "done"}, "done_reason": "stop"}),
        ]

        class Response:
            def __init__(self, body): self.body = body
            def raise_for_status(self): pass
            def json(self): return self.body
            def close(self): pass

        def sdk_value(value):
            if isinstance(value, dict):
                return NS(**{key: (item if key in {"input", "args", "arguments"} else sdk_value(item))
                             for key, item in value.items()})
            if isinstance(value, list): return [sdk_value(item) for item in value]
            return value

        for cls, provider_name, transport, first, final in cases:
            with self.subTest(adapter=cls.__name__), patch("openkyrozen.providers.usage._track_cost"):
                provider = cls.__new__(cls)
                provider.config = ProviderConfig(provider=provider_name, model_simple="m")
                provider._continuation_id = "same-provider"
                if transport == "chat":
                    send = Mock(side_effect=[sdk_value(first), sdk_value(final)])
                    provider._client = NS(chat=NS(completions=NS(create=send)))
                elif transport == "responses":
                    send = Mock(side_effect=[sdk_value(first), sdk_value(final)])
                    provider._client = NS(responses=NS(create=send))
                elif transport == "messages":
                    send = Mock(side_effect=[sdk_value(first), sdk_value(final)])
                    provider._client = NS(messages=NS(create=send))
                elif transport == "google":
                    send = Mock(side_effect=[sdk_value(first), sdk_value(final)])
                    provider._client = NS(models=NS(generate_content=send))
                elif transport == "bedrock":
                    send = Mock(side_effect=[first, final])
                    provider._client = NS(converse=send)
                else:
                    send = Mock(side_effect=[Response(first), Response(final)])
                    provider._requests = NS(post=send)
                    provider._base = "http://localhost:11434"
                with _google_schema_sdk() if transport == "google" else nullcontext():
                    initial = provider.chat_response([{"role": "user", "content": "go"}], tools=tools)
                self.assertEqual(initial.tool_calls, (call,))
                request = send.call_args_list[0].kwargs
                if transport == "chat":
                    self.assertEqual(request["tools"][0]["function"]["parameters"], schema)
                    self.assertNotIn("strict", request["tools"][0]["function"])
                elif transport == "responses":
                    self.assertEqual(request["tools"][0]["parameters"], schema)
                    self.assertIs(request["tools"][0]["strict"], False)
                    if cls is PerplexityProvider:
                        self.assertEqual(request["input"][0], {"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": "go"}]})
                elif transport == "messages":
                    self.assertEqual(request["tools"][0]["input_schema"], schema)
                elif transport == "google":
                    self.assertEqual(request["config"]["tools"][0]["function_declarations"][0]["parameters"], schema)
                elif transport == "bedrock":
                    self.assertEqual(request["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"], schema)
                elif transport == "ollama":
                    self.assertEqual(request["json"]["tools"][0]["function"]["parameters"], schema)
                with _google_schema_sdk() if transport == "google" else nullcontext():
                    final_response = provider.chat_response([initial, reply], tools=tools)
                self.assertEqual(final_response.text, "done")
                kwargs = send.call_args_list[1].kwargs
                if transport == "google":
                    replay = kwargs["contents"]
                    self.assertEqual(replay[0]["parts"][0]["thought_signature"], "opaque-signature")
                if transport == "chat":
                    self.assertEqual(kwargs["messages"][-1]["tool_call_id"], call.id)
                    self.assertTrue(json.loads(kwargs["messages"][-1]["content"])["is_error"])
                elif transport == "responses":
                    self.assertEqual(kwargs["input"][-1]["call_id"], call.id)
                    self.assertTrue(json.loads(kwargs["input"][-1]["output"])["is_error"])
                elif transport == "messages":
                    block = kwargs["messages"][-1]["content"][0]
                    self.assertEqual(block["tool_use_id"], call.id)
                    self.assertTrue(block["is_error"])
                elif transport == "google":
                    part = kwargs["contents"][-1]["parts"][0]["function_response"]
                    self.assertEqual(part["id"], call.id)
                    self.assertTrue(part["response"]["is_error"])
                elif transport == "bedrock":
                    result = kwargs["messages"][-1]["content"][0]["toolResult"]
                    self.assertEqual(result["toolUseId"], call.id)
                    self.assertNotIn("status", result)  # Unknown model IDs do not imply status support.
                elif transport == "ollama":
                    result = kwargs["json"]["messages"][-1]
                    self.assertEqual(result["tool_call_id"], call.id)
                    self.assertTrue(json.loads(result["content"])["is_error"])

    def test_google_rejects_unrepresentable_nested_schema_before_transport(self):
        from openkyrozen.providers.config import ProviderConfig
        from openkyrozen.providers.google import GoogleProvider

        provider = GoogleProvider.__new__(GoogleProvider)
        provider.config = ProviderConfig(provider="google", model_simple="m")
        send = Mock()
        provider._client = NS(models=NS(generate_content=send))
        tools = [{"name": "lookup", "description": "Lookup", "parameters": {
            "type": "object", "properties": {"value": {"oneOf": [
                {"type": "string"}, {"type": "integer"}]}}}}]
        with _google_schema_sdk(), self.assertRaises(ProviderContractError):
            provider.chat_response([{"role": "user", "content": "go"}], tools=tools)
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
