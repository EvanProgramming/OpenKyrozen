import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openkyrozen.providers.base import get_model_events
from openkyrozen.providers.models import (
    FinishReason, ModelResponse, ProviderContractError, ResponseComplete, ResponseError,
    TextDelta, ToolCallDelta, UsageUpdate,
)


class StreamEventContractTests(unittest.TestCase):
    def test_events_are_immutable_and_usage_is_defensive(self):
        usage = {"prompt_tokens": 4}
        event = UsageUpdate(usage)
        usage["prompt_tokens"] = 8
        self.assertEqual(event.usage["prompt_tokens"], 4)
        event.usage["prompt_tokens"] = 9
        self.assertEqual(event.usage["prompt_tokens"], 4)
        with self.assertRaises((AttributeError, TypeError)):
            TextDelta("x").text = "y"
        with self.assertRaises(ProviderContractError):
            ToolCallDelta(-1)

    def test_legacy_provider_stream_ends_in_explicit_unknown_completion(self):
        class LegacyProvider:
            name = "legacy"

            def chat(self, messages, model=None):
                return "legacy text", {"prompt_tokens": 1}

        events = list(get_model_events(LegacyProvider(), [{"role": "user", "content": "hi"}], "m"))
        self.assertEqual(events[0], TextDelta("legacy text"))
        self.assertIsInstance(events[-1], ResponseComplete)
        self.assertEqual(events[-1].response.finish_reason, FinishReason.UNKNOWN)
        self.assertTrue(events[-1].response.metadata["legacy"])

    def test_missing_terminal_event_becomes_contract_error(self):
        class BrokenProvider:
            name = "broken"

            def chat_response_stream(self, messages, model=None, *, tools=None):
                yield TextDelta("partial")

        events = list(get_model_events(BrokenProvider(), [], "m"))
        self.assertEqual(events[0], TextDelta("partial"))
        self.assertIsInstance(events[-1], ResponseError)
        self.assertIsInstance(events[-1].error, ProviderContractError)

    def test_outer_stream_timeout_is_delivered_as_terminal_error_event(self):
        from openkyrozen.providers.errors import ProviderError, ProviderErrorKind
        from openkyrozen.providers.retry import provider_event_stream

        class BoundedProvider:
            name = "bounded"
            config = SimpleNamespace(model_simple="m")

            @provider_event_stream
            def chat_response_stream(self, messages, model=None, *, tools=None):
                yield ResponseComplete(ModelResponse(text="done"))

        with patch("openkyrozen.providers.retry.bounded_stream",
                   side_effect=ProviderError(ProviderErrorKind.TIMEOUT)):
            events = list(BoundedProvider().chat_response_stream([], "m"))
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], ResponseError)
        self.assertIsInstance(events[0].error, ProviderError)

    def test_gemini_prompt_level_safety_block_is_a_blocked_completion(self):
        from openkyrozen.providers.config import ProviderConfig
        from openkyrozen.providers.google import GoogleProvider

        chunk = SimpleNamespace(response_id="blocked", model_version="gemini", candidates=[],
            prompt_feedback=SimpleNamespace(block_reason="SAFETY"), usage_metadata=None)
        provider = GoogleProvider.__new__(GoogleProvider)
        provider.config = ProviderConfig(provider="google", model_simple="gemini")
        provider._continuation_id = "gemini-block"
        provider._client = SimpleNamespace(models=SimpleNamespace(generate_content_stream=Mock(return_value=iter([chunk]))))
        with patch("openkyrozen.providers.usage._track_cost"):
            events = list(get_model_events(provider, [{"role": "user", "content": "blocked"}], "gemini"))
        self.assertIsInstance(events[-1], ResponseComplete)
        self.assertEqual(events[-1].response.finish_reason, FinishReason.BLOCKED)

    def test_final_response_remains_authoritative(self):
        response = ModelResponse(text="done", finish_reason=FinishReason.FINAL)
        self.assertEqual(ResponseComplete(response).response, response)

    def test_text_projection_rejects_native_calls_and_unsuccessful_terminal_state(self):
        from openkyrozen.providers.base import text_stream_projection
        from openkyrozen.providers.models import ToolCall

        with self.assertRaises(ProviderContractError):
            list(text_stream_projection([ToolCallDelta(0, "id", "tool"),
                ResponseComplete(ModelResponse(tool_calls=(ToolCall("id", "tool", {}),),
                    finish_reason=FinishReason.TOOL_REQUEST))]))
        with self.assertRaises(ProviderContractError):
            list(text_stream_projection([ResponseComplete(ModelResponse(text="partial",
                finish_reason=FinishReason.LENGTH))]))

    def test_openai_chat_assembles_fragmented_native_tool_calls_and_usage(self):
        from openkyrozen.providers.openai import OpenAICompatProvider

        def chunk(delta=None, finish=None, usage=None, response_id="resp"):
            choices = [] if delta is None and finish is None else [SimpleNamespace(
                delta=delta or SimpleNamespace(content=None, tool_calls=None), finish_reason=finish)]
            return SimpleNamespace(id=response_id, model="m", choices=choices, usage=usage)

        chunks = [
            chunk(SimpleNamespace(content="look", reasoning_content="opaque", tool_calls=[SimpleNamespace(index=0, id="call_",
                function=SimpleNamespace(name="read_", arguments='{"path":'))])),
            chunk(SimpleNamespace(content=None, reasoning_content=" reasoning", tool_calls=[SimpleNamespace(index=0, id="1",
                function=SimpleNamespace(name="file", arguments='"a|雪"}'))])),
            chunk(SimpleNamespace(content=None, tool_calls=None), finish="tool_calls"),
            chunk(usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2)),
        ]
        provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
        provider.config = SimpleNamespace(provider="openai", model_simple="m")
        provider._continuation_id = "streaming-openai"
        send = Mock(return_value=iter(chunks))
        provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=send)))

        with patch("openkyrozen.providers.usage._track_cost"):
            events = list(get_model_events(provider, [{"role": "user", "content": "go"}], "m",
                tools=[{"name": "read_file", "description": "read", "parameters": {
                    "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}]))
        self.assertEqual(events[0], TextDelta("look"))
        self.assertTrue(any(isinstance(event, ToolCallDelta) for event in events))
        self.assertTrue(any(isinstance(event, UsageUpdate) for event in events))
        response = events[-1].response
        self.assertIsInstance(events[-1], ResponseComplete)
        self.assertEqual(response.tool_calls[0].id, "call_1")
        self.assertEqual(response.tool_calls[0].arguments, {"path": "a|雪"})
        self.assertEqual(response.text, "look")
        self.assertEqual(response.usage["prompt_tokens"], 3)
        self.assertEqual(response.continuation.payload["assistant"]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(response.continuation.payload["assistant"]["reasoning_content"], "opaque reasoning")
        self.assertFalse(any("opaque" in event.text for event in events if isinstance(event, TextDelta)))

    def test_fallback_does_not_advertise_buffered_tool_deltas_as_streaming(self):
        from openkyrozen.providers.fallback import FallbackProvider
        from openkyrozen.providers.models import ProviderCapabilities

        class Child:
            name = "custom"
            config = SimpleNamespace(model_simple="m", model_complex="m", provider="custom")
            def get_capabilities(self, model=None):
                return ProviderCapabilities(native_tools=True, text_streaming=True, streaming_tool_calls=True)
            def chat_response(self, messages, model=None, *, tools=None):
                return ModelResponse(tool_calls=(ToolCall("call", "read_file", {"path": "x"}),),
                    finish_reason=FinishReason.TOOL_REQUEST)

        wrapper = FallbackProvider.__new__(FallbackProvider)
        wrapper._primary, wrapper._fallbacks = Child(), []
        capabilities = wrapper.get_capabilities("m")
        self.assertTrue(capabilities.native_tools)
        self.assertFalse(capabilities.streaming_tool_calls)

    def test_openai_stream_without_protocol_terminator_is_error(self):
        from openkyrozen.providers.openai import OpenAICompatProvider

        chunk = SimpleNamespace(id="r", model="m", choices=[SimpleNamespace(
            delta=SimpleNamespace(content="partial", tool_calls=None), finish_reason=None)], usage=None)
        provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
        provider.config = SimpleNamespace(provider="openai", model_simple="m")
        provider._continuation_id = "streaming-openai"
        provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=Mock(return_value=iter([chunk])))))
        with patch("openkyrozen.providers.usage._track_cost"):
            events = list(get_model_events(provider, [], "m"))
        self.assertIsInstance(events[-1], ResponseError)
        self.assertIsInstance(events[-1].error, ProviderContractError)

    def test_runtime_stream_option_returns_response_and_forwards_events(self):
        import tempfile
        from pathlib import Path
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.providers.base import LLMProvider
        from openkyrozen.providers.config import ProviderConfig

        class StreamingProvider(LLMProvider):
            def chat(self, messages, model=None):
                return "unused", None

            def chat_response_stream(self, messages, model=None, *, tools=None):
                yield TextDelta("live")
                yield ResponseComplete(ModelResponse(text="live", finish_reason=FinishReason.FINAL,
                    usage={"prompt_tokens": 2, "completion_tokens": 1}))

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"KYROZEN_DISABLE_VECTOR_INDEX": "1"}):
            app = build_application(memory=build_memory(Path(directory) / "state.sqlite3"))
            try:
                app.runtime.llm_provider = StreamingProvider(ProviderConfig(provider="custom", model_simple="m"))
                observed = []
                from openkyrozen.agent.events import RuntimeEventPublisher
                runtime_events = []
                token = app.runtime._runtime_event_publisher.set(RuntimeEventPublisher(
                    user_id="local", workspace_id="default", session_id="test",
                    on_runtime_event=runtime_events.append,
                ))
                try:
                    response = app.runtime._get_model_response([], "m", stream=True, on_event=observed.append)
                finally:
                    app.runtime._runtime_event_publisher.reset(token)
                self.assertEqual(response.text, "live")
                self.assertTrue(app.runtime._last_response_is_native.get())
                self.assertEqual(observed[0], TextDelta("live"))
                self.assertIsInstance(observed[-1], ResponseComplete)
                self.assertEqual([event.kind for event in runtime_events], [
                    "model.requested", "model.text_delta", "usage.updated", "model.completed",
                ])
                self.assertEqual(len({event.model_request_id for event in runtime_events}), 1)
            finally:
                app.close()

    def test_ollama_streams_native_ndjson_tool_calls_and_requires_done(self):
        import json
        from openkyrozen.providers.config import ProviderConfig
        from openkyrozen.providers.ollama import OllamaNativeProvider

        class HttpResponse:
            def __init__(self, lines): self.lines = lines
            def raise_for_status(self): pass
            def iter_lines(self): return iter(self.lines)
            def close(self): pass

        call = {"id": "call-1", "function": {"name": "read_file", "arguments": {"path": "a|雪"}}}
        lines = [
            json.dumps({"message": {"role": "assistant", "content": "reading"}}).encode(),
            json.dumps({"message": {"role": "assistant", "content": "", "tool_calls": [call]}}).encode(),
            json.dumps({"done": True, "done_reason": "stop", "prompt_eval_count": 4,
                        "eval_count": 2}).encode(),
        ]
        provider = OllamaNativeProvider.__new__(OllamaNativeProvider)
        provider.config = ProviderConfig(provider="ollama", model_simple="m", base_url="http://localhost:11434")
        provider._continuation_id = "ollama-stream"
        from unittest.mock import Mock
        post = Mock(return_value=HttpResponse(lines))
        provider._requests = SimpleNamespace(post=post)
        provider._base = "http://localhost:11434"
        tools = [{"name": "read_file", "description": "read", "parameters": {
            "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}]
        with patch("openkyrozen.providers.usage._track_cost"):
            events = list(get_model_events(provider, [{"role": "user", "content": "read"}], "m", tools=tools))
        self.assertEqual(events[0], TextDelta("reading"))
        self.assertTrue(any(isinstance(event, ToolCallDelta) for event in events))
        self.assertIsInstance(events[-1], ResponseComplete)
        self.assertEqual(events[-1].response.tool_calls[0].arguments, {"path": "a|雪"})
        self.assertIs(post.call_args.kwargs["stream"], True)
        provider._requests.post = Mock(return_value=HttpResponse(lines[:1]))
        events = list(get_model_events(provider, [{"role": "user", "content": "read"}], "m"))
        self.assertIsInstance(events[-1], ResponseError)
        self.assertIsInstance(events[-1].error, ProviderContractError)

    def test_anthropic_stream_interleaves_text_tools_and_cumulative_usage(self):
        from openkyrozen.providers.anthropic import AnthropicProvider
        from openkyrozen.providers.config import ProviderConfig

        class MessageStream:
            def __init__(self, events, final): self.events, self.final = events, final
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def __iter__(self): return iter(self.events)
            def get_final_message(self): return self.final
            def close(self): pass

        final = SimpleNamespace(id="resp", model="claude", stop_reason="tool_use", usage=
            SimpleNamespace(input_tokens=5, output_tokens=3), content=[
                SimpleNamespace(type="tool_use", id="tool-1", name="read_file", input={"path": "x"}),
                SimpleNamespace(type="text", text="checking")])
        stream = MessageStream([
            SimpleNamespace(type="message_start", message=SimpleNamespace(usage=SimpleNamespace(
                input_tokens=5, output_tokens=0))),
            SimpleNamespace(type="content_block_start", index=0,
                content_block=SimpleNamespace(type="tool_use", id="tool-1", name="read_file", input={})),
            SimpleNamespace(type="content_block_delta", index=0,
                delta=SimpleNamespace(type="input_json_delta", partial_json='{"path":"x"}')),
            SimpleNamespace(type="content_block_delta", index=1,
                delta=SimpleNamespace(type="text_delta", text="checking")),
            SimpleNamespace(type="message_delta", usage=SimpleNamespace(output_tokens=3)),
            SimpleNamespace(type="message_delta", usage=SimpleNamespace()),
            SimpleNamespace(type="message_stop")], final)
        provider = AnthropicProvider.__new__(AnthropicProvider)
        provider.config = ProviderConfig(provider="anthropic", model_simple="claude")
        provider._continuation_id = "anthropic-stream"
        provider._client = SimpleNamespace(messages=SimpleNamespace(stream=Mock(return_value=stream)))
        with patch("openkyrozen.providers.usage._track_cost"):
            events = list(get_model_events(provider, [{"role": "user", "content": "go"}], "claude",
                tools=[{"name": "read_file", "description": "read", "parameters": {"type": "object"}}]))
        self.assertIn(ToolCallDelta(0, "tool-1", "read_file"), events)
        self.assertIn(TextDelta("checking"), events)
        self.assertIsInstance(events[-1], ResponseComplete)
        self.assertEqual(events[-1].response.tool_calls[0].arguments, {"path": "x"})
        self.assertEqual(events[-1].response.usage["prompt_tokens"], 5)
        self.assertTrue(any(isinstance(event, UsageUpdate) and event.usage["prompt_tokens"] == 5
                            and event.usage["completion_tokens"] == 3 for event in events))
        usage_updates = [event for event in events if isinstance(event, UsageUpdate)]
        self.assertGreaterEqual(len(usage_updates), 2)
        self.assertTrue(all(event.usage["prompt_tokens"] == 5 for event in usage_updates))
        self.assertEqual(usage_updates[-1].usage["completion_tokens"], 3)

    def test_gemini_stream_preserves_empty_signature_parts_and_tool_arguments(self):
        from openkyrozen.providers.config import ProviderConfig
        from openkyrozen.providers.google import GoogleProvider
        from tests.test_provider_native_tools import _google_schema_sdk

        chunk = SimpleNamespace(response_id="response", model_version="gemini", usage_metadata=
            SimpleNamespace(prompt_token_count=2, candidates_token_count=2), prompt_feedback=None,
            candidates=[SimpleNamespace(finish_reason="STOP", content=SimpleNamespace(role="model", parts=[
                SimpleNamespace(text="", thought=False, thought_signature="signed", function_call=None),
                SimpleNamespace(text="working", thought=False, function_call=None),
                SimpleNamespace(text=None, thought=False, function_call=SimpleNamespace(
                    id="call-1", name="read_file", args={"path": "x"})),
            ]))])
        provider = GoogleProvider.__new__(GoogleProvider)
        provider.config = ProviderConfig(provider="google", model_simple="gemini")
        provider._continuation_id = "gemini-stream"
        provider._client = SimpleNamespace(models=SimpleNamespace(generate_content_stream=Mock(return_value=iter([chunk]))))
        tools = [{"name": "read_file", "description": "read", "parameters": {"type": "object"}}]
        with _google_schema_sdk(), patch("openkyrozen.providers.usage._track_cost"):
            events = list(get_model_events(provider, [{"role": "user", "content": "go"}], "gemini", tools=tools))
        response = events[-1].response
        self.assertEqual(response.tool_calls[0].arguments, {"path": "x"})
        self.assertEqual(response.continuation.payload["parts"][0]["thought_signature"], "signed")
        self.assertEqual(sum(isinstance(event, TextDelta) for event in events), 1)

    def test_bedrock_stream_collects_text_tools_usage_and_reasoning_signature(self):
        from openkyrozen.providers.bedrock import BedrockProvider
        from openkyrozen.providers.config import ProviderConfig

        stream = [
            {"contentBlockStart": {"contentBlockIndex": 0,
                "start": {"toolUse": {"toolUseId": "tool-1", "name": "read_file"}}}},
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": '{"path":"x"}'}}}},
            {"contentBlockStart": {"contentBlockIndex": 1,
                "start": {"reasoningContent": {"reasoningText": {"text": "thinking", "signature": "signed"}}}}},
            {"contentBlockDelta": {"contentBlockIndex": 1,
                "delta": {"reasoningContent": {"reasoningText": {"text": " more", "signature": "signed-2"}}}}},
            {"contentBlockStart": {"contentBlockIndex": 2, "start": {}}},
            {"contentBlockDelta": {"contentBlockIndex": 2, "delta": {"text": "hello"}}},
            {"metadata": {"usage": {"inputTokens": 4, "outputTokens": 3}}},
            {"messageStop": {"stopReason": "tool_use"}},
        ]
        class Client:
            def converse_stream(self, **kwargs): return {"stream": iter(stream)}
        provider = BedrockProvider.__new__(BedrockProvider)
        provider.config = ProviderConfig(provider="bedrock", model_simple="m")
        provider._continuation_id = "bedrock-stream"
        provider._client = Client()
        tools = [{"name": "read_file", "description": "read", "parameters": {"type": "object"}}]
        with patch("openkyrozen.providers.usage._track_cost"):
            events = list(get_model_events(provider, [{"role": "user", "content": "go"}], "m", tools=tools))
        error = getattr(events[-1], "error", None)
        self.assertIsInstance(events[-1], ResponseComplete, f"{error!r}: {getattr(error, '__cause__', None)!r}")
        response = events[-1].response
        self.assertEqual(response.text, "hello")
        self.assertEqual(response.tool_calls[0].arguments, {"path": "x"})
        self.assertEqual(response.continuation.payload["content"][1]["reasoningContent"]["reasoningText"]["signature"], "signed-2")

    def test_bedrock_throttling_event_is_classified_before_response_content(self):
        from openkyrozen.providers.bedrock import BedrockProvider
        from openkyrozen.providers.config import ProviderConfig
        from openkyrozen.providers.errors import ProviderErrorKind

        class Client:
            def converse_stream(self, **kwargs):
                return {"stream": iter([{"throttlingException": {"message": "retry"}}])}
        provider = BedrockProvider.__new__(BedrockProvider)
        provider.config = ProviderConfig(provider="bedrock", model_simple="m")
        provider._continuation_id = "bedrock-error"
        provider._client = Client()
        events = list(get_model_events(provider, [{"role": "user", "content": "go"}], "m"))
        self.assertIsInstance(events[-1], ResponseError)
        self.assertEqual(events[-1].error.kind, ProviderErrorKind.RATE_LIMIT)

    def test_openai_usage_is_charged_when_final_call_arguments_are_invalid(self):
        from openkyrozen.providers.openai import OpenAICompatProvider

        chunks = [
            SimpleNamespace(id="r", model="m", choices=[SimpleNamespace(finish_reason=None, delta=SimpleNamespace(
                content=None, tool_calls=[SimpleNamespace(index=0, id="c", function=SimpleNamespace(
                    name="lookup", arguments='{"bad":'))]))], usage=None),
            SimpleNamespace(id="r", model="m", choices=[SimpleNamespace(finish_reason="tool_calls", delta=SimpleNamespace(
                content=None, tool_calls=None))], usage=None),
            SimpleNamespace(id="r", model="m", choices=[], usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2)),
        ]
        provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
        provider.config = SimpleNamespace(provider="openai", model_simple="m")
        provider._continuation_id = "openai-invalid-args"
        provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=Mock(return_value=iter(chunks)))))
        tracker = Mock()
        with patch("openkyrozen.providers.usage._track_cost", tracker):
            events = list(get_model_events(provider, [{"role": "user", "content": "go"}], "m",
                tools=[{"name": "lookup", "description": "lookup", "parameters": {"type": "object"}}]))
        self.assertIsInstance(events[-1], ResponseError)
        tracker.assert_called_once()

    def test_responses_api_adapters_stream_fragmented_function_calls(self):
        from openkyrozen.providers.config import ProviderConfig
        from openkyrozen.providers.openai import OpenAIResponsesProvider
        from openkyrozen.providers.perplexity import PerplexityProvider

        final = SimpleNamespace(id="resp", model="m", status="completed", output_text="done",
            usage=SimpleNamespace(input_tokens=4, output_tokens=2, output_tokens_details=None),
            incomplete_details=None, output=[SimpleNamespace(type="function_call", call_id="call-1",
                name="read_file", arguments='{"path":"x"}')])
        stream = [
            SimpleNamespace(type="response.output_item.added", output_index=0, item=SimpleNamespace(
                type="function_call", call_id="call-1", name="read_file", arguments="")),
            SimpleNamespace(type="response.function_call_arguments.delta", output_index=0, delta='{"path":"x"}'),
            SimpleNamespace(type="response.output_text.delta", delta="done"),
            SimpleNamespace(type="response.completed", response=final),
        ]
        tool = {"name": "read_file", "description": "read", "parameters": {"type": "object"}}
        for cls, name, protocol in [(OpenAIResponsesProvider, "openai", "openai-responses"),
                                    (PerplexityProvider, "perplexity", "perplexity-responses")]:
            with self.subTest(adapter=cls.__name__), patch("openkyrozen.providers.usage._track_cost"):
                provider = cls.__new__(cls)
                provider.config = ProviderConfig(provider=name, model_simple="m")
                provider._continuation_id = f"{name}-stream"
                create = Mock(return_value=iter(stream))
                provider._client = SimpleNamespace(responses=SimpleNamespace(create=create))
                events = list(get_model_events(provider, [{"role": "user", "content": "go"}], "m", tools=[tool]))
                self.assertIsInstance(events[-1], ResponseComplete)
                response = events[-1].response
                self.assertEqual(response.tool_calls[0].arguments, {"path": "x"})
                self.assertEqual(response.continuation.protocol, protocol)
                if cls is PerplexityProvider:
                    self.assertEqual(create.call_args.kwargs["input"][0]["type"], "message")


if __name__ == "__main__":
    unittest.main()
