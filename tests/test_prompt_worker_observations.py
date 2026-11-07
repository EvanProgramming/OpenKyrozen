from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from benchmarks.prompt_worker import _normalize_tool_receipt, _context_observation, _run_conversation, _ProviderObservations
from openkyrozen.app.bootstrap import build_application, build_memory
from openkyrozen.providers.models import ToolCall
from openkyrozen.tools import ToolAdapters


class PromptWorkerObservationTests(unittest.TestCase):
    def test_real_native_receipts_preserve_background_relative_path_and_git_reference(self):
        from benchmarks.v3_fixtures import FIXTURES
        from benchmarks.v3_matched_live import prepare_fixture
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_APPROVAL_MODE": "never",
        }), ExitStack() as sqlite_owners:
            root = Path(directory)
            case = next(item for item in FIXTURES if item["fixture_id"] == "git_history-1")
            prepared = prepare_fixture(root, case)
            app = build_application(surface="cli", memory=sqlite_owners.enter_context(closing(build_memory(root / "state.sqlite3", index=None))), tools=ToolAdapters(root))
            self.addCleanup(app.close)
            runtime = app.runtime
            runtime._set_workspace_root(root)
            write = runtime.execute_call(runtime.current_session, ToolCall("native-write", "write_file", {"path": "notes.txt", "content": "value"}))
            raw = write.as_dict()
            self.assertIsInstance(raw["args"], str)
            observed = _normalize_tool_receipt(raw, root)
            self.assertEqual(observed["target_path"], "notes.txt")
            self.assertEqual(observed["call_id"], "native-write")
            self.assertEqual(observed['evidence'], raw)
            raw['arguments']['content'] = 'later mutation'
            self.assertEqual(observed['evidence']['arguments']['content'], 'value')
            expected = hashlib.sha256(json.dumps({"path": "notes.txt", "content": "value"}, sort_keys=True,
                ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            self.assertEqual(observed["args_hash"], expected)
            background = runtime.execute_call(runtime.current_session, ToolCall("native-process", "run_cmd", {
                "command": "printf ready", "background": True}))
            self.assertTrue(background.success, background.result)
            self.assertIs(_normalize_tool_receipt(background.as_dict(), root)["background"], True)
            show = runtime.execute_call(runtime.current_session, ToolCall("native-git", "git_show", {"reference": prepared["expected_commit_full"]}))
            self.assertTrue(show.success, show.result)
            self.assertEqual(_normalize_tool_receipt(show.as_dict(), root)["git_reference"], prepared["expected_commit_full"])
            projected = {**raw, "arguments": None}
            self.assertEqual(_normalize_tool_receipt(projected, root)["target_path"], "notes.txt")

    def test_real_native_script_observes_compaction_and_bounded_context(self):
        from benchmarks.v3_fixtures import FIXTURES
        from benchmarks.v3_matched_live import prepare_fixture, verify_fixture
        from openkyrozen.providers import ProviderConfig
        from openkyrozen.providers.base import LLMProvider
        from openkyrozen.providers.models import FinishReason, ModelResponse, ProviderCapabilities, provider_continuation
        case = next(item for item in FIXTURES if item["fixture_id"] == "long_context-1")
        class Provider(LLMProvider):
            def get_capabilities(self, model=None): return ProviderCapabilities(native_tools=True)
            def chat(self, messages, model=None): return self.chat_response(messages, model).as_legacy_tuple()
            def chat_response(self, messages, model=None, *, tools=None):
                content = messages[-1].get("content", "") if isinstance(messages[-1], dict) else ""
                text = json.dumps(case["verifier"]["expected"]) if content.startswith("Return only a JSON") else "Acknowledged the current facts."
                if any(isinstance(item, dict) and "UNTRUSTED TRANSCRIPT:" in item.get("content", "") for item in messages):
                    text = "Preserved facts: owner team-cedar; rollback release-17; latest approved region ap-southeast-1."
                return ModelResponse(text=text, finish_reason=FinishReason.FINAL,
                    continuation=provider_continuation(self, model, "fixture", {"assistant": {"role": "assistant", "content": text}}))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"KYROZEN_DISABLE_VECTOR_INDEX": "1"}), ExitStack() as sqlite_owners:
            root = Path(directory) / "workspace"
            prepared = prepare_fixture(root, case)
            app = build_application(surface="cli", memory=sqlite_owners.enter_context(closing(build_memory(Path(directory) / "state.sqlite3", index=None))), tools=ToolAdapters(root))
            self.addCleanup(app.close)
            runtime = app.runtime
            runtime._set_workspace_root(root)
            runtime.set_interaction_mode("ask")
            config = ProviderConfig(provider="custom", model_simple="fixture", model_complex="fixture", context_window_tokens=16384)
            runtime._provider_config = config
            runtime.llm_provider = runtime._default_turn.provider = Provider(config)
            observations = _ProviderObservations(runtime, runtime.llm_provider)
            with patch.object(runtime, "_schedule_post_turn_learning"):
                answer, records = _run_conversation(runtime, runtime.current_session, case["turns"], observations)
            worker = {"conversation_records": records, "request_context_records": observations.records,
                      "provider_error": False, "processes_alive": 0}
            result = verify_fixture(case, root, answer, worker, prepared)
            self.assertEqual(result["result"], "passed", answer)
            self.assertTrue(any(record["messages_compacted"] is True for record in records))
            summaries = [record for record in observations.records if record["kind"] == "summary"]
            self.assertTrue(summaries)
            self.assertTrue(all(record["message_count"] == 1 for record in summaries))
            self.assertTrue(all(record["retained_history_tokens"] is None for record in summaries))
            self.assertTrue(all(record["provider_input_tokens"] is None for record in observations.records))
            for altered in ("missing_dispatches", "oversized_summary", "superseded_answer"):
                import copy
                negative = copy.deepcopy(worker)
                negative_answer = answer
                if altered == "missing_dispatches": negative["request_context_records"] = []
                elif altered == "oversized_summary":
                    summary = next(item for item in negative["request_context_records"] if item["kind"] == "summary")
                    summary["estimated_input_tokens"] = 16384
                else: negative_answer = json.dumps({**case["verifier"]["expected"], "region": "eu-west-2"})
                self.assertEqual(verify_fixture(case, root, negative_answer, negative, prepared)["result"], "failed", altered)
            self.assertTrue(all(record["context"]["input_tokens"] + record["context"]["reserve_tokens"] <= 16384 for record in records))

    def test_stream_observer_preserves_terminal_close_and_counts_nested_bridge_once(self):
        from openkyrozen.providers.models import ModelResponse, ResponseComplete, TextDelta
        runtime = SimpleNamespace()
        seen = []
        class Provider:
            config = SimpleNamespace(context_window_tokens=16384)
            def chat_response(self, messages, model=None, *, tools=None):
                seen.append("response")
                return ModelResponse(text="hello", usage={"prompt_tokens": 80, "completion_tokens": 2})
            def chat_response_stream(self, messages, model=None, *, tools=None):
                response = self.chat_response(messages, model, tools=tools)
                seen.append("delta")
                yield TextDelta(response.text)
                seen.append("complete")
                yield ResponseComplete(response)
                seen.append("after-terminal")
        provider = Provider()
        observation = _ProviderObservations(runtime, provider)
        iterator = provider.chat_response_stream([{"role": "user", "content": "hello"}], "fixture", tools=[])
        self.assertEqual(next(iterator).text, "hello")
        self.assertIsInstance(next(iterator), ResponseComplete)
        iterator.close()  # Production stops consuming immediately at terminal.
        self.assertEqual(seen, ["response", "delta", "complete"])
        self.assertEqual(len(observation.records), 1)
        self.assertEqual(observation.records[0]["outcome"], "returned")
        self.assertEqual(observation.records[0]["provider_input_tokens"], 80)
        self.assertEqual(observation.records[0]["provider_usage_source"], "provider_reported_total")
        self.assertIsNone(observation.records[0]["retained_history_tokens"])

    def test_missing_context_components_remain_unknown(self):
        runtime = SimpleNamespace(context_usage=lambda: {"input_tokens": 200, "input_source": "provider_reported",
            "window_tokens": 1000, "reserve_tokens": 100, "breakdown": {"conversation": 160}})
        observed = _context_observation(runtime)
        self.assertEqual(observed["input_tokens"], 200)
        self.assertEqual(observed["input_source"], "provider_reported")
        self.assertIsNone(observed["native_input_overhead_tokens"])
        self.assertIsNone(observed["retained_history_tokens"])
        self.assertIsNone(observed["retransmitted_history_tokens"])
        self.assertIsNone(_context_observation(SimpleNamespace()))

    def test_scripted_turns_reuse_the_session_and_record_real_context_snapshots(self):
        session = SimpleNamespace(messages_compacted=False)
        seen = []
        context = {"input_tokens": 100, "reserve_tokens": 20, "window_tokens": 500,
                   "input_source": "estimated", "compaction": {"status": "not_needed"}}
        def chat(actual_session, prompt, **kwargs):
            self.assertIs(actual_session, session)
            seen.append(prompt)
            if len(seen) == 2:
                session.messages_compacted = True
                context["compaction"] = {"status": "summarized"}
            return f"answer-{len(seen)}"
        runtime = SimpleNamespace(chat=chat, context_usage=lambda: dict(context))
        answer, records = _run_conversation(runtime, session, ("Remember cedar", "Replace birch", "Recall current"))
        self.assertEqual(seen, ["Remember cedar", "Replace birch", "Recall current"])
        self.assertEqual(answer, "answer-3")
        self.assertEqual(records[0]["prompt_sha256"], hashlib.sha256(b"Remember cedar").hexdigest())
        self.assertEqual(records[0]["context"]["compaction"]["status"], "not_needed")
        self.assertEqual(records[1]["context"]["compaction"]["status"], "summarized")
        self.assertIs(records[1]["messages_compacted"], True)


if __name__ == "__main__":
    unittest.main()
