"""Native engine context checks using real runtime/compaction and offline providers."""
import copy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from openkyrozen.agent.compaction import ContextState, DIGEST_PREFIX, estimate_messages
from openkyrozen.agent.engine import AgentEngine
from openkyrozen.agent.turn import TurnContext
from openkyrozen.app.bootstrap import build_application, build_memory
from openkyrozen.providers import ProviderConfig
from openkyrozen.providers.base import LLMProvider
from openkyrozen.providers.errors import ProviderError, ProviderErrorKind
from openkyrozen.providers.models import (
    FinishReason, ModelResponse, ProviderCapabilities, ResponseComplete, ResponseError, TextDelta,
    ToolCall, ToolReply, provider_continuation, provider_identity, validate_tool_history,
)
from openkyrozen.tools import ToolAdapters


class NativeContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {"KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_APPROVAL_MODE": "never"})
        env.start()
        self.addCleanup(env.stop)
        self.app = build_application(surface="cli", memory=build_memory(self.root / "state.sqlite3", index=None),
                                     tools=ToolAdapters(self.root))
        self.addCleanup(self.app.close)
        self.runtime = self.app.runtime
        self.runtime._set_workspace_root(self.root)
        self.runtime.set_interaction_mode("agent")

    def test_tracked_native_history_compacts_atomic_groups_and_keeps_current_supersession(self):
        from openkyrozen.agent.compaction import compact_for_pressure
        old_user = {"role": "user", "content": "Original region eu-west-2; owner team-cedar; " + "x" * 2000}
        old_call = ModelResponse(tool_calls=(ToolCall("old-call", "read_file", {"path": "facts.txt"}),),
                                 finish_reason=FinishReason.TOOL_REQUEST)
        old_reply = ToolReply("old-call", "read_file", {"text": "rollback release-17 " + "x" * 2000})
        old_final = ModelResponse(text="Earlier region eu-west-2", finish_reason=FinishReason.FINAL)
        recent = [{"role": "user", "content": "Latest approved region ap-southeast-1"},
                  ModelResponse(text="Updated region", finish_reason=FinishReason.FINAL),
                  {"role": "user", "content": "Keep team-cedar"},
                  ModelResponse(text="Retained owner", finish_reason=FinishReason.FINAL)]
        unfinished = ModelResponse(tool_calls=(ToolCall("pending", "read_file", {"path": "pending.txt"}),),
                                   finish_reason=FinishReason.TOOL_REQUEST)
        current = {"role": "user", "content": "Return the current region and original rollback"}
        history = [old_user, old_call, old_reply, old_final, *recent]
        messages = [{"role": "system", "content": "Pinned state"}, *history, unfinished, current]
        original = list(messages)
        state = ContextState("fixture", configured_window=1800, reserve_tokens=100,
                             history_message_ids={id(item) for item in history + [unfinished]})
        seen = []
        def summarize(text, budget):
            seen.append(text)
            return "Original region eu-west-2; owner team-cedar; rollback release-17"
        result = compact_for_pressure(messages, state, summarize, force=True)
        self.assertFalse(result.impossible)
        self.assertTrue(result.compacted)
        self.assertEqual(messages, original)  # The canonical input is never mutated.
        self.assertIn("rollback release-17", "".join(seen))
        self.assertNotIn("Latest approved region", "".join(seen))
        for item in [*recent, unfinished, current]:
            self.assertTrue(any(candidate is item for candidate in result.messages))
        for item in [old_call, old_reply]:
            self.assertFalse(any(candidate is item for candidate in result.messages))
        self.assertIn("rollback release-17", result.history_digest["content"])

    def history(self):
        return ([{"role": "system", "content": "Keep the current task and trusted tool correlation."}]
                + [{"role": "user" if index % 2 == 0 else "assistant", "content": f"old-{index}:" + "x" * 1200}
                   for index in range(6)]
                + [{"role": "user" if index % 2 == 0 else "assistant", "content": f"recent-{index}: keep cedar decision"}
                   for index in range(4)])

    def run_native(self, provider, messages, *, window=None, complexity="simple", permitted=None):
        runtime = self.runtime
        context = copy.copy(runtime.execution_context)
        context.provider, context.model = provider, "context-fixture"
        token = runtime._turn_context.set(context)
        self.addCleanup(runtime._turn_context.reset, token)
        state = ContextState("context-fixture", configured_window=window, reserve_tokens=100)
        state.history_message_ids = {id(item) for item in messages[:-1]}
        runtime.short_term_memory = list(messages[1:-1])
        state_token = runtime._active_context_state.set(state)
        self.addCleanup(runtime._active_context_state.reset, state_token)
        def prepare(turn):
            turn.interaction_mode, turn.complexity = "agent", complexity
            turn.turn_start, turn.learning_run, turn.learned_context = time.time(), {"run_id": "context-fixture"}, ""
        turn = TurnContext("Continue the current work")
        with patch.object(runtime, "_permitted_tool_names", return_value=permitted or {"read_file", "write_file"}), \
                patch.object(runtime, "_prepare_turn", side_effect=prepare), \
                patch.object(runtime, "_build_messages", return_value=messages), \
                patch.object(runtime, "_summarize_context_with_chat_model", return_value="Decision: keep cedar; current work remains."), \
                patch.object(runtime, "_finish_learning_run", side_effect=lambda _run, _receipts, _task, answer, *_args: answer), \
                patch.object(runtime, "_schedule_post_turn_learning"):
            answer = AgentEngine(runtime).run(turn)
        return answer, turn, state

    def provider(self, callback, *, stream=False):
        class Provider(LLMProvider):
            def __init__(self):
                super().__init__(ProviderConfig(provider="custom", model_simple="context-fixture"))
                self.requests = []
                self.tool_requests = []
            def get_capabilities(self, model=None):
                return ProviderCapabilities(native_tools=True, text_streaming=stream, streaming_tool_calls=stream)
            def chat(self, messages, model=None):
                raise AssertionError("native context entered legacy text boundary")
            def chat_response(self, messages, model=None, *, tools=None):
                self.requests.append(copy.deepcopy(messages))
                self.tool_requests.append(copy.deepcopy(tools))
                return callback(self, messages, model)
            def chat_response_stream(self, messages, model=None, *, tools=None):
                self.requests.append(copy.deepcopy(messages))
                self.tool_requests.append(copy.deepcopy(tools))
                yield from callback(self, messages, model)
        return Provider()

    def availability(self, messages):
        blocks = [item["content"] for item in messages if isinstance(item, dict)
                  and item.get("role") == "system"
                  and item.get("content", "").startswith("## Current native tool availability\n")]
        self.assertEqual(len(blocks), 1)
        return blocks[0], json.loads(blocks[0].splitlines()[1])

    def test_guidance_replacement_preserves_history_mapping_and_fixed_token_accounting(self):
        provider = self.provider(lambda *_args: ModelResponse(text="Ready", finish_reason=FinishReason.FINAL))
        context = copy.copy(self.runtime.execution_context)
        context.provider = provider
        turn_token = self.runtime._turn_context.set(context)
        self.addCleanup(self.runtime._turn_context.reset, turn_token)
        state = ContextState("context-fixture", configured_window=5000, reserve_tokens=100)
        state_token = self.runtime._active_context_state.set(state)
        self.addCleanup(self.runtime._active_context_state.reset, state_token)
        history = {"role": "user", "content": "Earlier decision"}
        entry = self.runtime.conversation_history.entry("user.input", {"text": history["content"]})
        self.runtime.conversation_history.append(entry)
        messages = [{"role": "system", "content": "Fixed instructions"}, history,
                    {"role": "user", "content": "Current work"}]
        mapping_token = self.runtime._context_history_projection_ids.set((id(messages), {1: entry.id}, (1, 2)))
        self.addCleanup(self.runtime._context_history_projection_ids.reset, mapping_token)
        for names in ({"read_file"}, set()):
            self.runtime._get_model_response(messages, "context-fixture", tool_names=names)
            _, projection = self.availability(messages)
            self.assertEqual(projection["current_call_tools"], sorted(names))
            mapping = self.runtime._context_history_projection_ids.get()
            self.assertEqual(mapping, (id(messages), {2: entry.id}, (2, 3)))
            self.assertIs(messages[2], history)
            from openkyrozen.providers.context import context_accounting_messages
            _, breakdown = estimate_messages(context_accounting_messages(messages))
            self.assertEqual(state.status["breakdown"]["fixed_instructions"],
                             breakdown["fixed_instructions"] + state.native_input_overhead_tokens)

        # The same projection is fixed input, so a smaller window must fail before dispatch.
        state.window_tokens = 200
        from openkyrozen.agent.types import ContextTooLargeError
        with self.assertRaises(ContextTooLargeError):
            self.runtime._get_model_response(messages, "context-fixture", tool_names=set())
        self.assertEqual(len(provider.requests), 2)

    def test_real_plan_gate_expansion_read_and_completed_confirmation_have_current_guidance(self):
        (self.root / "notes.txt").write_text("ready\n", encoding="utf-8")
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                if streaming:
                    self.runtime.tasks.clear()
                def respond(provider, messages, model):
                    step = len(provider.requests)
                    names = {tool["name"] for tool in provider.tool_requests[-1]}
                    if step in {1, 3, 4}:
                        self.assertEqual(names, {"update_plan", "read_tool_artifact"}
                                         | ({"read_file"} if step > 1 else set()))
                    else:
                        self.assertIn("read_file", names)
                    block, projection = self.availability(messages)
                    current = names - ({"read_file"} if step > 2 else set())
                    self.assertEqual(projection["current_call_tools"], sorted(current))
                    self.assertEqual(projection["history_only_declarations"], ["read_file"] if step > 2 else [])
                    self.assertIn(("Planning-required exposure is temporary" if step == 1 else
                                   "An active Working Plan may expand" if step == 2 else
                                   "Completed-plan exposure is confirmation-only"), block)
                    validate_tool_history(messages, provider_id=provider_identity(provider), model=model, native=True)
                    if step == 1:
                        call = ToolCall("create-plan", "update_plan", {"expected_revision": 0, "operations": [
                            {"op": "add", "ref": "inspect", "task": {"description": "Inspect notes.txt",
                                "acceptance": [{"criterion": "ready marker exists", "verification": {
                                    "action": "read_file", "arguments": {"path": "notes.txt"},
                                    "expect": {"contains": "ready"}}}]}},
                            {"op": "start", "task": "inspect"}]})
                    elif step == 2:
                        reply = next(item for item in messages if isinstance(item, ToolReply)
                                     and item.call_id == "create-plan")
                        self.assertFalse(reply.is_error, reply.output)
                        call = ToolCall("inspect-file", "read_file", {"path": "notes.txt"})
                    elif step == 3:
                        reply = next(item for item in messages if isinstance(item, ToolReply)
                                     and item.call_id == "inspect-file")
                        self.assertFalse(reply.is_error, reply.output)
                        self.assertEqual(reply.output["data"]["content"], "ready\n")
                        pinned = next(item["content"] for item in messages if isinstance(item, dict)
                                      and item.get("content", "").startswith("## Pinned Working Plan state\n"))
                        snapshot = json.loads(pinned.splitlines()[-1])
                        self.assertEqual(snapshot["status"], "completed")
                        call = ToolCall("confirm-plan", "update_plan", {"plan_id": snapshot["plan_id"],
                            "expected_revision": snapshot["revision"], "operations": [
                                {"op": "suggest_complete", "task": snapshot["tasks"][0]["id"]}]})
                    else:
                        reply = next(item for item in messages if isinstance(item, ToolReply)
                                     and item.call_id == "confirm-plan")
                        self.assertFalse(reply.is_error, reply.output)
                        return ModelResponse(text="The ready marker was read and verified.", finish_reason=FinishReason.FINAL)
                    return ModelResponse(tool_calls=(call,), finish_reason=FinishReason.TOOL_REQUEST,
                        continuation=provider_continuation(provider, model, "fixture", {"call": call.id}))
                def stream(provider, messages, model):
                    yield ResponseComplete(respond(provider, messages, model))
                provider = self.provider(stream if streaming else respond, stream=streaming)
                answer, turn, _state = self.run_native(provider,
                    [{"role": "system", "content": "Inspect and verify the requested file."},
                     {"role": "user", "content": "Verify the ready marker in notes.txt"}],
                    complexity="complex", permitted=self.runtime._permitted_tool_names())
                self.assertEqual(len(provider.requests), 4, answer)
                self.assertEqual(turn.runtime_status, "completed", answer)
                self.assertEqual(self.runtime.tasks.working_plan.status, "completed")
                self.assertTrue(any(record["action"] == "read_file" and record["success"]
                                    and record.get("task_id") for record in turn.tool_records))

    def test_native_revises_seeded_plan_with_a_verifiable_command(self):
        test_file = self.root / "test_edge.py"
        test_file.write_text("import unittest\nclass Check(unittest.TestCase):\n def test_edge(self): self.assertEqual(0, 0)\n")
        self.runtime.tasks.edit_plan(complexity="complex", expected_revision=0, operations=[
            {"op": "add", "ref": "seed", "task": {"description": "Inspect edge cases",
                "acceptance": ["Complete the independent fixture objective."]}},
        ])
        command = {"command": "python -B -m unittest test_edge.py"}

        def respond(provider, messages, _model):
            snapshot = self.runtime.tasks.working_plan_snapshot()
            if len(provider.requests) == 1:
                call = ToolCall("revise", "update_plan", {"plan_id": snapshot["plan_id"],
                    "expected_revision": snapshot["revision"], "operations": [
                        {"op": "cancel", "task": snapshot["tasks"][0]["id"]},
                        {"op": "add", "ref": "check", "task": {"description": "Verify discovered edge",
                            "acceptance": [{"criterion": "Edge test passes", "verification": {
                                "action": "run_cmd", "arguments": command}}]}},
                        {"op": "start", "task": "check"},
                    ]})
            elif len(provider.requests) == 2:
                self.assertEqual(snapshot["tasks"][-1]["status"], "running")
                call = ToolCall("check-edge", "run_cmd", command)
            else:
                self.assertEqual(snapshot["status"], "completed")
                return ModelResponse(text="The edge test passed.", finish_reason=FinishReason.FINAL)
            return ModelResponse(tool_calls=(call,), finish_reason=FinishReason.TOOL_REQUEST,
                continuation=provider_continuation(provider, _model, "fixture", {"call": call.id}))

        with patch.object(self.runtime, "_effective_execution_capabilities",
                          return_value={"read", "write", "shell"}):
            answer, turn, _ = self.run_native(self.provider(respond),
                [{"role": "user", "content": "Verify the discovered edge case."}],
                complexity="complex", permitted={"update_plan", "run_cmd", "read_tool_artifact"})
        self.assertEqual(turn.runtime_status, "completed", answer)
        self.assertEqual([task["status"] for task in self.runtime.tasks.tasks], ["cancelled", "succeeded"])

    def continuation_plan(self, *, typed=True):
        (self.root / "notes.txt").write_text("ready\n")
        return self.runtime.tasks.edit_plan(complexity="complex", expected_revision=0, operations=[{
            "op": "add", "ref": "inspect", "task": {"description": "Inspect notes.txt",
                "acceptance": [{"criterion": "ready marker", "verification": {
                    "action": "read_file", "arguments": {"path": "notes.txt"},
                    "expect": {"contains": "ready"}}}] if typed else ["Read and explain notes.txt"]},
        }])

    def test_native_missing_plan_refusal_gets_one_structured_handoff_within_steps(self):
        for max_steps, expected in ((0, 1), (50, 2)):
            with self.subTest(max_steps=max_steps):
                self.runtime.tasks.clear()
                self.runtime.execution_context.max_steps = max_steps
                turn_id = f"missing-{max_steps}"
                token = self.runtime._conversation_turn_id.set(turn_id)
                self.addCleanup(self.runtime._conversation_turn_id.reset, token)
                def respond(provider, messages, model):
                    return ModelResponse(text="I will not create a plan.", finish_reason=FinishReason.FINAL,
                        usage={"prompt_tokens": 7, "completion_tokens": 3})
                provider = self.provider(respond)
                answer, turn, _ = self.run_native(provider, [{"role": "user", "content": "Inspect the source"}],
                    complexity="complex", permitted=self.runtime._permitted_tool_names())
                self.assertEqual(len(provider.requests), expected, answer)
                if expected == 2:
                    messages = provider.requests[1]
                    state = json.loads(messages[-1]["content"])["working_plan_continuation"]
                    self.assertEqual((state["plan_id"], state["revision"], state["status"]), (None, 0, "required"))
                    self.assertEqual(self.availability(messages)[1]["current_call_tools"],
                                     ["read_tool_artifact", "update_plan"])
                self.assertEqual((turn.turn_prompt_total, turn.turn_completion_total), (7 * expected, 3 * expected))
                self.assertEqual(turn.runtime_status, "incomplete")
                self.assertIsNone(self.runtime.tasks.working_plan)
                self.assertFalse(turn.tool_records)
                entries = [e for e in self.runtime.conversation_history.entries()
                           if e.kind == "plan.continuation" and e.turn_id == turn_id]
                self.assertEqual(len(entries), expected - 1)
                if entries:
                    self.assertEqual(entries[0].payload, {"plan_id": None, "revision": 0, "reason": "plan_required"})

    def test_native_missing_plan_feedback_allows_creation_without_rearming_the_allowance(self):
        (self.root / "notes.txt").write_text("ready\n")
        for verify, expected in ((True, 4), (False, 3)):
            with self.subTest(verify=verify):
                self.runtime.tasks.clear()
                self.runtime.execution_context.max_steps = 10
                def respond(provider, messages, model):
                    step = len(provider.requests)
                    usage = {"prompt_tokens": 7, "completion_tokens": 3}
                    if step == 2:
                        call = ToolCall("create", "update_plan", {"expected_revision": 0, "operations": [
                            {"op": "add", "ref": "inspect", "task": {"description": "Read ready marker",
                                "acceptance": [{"criterion": "ready marker", "verification": {
                                    "action": "read_file", "arguments": {"path": "notes.txt"},
                                    "expect": {"contains": "ready"}}}]}},
                            {"op": "start", "task": "inspect"}]})
                    elif step == 3 and verify:
                        call = ToolCall("read", "read_file", {"path": "notes.txt"})
                    else:
                        return ModelResponse(text="The source is ready.", finish_reason=FinishReason.FINAL, usage=usage)
                    return ModelResponse(tool_calls=(call,), finish_reason=FinishReason.TOOL_REQUEST, usage=usage,
                        continuation=provider_continuation(provider, model, "fixture", {"call": call.id}))
                provider = self.provider(respond)
                answer, turn, _ = self.run_native(provider, [{"role": "user", "content": "Verify notes.txt"}],
                    complexity="complex", permitted=self.runtime._permitted_tool_names())
                self.assertEqual(len(provider.requests), expected, answer)
                self.assertEqual((turn.turn_prompt_total, turn.turn_completion_total), (7 * expected, 3 * expected))
                self.assertEqual(turn.runtime_status, "completed" if verify else "incomplete")
                self.assertEqual(self.runtime.tasks.tasks[0]["status"], "succeeded" if verify else "running")

    def test_native_plan_feedback_preserves_bounded_pinned_context_and_full_acceptance(self):
        criterion = "unverified " + "x" * 400000
        plan = self.runtime.tasks.edit_plan(complexity="complex", expected_revision=0, operations=[{
            "op": "add", "ref": f"inspect-{i}", "task": {"description": f"Inspect source {i}",
                "acceptance": [criterion if i == 0 else "Source must be checked"]},
        } for i in range(13)])
        provider = self.provider(lambda *_: ModelResponse(text="This work is still pending.", finish_reason=FinishReason.FINAL))
        answer, turn, _ = self.run_native(provider, [{"role": "user", "content": "Inspect source"}],
            window=30000, complexity="complex", permitted=self.runtime._permitted_tool_names())
        self.assertEqual(len(provider.requests), 2, answer)
        feedback = provider.requests[1][-1]["content"]
        self.assertLess(len(feedback), 3000)
        payload = json.loads(feedback)["working_plan_continuation"]
        self.assertEqual(payload["eligible_task_count"], 13)
        self.assertEqual(len(payload["eligible_tasks"]), 12)
        self.assertEqual(payload["eligible_tasks"][0], {"id": plan["task_ids"][0], "status": "pending"})
        pinned = next(m["content"] for m in provider.requests[1] if isinstance(m, dict)
                      and m.get("content", "").startswith("## Pinned Working Plan state\n"))
        self.assertIn("artifact_id", json.loads(pinned.splitlines()[-1]))
        self.assertEqual(self.runtime.tasks.tasks[0]["acceptance"], [criterion])
        self.assertEqual(turn.runtime_status, "incomplete")
        self.assertFalse(turn.tool_records)

    def test_native_planned_empty_or_length_preserves_single_handoff_and_simple_fallback(self):
        for kind, expected in (("planned", 2), ("length", 1), ("blocked", 1), ("unknown", 1), ("simple", 3)):
            with self.subTest(kind=kind):
                self.runtime.tasks.recover()
                self.runtime.tasks.clear()
                plan = self.continuation_plan()
                if kind == "blocked":
                    self.runtime.tasks.edit_plan(plan_id=plan["plan_id"], expected_revision=1, operations=[{
                        "op": "block", "task": plan["task_ids"][0], "reason": "Access unavailable"}])
                elif kind == "unknown":
                    task = self.runtime.tasks.tasks[0]
                    task["checkpoint"] = {"recovery_effect": {"status": "unknown"}}
                    self.assertTrue(self.runtime.tasks._persist(task, expected_status="pending"))
                def respond(provider, messages, model):
                    if kind == "simple" and len(provider.requests) == 3:
                        return ModelResponse(text="The simple answer recovered.", finish_reason=FinishReason.FINAL)
                    return ModelResponse(text="Truncated" if kind == "length" else "",
                        finish_reason=FinishReason.LENGTH if kind == "length" else FinishReason.FINAL)
                provider = self.provider(respond)
                answer, turn, _ = self.run_native(provider, [{"role": "user", "content": "Inspect source"}],
                    complexity="simple" if kind == "simple" else "complex", permitted=self.runtime._permitted_tool_names())
                self.assertEqual(len(provider.requests), expected, answer)
                self.assertEqual(turn.runtime_status, "completed" if kind == "simple" else "blocked" if kind == "blocked" else "incomplete")
                if kind == "length":
                    self.assertIn("length limit", turn.protocol_error_message)
                self.assertFalse(turn.tool_records)

    def test_native_premature_final_continues_verified_plan_without_starting_work_itself(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                self.runtime.tasks.clear()
                plan = self.continuation_plan()
                turn_id = f"r164-{streaming}"
                token = self.runtime._conversation_turn_id.set(turn_id)
                self.addCleanup(self.runtime._conversation_turn_id.reset, token)
                def respond(provider, messages, model):
                    step = len(provider.requests)
                    validate_tool_history(messages, provider_id=provider_identity(provider), model=model, native=True)
                    usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
                    if step in {1, 4}:
                        return ModelResponse(text="The ready marker is verified.", finish_reason=FinishReason.FINAL, usage=usage)
                    if step == 2:
                        self.assertEqual(self.runtime.tasks.tasks[0]["status"], "pending")
                        self.assertEqual(self.runtime.tasks.tasks[0].get("evidence", []), [])
                        payload = json.loads(messages[-1]["content"])["working_plan_continuation"]
                        self.assertEqual((payload["plan_id"], payload["revision"]), (plan["plan_id"], 1))
                        self.assertEqual(payload["eligible_tasks"][0]["id"], plan["task_ids"][0])
                        self.assertEqual(payload["eligible_tasks"][0]["status"], "pending")
                        call = ToolCall("start", "update_plan", {"plan_id": plan["plan_id"], "expected_revision": 1,
                            "operations": [{"op": "start", "task": plan["task_ids"][0]}]})
                    else:
                        self.assertEqual(step, 3)
                        call = ToolCall("verify", "read_file", {"path": "notes.txt"})
                    return ModelResponse(tool_calls=(call,), finish_reason=FinishReason.TOOL_REQUEST, usage=usage,
                        continuation=provider_continuation(provider, model, "fixture", {"call": call.id}))
                def stream(provider, messages, model):
                    yield ResponseComplete(respond(provider, messages, model))
                provider = self.provider(stream if streaming else respond, stream=streaming)
                answer, turn, _ = self.run_native(provider, [{"role": "user", "content": "Verify notes.txt"}],
                    complexity="complex", permitted=self.runtime._permitted_tool_names())
                self.assertEqual(len(provider.requests), 4, answer)
                self.assertEqual((turn.turn_prompt_total, turn.turn_completion_total), (28, 12))
                self.assertEqual(turn.runtime_status, "completed", answer)
                self.assertEqual(self.runtime.tasks.working_plan.status, "completed")
                self.assertEqual([r["action"] for r in turn.tool_records], ["update_plan", "read_file"])
                entries = [e for e in self.runtime.conversation_history.entries()
                           if e.kind == "plan.continuation" and e.turn_id == turn_id]
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0].payload, {"plan_id": plan["plan_id"], "revision": 1,
                                                     "reason": "required_work_remains"})
                self.assertTrue(entries[0].request_id)
                self.assertFalse(any("required_work_remains" in json.dumps(m)
                                     for m in self.runtime.conversation_history.display_messages()))

    def test_native_continuation_does_not_rearm_on_revision_only_churn_or_exceed_steps(self):
        for max_steps, expected_requests in ((0, 1), (4, 3)):
            with self.subTest(max_steps=max_steps):
                self.runtime.tasks.clear()
                plan = self.continuation_plan()
                self.runtime.execution_context.max_steps = max_steps
                def respond(provider, messages, model):
                    if len(provider.requests) == 2:
                        return ModelResponse(tool_calls=(ToolCall("reorder", "update_plan", {
                            "plan_id": plan["plan_id"], "expected_revision": 1, "reason": "same strategy",
                            "operations": [{"op": "reorder", "tasks": plan["task_ids"]}]}),),
                            finish_reason=FinishReason.TOOL_REQUEST,
                            usage={"prompt_tokens": 7, "completion_tokens": 3})
                    return ModelResponse(text="I have no further actions.", finish_reason=FinishReason.FINAL,
                        usage={"prompt_tokens": 7, "completion_tokens": 3})
                provider = self.provider(respond)
                answer, turn, _ = self.run_native(provider, [{"role": "user", "content": "Verify notes.txt"}],
                    complexity="complex", permitted=self.runtime._permitted_tool_names())
                self.assertEqual(len(provider.requests), expected_requests, answer)
                self.assertEqual((turn.turn_prompt_total, turn.turn_completion_total),
                                 (7 * expected_requests, 3 * expected_requests))
                self.assertEqual(turn.runtime_status, "incomplete")
                self.assertEqual(self.runtime.tasks.tasks[0]["status"], "pending")
                self.assertEqual(self.runtime.tasks.tasks[0].get("evidence", []), [])

    def test_native_plan_continuation_stops_for_unverified_or_unsafe_state(self):
        for kind in ("blocked", "unknown_effect", "stale", "question", "approval", "scope", "unknown_manager", "child"):
            with self.subTest(kind=kind), ExitStack() as stack:
                self.runtime.tasks.recover()
                self.runtime.tasks.clear()
                plan = self.continuation_plan()
                task = self.runtime.tasks.tasks[0]
                if kind == "blocked":
                    self.runtime.tasks.edit_plan(plan_id=plan["plan_id"], expected_revision=1, operations=[{
                        "op": "block", "task": task["id"], "reason": "Repository unavailable"}])
                elif kind in {"unknown_effect", "stale"}:
                    changed = copy.deepcopy(task)
                    if kind == "unknown_effect":
                        changed["checkpoint"] = {"recovery_effect": {"status": "unknown"}}
                    else:
                        changed["status"] = "blocked"
                    self.runtime.tasks._persist(changed, expected_status="pending")
                elif kind in {"question", "approval", "scope"}:
                    state = self.runtime._interaction_controller.state()
                    if kind == "scope":
                        state["executing_plan"] = {"bounds": {"capabilities": ["read"], "effects": ["read_only"],
                                                              "workspace_paths": ["other"], "risk_ceiling": "normal"}}
                    else:
                        state["pending_question" if kind == "question" else "pending_plan"] = {"request_id": "pending"}
                    stack.enter_context(patch.object(self.runtime._interaction_controller, "state", return_value=state))
                elif kind == "child":
                    stack.enter_context(patch.object(self.runtime.execution_context, "child_run_id", "child"))
                elif kind == "unknown_manager":
                    stack.enter_context(patch.object(self.runtime.current_session, "process_manager", None))
                provider = self.provider(lambda *_: ModelResponse(text="This remains incomplete.", finish_reason=FinishReason.FINAL))
                answer, turn, _ = self.run_native(provider, [{"role": "user", "content": "Verify notes.txt"}],
                    complexity="complex", permitted=self.runtime._permitted_tool_names())
                self.assertEqual(len(provider.requests), 1, answer)
                self.assertNotEqual(turn.runtime_status, "completed")
                self.assertFalse(turn.tool_records)
                self.assertEqual(self.runtime.tasks.tasks[0].get("evidence", []), [])

    def test_native_cancelled_this_turn_is_final_but_prior_cancelled_plan_can_be_replaced(self):
        plan = self.continuation_plan()
        def cancel(provider, messages, model):
            if len(provider.requests) == 1:
                return ModelResponse(tool_calls=(ToolCall("cancel", "update_plan", {
                    "plan_id": plan["plan_id"], "expected_revision": 1,
                    "operations": [{"op": "cancel", "task": plan["task_ids"][0]}]}),),
                    finish_reason=FinishReason.TOOL_REQUEST)
            self.assertEqual(self.availability(messages)[1]["current_call_tools"], [])
            return ModelResponse(text="The requested work was cancelled.", finish_reason=FinishReason.FINAL)
        provider = self.provider(cancel)
        answer, turn, _ = self.run_native(provider, [{"role": "user", "content": "Cancel this work"}],
            complexity="complex", permitted=self.runtime._permitted_tool_names())
        self.assertEqual(len(provider.requests), 2, answer)
        self.assertEqual(turn.runtime_status, "cancelled")
        self.assertEqual(self.runtime.tasks.working_plan.status, "cancelled")
        def replace(provider, messages, model):
            if len(provider.requests) == 1:
                self.assertEqual({t["name"] for t in provider.tool_requests[-1]}, {"update_plan", "read_tool_artifact"})
                call = ToolCall("new", "update_plan", {"expected_revision": 0, "operations": [
                    {"op": "add", "ref": "inspect", "task": {"description": "Read ready marker",
                        "acceptance": [{"criterion": "ready marker", "verification": {
                            "action": "read_file", "arguments": {"path": "notes.txt"},
                            "expect": {"contains": "ready"}}}]}}, {"op": "start", "task": "inspect"}]})
            elif len(provider.requests) == 2:
                call = ToolCall("read", "read_file", {"path": "notes.txt"})
            else:
                return ModelResponse(text="The new task is verified.", finish_reason=FinishReason.FINAL)
            return ModelResponse(tool_calls=(call,), finish_reason=FinishReason.TOOL_REQUEST)
        provider = self.provider(replace)
        answer, turn, _ = self.run_native(provider, [{"role": "user", "content": "A new request: verify notes.txt"}],
            complexity="complex", permitted=self.runtime._permitted_tool_names())
        self.assertEqual(len(provider.requests), 3, answer)
        self.assertEqual(turn.runtime_status, "completed", answer)
        self.assertNotEqual(self.runtime.tasks.working_plan.id, plan["plan_id"])

    def test_native_plan_continuation_does_not_retry_a_denied_tool(self):
        self.continuation_plan()
        def respond(provider, messages, model):
            if len(provider.requests) == 1:
                return ModelResponse(tool_calls=(ToolCall("outside", "read_file", {"path": "../outside.txt"}),),
                    finish_reason=FinishReason.TOOL_REQUEST)
            return ModelResponse(text="This work cannot be completed.", finish_reason=FinishReason.FINAL)
        provider = self.provider(respond)
        answer, turn, _ = self.run_native(provider, [{"role": "user", "content": "Verify notes.txt"}],
            complexity="complex", permitted=self.runtime._permitted_tool_names())
        self.assertEqual(len(provider.requests), 2, answer)
        self.assertEqual(len(turn.tool_records), 1)
        self.assertFalse(turn.tool_records[0]["success"])
        self.assertEqual(turn.runtime_status, "incomplete")

    def test_native_continuation_hands_off_owned_process_or_delegation_uncertainty(self):
        from types import SimpleNamespace
        self.continuation_plan()
        turn = TurnContext("Verify notes.txt", interaction_mode="agent", complexity="complex", tool_records=[])
        engine = AgentEngine(self.runtime)
        self.assertIsNotNone(engine._working_plan_continuation(turn))
        with patch.object(self.runtime.subagent_manager, "coordinator", SimpleNamespace(list=lambda: [
                {"run_id": "outstanding", "status": "running", "version": 1}])):
            self.assertIsNone(engine._working_plan_continuation(turn))
        processes = self.runtime.current_session.process_manager
        processes._audit_event("started", "unknown-owned-process", owned_process_state="unknown")
        self.assertIsNone(engine._working_plan_continuation(turn))

    def run_discovery_plan(self, *, typed, streaming):
        (self.root / "catalog.py").write_text("def sorted_products(names):\n    return sorted(names, key=lambda n: (n.casefold(), n))\n")
        acceptance = ([{"criterion": "sorting implementation exists", "verification": {
            "action": "read_file", "arguments": {"path": "catalog.py"},
            "expect": {"contains": "def sorted_products(names):"}}}] if typed else
            ["catalog.py is read and its sorting function and tie-break rule are reported"])

        def respond(provider, messages, model):
            step = len(provider.requests)
            task = self.runtime.tasks.tasks[-1] if self.runtime.tasks.tasks else None
            if step == 1:
                call = ToolCall("create-plan", "update_plan", {"expected_revision": 0, "operations": [
                    {"op": "add", "ref": "inspect", "task": {
                        "description": "Inspect catalog.py read-only", "acceptance": acceptance}},
                    {"op": "start", "task": "inspect"}]})
            elif step == 2:
                call = ToolCall("discover", "discover_tools", {"names": ["read_file", "list_dir"]})
            elif step == 3:
                call = ToolCall("inspect-file", "read_file", {"path": "catalog.py"})
            elif step == 4 and not typed:
                call = ToolCall("rewrite-acceptance", "update_plan", {
                    "plan_id": self.runtime.tasks.working_plan.id, "expected_revision": 1,
                    "operations": [{"op": "update", "task": task["id"], "fields": {
                        "acceptance": [{"criterion": "sorting implementation exists", "verification": {
                            "action": "read_file", "arguments": {"path": "catalog.py"},
                            "expect": {"contains": "def sorted_products(names):"}}}]}}]})
            elif step == (4 if typed else 5):
                call = ToolCall("confirm-plan", "update_plan", {
                    "plan_id": self.runtime.tasks.working_plan.id, "expected_revision": 1,
                    "operations": [{"op": "suggest_complete", "task": task["id"]}]})
            else:
                if not typed and step == 7:
                    feedback = json.loads(messages[-1]["content"])["working_plan_continuation"]
                    self.assertEqual(feedback["eligible_tasks"][0]["status"], "running")
                    self.assertEqual(feedback["completion_evidence"], "required; prose and suggestions are not evidence")
                    self.assertEqual(task["status"], "running")
                return ModelResponse(text="Read the sorting implementation.", finish_reason=FinishReason.FINAL)
            return ModelResponse(tool_calls=(call,), finish_reason=FinishReason.TOOL_REQUEST,
                continuation=provider_continuation(provider, model, "fixture", {"call": call.id}))

        def stream(provider, messages, model):
            yield ResponseComplete(respond(provider, messages, model))
        provider = self.provider(stream if streaming else respond, stream=streaming)
        result = self.run_native(provider, [{"role": "system", "content": "Inspect the requested file."},
            {"role": "user", "content": "Find the sorting function and its tie-break rule."}],
            complexity="complex", permitted=self.runtime._permitted_tool_names())
        if not typed:
            self.assertEqual(len(provider.requests), 7, result[0])
        return result

    def test_native_discovery_does_not_claim_prose_task_execution(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                if streaming:
                    self.runtime.tasks.clear()
                answer, turn, _state = self.run_discovery_plan(typed=False, streaming=streaming)
                discovery = next(record for record in turn.tool_records if record["action"] == "discover_tools")
                read = next(record for record in turn.tool_records if record["action"] == "read_file")
                task = self.runtime.tasks.tasks[-1]
                self.assertTrue(discovery["success"])
                self.assertIsNone(discovery["task_id"])
                self.assertIsNone(discovery["requested_task_id"])
                self.assertEqual(read["task_id"], task["id"])
                self.assertEqual(task["checkpoint"]["action"], "read_file")
                self.assertEqual([item["action"] for item in task["evidence"]], ["read_file"])
                self.assertEqual(task["status"], "running")
                self.assertEqual(self.runtime.tasks.working_plan.status, "active")
                self.assertEqual(turn.runtime_status, "incomplete", answer)
                rewrite = next(record for record in turn.tool_records if record["call_id"] == "rewrite-acceptance")
                self.assertFalse(rewrite["success"])
                self.assertIn("executed task changes require cancellation", rewrite["result"])

    def test_native_discovery_preserves_typed_read_verification(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                if streaming:
                    self.runtime.tasks.clear()
                answer, turn, _state = self.run_discovery_plan(typed=True, streaming=streaming)
                discovery = next(record for record in turn.tool_records if record["action"] == "discover_tools")
                self.assertTrue(discovery["success"])
                self.assertIsNone(discovery["task_id"])
                self.assertIsNone(discovery["requested_task_id"])
                self.assertEqual(self.runtime.tasks.tasks[-1]["status"], "succeeded")
                self.assertEqual(self.runtime.tasks.working_plan.status, "completed")
                self.assertEqual(turn.runtime_status, "completed", answer)

    def test_native_discovery_does_not_inherit_delegated_task_binding(self):
        self.runtime.tasks.update_plan(complexity="simple", expected_revision=0, operations=[
            {"op": "add", "ref": "inspect", "task": {"description": "Inspect catalog", "acceptance": []}},
            {"op": "start", "task": "inspect"}])
        task = self.runtime.tasks.tasks[-1]
        (self.root / "catalog.py").write_text("catalog")
        with patch.object(self.runtime.current_session, "_delegation_task_id", task["id"], create=True):
            discovery = self.runtime._execute_turn_action("discover_tools", {"names": ["read_file"]},
                operation_scope="delegated-discovery", successful_operations=set(), call_id_source="native")
            self.runtime._record_turn_receipt(discovery)
            read = self.runtime._execute_turn_action("read_file", {"path": "catalog.py"},
                operation_scope="delegated-read", successful_operations=set(), call_id_source="native")
            record = self.runtime._record_turn_receipt(read)
        self.assertTrue(discovery.success)
        self.assertIsNone(discovery.task_id)
        self.assertEqual(record["task_id"], task["id"])
        task = self.runtime.tasks.tasks[-1]
        self.assertEqual(task["checkpoint"]["action"], "read_file")
        self.assertEqual(task["status"], "running")

    def test_replaced_discovery_retains_native_task_selection_guard(self):
        from types import MethodType
        from openkyrozen.agent.prompts import _discover_tools
        from openkyrozen.tools.models import tool_success
        original = self.runtime.tool_registry.get_spec("discover_tools")

        class SpoofedDiscovery:
            @property
            def __class__(self):
                return MethodType

            def __call__(self, arguments):
                return tool_success("replacement")

        spoofed = SpoofedDiscovery()
        spoofed.__self__ = self.runtime
        spoofed.__func__ = _discover_tools.__tool_result_executor__
        self.runtime.tasks.update_plan(complexity="simple", expected_revision=0, operations=[
            {"op": "add", "ref": "inspect", "task": {"description": "Inspect catalog", "acceptance": [],
                "execution": {"action": "discover_tools", "arguments": {"names": ["read_file"]}}}},
            {"op": "start", "task": "inspect"}])
        task_id = self.runtime.tasks.tasks[-1]["id"]
        for replacement in (lambda arguments: tool_success("replacement"), spoofed):
            with self.subTest(replacement=type(replacement).__name__):
                self.runtime.tool_registry.register_spec(original.replace(executor=replacement), replace=True)
                receipt = self.runtime._execute_turn_action("discover_tools", {"names": ["read_file"]},
                    task_id="stale-task", task_plan_id="stale-plan", operation_scope="replaced-discovery",
                    successful_operations=set(), call_id_source="native")
                self.assertFalse(receipt.success)
                self.assertEqual(receipt.failure, "invalid_arguments")
                self.assertEqual(receipt.task_id, "stale-task")
                receipt = self.runtime._execute_turn_action("discover_tools", {"names": ["read_file"]},
                    task_id=task_id, task_plan_id=self.runtime.tasks.working_plan.id,
                    operation_scope="matching-replacement", successful_operations=set(), call_id_source="native")
                record = self.runtime._record_turn_receipt(receipt)
                self.assertTrue(receipt.success)
                self.assertEqual(record["task_id"], task_id)
                self.assertEqual(self.runtime.tasks.tasks[-1]["status"], "running")

    def test_pressure_compacts_real_native_history_before_the_provider_request(self):
        provider = self.provider(lambda _provider, _messages, _model:
            ModelResponse(text="The cedar decision is preserved.", finish_reason=FinishReason.FINAL))
        messages = self.history()
        call = ToolCall("historical-read", "read_file", {"path": "notes.txt"})
        messages.extend((ModelResponse(tool_calls=(call,), finish_reason=FinishReason.TOOL_REQUEST,
            continuation=provider_continuation(provider, "context-fixture", "fixture", {"id": "history"})),
            ToolReply(call.id, call.name, {"text": "known fact"}), {"role": "user", "content": "Current pending work"}))
        answer, turn, state = self.run_native(provider, messages, window=1800)
        sent = provider.requests[0]
        self.availability(sent)
        self.assertLessEqual(estimate_messages(sent)[0] + state.reserve_tokens, 1800)
        self.assertTrue(any(isinstance(item, dict) and DIGEST_PREFIX in item.get("content", "") for item in sent))
        self.assertEqual(sent[-1]["content"], "Current pending work")
        validate_tool_history(sent, provider_id=provider_identity(provider), model="context-fixture", native=True)
        self.assertEqual(turn.runtime_status, "completed")
        self.assertIn("cedar", answer)

    def test_full_native_pressure_compacts_a_prefix_that_fits_on_its_own(self):
        from openkyrozen.agent.compaction import compact_for_pressure
        prefix = ([{"role": "system", "content": "fixed"}]
                  + [{"role": "user", "content": f"old-{index}:" + "x" * 1000} for index in range(6)]
                  + [{"role": "user", "content": "recent decision cedar"} for _ in range(4)])
        call = ToolCall("recent-call", "read_file", {"path": "notes.txt"})
        response = ModelResponse(tool_calls=(call,), finish_reason=FinishReason.TOOL_REQUEST)
        reply = ToolReply(call.id, call.name, {"text": "known " * 200})
        current = {"role": "user", "content": "Continue current work"}
        state = ContextState("context-fixture", configured_window=2100, reserve_tokens=100,
                             native_input_overhead_tokens=150)
        state.history_message_ids = {id(item) for item in prefix}
        self.assertLess(state.update(prefix)["input_tokens"] + state.reserve_tokens, 2100)
        self.assertGreater(state.update(prefix + [response, reply, current])["input_tokens"] + state.reserve_tokens, 2100)
        result = compact_for_pressure(prefix + [response, reply, current], state, lambda *_args: "Decision cedar retained")
        self.assertFalse(result.impossible)
        self.assertTrue(result.compacted)
        self.assertLessEqual(state.status["input_tokens"] + state.reserve_tokens, 2100)
        self.assertIs(result.messages[-3], response)
        self.assertIs(result.messages[-2], reply)
        self.assertIs(result.messages[-1], current)

    def test_native_reported_token_cache_reuses_only_the_same_schema_across_turns(self):
        messages = [{"role": "system", "content": "fixed"}, {"role": "user", "content": "current"}]
        first = ContextState("context-fixture", native_schema_fingerprint="schema-one", native_input_overhead_tokens=80)
        token = self.runtime._active_context_state.set(first)
        try:
            self.runtime._cache_reported_context_tokens(messages, 321)
        finally:
            self.runtime._active_context_state.reset(token)
        same = ContextState("context-fixture", native_schema_fingerprint="schema-one", native_input_overhead_tokens=80)
        self.runtime._load_reported_context_tokens(same)
        status = same.update(messages)
        self.assertEqual(status["input_source"], "provider_reported")
        self.assertEqual(status["input_tokens"], 321)
        different = ContextState("context-fixture", native_schema_fingerprint="schema-two", native_input_overhead_tokens=90)
        self.runtime._load_reported_context_tokens(different)
        self.assertEqual(different.update(messages)["input_source"], "estimated")
        legacy = ContextState("context-fixture")
        self.runtime._load_reported_context_tokens(legacy)
        self.assertEqual(legacy.update(messages)["input_source"], "estimated")

    def test_reported_context_counts_do_not_cross_provider_or_model_identity(self):
        messages = [{"role": "user", "content": "current"}]
        first = ContextState("model-A", native_schema_fingerprint="same", native_input_overhead_tokens=200)
        first.request_provider_id = "provider-one"
        token = self.runtime._active_context_state.set(first)
        try:
            self.runtime._cache_reported_context_tokens(messages, 7)
        finally:
            self.runtime._active_context_state.reset(token)
        for model, identity in (("model-B", "provider-one"), ("model-A", "provider-two")):
            with self.subTest(model=model, provider=identity):
                other = ContextState(model, native_schema_fingerprint="same", native_input_overhead_tokens=200)
                other.request_provider_id = identity
                self.runtime._load_reported_context_tokens(other)
                self.assertEqual(other.update(messages)["input_source"], "estimated")
        same = ContextState("model-A", native_schema_fingerprint="same", native_input_overhead_tokens=200)
        same.request_provider_id = "provider-one"
        self.runtime._load_reported_context_tokens(same)
        self.assertEqual(same.update(messages)["input_tokens"], 7)

    def test_explicit_native_model_switch_recomputes_catalog_window_before_transport(self):
        from openkyrozen.agent.types import ContextTooLargeError
        provider = self.provider(lambda *_args: ModelResponse(text="Must not run", finish_reason=FinishReason.FINAL))
        context = copy.copy(self.runtime.execution_context)
        context.provider = provider
        turn_token = self.runtime._turn_context.set(context)
        state = ContextState("deepseek-flash", reserve_tokens=100)
        state_token = self.runtime._active_context_state.set(state)
        try:
            with self.assertRaises(ContextTooLargeError):
                self.runtime._get_model_response([{"role": "system", "content": "x" * 600000},
                    {"role": "user", "content": "current"}], model="gpt-4o", tool_names={"read_file"})
            self.assertEqual(provider.requests, [])
            self.assertEqual(state.window_tokens, 128000)
        finally:
            self.runtime._active_context_state.reset(state_token)
            self.runtime._turn_context.reset(turn_token)

    def test_explicit_configured_window_survives_a_native_model_switch(self):
        provider = self.provider(lambda *_args: ModelResponse(text="Fits configured window", finish_reason=FinishReason.FINAL))
        context = copy.copy(self.runtime.execution_context)
        context.provider = provider
        turn_token = self.runtime._turn_context.set(context)
        state = ContextState("deepseek-flash", configured_window=5000, reserve_tokens=100)
        state_token = self.runtime._active_context_state.set(state)
        try:
            self.runtime._get_model_response([{"role": "user", "content": "current"}],
                model="gpt-4o", tool_names={"read_file"})
            self.assertEqual(state.window_tokens, 5000)
            self.assertEqual(state.window_source, "configured_override")
        finally:
            self.runtime._active_context_state.reset(state_token)
            self.runtime._turn_context.reset(turn_token)

    def test_native_schema_payload_is_reserved_before_provider_dispatch(self):
        provider = self.provider(lambda *_args: ModelResponse(text="Should not fit", finish_reason=FinishReason.FINAL))
        original = self.runtime.tool_registry.get_spec("read_file")
        self.runtime.tool_registry.register_spec(original.replace(description="Read contract: " + "x" * 20000), replace=True)
        messages = [{"role": "system", "content": "fixed"}, {"role": "user", "content": "Read a file"}]
        answer, turn, state = self.run_native(provider, messages, window=500)
        self.assertEqual(len(provider.requests), 0, answer)
        self.assertGreater(state.native_input_overhead_tokens, 500)
        self.assertEqual(turn.runtime_status, "incomplete")

    def test_native_continuation_payload_cannot_hide_from_the_context_limit(self):
        provider = self.provider(lambda *_args: ModelResponse(text="Should not fit", finish_reason=FinishReason.FINAL))
        messages = [{"role": "system", "content": "fixed"}, ModelResponse(text="previous response",
            finish_reason=FinishReason.FINAL, continuation=provider_continuation(provider, "context-fixture", "fixture",
                {"opaque_replay_signature": "x" * 12000})), {"role": "user", "content": "Continue"}]
        answer, turn, _state = self.run_native(provider, messages, window=500)
        self.assertEqual(len(provider.requests), 0, answer)
        self.assertEqual(turn.runtime_status, "incomplete")

    def test_impossible_fixed_native_context_never_calls_the_provider(self):
        provider = self.provider(lambda *_args: ModelResponse(text="Should not run", finish_reason=FinishReason.FINAL))
        messages = [{"role": "system", "content": "x" * 10000}, {"role": "user", "content": "Current work"}]
        answer, turn, _state = self.run_native(provider, messages, window=300)
        self.assertEqual(provider.requests, [])
        self.assertEqual(turn.runtime_status, "incomplete")
        self.assertIn("ContextTooLargeError", answer)

    def test_overflow_retries_only_generation_after_an_effect_without_replaying_the_write(self):
        def respond(provider, messages, model):
            if len(provider.requests) == 1:
                return ModelResponse(tool_calls=(ToolCall("write-once", "write_file", {"path": "result.txt", "content": "once"}),),
                    finish_reason=FinishReason.TOOL_REQUEST,
                    continuation=provider_continuation(provider, model, "fixture", {"id": "write"}))
            if len(provider.requests) == 2:
                raise ProviderError(ProviderErrorKind.CONTEXT_OVERFLOW)
            validate_tool_history(messages, provider_id=provider_identity(provider), model=model, native=True)
            return ModelResponse(text="The file was written once and its result retained.", finish_reason=FinishReason.FINAL)
        provider = self.provider(respond)
        original = self.runtime.tool_registry.get_spec("write_file")
        effects = []
        def write(arguments):
            effects.append(dict(arguments))
            return original.executor(arguments)
        self.runtime.tool_registry.register_spec(original.replace(executor=write), replace=True)
        messages = self.history() + [{"role": "user", "content": "Write result.txt once"}]
        answer, turn, state = self.run_native(provider, messages)
        self.assertEqual(len(provider.requests), 3, answer)
        for sent in provider.requests:
            self.availability(sent)
        self.assertEqual(len(effects), 1)
        self.assertEqual((self.root / "result.txt").read_text(), "once")
        self.assertTrue(state.overflow_retried)
        self.assertTrue(any(isinstance(item, ToolReply) and item.call_id == "write-once" for item in provider.requests[-1]))
        self.assertEqual(turn.runtime_status, "completed")

    def test_a_second_native_overflow_stops_without_further_generation(self):
        def respond(*_args):
            raise ProviderError(ProviderErrorKind.CONTEXT_OVERFLOW)
        provider = self.provider(respond)
        answer, turn, state = self.run_native(provider, self.history() + [{"role": "user", "content": "Current work"}])
        self.assertEqual(len(provider.requests), 2, answer)
        self.assertTrue(state.overflow_retried)
        self.assertEqual(turn.runtime_status, "incomplete")

    def test_overflow_after_a_stream_delta_is_never_replayed(self):
        def stream(*_args):
            yield TextDelta("Already delivered partial text.")
            yield ResponseError(ProviderError(ProviderErrorKind.CONTEXT_OVERFLOW))
        provider = self.provider(stream, stream=True)
        _answer, turn, state = self.run_native(provider, self.history() + [{"role": "user", "content": "Current work"}])
        self.assertEqual(len(provider.requests), 1)
        self.assertFalse(state.overflow_retried)
        self.assertEqual(turn.runtime_status, "incomplete")


if __name__ == "__main__":
    unittest.main()
