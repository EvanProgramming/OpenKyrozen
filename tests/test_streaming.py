import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import main
import server
from memory import MemoryBank
from providers import ProviderConfig


class _Request:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


class _DelayedProvider:
    def __init__(self):
        self.config = ProviderConfig(provider="deepseek", model_simple="deepseek-v4-flash")
        self.release = threading.Event()
        self.completed = threading.Event()

    def chat_stream(self, _messages, _model=None):
        yield "FIRST"
        self.release.wait(timeout=3)
        yield " SECOND"
        self.completed.set()


class StreamingEndpointTests(unittest.TestCase):
    def test_first_sse_content_arrives_before_provider_stream_completes(self):
        session_id = "stream-timing-regression"
        provider = _DelayedProvider()

        async def exercise():
            response = await server.api_chat_stream(_Request({
                "message": "hello", "session_id": session_id,
            }))
            iterator = response.body_iterator.__aiter__()
            started = time.monotonic()
            first = await asyncio.wait_for(anext(iterator), timeout=1)
            first_elapsed = time.monotonic() - started
            provider_complete_before_release = provider.completed.is_set()
            provider.release.set()
            remaining = []
            try:
                while True:
                    remaining.append(await asyncio.wait_for(anext(iterator), timeout=2))
            except StopAsyncIteration:
                pass
            return first, first_elapsed, provider_complete_before_release, remaining

        with tempfile.TemporaryDirectory(prefix="openkyrozen-stream-") as directory:
            memory = MemoryBank(Path(directory) / "state.sqlite3", workspace_id="stream-test")
            original_sessions = server._sessions
            server._sessions = {}
            try:
                with (patch.object(server._agent, "memory_bank", memory),
                      patch.object(server._agent, "llm_provider", provider),
                      patch.object(server._agent, "DEEPSEEK_MODEL", "deepseek-v4-flash"),
                      patch.object(server._agent, "_chat_turn", side_effect=lambda message, **_: (
                          main._call_llm_with_spinner([{"role": "user", "content": message}])
                      ))):
                    first, elapsed, provider_complete_before_release, remaining = asyncio.run(exercise())
            finally:
                server._sessions = original_sessions

        def text(item):
            return item.decode() if isinstance(item, bytes) else item

        first_payload = json.loads(text(first).split("data: ", 1)[1].splitlines()[0])
        payloads = [json.loads(text(item).split("data: ", 1)[1].splitlines()[0])
                    for item in remaining
                    if text(item).startswith("data: ") and text(item) != "data: [DONE]\n\n"]
        self.assertLess(elapsed, 1)
        self.assertFalse(provider_complete_before_release)
        self.assertEqual(first_payload, {"event": "content", "chunk": "FIRST"})
        self.assertEqual([item.get("event") for item in payloads[:3]], ["content", "usage", "completion"])
        self.assertEqual(payloads[0]["chunk"], " SECOND")
        self.assertEqual(sum(text(item) == "data: [DONE]\n\n" for item in remaining), 1)
        self.assertTrue(provider.completed.is_set())


if __name__ == "__main__":
    unittest.main()
