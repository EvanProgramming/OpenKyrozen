"""Offline regression checks for provider-independent response boundaries."""
import unittest
import copy
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from openkyrozen.providers import ProviderConfig
from openkyrozen.providers.openai import OpenAICompatProvider

from openkyrozen.providers.models import FinishReason, ModelResponse, ToolCall, ProviderCapabilities, model_response
from openkyrozen.providers.base import LLMProvider, get_model_response
from openkyrozen.providers.openai import OpenAIResponsesProvider
from openkyrozen.providers.azure import AzureOpenAIProvider
from openkyrozen.providers.anthropic import AnthropicProvider
from openkyrozen.providers.google import GoogleProvider, VertexProvider
from openkyrozen.providers.bedrock import BedrockProvider
from openkyrozen.providers.ollama import OllamaNativeProvider
from openkyrozen.providers.perplexity import PerplexityProvider
from openkyrozen.providers.fallback import FallbackProvider


class ProviderResponseRegressionTests(unittest.TestCase):
    def test_native_call_and_finish_are_preserved(self):
        provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
        provider.config = ProviderConfig(provider="custom", model_simple="model")
        provider._client = NS(chat=NS(completions=NS(create=Mock(return_value=NS(
            id="response-id", model="model", usage=None,
            choices=[NS(finish_reason="tool_calls", message=NS(content="", tool_calls=[
                NS(id="call-id", type="function", function=NS(name="read_file", arguments='{"path":"a"}')),
            ]))],
        )))))
        with patch("openkyrozen.providers.usage._track_cost"):
            response = provider.chat_response([])
        self.assertEqual(response.tool_calls[0].id, "call-id")
        self.assertEqual(response.tool_calls[0].arguments, {"path": "a"})
        self.assertEqual(response.finish_reason.value, "tool_request")
        self.assertEqual(response.metadata["response_id"], "response-id")

    def test_legacy_conversion_refuses_length_stop(self):
        provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
        provider.config = ProviderConfig(provider="custom", model_simple="model")
        provider._client = NS(chat=NS(completions=NS(create=Mock(return_value=NS(
            choices=[NS(finish_reason="length", message=NS(content="partial", tool_calls=[]))], usage=None,
        )))))
        with patch("openkyrozen.providers.usage._track_cost"), self.assertRaises(ValueError):
            provider.chat([])

    def test_runtime_receives_typed_calls_and_rejects_text_conversion(self):
        import tempfile
        from pathlib import Path
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.providers.models import FinishReason, ModelResponse, ToolCall
        response = ModelResponse(tool_calls=(ToolCall("id", "read_file", {"path": "a"}),),
                                 usage={"prompt_tokens": 2, "completion_tokens": 3},
                                 finish_reason=FinishReason.TOOL_REQUEST)
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"KYROZEN_DISABLE_VECTOR_INDEX": "1"}):
            application = build_application(memory=build_memory(Path(directory) / "state.sqlite3"))
            runtime = application.runtime
            runtime.llm_provider = NS(chat_response=Mock(return_value=response))
            try:
                self.assertIs(runtime._get_model_response([]), response)
                self.assertEqual(runtime._last_prompt_tokens, 2)
                self.assertIn("text conversion refused", runtime._get_llm_response([]))
                self.assertEqual(runtime.memory_bank.store.list_events("execution.receipt"), [])
            finally:
                application.close()



class ContractTests(unittest.TestCase):
    def test_call_validation_and_no_aliasing_of_sdk_arguments(self):
        arguments = {"nested": [None, True, 12, {"unicode": "雪"}]}
        call = ToolCall("id", "name", arguments)
        arguments["nested"].append("changed")
        self.assertEqual(call.arguments, {"nested": [None, True, 12, {"unicode": "雪"}]})
        cycle = {}
        cycle["cycle"] = cycle
        for value in (cycle, [], None, "{}", {1: "bad"}, {"x": float("nan")}, {"x": object()}, {"x": (1,)}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ToolCall("id", "name", value)
        for call_id, name in (("", "name"), (None, "name"), ("id", " "), ("id", 7)):
            with self.subTest(call_id=call_id, name=name), self.assertRaises(ValueError):
                ToolCall(call_id, name, {})

    def test_finish_mapping_and_checked_conversion(self):
        cases = [("stop", FinishReason.FINAL), ("completed", FinishReason.FINAL),
                 ("end_turn", FinishReason.FINAL), ("stop_sequence", FinishReason.FINAL),
                 ("tool_calls", FinishReason.TOOL_REQUEST), ("tool_use", FinishReason.TOOL_REQUEST),
                 ("length", FinishReason.LENGTH), ("MAX_TOKENS", FinishReason.LENGTH),
                 ("failed", FinishReason.ERROR), ("cancelled", FinishReason.CANCELLED),
                 ("content_filter", FinishReason.BLOCKED), ("SAFETY", FinishReason.BLOCKED),
                 (None, FinishReason.UNKNOWN), ("new_reason", FinishReason.UNKNOWN)]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                response = model_response(provider="test", model="m", text=" text ", usage=None, raw_finish_reason=raw,
                                          calls=[("id", "read", {})] if expected == FinishReason.TOOL_REQUEST else [])
                self.assertEqual(response.finish_reason, expected)
                self.assertEqual(response.metadata["raw_finish_reason"], raw)
                if expected in {FinishReason.FINAL, FinishReason.UNKNOWN}:
                    self.assertEqual(response.as_legacy_tuple(), ("text", None))
                else:
                    with self.assertRaises(ValueError):
                        response.as_legacy_tuple()
        for raw in ("incomplete", "in_progress", "queued", "pause_turn", "CONTINUATION"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                model_response(provider="test", model="m", text="partial", usage=None,
                               raw_finish_reason=raw).as_legacy_tuple()
        response = model_response(provider="test", model="m", text="", usage=None,
                                  raw_finish_reason="incomplete", finish_detail="max_output_tokens")
        self.assertEqual(response.finish_reason, FinishReason.LENGTH)

    def test_canonical_ids_json_validation_and_usage_retention(self):
        usage = {"prompt_tokens": 1, "prompt_cache_hit_tokens": 1, "reasoning_tokens": 2, "_estimated": 1}
        response = model_response(provider="test", model="m", text=" raw ", usage=usage,
                                  raw_finish_reason="stop", response_id="r", calls=[
                                      ("native", "one", '{"path":"a"}'), (None, "two", {}),
                                  ])
        self.assertEqual(response.text, " raw ")
        self.assertEqual(response.usage, usage)
        self.assertEqual([call.id for call in response.tool_calls], ["native", "call_r_1"])
        self.assertEqual(response.metadata["synthesized_call_ids"], ["call_r_1"])
        self.assertEqual(response.finish_reason, FinishReason.TOOL_REQUEST)
        for arguments in ('{"broken":', '[]', 'null', '{"a":1,"a":2}', '{"a":Infinity}'):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                model_response(provider="test", model="m", text="", usage=None, calls=[("id", "name", arguments)])
        with self.assertRaises(ValueError):
            ModelResponse(tool_calls=None)
        with self.assertRaises(ValueError):
            ModelResponse(finish_reason=FinishReason.TOOL_REQUEST)
        with self.assertRaises(ValueError):
            ModelResponse(tool_calls=(ToolCall("id", "one", {}), ToolCall("id", "two", {})))
        other = model_response(provider="test", model="m", text="", usage=None, calls=[(None, "name", {})])
        another = model_response(provider="test", model="m", text="", usage=None, calls=[(None, "name", {})])
        self.assertNotEqual(other.tool_calls[0].id, another.tool_calls[0].id)

    def test_legacy_provider_bridge_and_capabilities(self):
        class Legacy(LLMProvider):
            def chat(self, messages, model=None):
                return " legacy ", {"prompt_tokens": 1}
        provider = Legacy(ProviderConfig(provider="custom", model_simple="m"))
        response = provider.chat_response([])
        self.assertEqual(response.text, " legacy ")
        self.assertEqual(response.finish_reason, FinishReason.UNKNOWN)
        self.assertTrue(response.metadata["legacy"])
        self.assertEqual(response.as_legacy_tuple(), (" legacy ", {"prompt_tokens": 1}))
        self.assertEqual(provider.get_capabilities(), ProviderCapabilities())
        self.assertEqual(get_model_response(NS(chat=lambda *args: ("duck", None)), []).text, "duck")
        dynamic = Mock()
        dynamic.chat.return_value = ("mock", None)
        self.assertEqual(get_model_response(dynamic, []).text, "mock")
        with self.assertRaises(ValueError):
            get_model_response(NS(chat_response=lambda *args: ("invalid", None)), [])
        with self.assertRaises(ValueError):
            ProviderCapabilities(native_tools=1)
        self.assertEqual(ProviderCapabilities.intersection([]), ProviderCapabilities())


class AdapterContractTests(unittest.TestCase):
    def adapter(self, cls, wire):
        provider = cls.__new__(cls)
        provider.config = ProviderConfig(provider="custom", model_simple="quick", model_complex="slow")
        send = Mock(return_value=wire)
        provider._client = NS(chat=NS(completions=NS(create=send)), responses=NS(create=send),
                              messages=NS(create=send), models=NS(generate_content=send), converse=send)
        provider._base = "http://localhost:11434"
        provider._requests = NS(post=Mock(return_value=NS(raise_for_status=lambda: None, json=lambda: wire)))
        return provider

    def wires(self, text=" text ", calls=True):
        tools = [NS(id="id1", function=NS(name="first", arguments='{"n":1}')),
                 NS(id="id2", function=NS(name="second", arguments="{}"))] if calls else []
        chat = NS(id="r", model="actual", choices=[NS(finish_reason="tool_calls" if calls else "stop",
                  message=NS(content=text, tool_calls=tools))], usage=None)
        output = [NS(type="function_call", call_id=tool.id, id="item-" + tool.id,
                     name=tool.function.name, arguments=tool.function.arguments) for tool in tools]
        responses = NS(id="r", output_text=text, output=output, status="completed", usage=None)
        anthropic = NS(id="r", content=[NS(type="text", text=text)] + [
            NS(type="tool_use", id=tool.id, name=tool.function.name, input={"n": 1} if i == 0 else {})
            for i, tool in enumerate(tools)], stop_reason="tool_use" if calls else "end_turn", usage=None)
        google = NS(response_id="r", candidates=[NS(finish_reason="STOP", content=NS(parts=[NS(text=text)] + [
            NS(function_call=NS(id=tool.id, name=tool.function.name, args={"n": 1} if i == 0 else {}))
            for i, tool in enumerate(tools)]))], usage_metadata=None)
        bedrock = {"output": {"message": {"content": [{"text": text}] + [
            {"toolUse": {"toolUseId": tool.id, "name": tool.function.name, "input": {"n": 1} if i == 0 else {}}}
            for i, tool in enumerate(tools)]}}, "stopReason": "tool_use" if calls else "end_turn"}
        ollama = {"message": {"content": text, "tool_calls": [
            {"id": tool.id, "function": {"name": tool.function.name, "arguments": {"n": 1} if i == 0 else {}}}
            for i, tool in enumerate(tools)]}, "done_reason": "stop"}
        return [(OpenAICompatProvider, chat), (AzureOpenAIProvider, chat),
                (OpenAIResponsesProvider, responses), (PerplexityProvider, responses),
                (AnthropicProvider, anthropic), (GoogleProvider, google), (VertexProvider, google),
                (BedrockProvider, bedrock), (OllamaNativeProvider, ollama)]

    def test_all_adapter_families_preserve_calls_text_and_charge_once(self):
        for text, calls in ((" text ", True), ("", True), (" text ", False)):
            for cls, wire in self.wires(text, calls):
                with self.subTest(cls=cls.__name__, text=text, calls=calls), patch("openkyrozen.providers.usage._track_cost") as charge:
                    provider = self.adapter(cls, wire)
                    response = provider.chat_response([])
                    self.assertIsInstance(response, ModelResponse)
                    self.assertEqual(response.text, text)
                    self.assertEqual([call.id for call in response.tool_calls], ["id1", "id2"] if calls else [])
                    self.assertEqual(response.finish_reason, FinishReason.TOOL_REQUEST if calls else FinishReason.FINAL)
                    self.assertEqual(response.metadata["provider"], "custom")
                    charge.assert_called_once()
                    charge.reset_mock()
                    if calls:
                        with self.assertRaises(ValueError):
                            provider.chat([])
                    else:
                        self.assertEqual(provider.chat([])[0], text.strip())
                    charge.assert_called_once()
                    caps = provider.get_capabilities("quick")
                    self.assertFalse(caps.native_tools)
                    self.assertFalse(caps.streaming_tool_calls)
                    self.assertEqual(caps.text_streaming, cls is not OllamaNativeProvider)

    def test_missing_ids_and_malformed_args_across_transports(self):
        for cls, wire in self.wires():
            wire = copy.deepcopy(wire)
            if isinstance(wire, dict):
                tool = (wire["message"]["tool_calls"][0] if cls is OllamaNativeProvider
                        else wire["output"]["message"]["content"][1]["toolUse"])
                id_key = "id" if cls is OllamaNativeProvider else "toolUseId"
                tool.pop(id_key)
                target = tool["function"] if cls is OllamaNativeProvider else tool
                arg_key = "arguments" if cls is OllamaNativeProvider else "input"
            elif cls in {OpenAICompatProvider, AzureOpenAIProvider}:
                tool = wire.choices[0].message.tool_calls[0]; tool.id = None
                target, arg_key = tool.function, "arguments"
            elif cls in {OpenAIResponsesProvider, PerplexityProvider}:
                tool = wire.output[0]; tool.call_id = None
                target, arg_key = tool, "arguments"
            elif cls is AnthropicProvider:
                tool = wire.content[1]; tool.id = None
                target, arg_key = tool, "input"
            else:
                tool = wire.candidates[0].content.parts[1].function_call; tool.id = None
                target, arg_key = tool, "args"
            with self.subTest(cls=cls.__name__), patch("openkyrozen.providers.usage._track_cost"):
                response = self.adapter(cls, wire).chat_response([])
                self.assertIn(response.tool_calls[0].id, response.metadata["synthesized_call_ids"])
                if isinstance(target, dict):
                    target[arg_key] = []
                else:
                    setattr(target, arg_key, [])
                with self.assertRaises(ValueError):
                    self.adapter(cls, wire).chat_response([])

    def test_sdk_openai_payloads_use_call_id_not_output_item_id(self):
        from openai.types.chat import ChatCompletion
        from openai.types.responses import Response
        chat = ChatCompletion.model_validate({"id": "r", "object": "chat.completion", "created": 1,
            "model": "actual", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{"id": "call", "type": "function",
                "function": {"name": "read", "arguments": "{}"}}]}}]})
        response = Response.model_construct(id="r", model="actual", status="completed", usage=None,
            output=[{"type": "function_call", "call_id": "call", "id": "item", "name": "read", "arguments": "{}"}])
        # Validate the output item with the SDK rather than relying on dict attribute access.
        from openai.types.responses import ResponseFunctionToolCall
        response.output = [ResponseFunctionToolCall.model_validate(response.output[0])]
        with patch("openkyrozen.providers.usage._track_cost"):
            parsed = self.adapter(OpenAICompatProvider, chat).chat_response([])
            self.assertEqual(parsed.tool_calls[0].id, "call")
            self.assertEqual(parsed.metadata["model"], "actual")
            self.assertEqual(self.adapter(OpenAIResponsesProvider, response).chat_response([]).tool_calls[0].id, "call")

    def test_fallback_preserves_identity_and_does_not_replay_conversion_failure(self):
        class Probe(LLMProvider):
            def __init__(self, config, response, caps):
                super().__init__(config); self.response = response; self.caps = caps; self.models = []
            def chat(self, messages, model=None):
                return self.chat_response(messages, model).as_legacy_tuple()
            def chat_response(self, messages, model=None):
                self.models.append(model)
                if isinstance(self.response, Exception):
                    raise self.response
                return self.response
            def get_capabilities(self, model=None):
                return self.caps
        result = ModelResponse(tool_calls=(ToolCall("id", "read", {}),), finish_reason=FinishReason.TOOL_REQUEST,
                               metadata={"provider": "fallback", "model": "f-slow"})
        primary = Probe(ProviderConfig(provider="custom", model_simple="p-quick", model_complex="p-slow"),
                        ConnectionError("offline failure"), ProviderCapabilities(text_streaming=True, native_tools=True))
        fallback = Probe(ProviderConfig(provider="custom", model_simple="f-quick", model_complex="f-slow"),
                         result, ProviderCapabilities(text_streaming=True))
        wrapper = FallbackProvider.__new__(FallbackProvider)
        wrapper._primary, wrapper._fallbacks = primary, [fallback]
        self.assertIs(wrapper.chat_response([], "p-slow"), result)
        self.assertEqual(fallback.models, ["f-slow"])
        self.assertEqual(wrapper.get_capabilities(), ProviderCapabilities(text_streaming=True))
        primary.response = result
        with self.assertRaises(ValueError):
            wrapper.chat([], "p-slow")
        self.assertEqual(fallback.models, ["f-slow"])

    def test_perplexity_sdk_message_output_is_not_lost(self):
        try:
            from perplexity.types import ResponseCreateResponse
        except ImportError:
            self.skipTest("optional Perplexity SDK unavailable")
        payload = ResponseCreateResponse.model_validate({
            "background": False, "created_at": 1, "error": None, "id": "r", "model": "actual",
            "object": "response", "previous_response_id": None, "status": "completed", "store": False,
            "usage": None, "output": [
                {"id": "msg", "type": "message", "role": "assistant", "status": "completed",
                 "content": [{"type": "output_text", "text": " sdk text ", "annotations": []}]},
                {"id": "item", "type": "function_call", "call_id": "native", "name": "read",
                 "arguments": "{}", "status": "completed"},
            ],
        })
        with patch("openkyrozen.providers.usage._track_cost"):
            response = self.adapter(PerplexityProvider, payload).chat_response([])
        self.assertEqual(response.text, " sdk text ")
        self.assertEqual(response.tool_calls[0].id, "native")

    def test_other_installed_sdk_response_shapes(self):
        try:
            from anthropic.types import Message
            from google.genai.types import GenerateContentResponse
            from botocore.loaders import Loader
            from botocore.model import ServiceModel
            from botocore.validate import validate_parameters
        except ImportError:
            self.skipTest("optional native SDKs unavailable")
        anthropic = Message.model_validate({"id": "msg", "type": "message", "role": "assistant", "model": "claude",
            "content": [{"type": "text", "text": "text"}, {"type": "tool_use", "id": "native", "name": "read", "input": {}}],
            "stop_reason": "tool_use", "stop_sequence": None, "usage": {"input_tokens": 2, "output_tokens": 3}})
        google = GenerateContentResponse.model_validate({"candidates": [{"finish_reason": "STOP", "content": {
            "role": "model", "parts": [{"text": "text"}, {"function_call": {"id": "native", "name": "read", "args": {}}}]
        }}], "model_version": "gemini", "usage_metadata": {"prompt_token_count": 2, "candidates_token_count": 3}})
        bedrock = {"output": {"message": {"role": "assistant", "content": [{"text": "text"},
            {"toolUse": {"toolUseId": "native", "name": "read", "input": {}}}]}}, "stopReason": "tool_use",
            "usage": {"inputTokens": 2, "outputTokens": 3, "totalTokens": 5}, "metrics": {"latencyMs": 1}}
        model = ServiceModel(Loader().load_service_model("bedrock-runtime", "service-2"))
        validate_parameters(bedrock, model.operation_model("Converse").output_shape)
        with patch("openkyrozen.providers.usage._track_cost"):
            for cls, payload in ((AnthropicProvider, anthropic), (GoogleProvider, google), (BedrockProvider, bedrock)):
                with self.subTest(cls=cls.__name__):
                    response = self.adapter(cls, payload).chat_response([])
                    self.assertEqual(response.tool_calls[0].id, "native")
                    self.assertEqual(response.text, "text")
                    self.assertEqual(response.usage["prompt_tokens"], 2)

    def test_transport_finish_statuses_and_unknown_statuses(self):
        for cls, wire in self.wires(calls=False):
            cases = [(None, FinishReason.UNKNOWN)]
            if cls in {OpenAICompatProvider, AzureOpenAIProvider}:
                cases += [("length", FinishReason.LENGTH), ("content_filter", FinishReason.BLOCKED)]
            elif cls in {OpenAIResponsesProvider, PerplexityProvider}:
                cases += [("failed", FinishReason.ERROR), ("cancelled", FinishReason.CANCELLED), ("incomplete", FinishReason.UNKNOWN)]
            elif cls is AnthropicProvider:
                cases += [("max_tokens", FinishReason.LENGTH), ("refusal", FinishReason.BLOCKED)]
            elif cls in {GoogleProvider, VertexProvider}:
                cases += [("MAX_TOKENS", FinishReason.LENGTH), ("SAFETY", FinishReason.BLOCKED), ("MALFORMED_FUNCTION_CALL", FinishReason.ERROR)]
            elif cls is BedrockProvider:
                cases += [("max_tokens", FinishReason.LENGTH), ("guardrail_intervened", FinishReason.BLOCKED)]
            else:
                cases += [("length", FinishReason.LENGTH), ("error", FinishReason.ERROR)]
            for raw, expected in cases:
                payload = copy.deepcopy(wire)
                if cls in {OpenAICompatProvider, AzureOpenAIProvider}:
                    payload.choices[0].finish_reason = raw
                elif cls in {OpenAIResponsesProvider, PerplexityProvider}:
                    payload.status = raw
                elif cls is AnthropicProvider:
                    payload.stop_reason = raw
                elif cls in {GoogleProvider, VertexProvider}:
                    payload.candidates[0].finish_reason = raw
                else:
                    payload["stopReason" if cls is BedrockProvider else "done_reason"] = raw
                with self.subTest(cls=cls.__name__, raw=raw), patch("openkyrozen.providers.usage._track_cost"):
                    response = self.adapter(cls, payload).chat_response([])
                    self.assertEqual(response.finish_reason, expected)
                    self.assertEqual(response.metadata["raw_finish_reason"], raw)

    def test_malformed_native_response_is_not_replayed_on_fallback(self):
        primary = self.adapter(OpenAICompatProvider, self.wires()[0][1])
        primary._client.chat.completions.create.return_value.choices[0].message.tool_calls[0].function.arguments = "[]"
        fallback = NS(config=ProviderConfig(provider="custom", model_simple="f", model_complex="f"),
                      name="fallback", chat_response=Mock(return_value=ModelResponse(text="fallback")))
        wrapper = FallbackProvider.__new__(FallbackProvider)
        wrapper._primary, wrapper._fallbacks = primary, [fallback]
        with patch("openkyrozen.providers.usage._track_cost") as charge, self.assertRaises(ValueError):
            wrapper.chat_response([])
        primary._client.chat.completions.create.assert_called_once()
        fallback.chat_response.assert_not_called()
        charge.assert_called_once()

    def test_all_received_response_parsing_failures_stop_fallback(self):
        cases = self.wires(calls=False)
        for cls, wire in cases:
            wire = copy.deepcopy(wire)
            if cls in {OpenAICompatProvider, AzureOpenAIProvider}:
                wire.choices = []
            elif cls in {OpenAIResponsesProvider, PerplexityProvider}:
                wire.output = [NS(type="function_call", call_id="id", name="read", arguments=[]) ]
            elif cls is AnthropicProvider:
                wire.content = [NS(type="text", text=7)]
            elif cls in {GoogleProvider, VertexProvider}:
                wire.candidates[0].content.parts = [NS(text=7)]
            elif cls is BedrockProvider:
                wire["output"]["message"]["content"] = [{"toolUse": []}]
            else:
                wire["message"] = []
            primary = self.adapter(cls, wire)
            fallback = NS(config=ProviderConfig(provider="custom", model_simple="f", model_complex="f"),
                          name="fallback", chat_response=Mock(return_value=ModelResponse(text="fallback")))
            wrapper = FallbackProvider.__new__(FallbackProvider)
            wrapper._primary, wrapper._fallbacks = primary, [fallback]
            with self.subTest(cls=cls.__name__), patch("openkyrozen.providers.usage._track_cost"), self.assertRaises(ValueError):
                wrapper.chat_response([])
            fallback.chat_response.assert_not_called()

    def test_google_sdk_blocked_and_unsuccessful_finish_reasons(self):
        from openkyrozen.providers.models import normalize_finish
        for raw in ("MODEL_ARMOR", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_finish(raw), FinishReason.BLOCKED)
        self.assertEqual(normalize_finish("TOO_MANY_TOOL_CALLS"), FinishReason.ERROR)

    def test_google_unspecified_prompt_feedback_does_not_block_valid_reply(self):
        wire = self.wires(calls=False)[5][1]
        wire.prompt_feedback = NS(block_reason="BLOCKED_REASON_UNSPECIFIED")
        with patch("openkyrozen.providers.usage._track_cost"):
            self.assertEqual(self.adapter(GoogleProvider, wire).chat_response([]).finish_reason, FinishReason.FINAL)

    def test_ollama_http_error_retains_context_overflow_detail(self):
        import requests
        response = requests.Response()
        response.status_code = 400
        response.url = "http://localhost:11434/api/chat"
        response._content = b'{"error":"context length exceeded"}'
        provider = self.adapter(OllamaNativeProvider, {})
        provider._requests = NS(post=Mock(return_value=response), HTTPError=requests.HTTPError)
        from openkyrozen.providers.errors import ProviderError, ProviderErrorKind
        with self.assertRaises(ProviderError) as raised:
            provider.chat_response([])
        self.assertEqual(raised.exception.kind, ProviderErrorKind.CONTEXT_OVERFLOW)
