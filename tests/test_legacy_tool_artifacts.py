import copy
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openkyrozen.agent.turn import TurnContext
from openkyrozen.app.bootstrap import build_application, build_memory
from openkyrozen.tools import ToolAdapters


class LegacyToolArtifactsTests(unittest.TestCase):
    def test_archived_legacy_result_retains_content_beyond_display_limit(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_APPROVAL_MODE": "never",
            "KYROZEN_WORKSPACE_ROOT": directory,
        }):
            root = Path(directory)
            for index in range(250):
                (root / f"file_{index:04d}_long_name.txt").touch()
            (root / "note.txt").write_text("note", encoding="utf-8")
            app = build_application(surface="cli", memory=build_memory(root / "state.sqlite3", user_id="alice", index=None),
                                    tools=ToolAdapters(root), user_id="alice")
            self.addCleanup(app.close)
            runtime = app.runtime
            turn = TurnContext("Inspect files", interaction_mode="agent", complexity="simple",
                               learning_run={"run_id": "large-test"}, memory_packet="",
                               response_text='Action: {"action":"list_dir","args":"."}',
                               tool_calls=[{"action": "list_dir", "args": "."}])
            turn.agent_versions = {}
            requests = []
            def response(messages):
                requests.append(copy.deepcopy(messages))
                return ('Action: {"action":"read_file","args":"note.txt"}' if len(requests) == 1
                        else "Files inspected.")
            with patch.object(runtime, "_call_llm_with_spinner", side_effect=response), \
                    patch.object(runtime, "_update_tasks_panel"):
                runtime._execute_action_rounds(turn)
            second = "\n".join(item["content"] for item in requests[1])
            artifact_id = re.search(r"artifact_[0-9a-f]+", second).group()
            pages, offset = [], 0
            while True:
                result = runtime.tool_registry.get_spec("read_tool_artifact").executor(
                    {"artifact_id": artifact_id, "offset": offset, "limit": 1000}).data
                pages.append(result["content"])
                if result["next_offset"] is None:
                    break
                offset = result["next_offset"]
            self.assertIn("file_0249_long_name.txt", "".join(pages))

    def test_search_limit_synthesis_uses_latest_result_and_archives_earlier_one(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_APPROVAL_MODE": "never",
            "KYROZEN_WORKSPACE_ROOT": directory,
        }):
            root = Path(directory)
            app = build_application(surface="cli", memory=build_memory(root / "state.sqlite3", user_id="alice", index=None),
                                    tools=ToolAdapters(root), user_id="alice")
            self.addCleanup(app.close)
            runtime = app.runtime
            turn = TurnContext("Search", interaction_mode="agent", complexity="simple",
                               learning_run={"run_id": "search-test"}, memory_packet="",
                               response_text='Action: {"action":"search_web","args":"0"}',
                               tool_calls=[{"action": "search_web", "args": "0"}])
            turn.agent_versions = {}
            requests = []
            def response(messages):
                requests.append(copy.deepcopy(messages))
                number = len(requests)
                return (f'Action: {{"action":"search_web","args":"{number}"}}'
                        if number <= 6 else "Search failed; no results verified.")
            def batch(calls, **kwargs):
                for call in calls:
                    kwargs["on_receipt"](call, SimpleNamespace(action="search_web", args=call["args"], success=False))
            with patch.object(runtime, "_call_llm_with_spinner", side_effect=response), \
                    patch.object(runtime, "_execute_tool_batch", side_effect=batch), \
                    patch.object(runtime, "_record_turn_receipt", return_value={"action": "search_web", "success": False}), \
                    patch.object(runtime, "_tool_result_for_prompt", side_effect=lambda receipt: "failure-" + receipt.args), \
                    patch.object(runtime, "_render_tool_result"), patch.object(runtime, "_render_warning"), \
                    patch.object(runtime, "_update_tasks_panel"):
                runtime._execute_action_rounds(turn)
            final = "\n".join(item["content"] for item in requests[-1])
            self.assertNotIn("failure-5", final)
            self.assertIn("failure-6", final)

    def test_legacy_archival_failure_stops_before_next_model_request(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_APPROVAL_MODE": "never",
            "KYROZEN_WORKSPACE_ROOT": directory,
        }):
            root = Path(directory)
            (root / "first.txt").write_text("first", encoding="utf-8")
            (root / "second.txt").write_text("second", encoding="utf-8")
            app = build_application(surface="cli", memory=build_memory(root / "state.sqlite3", user_id="alice", index=None),
                                    tools=ToolAdapters(root), user_id="alice")
            self.addCleanup(app.close)
            runtime = app.runtime
            turn = TurnContext("Read both", interaction_mode="agent", complexity="simple",
                               learning_run={"run_id": "legacy-test"}, memory_packet="",
                               response_text='Action: {"action":"read_file","args":"first.txt"}',
                               tool_calls=[{"action": "read_file", "args": "first.txt"}])
            turn.agent_versions = {}
            with patch.object(runtime, "_call_llm_with_spinner", return_value=
                              'Action: {"action":"read_file","args":"second.txt"}') as call, \
                    patch.object(runtime._agent_engine, "_store_tool_output", side_effect=OSError("disk full")), \
                    patch.object(runtime, "_update_tasks_panel"):
                runtime._execute_action_rounds(turn)
            self.assertEqual(call.call_count, 1)
            self.assertIn("Could not retain earlier tool results", turn.protocol_error_message)
            self.assertEqual(len(turn.tool_records), 2)

    def test_legacy_artifact_read_accepts_json_and_enforces_scope(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_APPROVAL_MODE": "never",
            "KYROZEN_WORKSPACE_ROOT": directory,
        }):
            root = Path(directory)
            app = build_application(surface="cli", memory=build_memory(root / "state.sqlite3", user_id="alice", index=None),
                                    tools=ToolAdapters(root), user_id="alice")
            self.addCleanup(app.close)
            runtime = app.runtime
            scope = {"user_id": runtime.tasks.user_id, "workspace_id": runtime.tasks.workspace_id,
                     "session_id": runtime.tasks.session_id}
            artifact_id = runtime.memory_bank.store.artifacts.create("abcdef", **scope)
            result = runtime._run_tool("read_tool_artifact", '{"artifact_id":"' + artifact_id + '","offset":2,"limit":3}',
                                       return_receipt=True)
            self.assertTrue(result.success)
            self.assertEqual(result.tool_result.data["content"], "cde")
            hidden = runtime.memory_bank.store.artifacts.create("private", **{**scope, "user_id": "bob"})
            denied = runtime._run_tool("read_tool_artifact", '{"artifact_id":"' + hidden + '"}', return_receipt=True)
            self.assertFalse(denied.success)
            self.assertNotIn("private", denied.result)

    def test_legacy_rounds_archive_results_in_each_prompt_mode(self):
        for mode, profile in (("agent", "full"), ("agent", "compact"), ("ask", "full"), ("plan", "full")):
            with self.subTest(mode=mode, profile=profile):
                self._verify_legacy_rounds(mode, profile)

    def _verify_legacy_rounds(self, mode, profile):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "KYROZEN_DISABLE_VECTOR_INDEX": "1", "KYROZEN_APPROVAL_MODE": "never",
            "KYROZEN_WORKSPACE_ROOT": directory,
        }):
            root = Path(directory)
            (root / "first.txt").write_text("FIRST_UNIQUE_CONTENT", encoding="utf-8")
            (root / "second.txt").write_text("SECOND_UNIQUE_CONTENT", encoding="utf-8")
            (root / "third.txt").write_text("THIRD_UNIQUE_CONTENT", encoding="utf-8")
            app = build_application(surface="cli", memory=build_memory(root / "state.sqlite3", user_id="alice", index=None),
                                    tools=ToolAdapters(root), user_id="alice")
            self.addCleanup(app.close)
            runtime = app.runtime
            turn = TurnContext("Read both files", interaction_mode=mode, complexity="simple",
                               learning_run={"run_id": "legacy-test"}, memory_packet="",
                               response_text='Action: {"action":"read_file","args":"first.txt"}',
                               tool_calls=[{"action": "read_file", "args": "first.txt"}])
            turn.agent_versions = {}
            requests = []
            final = ('PlanProposal:\n```json\n{"title":"Read files","summary":"Use the inspected files",'
                     '"assumptions":[],"steps":[{"id":"step-1","title":"Review",'
                     '"description":"Review the files","acceptance":["Reviewed"]}]}\n```'
                     if mode == "plan" else "All files were read.")
            replies = iter(['Action: {"action":"read_file","args":"second.txt"}',
                            'Action: {"action":"read_file","args":"third.txt"}'])
            def response(messages):
                requests.append(copy.deepcopy(messages))
                if len(requests) <= 2:
                    return next(replies)
                if len(requests) == 3:
                    text = "\n".join(item["content"] for item in messages)
                    artifact_id = re.search(r"artifact_[0-9a-f]+", text).group()
                    args = json.dumps({"artifact_id": artifact_id, "offset": 0, "limit": 1000})
                    return "Action: " + json.dumps({"action": "read_tool_artifact", "args": args})
                return final
            with patch.object(runtime, "_call_llm_with_spinner", side_effect=response), \
                    patch.object(runtime, "_prompt_profile", return_value=profile), \
                    patch.object(runtime, "_update_tasks_panel"):
                runtime._execute_action_rounds(turn)
            self.assertEqual(len(requests), 4)
            first = "\n".join(item["content"] for item in requests[0])
            second = "\n".join(item["content"] for item in requests[1])
            self.assertIn("FIRST_UNIQUE_CONTENT", first)
            self.assertNotIn("FIRST_UNIQUE_CONTENT", second)
            self.assertIn("SECOND_UNIQUE_CONTENT", second)
            third = "\n".join(item["content"] for item in requests[2])
            self.assertNotIn("FIRST_UNIQUE_CONTENT", third)
            self.assertNotIn("SECOND_UNIQUE_CONTENT", third)
            self.assertIn("THIRD_UNIQUE_CONTENT", third)
            fourth = "\n".join(item["content"] for item in requests[3])
            self.assertIn("FIRST_UNIQUE_CONTENT", fourth)
            self.assertNotIn("THIRD_UNIQUE_CONTENT", fourth)
            artifact_id = re.search(r"artifact_[0-9a-f]+", second).group()
            receipt = runtime._run_tool("read_tool_artifact", '{"artifact_id":"' + artifact_id + '"}',
                                        return_receipt=True)
            self.assertTrue(receipt.success)
            self.assertIn("FIRST_UNIQUE_CONTENT", receipt.result)
            reopened = build_application(surface="cli", memory=build_memory(root / "state.sqlite3", user_id="alice", index=None),
                                         tools=ToolAdapters(root), user_id="alice")
            self.addCleanup(reopened.close)
            reopened.runtime.bind_interaction_scope(runtime.tasks.session_id, user_id="alice")
            restored = reopened.runtime._run_tool("read_tool_artifact", '{"artifact_id":"' + artifact_id + '"}',
                                                 return_receipt=True)
            self.assertTrue(restored.success)
            self.assertIn("FIRST_UNIQUE_CONTENT", restored.result)


if __name__ == "__main__":
    unittest.main()
