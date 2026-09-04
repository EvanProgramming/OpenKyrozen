import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
import server
from plugin_runtime import PluginRuntime


class PluginLifecycleTests(unittest.TestCase):
    def test_runtime_loads_once_and_isolates_each_hook_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            load_count = root / "load-count.txt"
            hook_log = root / "hooks.log"
            bad_plugin = root / "00_bad.py"
            good_plugin = root / "01_good.py"
            bad_plugin.write_text(
                "class Bad:\n"
                "    def on_turn_start(self, **kwargs): raise RuntimeError('start')\n"
                "    def on_tool_execute(self, **kwargs): raise RuntimeError('tool')\n"
                "    def on_turn_end(self, **kwargs): raise RuntimeError('end')\n"
                "def register(): return Bad()\n",
                encoding="utf-8",
            )
            good_plugin.write_text(
                f"from pathlib import Path\n"
                f"LOG = Path({str(hook_log)!r})\n"
                f"COUNT = Path({str(load_count)!r})\n"
                "def append(value):\n"
                "    with LOG.open('a', encoding='utf-8') as handle: handle.write(value + '\\n')\n"
                "class Good:\n"
                "    def on_turn_start(self, user_input, **kwargs): append('start:' + user_input)\n"
                "    def on_tool_execute(self, action, args, result, success, **kwargs): append(f'tool:{action}:{success}:{result}')\n"
                "    def on_turn_end(self, reply, success, **kwargs): append('end:' + reply + ':' + str(success))\n"
                "def register():\n"
                "    COUNT.write_text(str(int(COUNT.read_text()) + 1) if COUNT.exists() else '1', encoding='utf-8')\n"
                "    return Good()\n",
                encoding="utf-8",
            )
            events = []
            runtime = PluginRuntime("isolated", plugins_dir=root,
                                    event_recorder=lambda event, payload: events.append((event, payload)))
            runtime.load_once()
            runtime.load_once()
            runtime.turn_start(user_input="hello", user_id="local")
            runtime.tool_execute(action="read_file", args="x", result="ok", success=True)
            runtime.turn_end(reply="finished", success=True, user_id="local")

            self.assertEqual(load_count.read_text(encoding="utf-8"), "1")
            lines = hook_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines, ["start:hello", "tool:read_file:True:ok", "end:finished:True"])
            failures = [payload for event, payload in events if event == "plugin.hook_failed"]
            self.assertEqual(len(failures), 3)
            self.assertEqual(set(runtime.plugin_names), {"00_bad", "01_good"})

    def test_active_workspace_plugin_overrides_packaged_plugin_with_same_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active = root / "active"
            packaged = root / "packaged"
            active.mkdir()
            packaged.mkdir()
            (active / "sample.py").write_text(
                "class Plugin:\n"
                "    source = 'active'\n"
                "def register(): return Plugin()\n",
                encoding="utf-8",
            )
            (packaged / "sample.py").write_text(
                "class Plugin:\n"
                "    source = 'packaged'\n"
                "def register(): return Plugin()\n",
                encoding="utf-8",
            )
            (packaged / "fallback.py").write_text(
                "class Plugin:\n"
                "    source = 'fallback'\n"
                "def register(): return Plugin()\n",
                encoding="utf-8",
            )

            runtime = PluginRuntime("precedence", plugin_dirs=(active, packaged))

            self.assertEqual(runtime.load_once(), ("sample", "fallback"))
            self.assertEqual(runtime._plugins[0][1].source, "active")

    def test_bundled_turn_logger_produces_a_real_turn_log(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "kyrozen_turns.log"
            runtime = PluginRuntime(
                "bundled-test", plugins_dir=Path(__file__).parents[1] / "plugins",
            )
            with patch.dict(os.environ, {"KYROZEN_TURN_LOG": str(log_path)}):
                runtime.turn_start(user_input="hello")
                runtime.tool_execute(action="read_file", args="README.md", result="ok", success=True)
                runtime.turn_end(reply="done", success=True)
            log = log_path.read_text(encoding="utf-8")
            self.assertIn("USER: hello", log)
            self.assertIn("TOOL: read_file(README.md) -> ok", log)
            self.assertIn("AGENT: done", log)

    def test_chat_wrapper_emits_start_and_end_for_success_and_failure(self):
        class SpyRuntime:
            def __init__(self):
                self.calls = []

            def turn_start(self, **kwargs):
                self.calls.append(("start", kwargs))

            def turn_end(self, **kwargs):
                self.calls.append(("end", kwargs))

        runtime = SpyRuntime()
        with patch.object(main, "_plugin_runtime_for_surface", return_value=runtime), \
             patch.object(main, "_chat_turn_impl", return_value="success"):
            self.assertEqual(main._chat_turn("hello"), "success")
        self.assertEqual([call[0] for call in runtime.calls], ["start", "end"])
        self.assertTrue(runtime.calls[-1][1]["success"])

        runtime.calls.clear()
        with patch.object(main, "_plugin_runtime_for_surface", return_value=runtime), \
             patch.object(main, "_chat_turn_impl", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                main._chat_turn("hello")
        self.assertEqual([call[0] for call in runtime.calls], ["start", "end"])
        self.assertFalse(runtime.calls[-1][1]["success"])

    def test_web_and_scheduled_chat_turns_use_the_same_wrapper(self):
        class SpyRuntime:
            def __init__(self):
                self.calls = []

            def turn_start(self, **kwargs):
                self.calls.append(("start", kwargs))

            def turn_end(self, **kwargs):
                self.calls.append(("end", kwargs))

        runtime = SpyRuntime()
        session_id = "plugin-lifecycle-web-test"
        session = {"messages": [], "user_id": "local", "session_id": session_id,
                   "profile": "auto", "created": 0}
        with patch.object(main, "_plugin_runtime_for_surface", return_value=runtime), \
             patch.object(main, "_chat_turn_impl", return_value="web reply"):
            self.assertEqual(server._run_session_chat(session, "web turn"), "web reply")
            server._run_scheduled_job({"payload": {
                "type": "chat", "session_id": "plugin-lifecycle-scheduled-test",
                "message": "scheduled turn",
            }})
        self.assertEqual([call[0] for call in runtime.calls],
                         ["start", "end", "start", "end"])
        self.assertEqual(runtime.calls[0][1]["user_id"], server._SERVER_ACTOR_ID)
        server._sessions.pop(session_id, None)
        server._sessions.pop("plugin-lifecycle-scheduled-test", None)

    def test_unknown_tool_attempt_triggers_one_tool_hook(self):
        class SpyRuntime:
            def __init__(self):
                self.calls = []

            def tool_execute(self, **kwargs):
                self.calls.append(kwargs)

        runtime = SpyRuntime()
        with patch.object(main, "_plugin_runtime_for_surface", return_value=runtime):
            result = main._run_tool("does_not_exist", "secret=hidden")
        self.assertIn("unknown tool", result)
        self.assertEqual(len(runtime.calls), 1)
        self.assertEqual(runtime.calls[0]["action"], "does_not_exist")
        self.assertFalse(runtime.calls[0]["success"])


if __name__ == "__main__":
    unittest.main()
