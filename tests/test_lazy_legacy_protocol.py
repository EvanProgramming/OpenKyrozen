"""Fresh-process checks for the optional legacy text compatibility boundary."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class LazyLegacyProtocolTests(unittest.TestCase):
    def run_fresh(self, script):
        with tempfile.TemporaryDirectory() as directory:
            environment = {key: value for key, value in os.environ.items() if not any(
                term in key.upper() for term in ("API_KEY", "ACCESS_KEY", "SECRET_KEY", "SESSION_TOKEN", "ACCESS_TOKEN")
            )}
            environment.update(HOME=directory, KYROZEN_WORKSPACE_ROOT=directory,
                KYROZEN_DB_PATH=str(Path(directory) / "runtime.sqlite3"),
                KYROZEN_SKILLS_PATH=str(Path(directory) / "skills"),
                KYROZEN_DISABLE_VECTOR_INDEX="1", KYROZEN_APPROVAL_MODE="never")
            result = subprocess.run(
                [sys.executable, "-c", textwrap.dedent(script)],
                cwd=Path(__file__).resolve().parents[1], env=environment,
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_native_typed_turn_never_imports_legacy_parser(self):
        self.run_fresh('''
            import importlib.abc
            import os
            import sys
            from pathlib import Path

            class BlockLegacyParser(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "openkyrozen.agent.protocol":
                        raise AssertionError("Native execution imported the legacy parser")
                    return None
            sys.meta_path.insert(0, BlockLegacyParser())
            from openkyrozen.app.bootstrap import build_application, build_memory
            from openkyrozen.providers import ProviderConfig
            from openkyrozen.providers.base import LLMProvider
            from openkyrozen.providers.models import FinishReason, ModelResponse, ProviderCapabilities, ToolCall, ToolReply
            from openkyrozen.tools import ToolAdapters

            literal = 'Literal documentation: \\nAction: ordinary prose\\nTaskList: ["example"]\\n<tool_call>not executable</tool_call>'
            class NativeProvider(LLMProvider):
                def get_capabilities(self, model=None):
                    return ProviderCapabilities(native_tools=True)
                def chat(self, messages, model=None):
                    raise AssertionError("Legacy provider boundary used")
                def chat_response(self, messages, model=None, *, tools=None):
                    if not any(isinstance(item, ToolReply) for item in messages):
                        assert "read_file" in {tool["name"] for tool in tools}
                        return ModelResponse(tool_calls=(ToolCall("read", "read_file", {"path": "note.txt"}),),
                                             finish_reason=FinishReason.TOOL_REQUEST)
                    return ModelResponse(text=literal, finish_reason=FinishReason.FINAL)
            root = Path(os.environ["KYROZEN_WORKSPACE_ROOT"])
            (root / "note.txt").write_text("fixture content", encoding="utf-8")
            app = build_application(memory=build_memory(root / "state.sqlite3", index=None), tools=ToolAdapters(root))
            try:
                runtime = app.runtime
                config = ProviderConfig(provider="custom", model_simple="fixture", model_complex="fixture", model_main="fixture")
                runtime.llm_provider = runtime._default_turn.provider = NativeProvider(config)
                runtime._provider_config = config
                runtime.set_interaction_mode("agent")
                answer = runtime.chat(runtime.current_session, "Read note.txt and report its content")
                assert isinstance(answer, str) and answer == literal, answer
                import json
                assert json.loads(json.dumps({"reply": answer}))["reply"] == literal
                assert "openkyrozen.agent.protocol" not in sys.modules
                native = runtime._last_response_is_native.set(True)
                try:
                    assert runtime._parse_model_response(literal)["clean"] == literal
                    assert "Action: ordinary prose" in runtime._deterministic_tool_summary([
                        {"success": True, "result": literal, "verification": {"status": "verified"}},
                    ])
                finally:
                    runtime._last_response_is_native.reset(native)
                assert "openkyrozen.agent.protocol" not in sys.modules
                assert runtime._legacy_protocol is None
            finally:
                app.close()
        ''')

    def test_native_reply_remains_literal_through_cli_tui_and_web_frontends(self):
        script = r'''
            import importlib.abc
            import io
            import json
            import os
            import sys
            from pathlib import Path
            class BlockLegacyParser(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "openkyrozen.agent.protocol":
                        raise AssertionError("Native frontend imported the legacy parser")
                    return None
            sys.meta_path.insert(0, BlockLegacyParser())
            from openkyrozen.app.bootstrap import build_application, build_memory
            from openkyrozen.providers import ProviderConfig
            from openkyrozen.providers.base import LLMProvider
            from openkyrozen.providers.models import FinishReason, ModelResponse, ProviderCapabilities, ToolCall, ToolReply
            from openkyrozen.tools import ToolAdapters
            literal = 'Thought: literal documentation\nTaskDone: 0\nAction:\n```json\n{"action":"write_file","args":"should-not-exist.txt|bad"}\n```\nTaskList: ["example"]'
            class NativeProvider(LLMProvider):
                calls = 0
                def get_capabilities(self, model=None):
                    return ProviderCapabilities(native_tools=True)
                def chat(self, messages, model=None):
                    raise AssertionError("Legacy provider boundary used")
                def chat_response(self, messages, model=None, *, tools=None):
                    self.calls += 1
                    if not any(isinstance(item, ToolReply) for item in messages):
                        return ModelResponse(tool_calls=(ToolCall("read", "read_file", {"path": "note.txt"}),),
                                             finish_reason=FinishReason.TOOL_REQUEST)
                    return ModelResponse(text=literal, finish_reason=FinishReason.FINAL)
            root = Path(os.environ["KYROZEN_WORKSPACE_ROOT"])
            (root / "note.txt").write_text("fixture content", encoding="utf-8")
            app = build_application(surface=SURFACE, memory=build_memory(root / "state.sqlite3", index=None), tools=ToolAdapters(root))
            try:
                runtime = app.runtime
                config = ProviderConfig(provider="custom", model_simple="fixture", model_complex="fixture", model_main="fixture")
                provider = NativeProvider(config)
                runtime.llm_provider = runtime._default_turn.provider = provider
                runtime._provider_config = config
                runtime.set_interaction_mode("agent")
                if SURFACE == "cli":
                    from rich.console import Console
                    output = io.StringIO()
                    runtime.console = Console(file=output, width=200, force_terminal=False)
                    runtime._run_cli_chat("Read note.txt and report its content", runtime.interaction_envelope())
                    assert "Thought: literal documentation" in output.getvalue(), output.getvalue()
                    assert "TaskDone: 0" in output.getvalue(), output.getvalue()
                elif SURFACE == "tui":
                    from openkyrozen.interfaces.tui.backend import Backend
                    backend = Backend(app)
                    backend._output = io.StringIO()
                    backend._run_submit("Read note.txt and report its content", "native-frontend")
                    events = [json.loads(line) for line in backend._output.getvalue().splitlines()]
                    responses = [event for event in events if event["event"] == "response"]
                    assert responses and responses[-1]["text"] == literal, events
                    assert not any(event["event"] == "thinking" for event in events), events
                else:
                    from openkyrozen.interfaces.web.service import WebService
                    service = WebService(app)
                    session = service._get_or_create_session("native-web", service._SERVER_ACTOR_ID)
                    runtime.open_session("native-web", user_id=service._SERVER_ACTOR_ID).interaction.set_mode("agent")
                    answer = service._run_session_chat(session, "Read note.txt and report its content")
                    assert answer == literal, answer
                assert provider.calls == 2, provider.calls
                assert not (root / "should-not-exist.txt").exists()
                assert "openkyrozen.agent.protocol" not in sys.modules
                assert runtime._legacy_protocol is None
            finally:
                app.close()
        '''
        for surface in ("cli", "tui", "web"):
            with self.subTest(surface=surface):
                self.run_fresh(script.replace("SURFACE", repr(surface)))

    def test_native_plan_and_auto_modes_inspect_then_stage_a_typed_proposal(self):
        script = r'''
            import importlib.abc
            import os
            import sys
            from pathlib import Path
            class BlockLegacyParser(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "openkyrozen.agent.protocol":
                        raise AssertionError("Native planning imported the legacy parser")
                    return None
            sys.meta_path.insert(0, BlockLegacyParser())
            from openkyrozen.app.bootstrap import build_application, build_memory
            from openkyrozen.providers import ProviderConfig
            from openkyrozen.providers.base import LLMProvider
            from openkyrozen.providers.models import FinishReason, ModelResponse, ProviderCapabilities, ToolCall, ToolReply, provider_continuation, validate_replied_tool_definitions
            from openkyrozen.tools import ToolAdapters
            proposal = {"title": "Prepare result", "summary": "Create the requested result after approval.",
                "bounds": {"workspace_paths": ["result.txt"], "capabilities": ["read", "write"],
                           "effects": ["read_only", "filesystem"], "risk_ceiling": "normal"},
                "steps": [{"id": "result", "title": "Create result", "description": "Write result.txt",
                    "acceptance": [{"criterion": "Requested result exists", "verification": {
                        "action": "read_file", "arguments": {"path": "result.txt"}, "expect": {"equals": "ready"}}}],
                    "execution": {"action": "write_file", "arguments": {"path": "result.txt", "content": "ready"}}}]}
            class PlanProvider(LLMProvider):
                calls = 0
                def get_capabilities(self, model=None):
                    return ProviderCapabilities(native_tools=True)
                def chat(self, messages, model=None):
                    raise AssertionError("Native Plan Mode entered the text provider API")
                def chat_response(self, messages, model=None, *, tools=None):
                    self.calls += 1
                    validate_replied_tool_definitions(messages, tools)
                    if self.calls > (3 if DISCOVER_REQUEST else 2):
                        return ModelResponse(text="Approved execution remains pending.", finish_reason=FinishReason.FINAL)
                    names = {item["name"] for item in tools}
                    if not DISCOVER_REQUEST or self.calls == 1:
                        assert not names & {"github_read", "graph_query"}, names
                    else:
                        assert {"github_read", "graph_query"} <= names, names
                    assert not names & {"write_file", "run_cmd", "update_plan", "spawn_agents"}, names
                    if QUESTION_REQUEST:
                        assert "request_question" in names, names
                        return ModelResponse(tool_calls=(ToolCall("clarify", "request_question", {"questions": [{
                            "id": "format", "header": "Format", "prompt": "Which result format?", "choices": [
                                {"id": "plain", "label": "Plain"}, {"id": "rich", "label": "Rich"},
                            ]}]}),), finish_reason=FinishReason.TOOL_REQUEST,
                                             continuation=provider_continuation(self, model, "fixture", {"request": self.calls}))
                    if DISCOVER_REQUEST and self.calls == 1:
                        return ModelResponse(tool_calls=(ToolCall("discover", "discover_tools", {"names": ["github_read", "graph_query"]}),),
                            finish_reason=FinishReason.TOOL_REQUEST,
                            continuation=provider_continuation(self, model, "fixture", {"request": self.calls}))
                    if not any(isinstance(item, ToolReply) and item.name == "read_file" for item in messages):
                        assert "propose_plan" not in names, names
                        return ModelResponse(tool_calls=(ToolCall("inspect", "read_file", {"path": "note.txt"}),),
                                             finish_reason=FinishReason.TOOL_REQUEST,
                                             continuation=provider_continuation(self, model, "fixture", {"request": self.calls}))
                    assert "propose_plan" in names, names
                    return ModelResponse(tool_calls=(ToolCall("proposal", "propose_plan", proposal),),
                                         finish_reason=FinishReason.TOOL_REQUEST,
                                             continuation=provider_continuation(self, model, "fixture", {"request": self.calls}))
            root = Path(os.environ["KYROZEN_WORKSPACE_ROOT"])
            (root / "note.txt").write_text("Existing context", encoding="utf-8")
            app = build_application(memory=build_memory(root / "state.sqlite3", index=None), tools=ToolAdapters(root))
            try:
                runtime = app.runtime
                config = ProviderConfig(provider="custom", model_simple="fixture", model_complex="fixture", model_main="fixture")
                provider = PlanProvider(config)
                runtime.llm_provider = runtime._default_turn.provider = provider
                runtime._provider_config = config
                runtime.set_interaction_mode(PLAN_MODE)
                request = "Create result.txt after preparing the plan"
                answer = runtime.chat(runtime.current_session, request)
                state = runtime._interaction_controller.state()
                if QUESTION_REQUEST:
                    assert state["pending_plan"] is None, state
                    assert state["pending_question"]["questions"][0]["id"] == "format", state
                    assert state["pending_question"]["original_input"] == request, state
                    assert provider.calls == 1, provider.calls
                else:
                    assert state["pending_plan"] is not None, (state, answer)
                    assert state["pending_plan"]["version"] == 1, state
                    assert state["pending_plan"]["original_input"] == request, state
                    assert provider.calls == (3 if DISCOVER_REQUEST else 2), provider.calls
                    assert "Prepare result" in answer, answer
                assert state["executing_plan"] is None and runtime.tasks.working_plan is None, state
                assert not (root / "result.txt").exists()
                if not QUESTION_REQUEST:
                    pending = state["pending_plan"]
                    followup = runtime.chat(runtime.current_session, "accept plan", plan_acceptance={
                        "plan_id": pending["plan_id"], "version": pending["version"], "fingerprint": pending["fingerprint"],
                    })
                    assert "Native model request failed" not in followup, followup
                    assert "Approved execution remains pending" in followup, followup
                    assert provider.calls == (4 if DISCOVER_REQUEST else 3), provider.calls
                assert "openkyrozen.agent.protocol" not in sys.modules
            finally:
                app.close()
        '''
        for mode, question, discovery in (("plan", False, False), ("auto", False, False),
                                           ("plan", True, False), ("plan", False, True)):
            with self.subTest(mode=mode, question=question, discovery=discovery):
                self.run_fresh(script.replace("PLAN_MODE", repr(mode)).replace("QUESTION_REQUEST", repr(question))
                               .replace("DISCOVER_REQUEST", repr(discovery)))

    def test_native_accepted_plan_distinguishes_material_revision_from_safe_narrowing(self):
        script = r'''
            import copy
            import importlib.abc
            import os
            import sys
            from pathlib import Path
            class BlockLegacyParser(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "openkyrozen.agent.protocol":
                        raise AssertionError("Native revision imported the legacy parser")
                    return None
            sys.meta_path.insert(0, BlockLegacyParser())
            from openkyrozen.app.bootstrap import build_application, build_memory
            from openkyrozen.providers import ProviderConfig
            from openkyrozen.providers.base import LLMProvider
            from openkyrozen.providers.models import FinishReason, ModelResponse, ProviderCapabilities, ToolCall, ToolReply
            from openkyrozen.tools import ToolAdapters
            proposal = {"title": "Page", "summary": "Create the approved page", "bounds": {
                "workspace_paths": ["."] if REVISION_CASE == "narrow" else ["page.txt"],
                "capabilities": ["read", "write"], "effects": ["read_only", "filesystem"], "risk_ceiling": "normal"},
                "steps": [{"id": "page", "title": "Page", "description": "Write page.txt", "acceptance": [{
                    "criterion": "Approved page exists", "verification": {"action": "read_file",
                    "arguments": {"path": "page.txt"}, "expect": {"equals": "approved"}}}]}]}
            revised = copy.deepcopy(proposal)
            revised["bounds"]["workspace_paths"] = ["page.txt"]
            if REVISION_CASE == "material":
                revised["summary"] = "Create an additional page outcome"
                revised["bounds"]["workspace_paths"].append("extra.txt")
            class RevisionProvider(LLMProvider):
                calls = 0
                def get_capabilities(self, model=None):
                    return ProviderCapabilities(native_tools=True)
                def chat(self, messages, model=None):
                    raise AssertionError("Native revision entered text provider API")
                def chat_response(self, messages, model=None, *, tools=None):
                    self.calls += 1
                    replies = [item for item in messages if isinstance(item, ToolReply)]
                    if self.calls == 1:
                        assert "propose_plan" not in {item["name"] for item in tools}
                        return ModelResponse(tool_calls=(ToolCall("inspect", "read_file", {"path": "page.txt"}),), finish_reason=FinishReason.TOOL_REQUEST)
                    if self.calls == 2:
                        return ModelResponse(tool_calls=(ToolCall("revision", "propose_plan", revised),), finish_reason=FinishReason.TOOL_REQUEST)
                    assert REVISION_CASE == "narrow", "Material revision must stop pending approval"
                    if self.calls == 3:
                        control = next(item for item in replies if item.call_id == "revision")
                        assert control.output["data"]["requires_approval"] is False, control
                        return ModelResponse(tool_calls=(ToolCall("continue", "read_file", {"path": "page.txt"}),), finish_reason=FinishReason.TOOL_REQUEST)
                    return ModelResponse(text="Continued within updated approved scope.", finish_reason=FinishReason.FINAL)
            root = Path(os.environ["KYROZEN_WORKSPACE_ROOT"])
            (root / "page.txt").write_text("approved", encoding="utf-8")
            app = build_application(memory=build_memory(root / "state.sqlite3", index=None), tools=ToolAdapters(root))
            try:
                runtime = app.runtime
                config = ProviderConfig(provider="custom", model_simple="fixture", model_complex="fixture", model_main="fixture")
                provider = RevisionProvider(config)
                runtime.llm_provider = runtime._default_turn.provider = provider
                runtime._provider_config = config
                runtime.set_interaction_mode("agent")
                controller = runtime._interaction_controller
                initial = controller.propose_plan(proposal)
                accepted, _ = controller.accept_plan(runtime.tasks, plan_id=initial["plan_id"], version=1)
                answer = runtime.chat(runtime.current_session, "Review the approved page scope")
                state = controller.state()
                if REVISION_CASE == "material":
                    assert provider.calls == 2, provider.calls
                    assert state["pending_plan"]["version"] == 2, state
                    assert state["executing_plan"]["bounds"] == accepted["bounds"], state
                    denied = runtime._execute_turn_action("write_file", {"path": "page.txt", "content": "unapproved"},
                        operation_scope="post-proposal", successful_operations=set(), call_id="write", call_id_source="native")
                    assert not denied.success
                else:
                    assert provider.calls == 4, provider.calls
                    assert state["pending_plan"] is None and state["executing_plan"]["version"] == 2, state
                    assert state["executing_plan"]["bounds"]["workspace_paths"] == ["page.txt"], state
                    assert "Continued within updated approved scope" in answer, answer
                assert (root / "page.txt").read_text() == "approved"
                assert not (root / "extra.txt").exists()
                assert "openkyrozen.agent.protocol" not in sys.modules
            finally:
                app.close()
        '''
        for case in ("material", "narrow"):
            with self.subTest(case=case):
                self.run_fresh(script.replace("REVISION_CASE", repr(case)))

    def test_native_delegated_synthesis_uses_typed_output_or_keeps_verified_reports(self):
        script = r'''
            import importlib.abc
            import os
            import sys
            from pathlib import Path
            from types import SimpleNamespace
            class BlockLegacyParser(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "openkyrozen.agent.protocol":
                        raise AssertionError("Native delegated synthesis imported the legacy parser")
                    return None
            sys.meta_path.insert(0, BlockLegacyParser())
            from openkyrozen.app.bootstrap import build_application, build_memory
            from openkyrozen.providers import ProviderConfig
            from openkyrozen.providers.base import LLMProvider
            from openkyrozen.providers.errors import ProviderError, ProviderErrorKind
            from openkyrozen.providers.models import FinishReason, ModelResponse, ProviderCapabilities, ToolCall, ToolReply
            from openkyrozen.tools import ToolAdapters
            literal = "Thought: literal synthesized finding\nAction: documentation only\nAccepted child finding"
            class SynthesisProvider(LLMProvider):
                calls = 0
                def get_capabilities(self, model=None):
                    return ProviderCapabilities(native_tools=True)
                def chat(self, messages, model=None):
                    raise AssertionError("Native synthesis entered the text provider API")
                def chat_response(self, messages, model=None, *, tools=None):
                    self.calls += 1
                    if messages[0]["content"].startswith("Synthesize"):
                        assert not tools, tools
                        if SYNTHESIS_CASE == "error":
                            raise RuntimeError("Synthesis fixture failed")
                        if SYNTHESIS_CASE == "cancel":
                            return ModelResponse(finish_reason=FinishReason.CANCELLED)
                        if SYNTHESIS_CASE == "tool":
                            return ModelResponse(tool_calls=(ToolCall("unwanted", "write_file", {
                                "path": "unwanted.txt", "content": "must not execute"}),), finish_reason=FinishReason.TOOL_REQUEST)
                        return ModelResponse(text=literal, finish_reason=FinishReason.FINAL)
                    if not any(isinstance(item, ToolReply) for item in messages):
                        return ModelResponse(tool_calls=(ToolCall("inspect", "read_file", {"path": "note.txt"}),),
                                             finish_reason=FinishReason.TOOL_REQUEST)
                    return ModelResponse(text="Root inspection finished; retain child evidence.", finish_reason=FinishReason.FINAL)
            root = Path(os.environ["KYROZEN_WORKSPACE_ROOT"])
            (root / "note.txt").write_text("verified content", encoding="utf-8")
            app = build_application(memory=build_memory(root / "state.sqlite3", index=None), tools=ToolAdapters(root))
            try:
                runtime = app.runtime
                config = ProviderConfig(provider="custom", model_simple="fixture", model_complex="fixture", model_main="fixture")
                provider = SynthesisProvider(config)
                runtime.llm_provider = runtime._default_turn.provider = provider
                runtime._provider_config = config
                runtime.set_interaction_mode("agent")
                run = {"run_id": "child", "name": "Inspector", "version": 1, "status": "succeeded",
                       "assignment": {"task": "Inspect note"}, "parent_receipts": [], "reviews": [],
                       "report": {"summary": "Accepted child finding", "findings": ["note.txt was verified"],
                                  "evidence": ["note.txt: verified content"], "uncertainties": []}}
                calls = []
                def list_runs():
                    calls.append(True)
                    return [] if len(calls) == 1 else [run]
                runtime.subagent_manager.coordinator = SimpleNamespace(list=list_runs, close=lambda: None)
                events = []
                try:
                    answer = runtime.chat(runtime.current_session, "Read note.txt and report the findings", on_runtime_event=events.append)
                except ProviderError as exc:
                    assert SYNTHESIS_CASE == "cancel" and exc.kind == ProviderErrorKind.CANCELLED, exc
                    assert events[-1].kind == "turn.cancelled", events[-1]
                    assert run["status"] == "succeeded" and run["report"]["summary"] == "Accepted child finding"
                else:
                    assert SYNTHESIS_CASE != "cancel", "Cancelled synthesis must cancel the user turn"
                    if SYNTHESIS_CASE == "success":
                        assert answer == literal, answer
                    else:
                        assert "Verified delegated findings" in answer and "Accepted child finding" in answer, answer
                        assert "note.txt" in answer, answer
                assert provider.calls == 3, provider.calls
                assert not (root / "unwanted.txt").exists()
                assert "openkyrozen.agent.protocol" not in sys.modules
            finally:
                app.close()
        '''
        for case in ("success", "error", "tool", "cancel"):
            with self.subTest(case=case):
                self.run_fresh(script.replace("SYNTHESIS_CASE", repr(case)))

    def test_native_historical_artifact_failure_stops_without_provider_or_legacy_import(self):
        self.run_fresh(r'''
            import importlib.abc
            import os
            import sys
            from pathlib import Path
            from unittest.mock import patch
            class BlockLegacyParser(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "openkyrozen.agent.protocol":
                        raise AssertionError("Native artifact failure imported legacy cleanup")
                    return None
            sys.meta_path.insert(0, BlockLegacyParser())
            from openkyrozen.app.bootstrap import build_application, build_memory
            from openkyrozen.agent.conversation import model_response_to_dict, tool_result_to_dict
            from openkyrozen.providers import ProviderConfig
            from openkyrozen.providers.base import LLMProvider
            from openkyrozen.providers.models import FinishReason, ModelResponse, ProviderCapabilities, ToolCall, provider_continuation
            from openkyrozen.tools import ToolAdapters
            from openkyrozen.tools.models import tool_success
            class UncalledProvider(LLMProvider):
                calls = 0
                def chat(self, *args, **kwargs):
                    raise AssertionError("Legacy provider must not run")
                def get_capabilities(self, model=None):
                    return ProviderCapabilities(native_tools=True)
                def chat_response(self, *args, **kwargs):
                    self.calls += 1
                    raise AssertionError("Provider must not run when prior artifacts cannot be retained")
            root = Path(os.environ["KYROZEN_WORKSPACE_ROOT"])
            app = build_application(memory=build_memory(root / "state.sqlite3", index=None), tools=ToolAdapters(root))
            try:
                runtime = app.runtime
                config = ProviderConfig(provider="custom", model_simple="fixture", model_complex="fixture", model_main="fixture")
                provider = UncalledProvider(config)
                runtime.llm_provider = runtime._default_turn.provider = provider
                runtime._provider_config = config
                runtime.set_interaction_mode("agent")
                history = runtime.conversation_history
                history.append(history.entry("user.input", {"text": "Earlier read"}, turn_id="previous"))
                response = ModelResponse(tool_calls=(ToolCall("old", "read_file", {"path": "note.txt"}),),
                    finish_reason=FinishReason.TOOL_REQUEST,
                    continuation=provider_continuation(provider, "fixture", "fixture", {"call": "old"}))
                history.append(history.entry("model.response", {"response": model_response_to_dict(response)}, turn_id="previous"))
                history.append(history.entry("tool.result", {"call_id": "old", "name": "read_file",
                    "output": tool_result_to_dict(tool_success("Historical file output")), "is_error": False}, turn_id="previous", call_id="old"))
                with patch.object(runtime.memory_bank.store.artifacts, "get_or_create", side_effect=OSError("storage unavailable")):
                    answer = runtime.chat(runtime.current_session, "Continue inspecting note.txt")
                assert "Could not retain earlier tool results" in answer, answer
                assert provider.calls == 0, provider.calls
                assert "openkyrozen.agent.protocol" not in sys.modules
            finally:
                app.close()
        ''')

    def test_native_failure_and_cancellation_do_not_load_legacy_cleanup(self):
        script = '''
            import importlib.abc
            import os
            import sys
            from pathlib import Path
            class BlockLegacyParser(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "openkyrozen.agent.protocol":
                        raise AssertionError("Native failure imported legacy cleanup")
                    return None
            sys.meta_path.insert(0, BlockLegacyParser())
            from openkyrozen.app.bootstrap import build_application, build_memory
            from openkyrozen.providers import ProviderConfig
            from openkyrozen.providers.base import LLMProvider
            from openkyrozen.providers.errors import ProviderError, ProviderErrorKind
            from openkyrozen.providers.models import FinishReason, ModelResponse, ProviderCapabilities, ToolCall, ToolReply
            from openkyrozen.tools import ToolAdapters
            class FailingProvider(LLMProvider):
                def get_capabilities(self, model=None):
                    return ProviderCapabilities(native_tools=True)
                def chat(self, messages, model=None):
                    raise AssertionError("Legacy provider boundary used")
                def chat_response(self, messages, model=None, *, tools=None):
                    if not any(isinstance(item, ToolReply) for item in messages):
                        return ModelResponse(tool_calls=(ToolCall("read", "read_file", {"path": "note.txt"}),),
                                             finish_reason=FinishReason.TOOL_REQUEST)
                    if FAILURE_MODE == "cancel":
                        raise ProviderError(ProviderErrorKind.CANCELLED, terminal=True)
                    raise RuntimeError("Fixture provider failed after the read")
            root = Path(os.environ["KYROZEN_WORKSPACE_ROOT"])
            (root / "note.txt").write_text("Action: file data is literal", encoding="utf-8")
            app = build_application(memory=build_memory(root / "state.sqlite3", index=None), tools=ToolAdapters(root))
            try:
                runtime = app.runtime
                config = ProviderConfig(provider="custom", model_simple="fixture", model_complex="fixture", model_main="fixture")
                runtime.llm_provider = runtime._default_turn.provider = FailingProvider(config)
                runtime._provider_config = config
                runtime.set_interaction_mode("agent")
                try:
                    answer = runtime.chat(runtime.current_session, "Read note.txt and report its content")
                except ProviderError as exc:
                    assert FAILURE_MODE == "cancel" and exc.kind == ProviderErrorKind.CANCELLED, exc
                else:
                    assert FAILURE_MODE == "error" and "Native model request failed" in answer, answer
                assert "openkyrozen.agent.protocol" not in sys.modules
                assert runtime._legacy_protocol is None
            finally:
                app.close()
        '''
        for mode in ("error", "cancel"):
            with self.subTest(mode=mode):
                self.run_fresh(script.replace("FAILURE_MODE", repr(mode)))

    def test_legacy_helpers_load_adapter_on_first_use_and_keep_control_semantics(self):
        self.run_fresh('''
            import os
            import sys
            from pathlib import Path
            from openkyrozen.app.bootstrap import build_application, build_memory
            from openkyrozen.tools import ToolAdapters
            root = Path(os.environ["KYROZEN_WORKSPACE_ROOT"])
            app = build_application(memory=build_memory(root / "state.sqlite3", index=None), tools=ToolAdapters(root))
            try:
                runtime = app.runtime
                assert "openkyrozen.agent.protocol" not in sys.modules
                assert runtime._legacy_protocol is None
                parsed = runtime._parse_model_response('Action:\\n```json\\n{"action":"read_file","args":"note.txt"}\\n```')
                assert parsed["tool_calls"] == [{"action": "read_file", "args": "note.txt"}], parsed
                assert parsed["clean"] == ""
                adapter = runtime.legacy_protocol
                assert adapter is runtime.legacy_protocol
                assert "openkyrozen.agent.protocol" in sys.modules
                assert runtime._safe_fstring("{value}") == "{{value}}"
                assert runtime._clean_final_response("Answer.\\nTaskDone: 0") == "Answer."
            finally:
                app.close()
        ''')
