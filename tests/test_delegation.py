import copy
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from openkyrozen.agent.delegation import Delegation, NAMES, WorkspaceAccess, identicon, report
from openkyrozen.app.bootstrap import build_application, build_memory
from openkyrozen.app.config import load_agent_config, AgentConfigError
from openkyrozen.providers.config import ProviderConfig
from openkyrozen.providers import usage
from openkyrozen.security.capabilities import issue_capability_token
from openkyrozen.tools import ToolAdapters


def brief(index=0, **kwargs):
    return {"id": str(index), "profile": "researcher", "objective": f"Investigate component {index}",
            "context": "Read marker.txt and cite its exact content", "scope": ["marker.txt"],
            "dependencies": [], "acceptance": ["Exact source checked"],
            "deliverables": ["Findings with evidence"], "reason": "Independent component", **kwargs}


def result(summary="marker is ok", **kwargs):
    return {"status": "completed", "summary": summary, "findings": [summary], "evidence": ["marker.txt"],
            "artifacts": [], "checks": ["read marker.txt"], "uncertainties": [], "suggested_followups": [], **kwargs}


def reviewed(verdict="verified", **kwargs):
    return {"verdict": verdict, "summary": "Independent inspection of marker.txt", "findings": [],
            "evidence": ["Read exact content"], **kwargs}


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"KYROZEN_SKILLS_DIR": str(self.root / "skills"),
            "KYROZEN_WORKSPACE_ROOT": str(self.root), "KYROZEN_DISABLE_VECTOR_INDEX": "1"})
        self.env.start()
        self.app = build_application(memory=build_memory(self.root / "state.sqlite3"), tools=ToolAdapters(self.root))
        self.runtime = self.app.runtime
        self.runtime._provider_config = ProviderConfig(provider="deepseek", api_key="test-key",
            model_simple="test-model", model_complex="test-model")
        self.runtime.DEEPSEEK_MODEL = "test-model"
        self.runtime._execution_capability_token = issue_capability_token("test", frozenset({"read", "write", "network", "shell", "git"}))
        (self.root / "marker.txt").write_text("ok")
        self.calls = []
        self.call_lock = threading.Lock()
        self.runtime.get_provider = self.fake_provider

    def tearDown(self):
        self.app.close()
        self.env.stop()
        self.temp.cleanup()

    def fake_provider(self, config):
        runtime, calls, lock = self.runtime, self.calls, self.call_lock
        class Provider:
            def chat(inner, messages, model):
                with lock:
                    calls.append((runtime.current_session.session_id, config.provider, model,
                                  runtime._active_usage_run_id.get(), copy.deepcopy(messages)))
                usage._track_cost(config.provider, {"prompt_tokens": 10, "completion_tokens": 5}, model=model)
                review = "Independently verify" in messages[1]["content"]
                if len(messages) == 2:
                    return 'Action: {"action":"read_file","args":"marker.txt"}', {"prompt_tokens": 10, "completion_tokens": 5}
                answer = reviewed() if review else result()
                return json.dumps(answer), {"prompt_tokens": 10, "completion_tokens": 5}
        return Provider()

    def manager(self, invoke, concurrency=4):
        coordinator = Delegation(self.runtime.memory_bank, invoke,
            profiles=self.runtime.subagent_manager.profiles, root=self.root, concurrency=concurrency)
        self.addCleanup(coordinator.close)
        return coordinator

    def test_real_runtime_isolates_context_providers_usage_and_reviewer(self):
        source = "# Preserve physical lines and indentation.\ndef total(values):\n    return sum(values)\n"
        (self.root / "marker.txt").write_text(source)
        parent_context = self.runtime.execution_context
        from openkyrozen.agent.turn import ExecutionContext
        with patch.dict(os.environ, {"KYROZEN_PROVIDER_TIMEOUT_SECONDS": ""}):
            self.assertEqual(self.runtime._provider_timeout_seconds(), 90)
            timeout_context = self.runtime._turn_context.set(ExecutionContext(child_run_id="review-deadline-test"))
            try:
                self.assertEqual(self.runtime._provider_timeout_seconds(), 180)
                with patch.dict(os.environ, {"KYROZEN_PROVIDER_TIMEOUT_SECONDS": "2"}):
                    self.assertEqual(self.runtime._provider_timeout_seconds(), 2)
            finally:
                self.runtime._turn_context.reset(timeout_context)
        parent_context.last_prompt_tokens = 123
        self.runtime.short_term_memory = [{"role": "user", "content": "PRIVATE PARENT TRANSCRIPT"}]
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "other-key", "KYROZEN_API_KEY": "main-only", "KYROZEN_BASE_URL": "https://main-only.invalid", "KYROZEN_MODEL_SIMPLE": "main-model"}):
            coordinator = self.runtime.delegation()
            agents = coordinator.spawn([brief(0), brief(1, provider="anthropic", model="claude-test")])
            finished = coordinator.wait(timeout=10)
        self.assertEqual([r["status"] for r in finished], ["succeeded", "succeeded"])
        self.assertTrue(all(run["result"]["tool_records"][0]["result"] == source for run in finished))
        self.assertTrue(all(source in str(message["content"]) for call in self.calls if len(call[4]) > 2
                            for message in call[4][-1:]))
        self.assertIn(source, str(coordinator.detail(finished[0]["run_id"])["messages"][-2]["content"]))
        self.assertIs(parent_context, self.runtime.execution_context)
        self.assertEqual(parent_context.last_prompt_tokens, 123)
        self.assertNotIn("private-value", self.runtime._fix_safe_text('{"api_key":"private-value"}'))
        sessions = {item[0] for item in self.calls}
        self.assertEqual(len(sessions), 4)
        self.assertTrue(all("PRIVATE PARENT TRANSCRIPT" not in json.dumps(item[4]) for item in self.calls))
        for agent in agents:
            rows = self.runtime.memory_bank.store.list_usage_attempts(workspace_id=self.runtime.memory_bank.workspace_id, run_id=agent["run_id"])
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["session_id"] for row in rows}, {agent["run_id"]})
        self.assertIn("anthropic", {call[1] for call in self.calls})
        self.assertEqual(finished[0]["reviews"][0]["reviewer"], agents[1]["name"])

    def test_reviewer_repairs_its_missing_artifact_inspection(self):
        (self.root / "delivered.txt").write_text("actual delivered artifact")
        runtime = self.runtime
        class Provider:
            def chat(inner, messages, model):
                reviewing = "Independently verify" in messages[1]["content"]
                if len(messages) == 2:
                    return 'Action: {"action":"read_file","args":"marker.txt"}', None
                if not reviewing:
                    return json.dumps(result(artifacts=["delivered.txt"])), None
                if "Verification requires independent tool inspection" in messages[-1]["content"]:
                    return 'Action: {"action":"read_file","args":"delivered.txt"}', None
                return json.dumps(reviewed()), None
        runtime.get_provider = lambda _: Provider()
        coordinator = runtime.delegation()
        coordinator.spawn([brief()])
        finished = coordinator.wait(timeout=5)[0]
        self.assertEqual(finished["status"], "succeeded", finished)
        self.assertEqual(finished["corrections"], 0)
        self.assertEqual(len(finished["reviews"]), 1)
        self.assertTrue(finished["reviews"][0]["artifact_checks"][0]["independently_read"])

    def test_live_oracle_rejects_joint_false_positive(self):
        from scripts.subagent_autonomy_acceptance import false_decimal_sum_claim, ground_truth
        self.assertTrue(ground_truth()["decimal_values_sum_correctly"])
        false = {"status": "succeeded", "report": {"findings": [
            {"source": "totals.py", "claim": "sum cannot add Decimal to its integer start; raises TypeError"}]}}
        self.assertTrue(false_decimal_sum_claim([false]))
        false["report"]["findings"] = [{"source": "pricing.py", "claim": "float loses Decimal precision"}]
        self.assertFalse(false_decimal_sum_claim([false]))
        false["report"]["findings"] = [{"source": "totals.py", "claim": "sum starts at integer 0",
            "affected_inputs": "total([Decimal('3.30'), 4.40]) raises TypeError"}]
        self.assertFalse(false_decimal_sum_claim([false]))
        false["report"]["findings"] = [{"source": "totals.py", "claim": "sum with mixed Decimal and float raises TypeError"}]
        self.assertFalse(false_decimal_sum_claim([false]))
        false["report"]["findings"] = [{"source": "totals.py", "title": "sum starts at integer 0",
            "claim": "Builtin sum starts with int 0. Decimal + float is unsupported, so mixed inputs raise TypeError. Empty totals return int 0."}]
        self.assertFalse(false_decimal_sum_claim([false]))

    def test_readonly_calculation_checks_semantics_and_rejects_execution(self):
        runtime = self.runtime
        from openkyrozen.security.tool_policy import allowed_tool_names
        self.assertIn("calculate", allowed_tool_names(runtime.AVAILABLE_TOOLS, "readonly"))
        token = runtime._active_interaction_mode.set("ask")
        try:
            receipt = runtime.execute(runtime.current_session, "calculate", "sum([Decimal('1.00'), Decimal('2.00')])")
            self.assertTrue(receipt.success, receipt.result)
            self.assertIn("Decimal('3.00')", receipt.result)
            self.assertIn("13", runtime.AVAILABLE_TOOLS["calculate"]("10 - (-3)"))
            self.assertIn("False", runtime.AVAILABLE_TOOLS["calculate"]("[1] == (1,)"))
            for expression in ("__import__('os').system('true')", "open('marker.txt','w')", "Decimal('1').__class__",
                    "[x for x in range(10)]", "2 ** 1000000000", "'a' * 1000000000", "sum([1] * 1000000000)",
                    "Decimal('1e1000000000')", "int('" + "9" * 2000 + "')"):
                self.assertTrue(runtime.AVAILABLE_TOOLS["calculate"](expression).startswith("Error:"), expression)
            self.assertEqual((self.root / "marker.txt").read_text(), "ok")
        finally:
            runtime._active_interaction_mode.reset(token)

    def test_parallelism_more_than_twelve_names_dependencies_and_restart(self):
        barrier = threading.Barrier(4)
        def invoke(run, review, **kwargs):
            if not review and int(run["assignment_id"]) < 4:
                barrier.wait(timeout=3)
            return {"result": reviewed() if review else result(),
                    "tool_records": [{"action": "read_file", "success": True}]}
        coordinator = self.manager(invoke)
        agents = coordinator.spawn([brief(i) for i in range(25)])
        finished = coordinator.wait(timeout=10)
        self.assertTrue(all(run["status"] == "succeeded" for run in finished))
        self.assertEqual(set(run["name"] for run in agents[:12]), set(NAMES))
        self.assertEqual(set(run["name"] for run in agents[12:24]), {name+"2" for name in NAMES})
        self.assertTrue(agents[24]["name"].endswith("3"))
        for agent in agents:
            self.assertEqual(agent["icon"], identicon(agent["run_id"]))
            self.assertTrue(all(row == row[::-1] for row in agent["icon"]))
        restored = self.manager(invoke)
        self.assertEqual(len(restored.list()), 25)
        self.assertEqual(restored.list()[0]["status"], "succeeded")

    def test_dependencies_and_invalid_batches(self):
        completed = set()
        def invoke(run, review, **kwargs):
            if not review and run["assignment_id"] == "1":
                self.assertIn("0", completed)
            if review:
                completed.add(run["assignment_id"])
            return {"result": reviewed() if review else result(), "tool_records": [{"action": "read_file", "success": True}]}
        coordinator = self.manager(invoke, 1)
        with self.assertRaisesRegex(ValueError, "Cyclic"):
            coordinator.spawn([brief(0, dependencies=["1"]), brief(1, dependencies=["0"])])
        with self.assertRaisesRegex(ValueError, "Unknown dependency"):
            coordinator.spawn([brief(dependencies=["missing"])])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            coordinator.spawn([brief(), brief(1, objective=brief()["objective"])])
        coordinator.spawn([brief(1, dependencies=["0"]), brief(0)])
        self.assertTrue(all(run["status"] == "succeeded" for run in coordinator.wait(timeout=5)))

    def test_reviews_correction_and_no_unsupported_success(self):
        def invoke(run, review, feedback, **kwargs):
            if review:
                return {"result": reviewed("verified" if run["corrections"] else "changes_requested"),
                        "tool_records": [{"action": "read_file", "success": True}]}
            return {"result": result("corrected" if feedback else "incorrect"), "tool_records": []}
        coordinator = self.manager(invoke)
        coordinator.spawn([brief()])
        run = coordinator.wait(timeout=5)[0]
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["corrections"], 1)
        self.assertEqual(len(run["reviews"]), 2)
        unchecked = self.manager(lambda run, review, **kwargs: {"result": reviewed() if review else result()})
        unchecked.spawn([brief(1)])
        self.assertEqual(unchecked.wait(timeout=5)[-1]["status"], "unverified")

    def test_cancellation_and_interruption_never_replay_work(self):
        entered, release = threading.Event(), threading.Event()
        def invoke(run, **kwargs):
            entered.set()
            release.wait(3)
            return {"result": result()}
        coordinator = self.manager(invoke, 1)
        run = coordinator.spawn([brief()])[0]
        self.assertTrue(entered.wait(2))
        coordinator.cancel(run["run_id"])
        release.set()
        coordinator.futures[run["run_id"]].result(timeout=5)
        self.assertEqual(coordinator.wait(timeout=3)[0]["status"], "cancelled")
        coordinator.memory.store.append_event("subagent.state", {**run, "status": "running"},
            user_id=coordinator.memory.user_id, workspace_id=coordinator.memory.file_scope_id,
            session_id=coordinator.memory.session_id)
        recovered = self.manager(lambda *args, **kwargs: self.fail("must not replay"))
        self.assertEqual(recovered.list()[0]["status"], "interrupted")

    def test_ask_mode_cannot_mutate_and_missing_provider_is_explicit(self):
        class WritingProvider:
            def chat(inner, messages, model):
                if len(messages)==2:
                    return 'Action: {"action":"write_file","args":"marker.txt|bad"}', None
                return json.dumps(result(status="blocked", summary="Write denied")), None
        self.runtime.get_provider = lambda _: WritingProvider()
        token = self.runtime._active_interaction_mode.set("plan")
        try:
            coordinator = self.runtime.delegation()
            coordinator.spawn([brief(profile="coder")])
            self.assertEqual(coordinator.wait(timeout=5)[0]["status"], "blocked")
        finally:
            self.runtime._active_interaction_mode.reset(token)
        self.assertEqual((self.root/"marker.txt").read_text(), "ok")

    def test_ownership_and_command_serialization(self):
        access = WorkspaceAccess()
        entered = threading.Event()
        with access.acquire("a"):
            def command():
                with access.acquire():
                    entered.set()
            thread = threading.Thread(target=command)
            thread.start()
            self.assertFalse(entered.wait(.05))
            with access.acquire("b"):
                pass
        thread.join(2)
        self.assertTrue(entered.is_set())
        coordinator = self.runtime.delegation()
        run = coordinator.spawn([brief(profile="coder", scope=["owned.txt"])])[0]
        coordinator.wait(timeout=5)
        context = self.runtime.execution_context
        context.child_run_id, context.coordinator = run["run_id"], coordinator
        try:
            receipt = self.runtime.execute(self.runtime.current_session, "write_file", "marker.txt|bad")
            self.assertFalse(receipt.success)
            self.assertIn("assigned", receipt.result)
        finally:
            context.child_run_id = context.coordinator = None
        self.assertEqual((self.root/"marker.txt").read_text(), "ok")

    def test_configuration_strictness_and_scoped_runs(self):
        (self.root/"agent.yaml").write_text("subagents:\n  concurrency: 1\n  roles:\n    reviewer:\n      provider: anthropic\n      model: test-model\n")
        self.assertEqual(load_agent_config(self.root)["subagents"]["roles"]["reviewer"]["provider"], "anthropic")
        (self.root/"agent.yaml").write_text("subagents:\n  concurrency: true\n")
        with self.assertRaises(AgentConfigError):
            load_agent_config(self.root)
        (self.root/"agent.yaml").unlink()
        first = self.runtime.open_session("first")
        second = self.runtime.open_session("second")
        with self.runtime.use_session(first):
            run = self.runtime.delegation().spawn([brief()])[0]
            self.runtime.delegation().wait(timeout=5)
        with self.runtime.use_session(second):
            self.assertEqual(self.runtime.delegation().list(), [])
            with self.assertRaises(ValueError):
                self.runtime.delegation().detail(run["run_id"])

    def test_main_automatically_delegates_and_waits_before_claiming_success(self):
        runtime = self.runtime
        runtime.set_interaction_mode("agent")
        class MainProvider:
            def chat(inner, messages, model):
                if len(messages)>1 and messages[0]["content"].startswith("Synthesize"):
                    return "Both components were independently inspected and verified.", None
                if any("spawn_agents(" in message["content"] for message in messages):
                    return "The work is finished.", None
                action = {"action": "spawn_agents", "args": json.dumps({"assignments": [brief(0), brief(1)]})}
                if any("list_dir(" in message["content"] for message in messages):
                    return "Action: " + json.dumps(action), None
                return ('Plan:\n1. Inspect workspace structure.\n2. Spawn two specialist agents.\n3. Wait for verified results and synthesize.\n'
                        'Action: {"action":"list_dir","args":"."}'), None
        runtime.llm_provider = MainProvider()
        with (patch.object(runtime, "dispatch_learning_cycle", return_value={}),
              patch.object(runtime, "_classify_complexity", return_value="complex"),
              patch.object(runtime, "_touch_detached_learning_heartbeat"),
              patch.object(runtime, "_render_tool_result"),
              patch.object(runtime, "_update_tasks_panel")):
            reply = runtime.chat(runtime.current_session, "Inspect two independent components", on_event=lambda _: None)
        self.assertIn("verified", reply)

        from openkyrozen.agent.turn import TurnContext
        turn = TurnContext("Read one file")
        turn.learning_run = {"run_id": "coordination-direct-test"}
        turn.tool_results_text = "marker.txt contains ok"
        turn.interaction_mode = "agent"
        events = []
        callback_token = self.runtime._stream_event_callback.set(events.append)
        def direct(messages, *, private=False):
            self.assertTrue(private)
            self.runtime._emit_stream_event({"event": "content", "chunk": "private decision"})
            return json.dumps({"decision": "direct", "reason": "Single lookup", "assignments": []})
        try:
            with patch.object(self.runtime, "_call_llm_with_spinner", side_effect=direct):
                self.assertIsNone(self.runtime._plan_delegation(turn))
            self.assertEqual(events, [])
            self.runtime._emit_stream_event({"event": "content", "chunk": "normal reply"})
            self.assertEqual(len(events), 1)
            with patch.object(self.runtime, "_call_llm_with_spinner", return_value="invalid") as invalid:
                self.assertIsNone(self.runtime._plan_delegation(turn))
            self.assertEqual(invalid.call_count, 3)
            self.assertEqual(len(self.runtime.delegation().list()), 2)
        finally:
            self.runtime._stream_event_callback.reset(callback_token)
        self.assertEqual(len(runtime.delegation().list()), 2)
        self.assertEqual(runtime.tasks.tasks, [])
        self.assertTrue(all(run["status"] == "succeeded" for run in runtime.delegation().list()))

    def test_simple_main_reply_creates_no_agents(self):
        class MainProvider:
            def chat(inner, messages, model):
                return "Hello!", None
        self.runtime.llm_provider = MainProvider()
        with (patch.object(self.runtime, "dispatch_learning_cycle", return_value={}),
              patch.object(self.runtime, "_touch_detached_learning_heartbeat")):
            self.runtime.chat(self.runtime.current_session, "hi", on_event=lambda _: None)
        self.assertIsNone(self.runtime.subagent_manager.coordinator)

    def test_html_action_wrapper_recovers_before_automatic_delegation(self):
        self.runtime.set_interaction_mode("agent")
        wrapped = '<details><summary>Action: read_file</summary>\n```json\n{"path":"marker.txt"}\n```\n</details>'
        self.assertEqual(self.runtime._collect_tool_calls(wrapped), [])
        self.assertEqual(self.runtime._clean_final_response(wrapped), "")
        self.assertEqual(self.runtime._collect_tool_calls(wrapped.replace('{"path":"marker.txt"}', '{"action":"write_file","args":"marker.txt|bad"}')), [])
        xml = '<tool_calls><invoke name="read_file"><parameter name="path">marker.txt</parameter></invoke></tool_calls>'
        self.assertEqual(self.runtime._collect_tool_calls(xml), [])
        self.assertEqual(self.runtime._clean_final_response(xml), "")
        calls = []
        class MainProvider:
            def chat(inner, messages, model):
                calls.append(messages)
                if messages[0]["content"].startswith("Synthesize"):
                    return "The findings were independently verified.", None
                if len(calls) == 1:
                    return 'Action: {"action":"list_dir","args":"."}', None
                if len(calls) == 2:
                    self.assertIn('Invoke tools only as Action:', messages[0]["content"])
                    self.assertIn('Choose the next work distribution', messages[-1]["content"])
                    self.assertNotIn('TaskDone:', messages[-1]["content"])
                    return wrapped, None
                if len(calls) == 3:
                    return xml, None
                if len(calls) == 4:
                    return 'Action: ' + json.dumps({"action":"spawn_agents", "args":json.dumps({"assignments":[brief(0),brief(1)]})}), None
                return "Review finished.", None
        self.runtime.llm_provider = MainProvider()
        with (patch.object(self.runtime, "dispatch_learning_cycle", return_value={}),
              patch.object(self.runtime, "_touch_detached_learning_heartbeat"),
              patch.object(self.runtime, "_classify_complexity", return_value="simple")):
            reply = self.runtime.chat(self.runtime.current_session, "Review this project before launch", on_event=lambda _: None)
        self.assertIn("verified", reply)
        self.assertNotIn("<details>", reply)
        self.assertEqual(len(self.runtime.delegation().list()), 2)
        self.assertEqual((self.root / "marker.txt").read_text(), "ok")

    def test_main_decides_distribution_from_an_ordinary_request(self):
        self.runtime.set_interaction_mode("agent")
        request = "Review this project before launch. Find correctness and security bugs. Please do not edit anything."
        self.assertFalse(self.runtime._is_bug_report(request))
        self.assertFalse(self.runtime._is_bug_report("审查这些bug，不要修改代码"))
        self.assertTrue(self.runtime._is_bug_report("Review and fix the bug"))
        self.assertTrue(self.runtime._is_bug_report("Review and fix the bug. Do not edit unrelated files."))
        self.assertTrue(self.runtime._is_bug_report("审查并修复bug，不要修改无关文件"))
        self.runtime._start_fix_workflow("bug: the command fails")
        self.assertIsNone(self.runtime._prepare_fix_workflow(request))
        decisions = []
        class MainProvider:
            def chat(inner, messages, model):
                if messages[0]["content"].startswith("You coordinate"):
                    decisions.append(json.loads(messages[1]["content"]))
                    if len(decisions) == 1:
                        return "malformed decision", None
                    return json.dumps({"decision": "delegate", "reason": "Independent component investigations",
                        "assignments": [brief(0), brief(1)]}), None
                if messages[0]["content"].startswith("Synthesize"):
                    return "The findings were independently verified.", None
                if any("spawn_agents(" in message["content"] for message in messages):
                    return "Findings:\n- marker.txt was checked.\n- Both investigations have evidence.", None
                return 'Plan:\n1. Discover the source files.\nAction: {"action":"list_dir","args":"."}', None
        self.runtime.llm_provider = MainProvider()
        with (patch.object(self.runtime, "dispatch_learning_cycle", return_value={}),
              patch.object(self.runtime, "_touch_detached_learning_heartbeat")):
            reply = self.runtime.chat(self.runtime.current_session, request, on_event=lambda _: None)
        self.assertEqual(len(decisions), 2)
        self.assertIn("marker.txt", decisions[0]["discovered_evidence"])
        self.assertEqual(decisions[0]["request"], request)
        self.assertEqual(len(self.runtime.delegation().list()), 2)
        self.assertTrue(all(run["status"] == "succeeded" for run in self.runtime.delegation().list()))
        self.assertIn("verified", reply)
        self.assertIsNone(self.runtime._interaction_controller.state().get("pending_plan"))
        from openkyrozen.agent.turn import TurnContext
        from openkyrozen.agent.response_recovery import _recover_plan_response
        turn = TurnContext("Review service")
        turn.interaction_mode = "ask"
        text = "Findings:\n- First issue.\n- Second issue."
        parsed = self.runtime._parse_model_response(text)
        _, recovered, exhausted = _recover_plan_response(self.runtime, turn, text, parsed, [], inspection_complete=True)
        self.assertIsNone(recovered["plan_proposal"])
        self.assertFalse(exhausted)

    def test_synthesis_failure_preserves_verified_evidence(self):
        plan = 'PlanProposal:\n```json\n' + json.dumps({"title": "Wrong execution plan", "summary": "Approve edits",
            "assumptions": [], "steps": [{"id": "edit", "title": "Edit", "description": "Change source", "acceptance": ["Changed"]}]}) + '\n```'
        for index, synthesis in enumerate(("[LLM Error] Provider timed out", plan, "[LLM Error] Provider timed out")):
            class MainProvider:
                def chat(inner, messages, model):
                    if messages[0]["content"].startswith("Synthesize"):
                        return synthesis, None
                    if any("spawn_agents(" in message["content"] for message in messages):
                        if index == 2:
                            return '<tool_calls><invoke name="read_file"><parameter name="path">marker.txt</parameter></invoke></tool_calls>', None
                        return "Inspection finished.", None
                    return "Action: " + json.dumps({"action": "spawn_agents", "args": json.dumps({"assignments": [brief(0), brief(1)]})}), None
            self.runtime.llm_provider = MainProvider()
            session = self.runtime.open_session(f"synthesis-{index}")
            with (self.runtime.use_session(session), patch.object(self.runtime, "dispatch_learning_cycle", return_value={}),
                  patch.object(self.runtime, "_touch_detached_learning_heartbeat")):
                self.runtime.set_interaction_mode("agent")
                reply = self.runtime.chat(session, "Inspect two components", on_event=lambda _: None)
                self.assertIn("Verified delegated findings", reply)
                self.assertIn("marker.txt", reply)
                if index == 2:
                    self.assertIn("unsupported tool-call wrapper", reply)
                self.assertIsNone(self.runtime._interaction_controller.state().get("pending_plan"))

    def test_parent_cannot_turn_unverified_findings_into_success(self):
        class MainProvider:
            def chat(inner, messages, model):
                if any("spawn_agents(" in message["content"] for message in messages):
                    return "Everything succeeded completely.", None
                return "Action: " + json.dumps({"action": "spawn_agents", "args": json.dumps({"assignments": [brief()]})}), None
        def invoke(run, review, **kwargs):
            return {"result": reviewed("changes_requested", findings=["Artifact is broken"]) if review else
                result(uncertainties=["Input missing"]), "tool_records": [{"action": "read_file", "success": True}]}
        self.runtime.subagent_manager.coordinator = self.manager(invoke)
        self.runtime.llm_provider = MainProvider()
        with (patch.object(self.runtime, "dispatch_learning_cycle", return_value={}),
              patch.object(self.runtime, "_touch_detached_learning_heartbeat")):
            reply = self.runtime.chat(self.runtime.current_session, "Inspect a component", on_event=lambda _: None)
        self.assertIn("not fully verified", reply)
        self.assertIn("Artifact is broken", reply)
        self.assertIn("Input missing", reply)
        self.assertNotIn("Everything succeeded", reply)

    def test_conflicting_sequential_plan_is_repaired_before_delegation(self):
        from openkyrozen.agent.turn import TurnContext
        action = "Action: " + json.dumps({"action": "spawn_agents", "args": json.dumps({"assignments": [brief()]})})
        conflict = 'TaskList:\n```json\n["Spawn specialist", "Wait for review", "Synthesize report"]\n```\n' + action
        turn = TurnContext("Audit", fast_backend="off")
        with patch.object(self.runtime, "_call_llm_with_spinner", return_value=action) as repair:
            text, parsed = self.runtime._observe_turn_response(turn, conflict, [])
        self.assertEqual(repair.call_count, 1)
        self.assertEqual(self.runtime.tasks.tasks, [])
        self.assertEqual(parsed["tool_calls"][0]["action"], "spawn_agents")
        self.assertIsNone(parsed["protocol_error"])
        with patch.object(self.runtime, "_call_llm_with_spinner", return_value=action) as repair:
            _, standalone = self.runtime._observe_turn_response(turn, conflict.split("Action:")[0], [])
        self.assertEqual(repair.call_count, 1)
        self.assertEqual(standalone["tool_calls"][0]["action"], "spawn_agents")
        self.assertEqual(self.runtime.tasks.tasks, [])
        with patch.object(self.runtime, "_call_llm_with_spinner", return_value=conflict) as repair:
            _, rejected = self.runtime._observe_turn_response(turn, conflict, [])
        self.assertEqual(repair.call_count, 2)
        self.assertEqual(rejected["tool_calls"], [])
        self.assertIn("no action was executed", rejected["protocol_error"])
        self.assertEqual(self.runtime.tasks.tasks, [])

    def test_plan_without_tasklist_is_repaired_before_delegation(self):
        from openkyrozen.agent.turn import TurnContext
        action = "Action: " + json.dumps({"action":"spawn_agents","args":json.dumps({"assignments":[brief()]})})
        conflict = "Plan:\n1. Locate the exact files.\n2. Spawn specialists in parallel.\n3. Wait for reviews.\n4. Synthesize findings.\n" + 'Action: {"action":"read_file","args":"marker.txt"}'
        turn = TurnContext("Audit",fast_backend="off")
        with patch.object(self.runtime,"_call_llm_with_spinner",return_value=action):
            text,parsed = self.runtime._observe_turn_response(turn,conflict,[])
        self.assertNotIn("Plan:",text)
        self.assertFalse(parsed["has_plan"])
        self.assertEqual(parsed["tool_calls"][0]["action"],"spawn_agents")
        self.assertEqual(self.runtime.tasks.tasks,[])

    def test_malformed_report_has_two_repairs_and_provider_errors_are_not_success(self):
        class InvalidProvider:
            def chat(inner, messages, model):
                self.calls.append(len(messages))
                return "This is not structured JSON", None
        self.runtime.get_provider = lambda _: InvalidProvider()
        coordinator = self.runtime.delegation()
        coordinator.spawn([brief()])
        run = coordinator.wait(timeout=5)[0]
        self.assertEqual(run["status"], "failed")
        self.assertIn("two repairs", run["error"])
        self.assertEqual(len(self.calls), 3)
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "KYROZEN_API_KEY": "main-only"}):
            run = coordinator.spawn([brief(1, provider="anthropic")])[0]
            finished = coordinator.wait([run["run_id"]], 5)[0]
        self.assertEqual(finished["status"], "blocked")
        self.assertIn("Missing credentials", finished["error"])

    def test_cancel_during_provider_call_prevents_late_write(self):
        entered, release = threading.Event(), threading.Event()
        class SlowProvider:
            def chat(inner, messages, model):
                entered.set()
                release.wait(3)
                return 'Action: {"action":"write_file","args":"marker.txt|late"}', None
        self.runtime.get_provider = lambda _: SlowProvider()
        coordinator = self.runtime.delegation()
        run = coordinator.spawn([brief(profile="coder")])[0]
        self.assertTrue(entered.wait(2))
        coordinator.cancel(run["run_id"])
        release.set()
        coordinator.futures[run["run_id"]].result(timeout=5)
        self.assertEqual((self.root/"marker.txt").read_text(), "ok")
        self.assertEqual(coordinator.detail(run["run_id"])["status"], "cancelled")

    def test_reuse_keeps_identity_and_context(self):
        coordinator = self.runtime.delegation()
        run = coordinator.spawn([brief()])[0]
        coordinator.wait(timeout=5)
        reused = coordinator.send(run["run_id"], brief(1))
        self.assertEqual(reused["name"], run["name"])
        self.assertEqual(reused["icon"], run["icon"])
        coordinator.wait(timeout=5)
        worker_calls = [item for item in self.calls if item[0] == run["run_id"]]
        self.assertGreater(len(worker_calls[-1][4]), len(worker_calls[0][4]))

    def test_assignment_captures_current_turn_not_coordinator_creation(self):
        coordinator = self.runtime.delegation()
        token = self.runtime._active_interaction_mode.set("plan")
        class Provider:
            def chat(inner, messages, model):
                if len(messages) == 2:
                    return 'Action: {"action":"write_file","args":"marker.txt|bad"}', None
                return json.dumps(result(status="blocked")), None
        self.runtime.get_provider = lambda _: Provider()
        try:
            coordinator.spawn([brief(profile="coder")])
            self.assertEqual(coordinator.wait(timeout=5)[0]["status"], "blocked")
        finally:
            self.runtime._active_interaction_mode.reset(token)
        self.assertEqual((self.root / "marker.txt").read_text(), "ok")

    def test_review_reads_broken_artifact_then_author_corrects_and_rechecks(self):
        runtime = self.runtime
        class Provider:
            def chat(inner, messages, model):
                review = "Independently verify" in messages[1]["content"]
                correction = "Correct these review findings" in messages[-1]["content"]
                if review and len(messages) == 2:
                    return 'Action: {"action":"read_file","args":"marker.txt"}', None
                if not review and (len(messages) == 2 or correction):
                    content = "ok" if correction else "broken"
                    return 'Action: ' + json.dumps({"action": "write_file", "args": "marker.txt|" + content}), None
                if review:
                    good = (self.root / "marker.txt").read_text() == "ok"
                    return json.dumps(reviewed("verified" if good else "changes_requested",
                        findings=[] if good else ["marker.txt contains broken; required ok"])), None
                return json.dumps(result("Claimed artifact is correct")), None
        runtime.get_provider = lambda _: Provider()
        coordinator = runtime.delegation()
        coordinator.spawn([brief(profile="coder")])
        run = coordinator.wait(timeout=5)[0]
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["corrections"], 1)
        self.assertEqual((self.root / "marker.txt").read_text(), "ok")
        self.assertEqual([review["verdict"] for review in run["reviews"]], ["changes_requested", "verified"])
        self.assertTrue(all(review["tool_receipts"][0]["action"] == "read_file" for review in run["reviews"]))

    def test_disagreements_stop_after_two_corrections_and_block_dependents(self):
        def invoke(run, review, **kwargs):
            return {"result": reviewed("changes_requested", findings=["Still incorrect"]) if review else result(),
                "tool_records": [{"action": "read_file", "success": True}]}
        coordinator = self.manager(invoke)
        coordinator.spawn([brief(i, dependencies=[str(i-1)] if i else []) for i in reversed(range(6))])
        runs = coordinator.wait(timeout=5)
        self.assertEqual([run["status"] for run in runs], ["blocked"] * 5 + ["unverified"])
        self.assertEqual(len(runs[-1]["reviews"]), 3)
        self.assertEqual(runs[-1]["corrections"], 2)

    def test_overlapping_assignments_queue_without_starving_independent_work(self):
        started, independent, release = threading.Event(), threading.Event(), threading.Event()
        def invoke(run, review, **kwargs):
            if not review and run["assignment_id"] == "0":
                started.set()
                self.assertTrue(release.wait(4))
            if not review and run["assignment_id"] == "2":
                independent.set()
            return {"result": reviewed() if review else result(), "tool_records": [{"action": "read_file", "success": True}]}
        coordinator = self.manager(invoke, concurrency=2)
        try:
            runs = coordinator.spawn([brief(0, profile="coder"), brief(1, profile="coder"), brief(2, profile="coder", scope=["other.txt"])])
            self.assertTrue(started.wait(2))
            with self.assertRaisesRegex(ValueError, "Duplicate active"):
                coordinator.spawn([brief(3, objective=brief(0)["objective"], context="Different wording does not make distinct work")])
            self.assertTrue(independent.wait(2))
            self.assertEqual(coordinator.detail(runs[1]["run_id"])["status"], "queued")
        finally:
            release.set()
        self.assertTrue(all(run["status"] == "succeeded" for run in coordinator.wait(timeout=5)))

    def test_runtime_rejects_false_artifact_agreement_and_preserves_attempt_receipts(self):
        def invoke(run, review, feedback=None, **kwargs):
            if review:
                return {"result": reviewed(), "tool_records": [{"action": "read_file", "args": "marker.txt", "success": True}]}
            return {"result": result(artifacts=[{"path": "marker.txt", "sha256": "incorrect"}]),
                    "tool_records": [] if feedback else [{"receipt_id": "first-read", "action": "read_file", "success": True}]}
        coordinator = self.manager(invoke)
        coordinator.spawn([brief()])
        run = coordinator.wait(timeout=5)[0]
        self.assertEqual(run["status"], "unverified")
        self.assertEqual(len(run["reviews"]), 3)
        self.assertIn("hash disagrees", str(run["reviews"][-1]["findings"]))
        self.assertEqual(run["result"]["tool_records"][0]["receipt_id"], "first-read")
        self.assertEqual(len(run["result"]["tool_records"]), 1)
        coordinator.send(run["run_id"], brief(1, objective="New investigation"))
        reused = coordinator.wait(timeout=5)[0]
        self.assertEqual(len(reused["attempts"]), 3)
        self.assertEqual(len(reused["result"]["tool_records"]), 1)

    def test_partial_reports_are_reviewed_without_passing_incomplete_acceptance(self):
        def invoke(run, review, **kwargs):
            return {"result": reviewed() if review else result(status="partial", uncertainties=["Missing source"]),
                    "tool_records": [{"action": "read_file", "success": True}]}
        coordinator = self.manager(invoke)
        coordinator.spawn([brief()])
        run = coordinator.wait(timeout=5)[0]
        self.assertEqual(run["reviews"][0]["verdict"], "verified")
        self.assertEqual(run["status"], "unverified")
        self.assertEqual(run["report"]["uncertainties"], ["Missing source"])

    def test_native_sdk_request_formats_routing_costs_and_failures(self):
        import httpx
        import openai
        import anthropic
        import anthropic._base_client as claude_client
        claude_http = getattr(claude_client, "httpx2", httpx)
        from openkyrozen.providers.factory import get_provider
        real_openai, real_anthropic = openai.OpenAI, anthropic.Anthropic
        requests = []
        fail = threading.Event()
        def handle(request):
            body = json.loads(request.content)
            requests.append((request.url, dict(request.headers), body))
            is_claude = request.url.path == "/v1/messages"
            if fail.is_set():
                response = claude_http.Response if is_claude else httpx.Response
                return response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "invalid fixture credential"}})
            messages = body["messages"] if is_claude else body["input"]
            review = "Independently verify" in messages[0 if is_claude else 1]["content"]
            if len(messages) == (1 if is_claude else 2):
                text = 'Action: {"action":"read_file","args":"marker.txt"}'
            else:
                text = json.dumps(reviewed() if review else result())
            if is_claude:
                return claude_http.Response(200, json={"id": "msg_fixture", "type": "message", "role": "assistant",
                    "model": body["model"] + "-actual", "content": [{"type": "text", "text": text}],
                    "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 11, "output_tokens": 7}})
            return httpx.Response(200, json={"id": "resp_fixture", "object": "response", "created_at": 1,
                "status": "completed", "model": body["model"] + "-actual", "output": [{"id": "msg_fixture", "type": "message",
                    "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": []}]}],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}})
        transport = httpx.MockTransport(handle)
        self.runtime._provider_config = ProviderConfig(provider="openai", api_key="fixture-openai", base_url="https://openai.fixture/v1",
            model_simple="gpt-fixture", model_complex="gpt-fixture")
        self.runtime.DEEPSEEK_MODEL = "gpt-fixture"
        self.runtime.get_provider = get_provider
        (self.root / "agent.yaml").write_text("subagents:\n  roles:\n    researcher:\n      provider: anthropic\n      model: claude-fixture\n")
        with (patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fixture-claude", "KYROZEN_API_KEY": "wrong-main"}),
              patch("openai.OpenAI", side_effect=lambda **kw: real_openai(**kw, http_client=httpx.Client(transport=transport), max_retries=0)),
              patch("anthropic.Anthropic", side_effect=lambda **kw: real_anthropic(**kw, http_client=claude_http.Client(transport=claude_http.MockTransport(handle)), max_retries=0))):
            coordinator = self.runtime.delegation()
            coordinator.spawn([brief(0, provider="openai", model="gpt-assignment"), brief(1)])
            runs = coordinator.wait(timeout=10)
            self.assertEqual([run["status"] for run in runs], ["succeeded", "succeeded"], [run.get("error") for run in runs])
            self.assertEqual(runs[0]["result"]["metrics"]["provider_model"], "openai:gpt-assignment-actual")
            self.assertEqual(runs[1]["result"]["metrics"]["provider_model"], "anthropic:claude-fixture-actual")
            self.assertEqual(runs[1]["result"]["metrics"]["tokens"], 36)
            self.assertIsNotNone(runs[1]["result"]["metrics"]["cost_picos"])
            self.assertTrue(all(run["reviews"][0]["metrics"]["provider_model"] == "openai:gpt-fixture-actual" for run in runs))
            for url, headers, body in requests:
                if url.path == "/v1/messages":
                    self.assertEqual(url.host, "api.anthropic.com")
                    self.assertEqual(headers["x-api-key"], "fixture-claude")
                    self.assertIn("system", body)
                    self.assertEqual(body["max_tokens"], 4096)
                    self.assertTrue(all(message["role"] != "system" for message in body["messages"]))
                else:
                    self.assertEqual(url.path, "/v1/responses")
                    self.assertEqual(headers["authorization"], "Bearer fixture-openai")
                    self.assertEqual(body["input"][0]["role"], "system")
            fail.set()
            for i, provider in enumerate(("openai", "anthropic"), 2):
                agent = coordinator.spawn([brief(i, provider=provider)])[0]
                failed = coordinator.wait([agent["run_id"]], 5)[0]
                self.assertEqual(failed["status"], "failed")
                self.assertIn("401", failed["error"])
                rows = self.runtime.memory_bank.store.list_usage_attempts(run_id=agent["run_id"])
                self.assertEqual(rows[0]["completion_state"], "failed")
                self.assertEqual(rows[0]["provider"], provider)

    def test_mcp_orchestration_is_scoped_and_uses_plain_json_args(self):
        from fastapi.testclient import TestClient
        from openkyrozen.interfaces.web.service import WebService
        client = TestClient(WebService(self.app).app)
        def call(name, args, session="mcp-a"):
            response = client.post("/mcp", json={"jsonrpc": "2.0", "id": name, "method": "tools/call",
                "params": {"session_id": session, "name": name, "arguments": {"args": json.dumps(args)}}}).json()
            self.assertFalse(response["result"]["isError"], response)
            return json.loads(response["result"]["content"][0]["text"])
        run = call("spawn_agents", {"assignments": [brief()]})["agents"][0]
        runs = call("wait_subagents", {"run_ids": [run["run_id"]], "timeout": 5})["agents"]
        self.assertEqual(runs[0]["status"], "succeeded")
        self.assertEqual(call("list_subagents", {}, "mcp-b")["agents"], [])
        self.assertEqual(call("list_subagents", {"run_id": run["run_id"]})["run_id"], run["run_id"])
        self.assertEqual(call("cancel_subagent", {"run_id": run["run_id"]})["status"], "succeeded")
        session = self.runtime.open_session("mcp-a")
        with self.runtime.use_session(session):
            # A failed nested receipt is data, not a failure of the inspection tool.
            self.runtime.delegation().runs[run["run_id"]]["report"]["uncertainties"] = ["source not found during an earlier attempt"]
        self.assertEqual(call("wait_subagents", {"timeout": 0})["agents"][0]["status"], "succeeded")
        listing = client.post("/mcp", json={"jsonrpc":"2.0", "id": 1, "method":"tools/list"}).json()
        names = {tool["name"] for tool in listing["result"]["tools"]}
        self.assertTrue({"spawn_agents", "send_subagent", "list_subagents", "wait_subagents", "cancel_subagent"} <= names)

    def test_accepted_plan_workers_reconcile_before_dependent_mutations(self):
        runtime = self.runtime
        proposal = runtime.current_session.interaction.propose_plan({"title":"Create two files",
            "summary":"Write only first.txt and second.txt", "assumptions":[], "steps":[
                {"id": "first", "title":"Create first.txt", "description":"Write first.txt with ok",
                 "acceptance":["first.txt exists"]},
                {"id": "second", "title":"Create second.txt", "description":"Write second.txt with ok",
                 "acceptance":["second.txt exists"]}]})
        runtime.current_session.interaction.accept_plan(runtime.tasks,
            plan_id=proposal["plan_id"], version=proposal["version"])
        class Provider:
            def chat(inner, messages, model):
                reviewing = "Independently verify" in messages[1]["content"]
                if len(messages) == 2:
                    value = json.loads(messages[1]["content"].split("\n", 1)[-1]) if reviewing else json.loads(messages[1]["content"])
                    assignment = value["assignment"] if reviewing else value
                    path = assignment["scope"][0]
                    return "Action: " + json.dumps({"action":"read_file" if reviewing else "write_file",
                        "args":path if reviewing else path+"|ok"}), None
                if reviewing:
                    return json.dumps(reviewed()), None
                return json.dumps(result(status="blocked" if "Error:" in messages[-1]["content"] else "completed")), None
        runtime.get_provider = lambda _: Provider()
        token = runtime._active_interaction_mode.set("agent")
        try:
            coordinator = runtime.delegation()
            wrong = coordinator.spawn([brief("wrong", profile="coder", scope=["outside-plan.txt"])])[0]
            self.assertEqual(coordinator.wait([wrong["run_id"]],5)[0]["status"], "blocked")
            self.assertFalse((self.root/"outside-plan.txt").exists())
            agents = coordinator.spawn([brief(0, profile="coder", scope=["first.txt"]),
                brief(1, profile="coder", scope=["second.txt"], dependencies=["0"])])
            finished = coordinator.wait([run["run_id"] for run in agents],5)
        finally:
            runtime._active_interaction_mode.reset(token)
        self.assertEqual([run["status"] for run in finished], ["succeeded", "succeeded"], finished)
        self.assertTrue(all(task["status"] == "succeeded" for task in runtime.tasks.tasks))
        self.assertEqual((self.root/"second.txt").read_text(), "ok")

    def test_real_writes_overlap_only_with_disjoint_ownership_and_main_waits(self):
        access = self.runtime.delegation().access
        barrier = threading.Barrier(2)
        observed = []
        def write(path):
            with access.claim({str((self.root/path).resolve())}, path, lambda:False):
                with access.acquire(str((self.root/path).resolve()), owner=path):
                    barrier.wait(timeout=3)
                    (self.root/path).write_text(path)
                    observed.append(path)
        threads = [threading.Thread(target=write,args=(path,)) for path in ("a.txt","b.txt")]
        for thread in threads: thread.start()
        for thread in threads: thread.join(4)
        self.assertEqual(set(observed), {"a.txt", "b.txt"})
        entered = threading.Event()
        with access.claim({str((self.root/"a.txt").resolve())}, "worker", lambda:False):
            def main_write():
                receipt = self.runtime.execute(self.runtime.current_session, "write_file", "a.txt|main")
                self.assertTrue(receipt.success)
                entered.set()
            thread = threading.Thread(target=main_write)
            thread.start()
            self.assertFalse(entered.wait(.05))
        thread.join(3)
        self.assertTrue(entered.is_set())
        self.assertEqual((self.root/"a.txt").read_text(), "main")


if __name__ == "__main__":
    unittest.main()
