from __future__ import annotations
import openkyrozen.providers.usage as usage_ledger

from contextlib import contextmanager
import os
import sys
import time
from typing import Any, Iterator
from openkyrozen.providers.base import LLMProvider
from openkyrozen.providers.models import received_response, ModelResponse, model_response
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.providers.retry import _retry_with_backoff, provider_call, provider_stream, remaining_timeout, close_stream, mark_response_received


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
    def _request(messages: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        system: list[dict[str, str]] = []
        conversation: list[dict[str, Any]] = []
        for item in messages:
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
    def chat_response(self, messages: list[dict[str, str]], model: str | None = None) -> ModelResponse:
        with self._request_client() as client:
            model = model or self.config.model_simple
            conversation, system = self._request(messages)
            started = time.monotonic()
            kwargs: dict[str, Any] = {
                "modelId": model,
                "messages": conversation,
                "inferenceConfig": {"maxTokens": 4096},
            }
            if system:
                kwargs["system"] = system
            response = _retry_with_backoff(lambda: client.converse(**kwargs))
            with received_response():
                usage = self._usage(response)
                usage_ledger._track_cost(self.config.provider, usage, model=model,
                            latency_ms=round((time.monotonic() - started) * 1000))
                content = response.get("output", {}).get("message", {}).get("content", [])
                text = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
                calls = [(item["toolUse"].get("toolUseId"), item["toolUse"].get("name"), item["toolUse"].get("input"))
                         for item in content if isinstance(item, dict) and "toolUse" in item]
                return model_response(provider=self.name, model=model, text=text, usage=usage,
                                      calls=calls, response_id=response.get("ResponseMetadata", {}).get("RequestId"),
                                      raw_finish_reason=response.get("stopReason"))

    @provider_stream
    def chat_stream(self, messages: list[dict[str, str]], model: str | None = None) -> Iterator[str]:
        with self._request_client() as client:
            model = model or self.config.model_simple
            conversation, system = self._request(messages)
            started = time.monotonic()
            kwargs: dict[str, Any] = {
                "modelId": model,
                "messages": conversation,
                "inferenceConfig": {"maxTokens": 4096},
            }
            if system:
                kwargs["system"] = system
            response = _retry_with_backoff(lambda: client.converse_stream(**kwargs))
            mark_response_received()
            final_usage: dict[str, int | None] | None = None
            completed = False
            try:
                for event in response.get("stream", []):
                    if "contentBlockDelta" in event:
                        delta = event["contentBlockDelta"].get("delta", {}).get("text", "")
                        if delta:
                            yield str(delta)
                    if "metadata" in event:
                        final_usage = self._usage(event["metadata"])
                completed = True
            finally:
                close_stream(response.get("stream"))
                if completed:
                    usage_ledger._track_cost(self.config.provider, final_usage, model=model,
                                latency_ms=round((time.monotonic() - started) * 1000))
