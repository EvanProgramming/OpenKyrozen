import unittest
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from openkyrozen.agent.events import RuntimeEvent, RuntimeEventPublisher


class RuntimeEventContractTests(unittest.TestCase):
    def test_event_payload_is_validated_and_defensively_snapshotted(self):
        payload = {"nested": {"value": "before"}}
        event = RuntimeEvent(
            kind="tool.result", user_id="user", workspace_id="workspace",
            session_id="session", turn_id="turn", sequence=1, payload=payload,
        )
        payload["nested"]["value"] = "after"
        self.assertEqual(event.payload["nested"]["value"], "before")
        event.payload["nested"]["value"] = "mutated by consumer"
        self.assertEqual(event.payload["nested"]["value"], "before")
        with self.assertRaises((TypeError, ValueError)):
            RuntimeEvent(
                kind="tool.result", user_id="user", workspace_id="workspace",
                session_id="session", turn_id="turn", sequence=1,
                payload={"invalid": object()},
            )
        with self.assertRaises(ValueError):
            RuntimeEvent(kind="tool.result", user_id="u", workspace_id="w", session_id="s",
                         sequence=1, payload={}, event_id="", timestamp="not-a-time")

    def test_publisher_assigns_monotonic_order_and_isolates_observer_failure(self):
        published = []

        def observer(event):
            published.append(event)
            if event.kind == "model.text_delta":
                raise RuntimeError("observer failure")

        publisher = RuntimeEventPublisher(
            user_id="user", workspace_id="workspace", session_id="session",
            on_runtime_event=observer,
        )
        started = publisher.publish("turn.started", {"message": "hi"})
        delta = publisher.publish("model.text_delta", {"text": "hello"})
        self.assertEqual((started.sequence, delta.sequence), (1, 2))
        self.assertNotEqual(started.event_id, delta.event_id)
        self.assertEqual(started.turn_id, delta.turn_id)
        self.assertEqual(published, [started, delta])

    def test_concurrent_publish_keeps_observer_delivery_in_sequence_order(self):
        received = []
        publisher = RuntimeEventPublisher(
            user_id="user", workspace_id="workspace", session_id="session",
            on_runtime_event=lambda event: received.append(event.sequence),
        )
        entered, release = threading.Event(), threading.Event()
        def observe(event):
            if event.sequence == 1:
                entered.set()
                release.wait(timeout=1)
            received.append(event.sequence)
        publisher._runtime_observer = observe

        first = threading.Thread(target=publisher.publish, args=("tool.call", {"index": 1}))
        second = threading.Thread(target=publisher.publish, args=("tool.call", {"index": 2}))
        first.start()
        self.assertTrue(entered.wait(timeout=1))
        second.start()
        time.sleep(0.01)
        self.assertEqual(received, [])
        release.set()
        first.join(timeout=1)
        second.join(timeout=1)
        self.assertEqual(received, [1, 2])

    def test_turn_publishers_have_isolated_sequences_and_identities(self):
        first = RuntimeEventPublisher(user_id="u", workspace_id="w", session_id="s")
        second = RuntimeEventPublisher(user_id="u", workspace_id="w", session_id="s")
        a = first.publish("turn.started", {})
        b = second.publish("turn.started", {})
        self.assertEqual((a.sequence, b.sequence), (1, 1))
        self.assertNotEqual(a.turn_id, b.turn_id)

    def test_child_publisher_has_isolated_identity_sequence_and_status(self):
        parent = RuntimeEventPublisher(user_id="u", workspace_id="w", session_id="parent")
        child = parent.fork(session_id="child")
        child_event = child.publish("model.failed", {"error_type": "ProviderError"})
        parent_event = parent.publish("tool.call", {"tool_name": "read_file"})
        self.assertEqual(child_event.session_id, "child")
        self.assertEqual(child_event.sequence, 1)
        self.assertNotEqual(child_event.turn_id, parent_event.turn_id)
        self.assertTrue(child.last_model_failed)
        self.assertFalse(parent.last_model_failed)


class RuntimeEventChatTests(unittest.TestCase):
    def setUp(self):
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.tools import ToolAdapters

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.environment = patch.dict(os.environ, {
            "KYROZEN_DISABLE_VECTOR_INDEX": "1",
            "KYROZEN_WORKSPACE_ROOT": self.temporary.name,
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        root = Path(self.temporary.name)
        self.app = build_application(
            surface="cli", memory=build_memory(root / "state.sqlite3", user_id="alice", index=None),
            tools=ToolAdapters(root), user_id="alice",
        )
        self.addCleanup(self.app.close)

    def test_chat_publishes_ordered_canonical_events_and_legacy_projection(self):
        runtime = self.app.runtime
        events, legacy = [], []

        def run_turn(*_args, **_kwargs):
            runtime._emit_stream_event({"event": "content", "chunk": "literal Action: text"})
            return "done"

        with patch.object(runtime, "_chat_turn", side_effect=run_turn):
            reply = runtime.chat(runtime.current_session, "hi", on_event=legacy.append,
                                 on_runtime_event=events.append)
        self.assertEqual(reply, "done")
        self.assertEqual([item.kind for item in events], [
            "turn.started", "model.text_delta", "turn.completed",
        ])
        self.assertEqual([item.sequence for item in events], [1, 2, 3])
        self.assertEqual(events[1].payload, {"text": "literal Action: text", "legacy_protocol": True})
        self.assertEqual(legacy, [{"event": "content", "chunk": "literal Action: text"}])
        self.assertEqual(len({item.turn_id for item in events}), 1)
        self.assertTrue(all(item.session_id == runtime.current_session.session_id for item in events))

    def test_legacy_receipt_projection_is_bounded_before_publication(self):
        events, legacy = [], []
        publisher = RuntimeEventPublisher(
            user_id="alice", workspace_id="workspace", session_id="session",
            on_runtime_event=events.append, on_legacy_event=legacy.append,
        )
        event = publisher.publish_legacy({"event": "tool_receipt", "tool_receipt": {
            "call_id": "large-result", "result": "x" * 200_000,
        }})
        self.assertEqual(event.kind, "tool.result")
        self.assertLess(len(str(event.payload).encode()), 128_000)
        self.assertEqual(legacy[0]["event"], "tool_receipt")
        self.assertIn("<truncated>", legacy[0]["tool_receipt"]["result"])

    def test_tool_events_inherit_the_model_request_identity(self):
        runtime = self.app.runtime
        events = []

        def run_turn(*_args, **_kwargs):
            runtime._last_model_request_id.set("model-request-1")
            runtime._publish_runtime_event("tool.call", {"tool_name": "read_file"}, call_id="call-1")
            return "done"

        with patch.object(runtime, "_chat_turn", side_effect=run_turn):
            runtime.chat(runtime.current_session, "hi", on_runtime_event=events.append)
        tool_call = next(event for event in events if event.kind == "tool.call")
        self.assertEqual(tool_call.model_request_id, "model-request-1")

    def test_legacy_stream_request_identity_is_retained_for_tool_events(self):
        runtime = self.app.runtime
        events = []

        class LegacyStreamProvider:
            name = "legacy"

            def chat_stream(self, _messages, _model=None):
                yield "reply"

        runtime.llm_provider = LegacyStreamProvider()
        token = runtime._runtime_event_publisher.set(RuntimeEventPublisher(
            user_id="alice", workspace_id="workspace", session_id="session",
            on_runtime_event=events.append,
        ))
        try:
            runtime._get_llm_response([{"role": "user", "content": "hi"}], stream=True,
                                     on_chunk=lambda _chunk: None)
            runtime._publish_runtime_event("tool.call", {"tool_name": "read_file"}, call_id="call-1")
        finally:
            runtime._runtime_event_publisher.reset(token)
        requested = next(event for event in events if event.kind == "model.requested")
        tool_call = next(event for event in events if event.kind == "tool.call")
        self.assertEqual(tool_call.model_request_id, requested.model_request_id)

    def test_chat_emits_one_failed_terminal_event_when_execution_raises(self):
        runtime = self.app.runtime
        events = []
        with patch.object(runtime, "_chat_turn", side_effect=ValueError("failure")):
            with self.assertRaisesRegex(ValueError, "failure"):
                runtime.chat(runtime.current_session, "hi", on_runtime_event=events.append)
        self.assertEqual([item.kind for item in events], ["turn.started", "turn.failed"])
        self.assertEqual(events[-1].payload, {"error_type": "ValueError"})

    def test_chat_emits_cancelled_terminal_event_for_interrupt(self):
        runtime = self.app.runtime
        events = []
        with patch.object(runtime, "_chat_turn", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                runtime.chat(runtime.current_session, "hi", on_runtime_event=events.append)
        self.assertEqual([event.kind for event in events], ["turn.started", "turn.cancelled"])

    def test_chat_emits_cancelled_terminal_event_for_async_cancellation(self):
        import asyncio
        runtime = self.app.runtime
        events = []
        with patch.object(runtime, "_chat_turn", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                runtime.chat(runtime.current_session, "hi", on_runtime_event=events.append)
        self.assertEqual([event.kind for event in events], ["turn.started", "turn.cancelled"])

    def test_native_response_text_does_not_enter_legacy_control_parser(self):
        runtime = self.app.runtime
        text = "Action: ordinary prose\nTaskList: ordinary prose"
        token = runtime._last_response_is_native.set(True)
        try:
            with patch.object(runtime, "_collect_tool_calls", side_effect=AssertionError("legacy parser used")), \
                    patch.object(runtime, "_collect_unwrapped_tool_calls", side_effect=AssertionError("legacy parser used")):
                parsed = runtime._parse_model_response(text)
                self.assertEqual(parsed["clean"], text)
                self.assertEqual(parsed["tool_calls"], [])
                self.assertFalse(parsed["has_tasklist"])
                self.assertEqual(runtime._clean_final_response(text), text)
        finally:
            runtime._last_response_is_native.reset(token)

    def test_denied_structured_call_has_receipt_but_no_execution_start(self):
        from openkyrozen.providers.models import ToolCall

        runtime = self.app.runtime
        events = []
        with runtime.use_session(runtime.current_session):
            token = runtime._runtime_event_publisher.set(RuntimeEventPublisher(
                user_id="alice", workspace_id="workspace", session_id="session",
                on_runtime_event=events.append,
            ))
            try:
                receipt = runtime.execute_call(
                    runtime.current_session,
                    ToolCall("call-denied", "write_file", {"path": "blocked.txt", "content": "x"}),
                    capabilities=[],
                )
            finally:
                runtime._runtime_event_publisher.reset(token)
        self.assertFalse(receipt.success)
        self.assertEqual(receipt.call_id, "call-denied")
        self.assertNotIn("tool.execution_started", [event.kind for event in events])
        result = next(event for event in events if event.kind == "tool.result")
        self.assertEqual(result.call_id, "call-denied")
        self.assertFalse(result.payload["success"])

    def test_task_events_are_published_after_durable_append(self):
        tasks = self.app.runtime.current_session.tasks
        observed = []

        def after_append(kind, payload, task_id):
            observed.append((kind, task_id, tasks.store.list_events(
                kind, workspace_id=tasks.workspace_id, session_id=tasks.session_id,
                user_id=tasks.user_id,
            )))

        token = tasks._runtime_event_callback.set(after_append)
        try:
            task_id = tasks.add_task("event persistence check")
        finally:
            tasks._runtime_event_callback.reset(token)
        self.assertEqual(observed[0][0:2], ("task.created", tasks.tasks[task_id]["id"]))
        self.assertTrue(observed[0][2])

    def test_approval_events_are_correlated_and_do_not_expose_arguments(self):
        from openkyrozen.security.approvals import ApprovalRequest

        runtime = self.app.runtime
        events = []
        from openkyrozen.security.permissions import _call_approval_callback
        token = runtime._runtime_event_publisher.set(RuntimeEventPublisher(
            user_id="alice", workspace_id="workspace", session_id="session",
            on_runtime_event=events.append,
        ))
        request = ApprovalRequest(
            call_id="call-approval", tool_name="write_file", arguments={"path": "secret.txt"},
            workspace_id="workspace", session_id="session", tool_version="1",
            tool_source="builtin", capability="write", risk="high", side_effects="filesystem",
        )
        try:
            self.assertTrue(_call_approval_callback(runtime, lambda _request: True, "write_file", {}, request=request))
            nested = lambda *_args: _call_approval_callback(
                runtime, lambda _request: True, "write_file", {}, request=request,
            )
            nested._runtime_approval_wrapper = True
            self.assertTrue(_call_approval_callback(runtime, nested, "write_file", {}, request=request))
        finally:
            runtime._runtime_event_publisher.reset(token)
        self.assertEqual([event.kind for event in events], [
            "approval.requested", "approval.resolved", "approval.requested", "approval.resolved",
        ])
        self.assertTrue(all(event.call_id == "call-approval" for event in events))
        self.assertEqual(events[-1].payload["outcome"], "approved")
        self.assertNotIn("secret.txt", str([event.payload for event in events]))

    def test_missing_provider_is_a_failed_turn_with_correlated_request(self):
        runtime = self.app.runtime
        events = []
        runtime.llm_provider = None
        with patch.object(runtime.execution_context, "provider", None), \
                patch.object(runtime, "_chat_turn", side_effect=lambda *_args, **_kwargs:
                    runtime._get_llm_response([{"role": "user", "content": "hi"}])):
            with self.assertRaises(runtime.ProviderUnavailableError):
                runtime.chat(runtime.current_session, "hi", on_runtime_event=events.append)
        requested = next(event for event in events if event.kind == "model.requested")
        failed = next(event for event in events if event.kind == "model.failed")
        self.assertEqual(failed.model_request_id, requested.model_request_id)
        self.assertEqual(events[-1].kind, "turn.failed")

    def test_context_preparation_failure_is_not_reported_as_completed(self):
        from openkyrozen.agent.types import ContextTooLargeError

        runtime = self.app.runtime
        events = []

        def run_turn(*_args, **_kwargs):
            with patch.object(runtime, "_prepare_context_for_call", side_effect=ContextTooLargeError("too large")):
                return runtime._call_llm_with_spinner([{"role": "user", "content": "hi"}])

        with patch.object(runtime, "_chat_turn", side_effect=run_turn):
            runtime.chat(runtime.current_session, "hi", on_runtime_event=events.append)
        self.assertEqual([event.kind for event in events], ["turn.started", "turn.failed"])

    def test_rejected_native_call_still_emits_correlated_tool_call(self):
        from openkyrozen.providers.models import ToolCall

        runtime = self.app.runtime
        events = []

        def run_turn(*_args, **_kwargs):
            return runtime.execute_call(
                runtime.current_session, ToolCall("invalid-task-call", "calculate", {"expression": "2+2"}),
                task_id="missing-task",
            ).result

        with patch.object(runtime, "_chat_turn", side_effect=run_turn):
            runtime.chat(runtime.current_session, "hi", on_runtime_event=events.append)
        kinds = [event.kind for event in events]
        self.assertLess(kinds.index("tool.call"), kinds.index("tool.result"))
        self.assertNotIn("tool.execution_started", kinds)
        tool_call = next(event for event in events if event.kind == "tool.call")
        tool_result = next(event for event in events if event.kind == "tool.result")
        self.assertEqual(tool_call.call_id, "invalid-task-call")
        self.assertEqual(tool_result.call_id, "invalid-task-call")
        self.assertEqual(kinds[-1], "turn.completed")


if __name__ == "__main__":
    unittest.main()
