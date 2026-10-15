import tempfile
import unittest
from pathlib import Path
import os
from unittest.mock import patch

from openkyrozen.agent.conversation import (
    ConversationHistory, ConversationHistoryError, model_response_to_dict,
)
from openkyrozen.persistence.store import EventStore
from openkyrozen.providers.models import FinishReason, ModelResponse, ProviderContinuation, ToolCall
from openkyrozen.tools.models import ToolError, ToolResult


class ConversationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.directory.name) / "events.sqlite3")
        self.history = ConversationHistory(
            self.store, user_id="alice", workspace_id="project", session_id="session-a",
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_append_is_idempotent_and_private_from_generic_event_api(self):
        entry = self.history.entry("user.input", {"text": "private prompt"}, entry_id="entry-a")
        first = self.history.append(entry, expected_head=None)
        second = self.history.append(entry, expected_head=None)
        self.assertEqual(first, second)
        self.assertEqual([item.id for item in self.history.entries()], ["entry-a"])
        self.assertEqual(self.store.list_events(workspace_id="project", user_id="alice"), [])

    def test_conflicting_duplicate_and_stale_head_fail(self):
        entry = self.history.entry("user.input", {"text": "one"}, entry_id="entry-a")
        head = self.history.append(entry)
        with self.assertRaises(ConversationHistoryError):
            self.history.append(self.history.entry("user.input", {"text": "two"}, entry_id="entry-a"))
        with self.assertRaises(ConversationHistoryError):
            self.history.append(self.history.entry("user.input", {"text": "later"}), expected_head=None)
        self.assertEqual(self.history.head, head)

    def test_unsupported_entry_version_and_incomplete_native_group_fail_safely(self):
        from openkyrozen.providers.models import ProviderContractError

        with self.assertRaises(ConversationHistoryError):
            self.history.entry("user.input", {"text": "old"}, version=2)
        response = ModelResponse(
            text="", tool_calls=(ToolCall("call-1", "read_file", {"path": "a"}),),
            finish_reason=FinishReason.TOOL_REQUEST,
            continuation=ProviderContinuation("fixture", "m1", "test", {"opaque": True}),
        )
        self.history.append(self.history.entry("model.response", {
            "response": model_response_to_dict(response),
        }))
        with self.assertRaises(ProviderContractError):
            self.history.provider_messages(provider_id="fixture", model="m1")

    def test_entries_are_defensive_and_scope_isolated(self):
        payload = {"nested": {"value": 1}}
        entry = self.history.entry("user.input", payload, entry_id="entry-a")
        payload["nested"]["value"] = 2
        self.history.append(entry)
        payload_read = self.history.entries()[0].payload
        payload_read["nested"]["value"] = 3
        self.assertEqual(self.history.entries()[0].payload["nested"]["value"], 1)
        other = ConversationHistory(self.store, user_id="alice", workspace_id="project", session_id="session-b")
        self.assertEqual(other.entries(), ())

    def test_explicit_legacy_replacement_branches_without_deleting_prior_records(self):
        first = self.history.entry("user.input", {"text": "old"})
        self.history.append(first)
        self.history.reconcile_legacy([{"role": "user", "content": "replacement"}])
        self.assertIn(first.id, [entry.id for entry in self.history.entries()])
        self.assertEqual(self.history.display_messages(), [{"role": "user", "content": "replacement"}])

    def test_native_provider_projection_round_trips_continuation_and_results(self):
        response = ModelResponse(
            text="", tool_calls=(ToolCall("call-1", "read_file", {"path": "a|b"}),),
            usage={"prompt_tokens": 3}, finish_reason=FinishReason.TOOL_REQUEST,
            metadata={"provider": "stub", "model": "m1"},
            continuation=ProviderContinuation("stub", "m1", "fixture", {"opaque": [1, 2]}),
        )
        from openkyrozen.agent.conversation import model_response_to_dict, tool_result_to_dict
        encoded_response = model_response_to_dict(response)
        result = ToolResult(data={"contents": "same text"}, text="same text",
                            error=ToolError("missing", "not found"))
        self.history.append(self.history.entry("model.response", {"response": encoded_response}, call_id="call-1"))
        self.history.append(self.history.entry("tool.result", {
            "call_id": "call-1", "name": "read_file", "output": tool_result_to_dict(result), "is_error": True,
        }, call_id="call-1"))
        projected = self.history.provider_messages(provider_id="stub", model="m1")
        self.assertEqual(projected[0].continuation.payload, {"opaque": [1, 2]})
        self.assertEqual(projected[1].output["data"]["contents"], "same text")
        with self.assertRaises(ValueError):
            self.history.provider_messages(provider_id="stub", model="m2")

    def test_workspace_checkpoint_links_private_journal_and_rollback_selects_branch(self):
        from openkyrozen.workspace.history import HistoryManager
        from openkyrozen.workspace.context import source_scope_id
        root = Path(self.directory.name) / "workspace"
        state = Path(self.directory.name) / "state"
        root.mkdir()
        state.mkdir()
        manager = HistoryManager(self.store, root, state, source_scope_id=source_scope_id(root),
                                 user_id="alice", workspace_id="project", session_id="session-a")
        user = self.history.entry("user.input", {"text": "base"})
        self.history.append(user)
        baseline = manager.ensure_root()
        checkpoint = self.store.conversation_checkpoint(baseline["id"], user_id="alice",
                                                        workspace_id="project", session_id="session-a")
        self.assertEqual(checkpoint["journal_head"], user.id)
        later = self.history.entry("user.input", {"text": "abandoned"})
        self.history.append(later)
        node = manager.commit_turn(manager.begin_turn(conversation=[], interaction={}, tasks=[]),
                                   user_message="abandoned", assistant_message="done", conversation=[],
                                   interaction={}, tasks=[], journal_head=later.id)
        manager.rollback(baseline["id"], confirm="rollback", expected_head=node["id"])
        self.assertEqual([item.payload["text"] for item in self.history.active_entries()
                          if item.kind == "user.input"], ["base"])
        self.assertIn(later.id, [item.id for item in self.history.entries()])


class ConversationRuntimeTests(unittest.TestCase):
    def test_provider_error_is_not_journaled_as_a_completed_turn(self):
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.tools import ToolAdapters

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_WORKSPACE_ROOT": temp,
        }):
            root = Path(temp)
            app = build_application(surface="cli", memory=build_memory(root / "state.sqlite3", user_id="alice", index=None),
                                    tools=ToolAdapters(root), user_id="alice")
            try:
                runtime = app.runtime

                def failed_turn(*_args, **_kwargs):
                    runtime._turn_failed.set(True)
                    return "[LLM Error] provider failed"

                with patch.object(runtime, "_chat_turn_impl", side_effect=failed_turn):
                    runtime.chat(runtime.current_session, "question")
                outcome = [entry for entry in runtime.conversation_history.entries()
                           if entry.kind == "turn.outcome"][-1]
                self.assertEqual(outcome.payload["status"], "failed")
            finally:
                app.close()

    def test_session_restart_restores_private_journal_without_replaying_tools(self):
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.providers.models import FinishReason
        from openkyrozen.tools import ToolAdapters

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_WORKSPACE_ROOT": temp,
        }):
            root = Path(temp)
            database = root / "state.sqlite3"
            first = build_application(surface="cli", memory=build_memory(database, user_id="alice", index=None),
                                      tools=ToolAdapters(root), user_id="alice")
            try:
                runtime = first.runtime
                history = runtime.conversation_history
                history.append(history.entry("user.input", {"text": "remember me"}))
                history.append(history.entry("model.response", {"response": {
                    "text": "restored answer", "tool_calls": [], "usage": None,
                    "finish_reason": FinishReason.FINAL.value,
                    "metadata": {"provider": "fixture", "model": "m1"}, "continuation": None,
                }}))
            finally:
                first.close()

            second = build_application(surface="cli", memory=build_memory(database, user_id="alice", index=None),
                                       tools=ToolAdapters(root), user_id="alice")
            try:
                restored = second.runtime.open_session("surface:cli")
                self.assertEqual(restored.messages, [{"role": "user", "content": "remember me"}])
                projected = second.runtime.conversation_history.provider_messages(provider_id="fixture", model="m1")
                self.assertEqual(projected[-1], {"role": "assistant", "content": "restored answer"})
                self.assertFalse(any(item.kind.startswith("tool") for item in
                                     second.runtime.conversation_history.entries()))
            finally:
                second.close()

    def test_tool_receipt_records_correlated_typed_result_once(self):
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.providers.models import ToolCall
        from openkyrozen.tools import ToolAdapters

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_WORKSPACE_ROOT": temp,
            "KYROZEN_CLI_CAPABILITIES": "full", "KYROZEN_APPROVAL_MODE": "never",
        }):
            root = Path(temp)
            app = build_application(surface="cli", memory=build_memory(root / "state.sqlite3", user_id="alice", index=None),
                                    tools=ToolAdapters(root), user_id="alice")
            try:
                runtime = app.runtime
                turn = runtime._conversation_turn_id.set("turn-receipt")
                head = runtime._conversation_expected_head.set(None)
                try:
                    receipt = runtime.execute_call(runtime.current_session,
                                                   ToolCall("call-receipt", "calculate", {"expression": "2+2"}))
                finally:
                    runtime._conversation_expected_head.reset(head)
                    runtime._conversation_turn_id.reset(turn)
                results = [item for item in runtime.conversation_history.entries()
                           if item.kind == "tool.result"]
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].call_id, "call-receipt")
                self.assertEqual(results[0].payload["output"]["data"], receipt.tool_result.data)
                self.assertEqual(results[0].payload["is_error"], not receipt.success)
            finally:
                app.close()

    def test_repeated_equal_inputs_are_distinct_runtime_turns_and_interfaces_do_not_duplicate(self):
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.tools import ToolAdapters
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_WORKSPACE_ROOT": temp,
        }):
            root = Path(temp)
            app = build_application(surface="cli", memory=build_memory(root / "state.sqlite3", user_id="alice", index=None),
                                    tools=ToolAdapters(root), user_id="alice")
            try:
                runtime = app.runtime
                with patch.object(runtime, "_chat_turn_impl", return_value="same answer"):
                    self.assertEqual(runtime.chat(runtime.current_session, "same request"), "same answer")
                    self.assertEqual(runtime.chat(runtime.current_session, "same request"), "same answer")
                entries = runtime.conversation_history.entries()
                user_entries = [item for item in entries if item.kind == "user.input"]
                self.assertEqual([item.payload["text"] for item in user_entries[-2:]], ["same request", "same request"])
                self.assertEqual([item.kind for item in entries].count("turn.outcome"), 2)
                self.assertEqual(runtime.short_term_memory[-4:], [
                    {"role": "user", "content": "same request"},
                    {"role": "assistant", "content": "same answer"},
                    {"role": "user", "content": "same request"},
                    {"role": "assistant", "content": "same answer"},
                ])
                self.assertFalse(any(item["event_type"].startswith("conversation.") for item in
                                     runtime.memory_bank.store.list_events(
                                         workspace_id=runtime.interaction_workspace_id(), user_id="alice")))
            finally:
                app.close()

    def test_model_response_is_journaled_before_next_dispatch_and_write_failure_is_terminal(self):
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.providers.models import FinishReason, ModelResponse
        from openkyrozen.tools import ToolAdapters
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_WORKSPACE_ROOT": temp,
        }):
            root = Path(temp)
            app = build_application(surface="cli", memory=build_memory(root / "state.sqlite3", user_id="alice", index=None),
                                    tools=ToolAdapters(root), user_id="alice")
            try:
                runtime = app.runtime
                class Provider:
                    name = "fixture"
                    calls = 0
                    def chat_response(self, _messages, model=None):
                        self.calls += 1
                        return ModelResponse("answer", finish_reason=FinishReason.FINAL,
                                             metadata={"provider": self.name, "model": model})
                provider = Provider()
                runtime.llm_provider = provider
                turn = runtime._conversation_turn_id.set("turn-fixture")
                record = runtime._record_foreground_model.set(True)
                try:
                    response = runtime._get_model_response([{"role": "user", "content": "question"}], "fixture-model")
                    self.assertEqual(response.text, "answer")
                    saved = [entry for entry in runtime.conversation_history.entries()
                             if entry.kind == "model.response"]
                    self.assertEqual(saved[-1].payload["response"]["text"], "answer")
                    runtime.memory_bank.store.append_conversation_entry = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full"))
                    with self.assertRaises(OSError):
                        runtime._get_model_response([{"role": "user", "content": "question 2"}], "fixture-model")
                    self.assertEqual(provider.calls, 2)
                finally:
                    runtime._record_foreground_model.reset(record)
                    runtime._conversation_turn_id.reset(turn)
            finally:
                app.close()

    def test_context_uses_stable_prefix_metrics_and_does_not_inject_graph_context(self):
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.tools import ToolAdapters
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_WORKSPACE_ROOT": temp,
        }):
            root = Path(temp)
            app = build_application(surface="cli", memory=build_memory(root / "state.sqlite3", user_id="alice", index=None),
                                    tools=ToolAdapters(root), user_id="alice")
            try:
                runtime = app.runtime
                history = runtime.conversation_history
                history.append(history.entry("user.input", {"text": "old"}))
                history.append(history.entry("turn.outcome", {"status": "completed", "text": "old answer"}))
                runtime.short_term_memory = history.display_messages()
                with patch.object(runtime, "_project_graph_context", side_effect=AssertionError("graph was injected")):
                    first = runtime._build_messages("fresh one")
                    fingerprint = runtime._stable_prefix_info.get()["fingerprint"]
                    self.assertEqual(runtime._context_history_metrics.get()["new_entry_count"], 2)
                    second = runtime._build_messages("fresh two")
                    self.assertEqual(runtime._stable_prefix_info.get()["fingerprint"], fingerprint)
                    metrics = runtime._context_history_metrics.get()
                    self.assertEqual(metrics["retransmitted_entry_count"], 2)
                    self.assertEqual(metrics["duplicate_entry_count"], 0)
                self.assertEqual(sum(item["role"] == "user" and item["content"] == "fresh one" for item in first), 1)
                self.assertEqual(sum(item["role"] == "user" and item["content"] == "fresh two" for item in second), 1)
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
