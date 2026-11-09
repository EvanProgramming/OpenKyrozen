"""Native engine context checks using real runtime/compaction and offline providers."""
import copy
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
                return ModelResponse(text="Read the sorting implementation.", finish_reason=FinishReason.FINAL)
            return ModelResponse(tool_calls=(call,), finish_reason=FinishReason.TOOL_REQUEST,
                continuation=provider_continuation(provider, model, "fixture", {"call": call.id}))

        def stream(provider, messages, model):
            yield ResponseComplete(respond(provider, messages, model))
        provider = self.provider(stream if streaming else respond, stream=streaming)
        return self.run_native(provider, [{"role": "system", "content": "Inspect the requested file."},
            {"role": "user", "content": "Find the sorting function and its tie-break rule."}],
            complexity="complex", permitted=self.runtime._permitted_tool_names())

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
