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


class _DSMLProvider:
    config = ProviderConfig(provider="deepseek", model_simple="deepseek-v4-flash")

    def chat_stream(self, _messages, _model=None):
        # Split both the opening DSML tag and the legacy marker across deltas.
        yield from (
            "Visible ✅ ",
            "<｜DS",
            "ML｜tool_calls>",
            '<｜DSML｜invoke name="write_file">',
            '<｜DSML｜parameter name="path" string="true">README.md</｜DSML｜parameter>',
            "</｜DSML｜invoke>",
            "</｜DSML｜tool_calls>",
            " after ",
            "｜｜DS",
            "ML｜｜",
            "invoke parameter calls",
            "｜｜DSML｜｜",
            "done",
        )


class _InlineControlProvider:
    config = ProviderConfig(provider="deepseek", model_simple="deepseek-v4-flash")

    def chat_stream(self, _messages, _model=None):
        # Inline and split legacy controls must never reach the browser.  Keep
        # ordinary words that merely discuss actions and invocation intact.
        yield from (
            "Visible progress. ",
            "Act",
            "ion: list_dir",
            '("tests")Action: run_cmd',
            ' "git status" ',
            "Pl",
            "an: inspect first",
            " Thought: internal note ",
            "The action word and invoke word are ordinary prose.",
        )


class _ProsePrefixedAliasProvider:
    config = ProviderConfig(provider="deepseek", model_simple="deepseek-v4-flash")

    def chat_stream(self, _messages, _model=None):
        # Alias-formatted controls may arrive after prose and across deltas.
        yield from (
            "Visible progress. ",
            "run_",
            "cmd: ```bash\n",
            "git status\n",
            "```",
            " after the command.\nread_file: README.md\n",
            "The action word and plan word are ordinary prose.",
        )


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

    def test_deepseek_dsml_is_filtered_from_stream_and_persisted_reply(self):
        session_id = "stream-dsml-regression"
        provider = _DSMLProvider()

        async def exercise():
            response = await server.api_chat_stream(_Request({
                "message": "check the notes project", "session_id": session_id,
            }))
            iterator = response.body_iterator.__aiter__()
            frames = []
            try:
                while True:
                    frames.append(await asyncio.wait_for(anext(iterator), timeout=2))
            except StopAsyncIteration:
                return frames

        with tempfile.TemporaryDirectory(prefix="openkyrozen-dsml-") as directory:
            memory = MemoryBank(Path(directory) / "state.sqlite3", workspace_id="dsml-test")
            original_sessions = server._sessions
            server._sessions = {}
            try:
                with (patch.object(server._agent, "memory_bank", memory),
                      patch.object(server._agent, "llm_provider", provider),
                      patch.object(server._agent, "DEEPSEEK_MODEL", "deepseek-v4-flash"),
                      patch.object(server._agent, "_chat_turn", side_effect=lambda message, **_: (
                          main._call_llm_with_spinner([{"role": "user", "content": message}])
                      ))):
                    frames = asyncio.run(exercise())
            finally:
                server._sessions = original_sessions

            def text(item):
                return item.decode() if isinstance(item, bytes) else item

            payloads = []
            done = 0
            for frame in frames:
                value = text(frame)
                if value == "data: [DONE]\n\n":
                    done += 1
                elif value.startswith("data: "):
                    payloads.append(json.loads(value.split("data: ", 1)[1].splitlines()[0]))

            content = "".join(item["chunk"] for item in payloads if item.get("event") == "content")
            self.assertEqual(content, "Visible ✅  after done")
            self.assertNotRegex(content, r"DSML|invoke|parameter|tool_calls|function_calls")
            assistant_messages = [
                event["payload"] for event in memory.store.list_events(
                    "session.message", workspace_id="dsml-test", session_id=session_id,
                ) if event["payload"].get("role") == "assistant"
            ]
            self.assertEqual(assistant_messages[-1]["content"], content)
            event_names = [item.get("event") for item in payloads]
            self.assertLess(event_names.index("usage"), event_names.index("completion"))
        self.assertEqual(done, 1)

    def test_inline_controls_are_filtered_before_sse_and_persistence(self):
        session_id = "stream-inline-control-regression"
        provider = _InlineControlProvider()

        async def exercise():
            response = await server.api_chat_stream(_Request({
                "message": "review the project", "session_id": session_id,
            }))
            iterator = response.body_iterator.__aiter__()
            frames = []
            try:
                while True:
                    frames.append(await asyncio.wait_for(anext(iterator), timeout=2))
            except StopAsyncIteration:
                return frames

        with tempfile.TemporaryDirectory(prefix="openkyrozen-inline-control-") as directory:
            memory = MemoryBank(Path(directory) / "state.sqlite3", workspace_id="inline-control-test")
            original_sessions = server._sessions
            server._sessions = {}
            try:
                with (patch.object(server._agent, "memory_bank", memory),
                      patch.object(server._agent, "llm_provider", provider),
                      patch.object(server._agent, "DEEPSEEK_MODEL", "deepseek-v4-flash"),
                      patch.object(server._agent, "_chat_turn", side_effect=lambda message, **_: (
                          main._call_llm_with_spinner([{"role": "user", "content": message}])
                      ))):
                    frames = asyncio.run(exercise())
            finally:
                server._sessions = original_sessions

            def text(item):
                return item.decode() if isinstance(item, bytes) else item

            payloads = [json.loads(text(frame).split("data: ", 1)[1].splitlines()[0])
                        for frame in frames
                        if text(frame).startswith("data: ") and text(frame) != "data: [DONE]\n\n"]
            content = "".join(item["chunk"] for item in payloads if item.get("event") == "content")
            self.assertIn("Visible progress.", content)
            self.assertIn("The action word and invoke word are ordinary prose.", content)
            self.assertNotRegex(content, r"Action:|Thought:|Plan:|TaskDone:|DefineTool:")
            self.assertNotRegex(content, r"</?\s*(?:invoke|parameter|calls|tool_calls|function_calls)\b")
            assistant_messages = [
                event["payload"] for event in memory.store.list_events(
                    "session.message", workspace_id="inline-control-test", session_id=session_id,
                ) if event["payload"].get("role") == "assistant"
            ]
            self.assertEqual(assistant_messages[-1]["content"], main._clean_final_response(content))

    def test_prose_prefixed_alias_controls_are_filtered_before_sse_and_persistence(self):
        session_id = "stream-prose-alias-regression"
        provider = _ProsePrefixedAliasProvider()

        async def exercise():
            response = await server.api_chat_stream(_Request({
                "message": "review the project", "session_id": session_id,
            }))
            iterator = response.body_iterator.__aiter__()
            frames = []
            try:
                while True:
                    frames.append(await asyncio.wait_for(anext(iterator), timeout=2))
            except StopAsyncIteration:
                return frames

        with tempfile.TemporaryDirectory(prefix="openkyrozen-prose-alias-") as directory:
            memory = MemoryBank(Path(directory) / "state.sqlite3", workspace_id="prose-alias-test")
            original_sessions = server._sessions
            server._sessions = {}
            try:
                with (patch.object(server._agent, "memory_bank", memory),
                      patch.object(server._agent, "llm_provider", provider),
                      patch.object(server._agent, "DEEPSEEK_MODEL", "deepseek-v4-flash"),
                      patch.object(server._agent, "_chat_turn", side_effect=lambda message, **_: (
                          main._call_llm_with_spinner([{"role": "user", "content": message}])
                      ))):
                    frames = asyncio.run(exercise())
            finally:
                server._sessions = original_sessions

            def text(item):
                return item.decode() if isinstance(item, bytes) else item

            payloads = [json.loads(text(frame).split("data: ", 1)[1].splitlines()[0])
                        for frame in frames
                        if text(frame).startswith("data: ") and text(frame) != "data: [DONE]\n\n"]
            content = "".join(item["chunk"] for item in payloads if item.get("event") == "content")
            self.assertIn("Visible progress.", content)
            self.assertIn("The action word and plan word are ordinary prose.", content)
            self.assertNotRegex(content, r"(?:run_cmd|read_file|bash|shell)\s*:")
            self.assertNotIn("```", content)
            assistant_messages = [
                event["payload"] for event in memory.store.list_events(
                    "session.message", workspace_id="prose-alias-test", session_id=session_id,
                ) if event["payload"].get("role") == "assistant"
            ]
            self.assertEqual(assistant_messages[-1]["content"], main._clean_final_response(content))


if __name__ == "__main__":
    unittest.main()
