from contextlib import ExitStack, closing
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
                yield ToolCallDelta(0, id_delta="call", name_delta="search", arguments_delta='{"token":"stream-secret"}')
                yield ResponseComplete(ModelResponse(text="live", finish_reason=FinishReason.FINAL,
                    usage={"prompt_tokens": 2, "completion_tokens": 1}))

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"KYROZEN_DISABLE_VECTOR_INDEX": "1"}), ExitStack() as sqlite_owners:
            app = build_application(memory=sqlite_owners.enter_context(closing(build_memory(Path(directory) / "state.sqlite3"))))
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
                self.assertFalse(app.runtime._last_response_is_native.get())
                self.assertEqual(observed[0], TextDelta("live"))
                self.assertIsInstance(observed[-1], ResponseComplete)
                self.assertEqual([event.kind for event in runtime_events], [
                    "model.requested", "model.text_delta", "model.tool_call_delta",
                    "usage.updated", "model.completed",
                ])
                self.assertEqual(len({event.model_request_id for event in runtime_events}), 1)
                delta = next(event for event in runtime_events if event.kind == "model.tool_call_delta")
                self.assertNotIn("stream-secret", str(delta.payload))
                self.assertEqual(delta.payload["arguments_delta"], "<redacted>")
                self.assertEqual(delta.payload["arguments_length"], len('{"token":"stream-secret"}'))
                response = app.runtime._get_model_response([], "m", stream=True, tool_names=["read_file"])
                self.assertTrue(app.runtime._last_response_is_native.get())
            finally:
                app.close()

    def test_legacy_stream_error_text_preserves_context_overflow_recovery(self):
        import tempfile
        from pathlib import Path
        from openkyrozen.agent.types import ContextOverflowError
        from openkyrozen.app.bootstrap import build_application, build_memory

        class LegacyStreamProvider:
            name = "ollama"

            def chat_stream(self, messages, model=None):
                yield "[Ollama Error] context length exceeded"

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"KYROZEN_DISABLE_VECTOR_INDEX": "1"}), ExitStack() as sqlite_owners:
            app = build_application(memory=sqlite_owners.enter_context(closing(build_memory(Path(directory) / "state.sqlite3"))))
            try:
                app.runtime.llm_provider = LegacyStreamProvider()
                context_token = app.runtime._active_context_state.set(object())
                try:
                    with self.assertRaises(ContextOverflowError):
                        app.runtime._get_llm_response([], stream=True, on_chunk=lambda _chunk: None)
                finally:
                    app.runtime._active_context_state.reset(context_token)
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
            SimpleNamespace(prompt_token_count=2, candidates_token_count=2, thoughts_token_count=11), prompt_feedback=None,
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
        with _google_schema_sdk(), patch("openkyrozen.providers.usage._track_cost") as charge:
            events = list(get_model_events(provider, [{"role": "user", "content": "go"}], "gemini", tools=tools))
        response = events[-1].response
        self.assertEqual(response.tool_calls[0].arguments, {"path": "x"})
        self.assertEqual(response.continuation.payload["parts"][0]["thought_signature"], "signed")
        self.assertEqual(sum(isinstance(event, TextDelta) for event in events), 1)
        self.assertEqual(response.usage["completion_tokens"], 13)
        self.assertEqual(response.usage["reasoning_tokens"], 11)
        self.assertTrue(any(isinstance(event, UsageUpdate) and event.usage["completion_tokens"] == 13
                            for event in events))
        self.assertEqual(charge.call_args.args[1], response.usage)

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

    def test_strict_chat_rejects_changed_response_identity_and_hashes_invalid_arguments(self):
        import hashlib
        import json
        from openkyrozen.providers.openai import OpenAICompatProvider

        malformed = '{"path":"partial'
        chunks = [
            SimpleNamespace(id="original", model="m", choices=[SimpleNamespace(index=0, finish_reason=None,
                delta=SimpleNamespace(tool_calls=[SimpleNamespace(index=0, id="call-1", type="function",
                    function=SimpleNamespace(name="read_file", arguments=malformed))]))], usage=None),
            SimpleNamespace(id="different", model="m", choices=[SimpleNamespace(index=0,
                finish_reason="tool_calls", delta=SimpleNamespace())],
                usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2)),
        ]
        provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
        provider.config = SimpleNamespace(provider="deepseek", model_simple="m")
        provider._tool_wire_decoder = lambda raw, _schema, *, tool_name: json.dumps(json.loads(raw))
        send = Mock()
        provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=send)))
        tools = [{"name": "read_file", "description": "read", "parameters": {"type": "object"}}]

        send.return_value = iter(chunks)
        with patch("openkyrozen.providers.usage._track_cost"):
            terminal = list(get_model_events(provider, [], "m", tools=tools))[-1]
        self.assertIsInstance(terminal, ResponseError)
        self.assertEqual(terminal.error.wire_evidence["issue"], "response_identity_changed")
        self.assertFalse(terminal.error.malformed_tool_json)

        chunks[1].id = "original"
        send.return_value = iter(chunks)
        with patch("openkyrozen.providers.usage._track_cost"):
            terminal = list(get_model_events(provider, [], "m", tools=tools))[-1]
        self.assertIsInstance(terminal, ResponseError)
        self.assertTrue(terminal.error.malformed_tool_json)
        evidence = terminal.error.wire_evidence
        self.assertEqual(evidence["response_id"], "original")
        self.assertEqual(evidence["finish_reason"], "tool_calls")
        self.assertEqual(evidence["calls"][0]["arguments_bytes"], len(malformed.encode()))
        self.assertEqual(evidence["calls"][0]["arguments_sha256"], hashlib.sha256(malformed.encode()).hexdigest())
        self.assertEqual(evidence["malformed_argument"], {
            "index": 0, "tool_name": "read_file", "byte_offset": 8,
        })
        self.assertNotIn("partial", repr(evidence))

    def test_strict_chat_assembles_interleaved_calls_before_decoding(self):
        from openkyrozen.providers.openai import OpenAICompatProvider

        def frame(calls=(), finish=None, usage=None):
            return SimpleNamespace(id="r", model="m", choices=[SimpleNamespace(
                index=0, finish_reason=finish, delta=SimpleNamespace(tool_calls=calls))], usage=usage)

        def part(index, arguments, call_id=None):
            return SimpleNamespace(index=index, id=call_id, type="function" if call_id else None,
                function=SimpleNamespace(name="read_file" if call_id else None, arguments=arguments))

        usage = SimpleNamespace(prompt_tokens=4, completion_tokens=2)
        chunks = [frame([part(0, '{"path":', "a")]), frame([part(1, '{"path":"b', "b")]),
                  frame([part(0, '"a"}'), part(1, '"}')]), frame(finish="tool_calls", usage=usage)]
        provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
        provider.config = SimpleNamespace(provider="deepseek", model_simple="m")
        provider._tool_wire_decoder = Mock(side_effect=lambda raw, _schema, *, tool_name: raw)
        create = Mock(return_value=iter(chunks))
        provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        tools = [{"name": "read_file", "description": "read", "parameters": {"type": "object"}}]
        with patch("openkyrozen.providers.usage._track_cost"):
            terminal = list(get_model_events(provider, [], "m", tools=tools))[-1]
        self.assertIsInstance(terminal, ResponseComplete)
        self.assertEqual([(call.id, call.arguments) for call in terminal.response.tool_calls],
                         [("a", {"path": "a"}), ("b", {"path": "b"})])
        self.assertEqual([args[0] for args, _ in provider._tool_wire_decoder.call_args_list],
                         ['{"path":"a"}', '{"path":"b"}'])

        create.return_value = iter([*chunks[:-1], frame(finish="length", usage=usage)])
        provider._tool_wire_decoder.reset_mock()
        with patch("openkyrozen.providers.usage._track_cost"):
            self.assertIsInstance(list(get_model_events(provider, [], "m", tools=tools))[-1], ResponseError)
        provider._tool_wire_decoder.assert_not_called()

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

    def test_responses_stream_can_use_complete_arguments_from_the_same_call(self):
        from openkyrozen.providers.config import ProviderConfig
        from openkyrozen.providers.models import ToolReply
        from openkyrozen.providers.openai import OpenAIResponsesProvider

        arguments = '{"path":"x"}'
        tool = {"name": "read_file", "description": "read", "parameters": {"type": "object"}}
        messages = [{"role": "user", "content": "go"}]
        for case in ("matched", "initial fragment", "wrong id", "wrong name", "duplicate", "invalid",
                     "redundant terminal", "redundant copies", "conflicting terminal", "typed conflict", "precision conflict",
                     "terminal and copies disagree", "two truncated copies", "truncated copies with different prefix",
                     "truncated copies without agreeing deltas", "fragments disagree", "wrong delta id", "duplicate open", "conflicting item",
                     "incomplete", "wrong terminal status", "second call invalid"):
            with self.subTest(case=case), patch("openkyrozen.providers.usage._track_cost") as track:
                item = SimpleNamespace(type="function_call", id="fc-1", call_id="call-1",
                    name="read_file", arguments='{"path":')
                if case in ("redundant terminal", "redundant copies", "terminal and copies disagree"):
                    item.arguments = '{"path":"x","path":"x"}'
                elif case == "conflicting terminal":
                    item.arguments = '{"path":"x","path":"y"}'
                elif case == "typed conflict":
                    item.arguments = '{"path":true,"path":1}'
                elif case == "precision conflict":
                    item.arguments = '{"path":9007199254740992.0,"path":9007199254740993.0}'
                if case == "truncated copies with different prefix":
                    item.arguments = '{"different":'
                original_arguments = item.arguments
                status = "incomplete" if case == "incomplete" else "completed"
                response = SimpleNamespace(id="resp", model="m",
                    status="tool_calls" if case == "wrong terminal status" else status, output_text="",
                    usage=SimpleNamespace(input_tokens=4, output_tokens=2, output_tokens_details=None),
                    incomplete_details=None, output=[item])
                if case == "second call invalid":
                    response.output.append(SimpleNamespace(type="function_call", id="fc-2",
                        call_id="call-2", name="read_file", arguments='{"path":"y","path":"z"}'))
                done = SimpleNamespace(type="response.function_call_arguments.done", output_index=0,
                    item_id="other" if case == "wrong id" else item.id,
                    name="write_file" if case == "wrong name" else item.name,
                    arguments=('{"path":"x","path":"y"}' if case == "invalid"
                               else item.arguments if case == "redundant copies" or "truncated copies" in case
                               else '{"path":9007199254740992.0}' if case == "precision conflict"
                               else '{"path":"y"}' if case == "terminal and copies disagree" else arguments))
                prefix = arguments[:3] if case == "initial fragment" else ""
                opening_item = SimpleNamespace(**{**vars(item), "arguments": prefix})
                opening = SimpleNamespace(type="response.output_item.added", output_index=0, item=opening_item)
                completed_item = SimpleNamespace(**{**vars(item),
                    "arguments": ('{"path":"y"}' if case == "conflicting item"
                                  else arguments if "truncated copies" in case else done.arguments)})
                streamed = (arguments if "truncated copies" in case
                            and case != "truncated copies without agreeing deltas" else done.arguments)
                stream = [
                    opening,
                    *([opening] if case == "duplicate open" else []),
                    SimpleNamespace(type="response.function_call_arguments.delta", output_index=0,
                        item_id="other" if case == "wrong delta id" else item.id,
                        delta='{"path":"y"}' if case == "fragments disagree" else streamed[len(prefix):]),
                    done,
                    *([done] if case == "duplicate" else []),
                    SimpleNamespace(type="response.output_item.done", output_index=0, item=completed_item),
                    SimpleNamespace(type="response." + status, response=response),
                ]
                provider = OpenAIResponsesProvider.__new__(OpenAIResponsesProvider)
                provider.config = ProviderConfig(provider="openai", model_simple="m")
                provider._continuation_id = "responses-copy"
                create = Mock(return_value=iter(stream))
                provider._client = SimpleNamespace(responses=SimpleNamespace(create=create))
                events = list(get_model_events(provider, messages, "m", tools=[tool]))
                track.assert_called_once()
                if case not in {"matched", "initial fragment", "redundant terminal", "redundant copies",
                                "two truncated copies"}:
                    self.assertIsInstance(events[-1], ResponseError)
                    self.assertFalse(any(isinstance(event, ResponseComplete) for event in events))
                    if case == "invalid":
                        wire = events[-1].error.wire_evidence
                        self.assertEqual(wire["json_error"], "Expecting value")
                        self.assertEqual(wire["declared_tool"], "read_file")
                        self.assertNotIn('{"path":', repr(wire))
                    if case == "second call invalid":
                        self.assertTrue(events[-1].error.malformed_tool_json)
                        self.assertEqual(len(events[-1].error.wire_evidence["terminal"]), 2)
                    if case in ("conflicting terminal", "typed conflict", "precision conflict", "terminal and copies disagree"):
                        self.assertTrue(events[-1].error.malformed_tool_json)
                        wire = events[-1].error.wire_evidence
                        self.assertEqual(wire["json_error"], "Duplicate object member")
                        self.assertEqual(wire["declared_tool"], "read_file")
                        self.assertNotIn(original_arguments, repr(wire))
                    continue
                self.assertIsInstance(events[-1], ResponseComplete)
                result = events[-1].response
                self.assertEqual(result.tool_calls[0].arguments, {"path": "x"})
                self.assertEqual(result.continuation.payload["output"][0]["arguments"], arguments)
                self.assertEqual(item.arguments, original_arguments)
                create.return_value = SimpleNamespace(id="end", model="m", status="completed",
                    output_text="done", usage=None, output=[])
                provider.chat_response([*messages, result, ToolReply("call-1", "read_file", {"value": "ok"})],
                    "m", tools=[tool])
                continued = create.call_args.kwargs["input"]
                self.assertEqual(continued[-2]["arguments"], arguments)
                self.assertEqual(continued[-1]["call_id"], "call-1")

    def test_responses_stream_accepts_one_redundant_closing_brace_only_with_schema_and_copy_agreement(self):
        import json

        from openkyrozen.providers.config import ProviderConfig
        from openkyrozen.providers.openai import OpenAIResponsesProvider
        from openkyrozen.tools.catalog import builtin_metadata

        tool = {"name": "update_plan", "description": "Edit the active plan",
                "parameters": builtin_metadata("update_plan")["input_schema"]}
        arguments = {"expected_revision": 0, "operations": [{"op": "add", "ref": "verify", "task": {
            "description": "Verify the implementation", "acceptance": ["Run the original tests"], "execution": None,
        }}]}
        valid = json.dumps(arguments)
        mismatched = valid.replace('"execution": null', '"execution": "run tests"') + "}"
        extra = valid + "}"
        for terminal, done, deltas, complete, should_accept in (
            (extra, extra, extra, extra, True),
            (extra + "}", extra + "}", extra + "}", extra + "}", False),
            (valid + "]", valid + "]", valid + "]", valid + "]", False),
            (extra, extra, "", extra, False),
            (extra, extra, extra, mismatched, False),
            (extra, '{"expected_revision":2}}', '{"expected_revision":2}}', '{"expected_revision":2}}', False),
            *[(bad, bad, bad, bad, False) for bad in (
                mismatched, '{"expected_revision":1,"expected_revision":1}}',
                '{"expected_revision":1} {"expected_revision":2}', '{}' + "}",
            )],
        ):
            with self.subTest(terminal=terminal, done=done, deltas=deltas, complete=complete):
                item = SimpleNamespace(type="function_call", name=tool["name"], id="fc-1",
                    call_id="call-1", status="completed", arguments=terminal)
                response = SimpleNamespace(id="resp", model="m", status="completed", output_text="",
                    usage=SimpleNamespace(input_tokens=4, output_tokens=2, output_tokens_details=None),
                    incomplete_details=None, output=[item])
                events = [
                    SimpleNamespace(type="response.output_item.added", output_index=0,
                        item=SimpleNamespace(**{**vars(item), "arguments": ""})),
                    SimpleNamespace(type="response.function_call_arguments.delta", output_index=0,
                        item_id=item.id, delta=deltas),
                    SimpleNamespace(type="response.function_call_arguments.done", output_index=0,
                        item_id=item.id, name=item.name, arguments=done),
                    SimpleNamespace(type="response.output_item.done", output_index=0,
                        item=SimpleNamespace(**{**vars(item), "arguments": complete})),
                    SimpleNamespace(type="response.completed", response=response),
                ]
                provider = OpenAIResponsesProvider.__new__(OpenAIResponsesProvider)
                provider.config = ProviderConfig(provider="openai", model_simple="m")
                provider._continuation_id = "responses-brace"
                create = Mock(return_value=iter(events))
                provider._client = SimpleNamespace(responses=SimpleNamespace(create=create))
                with patch("openkyrozen.providers.usage._track_cost"):
                    result = list(get_model_events(provider, [{"role": "user", "content": "go"}],
                                                   "m", tools=[tool]))[-1]
                create.assert_called_once()
                if should_accept:
                    self.assertIsInstance(result, ResponseComplete)
                    self.assertEqual(result.response.tool_calls[0].arguments, arguments)
                    self.assertEqual(result.response.continuation.payload["output"][0]["arguments"], valid)
                    self.assertEqual(item.arguments, terminal)
                else:
                    self.assertIsInstance(result, ResponseError)

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
