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

    def test_final_response_remains_authoritative(self):
        response = ModelResponse(text="done", finish_reason=FinishReason.FINAL)
        self.assertEqual(ResponseComplete(response).response, response)

    def test_openai_chat_assembles_fragmented_native_tool_calls_and_usage(self):
        from openkyrozen.providers.openai import OpenAICompatProvider

        def chunk(delta=None, finish=None, usage=None, response_id="resp"):
            choices = [] if delta is None and finish is None else [SimpleNamespace(
                delta=delta or SimpleNamespace(content=None, tool_calls=None), finish_reason=finish)]
            return SimpleNamespace(id=response_id, model="m", choices=choices, usage=usage)

        chunks = [
            chunk(SimpleNamespace(content="look", tool_calls=[SimpleNamespace(index=0, id="call_",
                function=SimpleNamespace(name="read_", arguments='{"path":'))])),
            chunk(SimpleNamespace(content=None, tool_calls=[SimpleNamespace(index=0, id="1",
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
                response = app.runtime._get_model_response([], "m", stream=True, on_event=observed.append)
                self.assertEqual(response.text, "live")
                self.assertEqual(observed[0], TextDelta("live"))
                self.assertIsInstance(observed[-1], ResponseComplete)
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
