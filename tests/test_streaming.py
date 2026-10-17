import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from openkyrozen.app.bootstrap import build_application

_application = build_application(surface="cli")

main = _application.runtime
from openkyrozen.interfaces.web.service import WebService
server = WebService(_application)
from openkyrozen.app.bootstrap import build_memory as MemoryBank
from openkyrozen.providers import ProviderConfig


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
            "Ask",
            'User\n```json\n{"questions": []}\n```\n',
            "Plan",
            'Proposal\n```json\n{"steps": []}\n```',
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


class _ToolNameProseProvider:
    config = ProviderConfig(provider="deepseek", model_simple="deepseek-v4-flash")

    def chat_stream(self, _messages, _model=None):
        # Tool names in explanations and examples are ordinary user-facing prose.
        yield from (
            "Use run_cmd: to execute a command.\n",
            "The tool name is read_file: and it reads text.\n",
            "For example, browser_open: accepts a URL.\n",
            "The following is a label, not a call: write_file: README.md\n",
            "Use plan: for a project plan.\n",
            "工具名称是 read_file:，用于读取文本。\n",
            "run_cmd: is a shell command tool.",
        )


class _SplitGenericInvokeProvider:
    config = ProviderConfig(provider="deepseek", model_simple="deepseek-v4-flash")
    marker = '< invoke name="read_file">read_file("README.md")</ invoke>'

    def chat_stream(self, _messages, _model=None):
        yield "Visible progress. "
        yield from self.marker
        yield " after marker."


class StreamingEndpointTests(unittest.TestCase):
    def test_native_control_words_pass_through_sse_without_legacy_parsing(self):
        session_id = f"native-event-{time.time_ns()}"
        native_text = "Action: and TaskList: are ordinary prose here."

        async def exercise():
            response = await server.api_chat_stream(_Request({
                "message": "hello", "session_id": session_id,
            }))
            frames = []
            async for frame in response.body_iterator:
                frames.append(frame.decode() if isinstance(frame, bytes) else frame)
            return frames

        with tempfile.TemporaryDirectory(prefix="openkyrozen-native-events-") as directory:
            memory = MemoryBank(Path(directory) / "state.sqlite3", workspace_id="native-events")
            original_sessions = server._sessions
            server._sessions = {}
            def emit_native_text(*_args, **_kwargs):
                server._agent._publish_runtime_event("model.text_delta", {"text": native_text})
                return native_text

            try:
                with (patch.object(server._agent.current_session, "memory", memory),
                      patch.object(server._agent, "_chat_turn", side_effect=emit_native_text),
                      patch.object(server._agent, "_clean_final_response", side_effect=lambda text: text),
                      patch.object(server._agent, "_collect_tool_calls", side_effect=AssertionError("legacy parser used")),
                      patch.object(server._agent, "_stream_buffer_has_tool_prefix", side_effect=AssertionError("legacy parser used")),
                      patch.object(server._agent, "normalize_provider_control", side_effect=AssertionError("legacy parser used"))):
                    frames = asyncio.run(exercise())
            finally:
                server._sessions = original_sessions

        payloads = [json.loads(frame.split("data: ", 1)[1].splitlines()[0])
                    for frame in frames if frame.startswith("data: ") and frame != "data: [DONE]\n\n"]
        content = [item for item in payloads if item.get("event") == "content"]
        self.assertEqual(content, [{"event": "content", "chunk": native_text}])

    def test_reasoning_and_tool_use_tags_are_filtered_at_every_boundary(self):
        marker = (
            "<notes><thought>private</thought>"
            "<tool_use><read_file><path>README.md</path></read_file></tool_use></notes>"
        )
        expected = "Visible  answer."
        for split in range(1, len(marker)):
            stream_filter = main.DeepSeekDSMLFilter()
            content = stream_filter.feed("Visible " + marker[:split])
            content += stream_filter.feed(marker[split:] + " answer.", final=True)
            self.assertEqual(content, expected, split)

    def test_first_sse_content_arrives_before_provider_stream_completes(self):
        session_id = "stream-timing-regression"
        provider = _DelayedProvider()

        async def exercise():
            response = await server.api_chat_stream(_Request({
                "message": "hello", "session_id": session_id,
            }))
            iterator = response.body_iterator.__aiter__()
            started = time.monotonic()
            leading = []
            while True:
                first = await asyncio.wait_for(anext(iterator), timeout=1)
                first_text = first.decode() if isinstance(first, bytes) else first
                if '"event": "content"' in first_text:
                    break
                leading.append(first)
            first_elapsed = time.monotonic() - started
            provider_complete_before_release = provider.completed.is_set()
            provider.release.set()
            remaining = []
            try:
                while True:
                    remaining.append(await asyncio.wait_for(anext(iterator), timeout=2))
            except StopAsyncIteration:
                pass
            return leading, first, first_elapsed, provider_complete_before_release, remaining

        with tempfile.TemporaryDirectory(prefix="openkyrozen-stream-") as directory:
            memory = MemoryBank(Path(directory) / "state.sqlite3", workspace_id="stream-test")
            original_sessions = server._sessions
            server._sessions = {}
            try:
                with (patch.object(server._agent.current_session, "memory", memory),
                      patch.object(server._agent, "llm_provider", provider),
                      patch.object(server._agent.execution_context, "model", "deepseek-v4-flash"),
                      patch.object(server._agent, "_chat_turn", side_effect=lambda message, **_: (
                          main._call_llm_with_spinner([{"role": "user", "content": message}])
                      ))):
                    leading, first, elapsed, provider_complete_before_release, remaining = asyncio.run(exercise())
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
        compatibility = [item for item in payloads if item.get("event") != "runtime_event"]
        self.assertEqual([item.get("event") for item in compatibility[:5]],
                         ["content", "interaction", "usage", "context", "completion"])
        self.assertEqual(compatibility[0]["chunk"], " SECOND")
        self.assertEqual(compatibility[1]["interaction"]["preference_mode"], "auto")
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
                with (patch.object(server._agent.current_session, "memory", memory),
                      patch.object(server._agent, "llm_provider", provider),
                      patch.object(server._agent.execution_context, "model", "deepseek-v4-flash"),
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
            self.assertEqual(assistant_messages, [])
            responses = [entry for entry in memory.store.list_conversation_entries(
                user_id=server._SERVER_ACTOR_ID, workspace_id=memory.workspace_id, session_id=session_id,
            ) if entry.get("kind") == "model.response"]
            self.assertIn("DSML", responses[-1]["payload"]["response"]["text"])
            self.assertNotIn("DSML", content)
            event_names = [item.get("event") for item in payloads]
            self.assertLess(event_names.index("usage"), event_names.index("completion"))
            completion = next(item for item in payloads if item.get("event") == "completion")
            self.assertEqual(completion["reply"], content)
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
                with (patch.object(server._agent.current_session, "memory", memory),
                      patch.object(server._agent, "llm_provider", provider),
                      patch.object(server._agent.execution_context, "model", "deepseek-v4-flash"),
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
            self.assertNotRegex(content, r"Action:|Thought:|Plan:|TaskDone:|DefineTool:|AskUser|PlanProposal")
            self.assertNotRegex(content, r"</?\s*(?:invoke|parameter|calls|tool_calls|function_calls)\b")
            assistant_messages = [
                event["payload"] for event in memory.store.list_events(
                    "session.message", workspace_id="inline-control-test", session_id=session_id,
                ) if event["payload"].get("role") == "assistant"
            ]
            self.assertEqual(assistant_messages, [])
            responses = [entry for entry in memory.store.list_conversation_entries(
                user_id=server._SERVER_ACTOR_ID, workspace_id=memory.workspace_id, session_id=session_id,
            ) if entry.get("kind") == "model.response"]
            self.assertIn("Action:", responses[-1]["payload"]["response"]["text"])
            self.assertNotIn("Action:", content)

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
                with (patch.object(server._agent.current_session, "memory", memory),
                      patch.object(server._agent, "llm_provider", provider),
                      patch.object(server._agent.execution_context, "model", "deepseek-v4-flash"),
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
            self.assertEqual(assistant_messages, [])
            responses = [entry for entry in memory.store.list_conversation_entries(
                user_id=server._SERVER_ACTOR_ID, workspace_id=memory.workspace_id, session_id=session_id,
            ) if entry.get("kind") == "model.response"]
            self.assertTrue(responses)

    def test_tool_name_prose_survives_sse_and_persistence(self):
        session_id = "stream-tool-name-prose-regression"
        provider = _ToolNameProseProvider()

        async def exercise():
            response = await server.api_chat_stream(_Request({
                "message": "explain the tools", "session_id": session_id,
            }))
            iterator = response.body_iterator.__aiter__()
            frames = []
            try:
                while True:
                    frames.append(await asyncio.wait_for(anext(iterator), timeout=2))
            except StopAsyncIteration:
                return frames

        with tempfile.TemporaryDirectory(prefix="openkyrozen-tool-name-prose-") as directory:
            memory = MemoryBank(Path(directory) / "state.sqlite3", workspace_id="tool-name-prose-test")
            original_sessions = server._sessions
            server._sessions = {}
            try:
                with (patch.object(server._agent.current_session, "memory", memory),
                      patch.object(server._agent, "llm_provider", provider),
                      patch.object(server._agent.execution_context, "model", "deepseek-v4-flash"),
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
            expected = "".join(provider.chat_stream([], None))
            self.assertEqual(content, expected)
            assistant_messages = [
                event["payload"] for event in memory.store.list_events(
                    "session.message", workspace_id="tool-name-prose-test", session_id=session_id,
                ) if event["payload"].get("role") == "assistant"
            ]
            self.assertEqual(assistant_messages, [])
            responses = [entry for entry in memory.store.list_conversation_entries(
                user_id=server._SERVER_ACTOR_ID, workspace_id=memory.workspace_id, session_id=session_id,
            ) if entry.get("kind") == "model.response"]
            self.assertEqual(responses[-1]["payload"]["response"]["text"], expected)

    def test_generic_invoke_tags_split_at_every_boundary_stay_out_of_sse(self):
        marker = _SplitGenericInvokeProvider.marker
        expected = "Visible progress.  after marker."
        for control in (marker, '<action>\nrun_cmd\ncurl -w "HTTP_%{http_code}" /\n</action>'):
            for split in range(1, len(control)):
                stream_filter = main.DeepSeekDSMLFilter()
                content = stream_filter.feed("Visible progress. " + control[:split])
                content += stream_filter.feed(control[split:] + " after marker.", final=True)
                self.assertEqual(content, expected, (control, split))

        session_id = "stream-generic-invoke-regression"
        provider = _SplitGenericInvokeProvider()

        async def exercise():
            response = await server.api_chat_stream(_Request({
                "message": "read the README", "session_id": session_id,
            }))
            iterator = response.body_iterator.__aiter__()
            frames = []
            try:
                while True:
                    frames.append(await asyncio.wait_for(anext(iterator), timeout=2))
            except StopAsyncIteration:
                return frames

        with tempfile.TemporaryDirectory(prefix="openkyrozen-generic-invoke-") as directory:
            memory = MemoryBank(Path(directory) / "state.sqlite3", workspace_id="generic-invoke-test")
            original_sessions = server._sessions
            server._sessions = {}
            try:
                with (patch.object(server._agent.current_session, "memory", memory),
                      patch.object(server._agent, "llm_provider", provider),
                      patch.object(server._agent.execution_context, "model", "deepseek-v4-flash"),
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
            self.assertEqual(content, expected)
            self.assertNotRegex(content, r"<\s*/?\s*(?:invoke|parameter|calls|tool_calls|function_calls)\b")
            self.assertNotIn("read_file(\"README.md\")", content)
            assistant_messages = [
                event["payload"] for event in memory.store.list_events(
                    "session.message", workspace_id="generic-invoke-test", session_id=session_id,
                ) if event["payload"].get("role") == "assistant"
            ]
            self.assertEqual(assistant_messages, [])
            responses = [entry for entry in memory.store.list_conversation_entries(
                user_id=server._SERVER_ACTOR_ID, workspace_id=memory.workspace_id, session_id=session_id,
            ) if entry.get("kind") == "model.response"]
            self.assertIn("invoke name=\"read_file\"", responses[-1]["payload"]["response"]["text"])
            self.assertNotIn("<invoke", content)


if __name__ == "__main__":
    unittest.main()
