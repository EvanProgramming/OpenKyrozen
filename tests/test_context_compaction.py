import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main
import server
from context_compaction import (
    ContextState, DIGEST_PREFIX, OMISSION_PREFIX, compact_for_pressure,
    retain_context_digests,
)
from memory import MemoryBank
from providers import ProviderConfig, detect_provider


class _Request:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


def _messages(old_count=4, *, chars=1_000):
    messages = [{"role": "system", "content": "fixed instructions"}]
    for index in range(old_count):
        messages.append({"role": "user" if index % 2 == 0 else "assistant",
                         "content": f"old-{index}:" + "x" * chars})
    for index in range(4):
        messages.append({"role": "user" if index % 2 == 0 else "assistant",
                         "content": f"recent-{index}:" + "y" * chars})
    messages.append({"role": "user", "content": "pending request"})
    return messages


class ContextCompactionTests(unittest.TestCase):
    def test_no_compaction_below_model_pressure(self):
        messages = _messages(chars=20)
        state = ContextState("gpt-4o", reserve_tokens=100)
        state.history_message_ids = {id(item) for item in messages[:-1]}
        result = compact_for_pressure(messages, state, lambda _text, _limit: "summary")
        self.assertFalse(result.compacted)
        self.assertEqual(result.messages, messages)
        self.assertEqual(state.status["compaction"]["status"], "not_needed")

    def test_catalog_aliases_and_environment_override_resolve_context_windows(self):
        self.assertEqual(ContextState("deepseek-flash").window_tokens, 1_048_576)
        self.assertEqual(ContextState("gemini-3.1-pro-preview").window_tokens, 1_048_576)
        self.assertEqual(ContextState("claude-fable-5-1").window_tokens, 1_000_000)
        self.assertEqual(ContextState("llama3.2").window_tokens, 131_072)
        with patch.dict(os.environ, {"KYROZEN_CONTEXT_WINDOW_TOKENS": "64000"}, clear=False):
            self.assertEqual(detect_provider().context_window_tokens, 64_000)

    def test_preflight_compaction_keeps_recent_turns_and_pending_request(self):
        messages = _messages(old_count=6, chars=1_000)
        state = ContextState("custom", configured_window=2_000, reserve_tokens=100)
        state.history_message_ids = {id(item) for item in messages[:-1]}
        result = compact_for_pressure(messages, state, lambda _text, _limit: "key decision")
        joined = "\n".join(item["content"] for item in result.messages)
        self.assertTrue(result.compacted)
        self.assertIn(DIGEST_PREFIX, joined)
        self.assertIn("recent-3:", joined)
        self.assertIn("pending request", joined)
        self.assertNotIn("old-0:", joined)
        self.assertEqual(state.status["window_source"], "configured_override")

    def test_oversized_history_is_batched_into_one_bounded_digest(self):
        messages = _messages(old_count=30, chars=1_100)
        state = ContextState("custom", configured_window=2_000, reserve_tokens=100)
        state.history_message_ids = {id(item) for item in messages}
        batches = []

        def summarize(text, _limit):
            batches.append(text)
            return "summary value"

        result = compact_for_pressure(messages, state, summarize)
        digests = [item for item in result.messages if item["content"].startswith(DIGEST_PREFIX)]
        self.assertGreater(len(batches), 1)
        self.assertEqual(len(digests), 1)
        self.assertLess(len(digests[0]["content"]), 1_000)

    def test_summary_failure_trims_only_old_context_with_visible_marker(self):
        messages = _messages(old_count=6, chars=1_000)
        state = ContextState("custom", configured_window=2_000, reserve_tokens=100)
        state.history_message_ids = {id(item) for item in messages[:-1]}
        result = compact_for_pressure(messages, state, lambda _text, _limit: None)
        joined = "\n".join(item["content"] for item in result.messages)
        self.assertIn(OMISSION_PREFIX, joined)
        self.assertIn("recent-0:", joined)
        self.assertIn("pending request", joined)
        self.assertNotIn("old-0:", joined)
        self.assertEqual(state.status["compaction"]["status"], "trimmed_after_summary_failure")

    def test_tool_receipts_are_compacted_without_dropping_recent_receipts(self):
        lines = [f"tool-{index}:" + "z" * 600 for index in range(20)]
        messages = [
            {"role": "system", "content": "fixed"},
            {"role": "user", "content": "request"},
            {"role": "assistant", "content": "action"},
            {"role": "user", "content": "The tools returned:\n" + "\n".join(lines)},
        ]
        state = ContextState("custom", configured_window=1_200, reserve_tokens=20)
        result = compact_for_pressure(messages, state, lambda _text, _limit: "tool digest")
        tool_content = result.messages[-1]["content"]
        self.assertIn(DIGEST_PREFIX, tool_content)
        self.assertIn("tool-19:", tool_content)
        self.assertNotIn("tool-0:", tool_content)

    def test_unknown_window_overflow_compacts_and_retries_once(self):
        class Provider:
            config = ProviderConfig(provider="openai", model_simple="custom-model")

            def __init__(self):
                self.original_calls = []

            def chat(self, messages, _model=None):
                if any("UNTRUSTED TRANSCRIPT:" in item["content"] for item in messages):
                    return "digest summary", {"prompt_tokens": 12, "completion_tokens": 1}
                self.original_calls.append(messages)
                if len(self.original_calls) == 1:
                    raise RuntimeError("maximum context length exceeded")
                return "accepted", {"prompt_tokens": 20, "completion_tokens": 2}

        provider = Provider()
        messages = _messages(old_count=6, chars=900)
        previous_provider = main.llm_provider
        previous_memory = main.short_term_memory
        state = ContextState("custom-model")
        state.history_message_ids = {id(item) for item in messages[:-1]}
        state_token = main._active_context_state.set(state)
        stream_token = main._stream_event_callback.set(lambda _event: None)
        try:
            main.llm_provider = provider
            main.short_term_memory = [item for item in messages if item["role"] != "system"]
            reply = main._call_llm_with_spinner(messages, model="custom-model")
        finally:
            main.llm_provider = previous_provider
            main.short_term_memory = previous_memory
            main._stream_event_callback.reset(stream_token)
            main._active_context_state.reset(state_token)
        self.assertEqual(reply, "accepted")
        self.assertEqual(len(provider.original_calls), 2)
        self.assertTrue(any(DIGEST_PREFIX in item["content"] for item in provider.original_calls[1]))
        self.assertTrue(state.overflow_retried)

    def test_fixed_context_that_cannot_fit_does_not_call_or_retry_provider(self):
        class Provider:
            config = ProviderConfig(provider="openai", model_simple="gpt-4o")

            def __init__(self):
                self.calls = 0

            def chat(self, _messages, _model=None):
                self.calls += 1
                raise AssertionError("provider must not be called")

        provider = Provider()
        messages = [
            {"role": "system", "content": "x" * 10_000},
            {"role": "user", "content": "current work"},
        ]
        previous_provider = main.llm_provider
        state_token = main._active_context_state.set(ContextState("custom", configured_window=100, reserve_tokens=20))
        stream_token = main._stream_event_callback.set(lambda _event: None)
        try:
            main.llm_provider = provider
            reply = main._call_llm_with_spinner(messages, model="custom")
        finally:
            main.llm_provider = previous_provider
            main._stream_event_callback.reset(stream_token)
            main._active_context_state.reset(state_token)
        self.assertIn("cannot fit fixed instructions", reply)
        self.assertEqual(provider.calls, 0)

    def test_digest_survives_the_32_message_history_window(self):
        digest = {"role": "user", "content": DIGEST_PREFIX + " summary"}
        messages = [digest] + [
            {"role": "user" if index % 2 == 0 else "assistant", "content": str(index)}
            for index in range(40)
        ]
        retained = retain_context_digests(messages, 32)
        self.assertEqual(len(retained), 32)
        self.assertEqual(retained[0], digest)
        self.assertEqual(retained[-1]["content"], "39")


class ContextApiTests(unittest.TestCase):
    context = {
        "model": "gpt-4o", "window_tokens": 128_000, "window_source": "model_catalog",
        "input_tokens": 82_000, "input_source": "estimated", "reserve_tokens": 4_096,
        "remaining_tokens": 41_904,
        "breakdown": {"fixed_instructions": 2_000, "memory": 1_000, "conversation": 70_000,
                      "tool_results": 8_000, "pending_request": 1_000},
        "compaction": {"status": "summarized", "kind": "conversation", "omitted_entries": 8},
    }

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="openkyrozen-context-api-")
        self.addCleanup(self.directory.cleanup)
        self.memory = MemoryBank(Path(self.directory.name) / "state.sqlite3", workspace_id="context-api")
        self.original_sessions = server._sessions
        server._sessions = {}
        self.addCleanup(lambda: setattr(server, "_sessions", self.original_sessions))

    def _history_stub(self):
        return SimpleNamespace(current=lambda: None)

    def test_api_sse_ui_and_restore_expose_content_free_context_status(self):
        self.assertIn('id="context-display"', server.CHAT_HTML)
        self.assertIn('id="context-breakdown"', server.CHAT_HTML)
        self.assertIn("function renderContext", server.CHAT_HTML)

        def run(session, _message):
            session["context"] = dict(self.context)
            return "ok"

        request = _Request({"session_id": "context-api", "message": "hello"})

        async def exercise():
            response = await server.api_chat(request)
            stream = await server.api_chat_stream(request)
            chunks = []
            async for chunk in stream.body_iterator:
                chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
            return response, "".join(chunks)

        with patch.object(server._agent, "memory_bank", self.memory), \
                patch.object(server._agent, "history_manager", return_value=self._history_stub()), \
                patch.object(server, "_run_session_chat", side_effect=run), \
                patch.object(server, "_emit_chat_completed"):
            response, stream_text = asyncio.run(exercise())
            body = response
            self.assertEqual(body["context"]["window_tokens"], 128_000)
            self.assertIn('"event": "context"', stream_text)

            self.memory.store.append_event(
                "context.status", self.context, user_id=server._SERVER_ACTOR_ID,
                workspace_id=self.memory.workspace_id, session_id="restored-context",
            )
            restored = server._get_or_create_session("restored-context")
            self.assertEqual(restored["context"]["input_tokens"], 82_000)


if __name__ == "__main__":
    unittest.main()
