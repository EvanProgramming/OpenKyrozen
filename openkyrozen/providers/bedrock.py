from __future__ import annotations
import openkyrozen.providers.usage as usage_ledger

import json
from contextlib import contextmanager
import os
import sys
import time
from typing import Any, Iterator
from openkyrozen.providers.base import LLMProvider, text_stream_projection
from openkyrozen.providers.models import (received_response, ModelResponse, model_response, ToolReply,
    ProviderContractError, validate_tool_definitions, validate_tool_history, provider_identity,
    validate_replied_tool_definitions, with_provider_continuation, _snapshot_from_sdk,
    TextDelta, ToolCallDelta, UsageUpdate, ResponseComplete)
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.providers.retry import (_retry_with_backoff, provider_call, provider_stream,
    provider_event_stream, remaining_timeout, close_stream, mark_response_received)


class BedrockProvider(LLMProvider):
    """Amazon Bedrock Converse adapter using the default boto3 credential chain."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        try:
            import boto3
        except ImportError:
            sys.exit("The 'boto3' package is required for Bedrock. Install it with: pip install boto3")
        from botocore.config import Config
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
            config=Config(connect_timeout=90.0, read_timeout=90.0, retries={"total_max_attempts": 1}),
        )

    def get_capabilities(self, model: str | None = None):
        from openkyrozen.providers.models import ProviderCapabilities
        supported = self.config.provider == "bedrock"
        return ProviderCapabilities(native_tools=supported, text_streaming=True, streaming_tool_calls=supported)

    @contextmanager
    def _request_client(self):
        # Injected transports without botocore metadata manage their own timeout settings.
        if not hasattr(self._client, "meta"):
            yield self._client
            return
        import boto3
        from botocore.config import Config
        timeout = remaining_timeout()
        client = boto3.client("bedrock-runtime", region_name=self._client.meta.region_name,
                              endpoint_url=self._client.meta.endpoint_url,
                              config=self._client.meta.config.merge(Config(
                                  connect_timeout=timeout, read_timeout=timeout,
                                  retries={"total_max_attempts": 1})))
        try:
            yield client
        finally:
            client.close()

    @staticmethod
    def _request(messages: list, model: str, provider) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        system: list[dict[str, str]] = []
        conversation: list[dict[str, Any]] = []
        for item in messages:
            if isinstance(item, ModelResponse):
                if item.continuation.protocol != "bedrock-converse":
                    raise ProviderContractError("Continuation protocol does not match Bedrock Converse")
                conversation.append({"role": "assistant", "content": item.continuation.payload["content"]})
                continue
            if isinstance(item, ToolReply):
                block = {"toolResult": {"toolUseId": item.call_id,
                    "content": [{"json": {"is_error": item.is_error, "result": item.output}}]}}
                supported_error_status = any(marker in model.lower() for marker in (
                    "amazon.nova-", "anthropic.claude-3", "anthropic.claude-4",
                    "anthropic.claude-sonnet-4", "anthropic.claude-opus-4", "anthropic.claude-haiku-4"))
                if item.is_error and supported_error_status:
                    block["toolResult"]["status"] = "error"
                if conversation and conversation[-1]["role"] == "user" and all(
                        "toolResult" in old for old in conversation[-1]["content"]):
                    conversation[-1]["content"].append(block)
                else:
                    conversation.append({"role": "user", "content": [block]})
                continue
            if item.get("role") == "system":
                system.append({"text": str(item.get("content", ""))})
            else:
                conversation.append({
                    "role": "assistant" if item.get("role") == "assistant" else "user",
                    "content": [{"text": str(item.get("content", ""))}],
                })
        return conversation, system

    @staticmethod
    def _usage(data: dict[str, Any] | None) -> dict[str, int | None] | None:
        usage = (data or {}).get("usage")
        if not usage:
            return None
        return {
            "prompt_tokens": usage.get("inputTokens", 0) or 0,
            "completion_tokens": usage.get("outputTokens", 0) or 0,
        }

    def chat(self, messages: list[dict[str, str]], model: str | None = None) -> tuple[str, dict | None]:
        return self.chat_response(messages, model).as_legacy_tuple()

    @provider_call
    def chat_response(self, messages: list[dict[str, str]] | list, model: str | None = None, *, tools=None) -> ModelResponse:
        with self._request_client() as client:
            model = model or self.config.model_simple
            tools = validate_tool_definitions(tools)
            validate_tool_history(messages, provider_id=provider_identity(self), model=model, native=tools is not None)
            validate_replied_tool_definitions(messages, tools)
            conversation, system = self._request(messages, model, self)
            started = time.monotonic()
            kwargs: dict[str, Any] = {
                "modelId": model,
                "messages": conversation,
                "inferenceConfig": {"maxTokens": 4096},
            }
            if system:
                kwargs["system"] = system
            if tools:
                kwargs["toolConfig"] = {"tools": [{"toolSpec": {"name": item["name"],
                    "description": item["description"], "inputSchema": {"json": item["parameters"]}}}
                    for item in tools]}
            response = _retry_with_backoff(lambda: client.converse(**kwargs))
            with received_response():
                usage = self._usage(response)
                usage_ledger._track_cost(self.config.provider, usage, model=model,
                            latency_ms=round((time.monotonic() - started) * 1000))
                content = response.get("output", {}).get("message", {}).get("content", [])
                text = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
                calls = [(item["toolUse"].get("toolUseId"), item["toolUse"].get("name"), item["toolUse"].get("input"))
                         for item in content if isinstance(item, dict) and "toolUse" in item]
                result = model_response(provider=self.name, model=model, text=text, usage=usage,
                                        calls=calls, response_id=response.get("ResponseMetadata", {}).get("RequestId"),
                                        raw_finish_reason=response.get("stopReason"))
                return with_provider_continuation(result, self, model, "bedrock-converse",
                                                  {"content": response.get("output", {}).get("message", {}).get("content", [])})

    @provider_event_stream
    def chat_response_stream(self, messages: list, model: str | None = None, *, tools=None):
        model = model or self.config.model_simple
        tools = validate_tool_definitions(tools)
        validate_tool_history(messages, provider_id=provider_identity(self), model=model, native=tools is not None)
        validate_replied_tool_definitions(messages, tools)
        conversation, system = self._request(messages, model, self)
        kwargs: dict[str, Any] = {"modelId": model, "messages": conversation,
            "inferenceConfig": {"maxTokens": 4096}}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["toolConfig"] = {"tools": [{"toolSpec": {"name": item["name"],
                "description": item["description"], "inputSchema": {"json": item["parameters"]}}}
                for item in tools]}
        started = time.monotonic()
        blocks: dict[int, dict[str, Any]] = {}
        call_index: dict[int, int] = {}
        usage = None
        stop_reason = None
        stopped = False
        with self._request_client() as client:
            response = client.converse_stream(**kwargs)
            stream = response.get("stream", [])
            try:
                for event in stream:
                    error_event = next((key for key in ("throttlingException", "internalServerException",
                        "modelStreamErrorException", "serviceUnavailableException", "validationException",
                        "accessDeniedException", "modelTimeoutException") if key in event), None)
                    if error_event:
                        from openkyrozen.providers.errors import ProviderError, ProviderErrorKind
                        kind = (ProviderErrorKind.RATE_LIMIT if error_event == "throttlingException" else
                                ProviderErrorKind.AUTHENTICATION if error_event == "accessDeniedException" else
                                ProviderErrorKind.INVALID_REQUEST if error_event == "validationException" else
                                ProviderErrorKind.TIMEOUT if error_event == "modelTimeoutException" else
                                ProviderErrorKind.SERVER)
                        raise ProviderError(kind, provider=self.name, model=model, provider_code=error_event)
                    mark_response_received()
                    if "contentBlockStart" in event:
                        item = event["contentBlockStart"]
                        block_number = item["contentBlockIndex"]
                        start = item.get("start", {})
                        blocks[block_number] = _snapshot_from_sdk(start)
                        tool = start.get("toolUse")
                        if tool is not None:
                            call_number = len(call_index)
                            call_index[block_number] = call_number
                            yield ToolCallDelta(call_number, str(tool.get("toolUseId", "")),
                                                str(tool.get("name", "")))
                    elif "contentBlockDelta" in event:
                        item = event["contentBlockDelta"]
                        block_number = item["contentBlockIndex"]
                        delta = item.get("delta", {})
                        block = blocks.setdefault(block_number, {})
                        if "text" in delta:
                            text = str(delta["text"])
                            block["text"] = block.get("text", "") + text
                            yield TextDelta(text)
                        elif "toolUse" in delta:
                            fragment = str(delta["toolUse"].get("input", ""))
                            tool = block.setdefault("toolUse", {"toolUseId": "", "name": "", "input": ""})
                            tool["input"] = tool.get("input", "") + fragment
                            yield ToolCallDelta(call_index[block_number], arguments_delta=fragment)
                        elif "reasoningContent" in delta:
                            incoming = delta["reasoningContent"]
                            incoming = incoming.get("reasoningText", incoming)
                            content = block.setdefault("reasoningContent", {}).setdefault("reasoningText", {})
                            for key, value in incoming.items():
                                content[key] = content.get(key, "") + value if key == "text" else value
                    elif "metadata" in event:
                        usage = self._usage(event["metadata"])
                        if usage is not None:
                            yield UsageUpdate(usage)
                    elif "messageStop" in event:
                        stopped = True
                        stop_reason = event["messageStop"].get("stopReason")
                if not stopped:
                    raise ProviderContractError("Bedrock stream ended without messageStop")
                usage_ledger._track_cost(self.config.provider, usage, model=model,
                    latency_ms=round((time.monotonic() - started) * 1000))
                content = [blocks[index] for index in sorted(blocks)]
                calls = []
                for block in content:
                    tool = block.get("toolUse")
                    if tool:
                        arguments = tool.get("input", "{}")
                        if isinstance(arguments, str):
                            try:
                                arguments = json.loads(arguments)
                            except (ValueError, RecursionError) as exc:
                                raise ProviderContractError("Bedrock stream returned invalid tool arguments") from exc
                        if not isinstance(arguments, dict):
                            raise ProviderContractError("Bedrock streamed tool arguments must be an object")
                        tool["input"] = arguments
                        calls.append((tool.get("toolUseId"), tool.get("name"), tool.get("input", "{}")))
                    reasoning = block.get("reasoningContent")
                    if isinstance(reasoning, dict) and "reasoningText" not in reasoning:
                        block["reasoningContent"] = {"reasoningText": reasoning}
                text = "".join(block.get("text", "") for block in content if isinstance(block.get("text"), str))
                result = model_response(provider=self.name, model=model, text=text, usage=usage,
                    calls=calls, raw_finish_reason=stop_reason)
                result = with_provider_continuation(result, self, model, "bedrock-converse", {"content": content})
                yield ResponseComplete(result)
            finally:
                close_stream(stream)

    @provider_stream
    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        yield from text_stream_projection(self.chat_response_stream(messages, model))
