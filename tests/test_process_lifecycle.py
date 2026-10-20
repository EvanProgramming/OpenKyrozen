import sys
import time
import unittest
import shlex
import tempfile


class ProcessLifecycleTests(unittest.TestCase):
    def test_runtime_exposes_session_process_tools_and_application_close_stops_process(self):
        from pathlib import Path
        from unittest.mock import patch
        from openkyrozen.app.bootstrap import build_application, build_memory
        from openkyrozen.tools import ToolAdapters
        from openkyrozen.tools.models import CommandResult, invoke_tool_result

        with tempfile.TemporaryDirectory() as root, patch.dict('os.environ', {'KYROZEN_DISABLE_VECTOR_INDEX': '1'}):
            workspace = Path(root) / 'workspace'
            workspace.mkdir()
            app = build_application(memory=build_memory(Path(root) / 'state.sqlite3'), tools=ToolAdapters(workspace))
            try:
                started = app.runtime.run_command({
                    'command': shlex.join([sys.executable, '-c', 'import time; print("ready",flush=True); time.sleep(30)']),
                    'background': True,
                })
                self.assertIsInstance(started, CommandResult)
                self.assertTrue(started.success)
                self.assertIn('read_process', app.runtime.tool_registry.specs)
                deadline = time.monotonic() + 2
                read = invoke_tool_result(app.runtime._read_process, {'process_id': started.process_id, 'stream': 'stdout'})
                while not read.data.get('output') and time.monotonic() < deadline:
                    time.sleep(.02)
                    read = invoke_tool_result(app.runtime._read_process, {'process_id': started.process_id, 'stream': 'stdout'})
                self.assertTrue(read.success)
                self.assertEqual(read.data['output'], 'ready\n')
                handle = app.runtime.current_session.process_manager._handles[started.process_id]
            finally:
                app.close()
            self.assertIsNotNone(handle.process.poll())

    def test_handles_are_scoped_and_output_is_read_as_separate_streams(self):
        from openkyrozen.tools.processes import SessionProcessManager

        with tempfile.TemporaryDirectory() as root:
            owner = SessionProcessManager('user', root, 'session-a')
            other = SessionProcessManager('user', root, 'session-b')
            handle = owner.start(
                shlex.join([sys.executable, '-c', 'import sys,time; print("out",flush=True); print("err",file=sys.stderr,flush=True); time.sleep(30)']),
                cwd=root, env=None, timeout_seconds=30,
            )
            self.assertIsNone(other.read(handle.process_id))
            page = owner.read(handle.process_id)
            deadline = time.monotonic() + 3
            while not page['output'] and time.monotonic() < deadline:
                time.sleep(.02)
                page = owner.read(handle.process_id)
            self.assertIn('out', page['output'])
            self.assertTrue(page['running'])
            err = owner.read(handle.process_id, stream='stderr')
            self.assertIn('err', err['output'])
            owner.stop(handle.process_id)
            self.assertFalse(owner.read(handle.process_id)['running'])
            owner.close()
            other.close()

    def test_stop_terminates_owned_process_and_unknown_handle_is_rejected(self):
        from openkyrozen.tools.processes import SessionProcessManager

        with tempfile.TemporaryDirectory() as root:
            manager = SessionProcessManager('user', root, 'session-a')
            handle = manager.start(shlex.join([sys.executable, '-c', 'import time; time.sleep(30)']),
                                   cwd=root, env=None, timeout_seconds=30)
            self.assertIsNone(manager.stop('foreign-id'))
            self.assertTrue(manager.stop(handle.process_id))
            self.assertFalse(manager.read(handle.process_id)['running'])
            manager.close()
            with self.assertRaises(RuntimeError):
                manager.start(shlex.join([sys.executable, '-c', 'pass']), cwd=root,
                              env=None, timeout_seconds=30)

    def test_background_timeout_is_enforced_without_a_read_poll(self):
        from openkyrozen.tools.processes import SessionProcessManager
        with tempfile.TemporaryDirectory() as root:
            manager = SessionProcessManager('user', root, 'session-a')
            handle = manager.start(shlex.join([sys.executable, '-c', 'import time; time.sleep(30)']),
                                   cwd=root, env=None, timeout_seconds=.1)
            page = manager.read(handle.process_id)
            deadline = time.monotonic() + 3
            while not page['timed_out'] and time.monotonic() < deadline:
                time.sleep(.02)
                page = manager.read(handle.process_id)
            self.assertFalse(page['running'])
            self.assertTrue(page['timed_out'])
            manager.close()

    def test_process_output_cap_is_reported_and_can_be_paged(self):
        from unittest.mock import patch
        from openkyrozen.tools.processes import SessionProcessManager
        with tempfile.TemporaryDirectory() as root, patch('openkyrozen.tools.processes._MAX_OUTPUT_CHARS', 10):
            manager = SessionProcessManager('user', root, 'session-a')
            handle = manager.start(shlex.join([sys.executable, '-c', 'print("abcdefghijklmnop",end="")']),
                                   cwd=root, env=None, timeout_seconds=30)
            deadline = time.monotonic() + 2
            page = manager.read(handle.process_id)
            while not page['output_truncated'] and time.monotonic() < deadline:
                time.sleep(.01)
                page = manager.read(handle.process_id)
            self.assertEqual(page['output'], 'abcdefghij')
            self.assertTrue(page['output_truncated'])
            manager.close()
