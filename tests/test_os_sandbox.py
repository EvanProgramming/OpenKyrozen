import os
import shutil
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _filesystem_escape_probe(workspace, sibling_file, symlink_file):
    code = (
        "from pathlib import Path\n"
        f"Path({str(Path(workspace) / 'inside.txt')!r}).write_text('inside')\n"
        f"targets = [{str(sibling_file)!r}, {str(symlink_file)!r}]\n"
        "forbidden = False\n"
        "for target in targets:\n"
        "    try: Path(target).write_text('escaped')\n"
        "    except OSError: pass\n"
        "    else: forbidden = True\n"
        "if forbidden: raise SystemExit('filesystem escape succeeded')"
    )
    return shlex.join([sys.executable, '-c', code])


class OSSandboxTests(unittest.TestCase):
    def test_required_unavailable_sandbox_blocks_command_before_execution(self):
        from openkyrozen.tools import ToolAdapters

        with tempfile.TemporaryDirectory() as root, patch('openkyrozen.tools.sandbox.sys.platform', 'win32'), \
                patch.dict(os.environ, {'KYROZEN_SANDBOX_REQUIRED': '1'}):
            workspace = Path(root) / 'workspace'
            workspace.mkdir()
            marker = Path(root) / 'outside.txt'
            result = ToolAdapters(workspace).run_command({
                'command': f'echo escaped > {shlex.quote(str(marker))}',
            })
            self.assertFalse(result.success)
            self.assertEqual(result.failure, 'sandbox_unavailable')
            self.assertEqual(result.sandbox_backend, 'unavailable')
            self.assertFalse(marker.exists())

    def test_command_environment_cannot_disable_required_sandbox(self):
        from openkyrozen.tools import ToolAdapters

        with tempfile.TemporaryDirectory() as root, patch('openkyrozen.tools.sandbox.sys.platform', 'win32'), \
                patch.dict(os.environ, {'KYROZEN_SANDBOX_REQUIRED': '1'}):
            workspace = Path(root) / 'workspace'
            workspace.mkdir()
            marker = Path(root) / 'outside.txt'
            result = ToolAdapters(workspace).run_command({
                'command': f'echo escaped > {shlex.quote(str(marker))}',
                'env': {'KYROZEN_SANDBOX_REQUIRED': '0'},
            })
            self.assertFalse(result.success)
            self.assertEqual(result.failure, 'sandbox_unavailable')
            self.assertFalse(marker.exists())

    def test_optional_unavailable_sandbox_is_reported_in_structured_result(self):
        from openkyrozen.tools import ToolAdapters
        from openkyrozen.tools.models import command_tool_result

        with tempfile.TemporaryDirectory() as root, patch('openkyrozen.tools.sandbox.sys.platform', 'win32'), \
                patch.dict(os.environ, {'KYROZEN_SANDBOX_REQUIRED': ''}):
            result = ToolAdapters(root).run_command({'command': 'echo fallback-ran'})
            self.assertTrue(result.success)
            self.assertEqual(result.sandbox_backend, 'unavailable')
            self.assertIn('unavailable', result.output.lower())
            self.assertEqual(command_tool_result(result).data['sandbox_backend'], 'unavailable')

    def test_installed_but_unusable_backend_is_reported_and_optional_mode_falls_back(self):
        from openkyrozen.tools import ToolAdapters

        with tempfile.TemporaryDirectory() as root, \
                patch('openkyrozen.tools.sandbox.sys.platform', 'linux'), \
                patch('openkyrozen.tools.sandbox.shutil.which', return_value='/usr/bin/bwrap'), \
                patch('openkyrozen.tools.sandbox._probe_backend', return_value='uid map denied'), \
                patch.dict(os.environ, {'KYROZEN_SANDBOX_REQUIRED': ''}):
            workspace = Path(root) / 'workspace'
            workspace.mkdir()
            marker = Path(root) / 'fallback.txt'
            result = ToolAdapters(workspace).run_command({
                'command': f'echo ran > {shlex.quote(str(marker))}',
            })
            self.assertTrue(result.success)
            self.assertEqual(result.sandbox_backend, 'unavailable')
            self.assertEqual(result.sandbox_reason, 'bubblewrap could not enforce filesystem restrictions: uid map denied')
            self.assertIn('without filesystem protection', result.output.lower())
            self.assertTrue(marker.exists())

    def test_installed_but_unusable_backend_blocks_when_sandbox_is_required(self):
        from openkyrozen.tools import ToolAdapters

        with tempfile.TemporaryDirectory() as root, \
                patch('openkyrozen.tools.sandbox.sys.platform', 'linux'), \
                patch('openkyrozen.tools.sandbox.shutil.which', return_value='/usr/bin/bwrap'), \
                patch('openkyrozen.tools.sandbox._probe_backend', return_value='uid map denied'), \
                patch.dict(os.environ, {'KYROZEN_SANDBOX_REQUIRED': '1'}):
            workspace = Path(root) / 'workspace'
            workspace.mkdir()
            marker = Path(root) / 'blocked.txt'
            result = ToolAdapters(workspace).run_command({
                'command': f'echo blocked > {shlex.quote(str(marker))}',
            })
            self.assertFalse(result.success)
            self.assertEqual(result.failure, 'sandbox_unavailable')
            self.assertEqual(result.sandbox_reason, 'bubblewrap could not enforce filesystem restrictions: uid map denied')
            self.assertFalse(marker.exists())

    @unittest.skipUnless(sys.platform == 'darwin', 'requires macOS sandbox-exec')
    def test_macos_sandbox_allows_workspace_writes_and_denies_sibling_writes(self):
        from openkyrozen.tools import ToolAdapters

        with tempfile.TemporaryDirectory(dir=Path.home()) as root:
            root = Path(root).resolve()
            workspace = root / 'workspace'
            workspace.mkdir()
            sibling_file = root / 'escaped.txt'
            outside = root / 'outside'
            outside.mkdir()
            (workspace / 'escape-link').symlink_to(outside, target_is_directory=True)
            command = _filesystem_escape_probe(
                workspace, sibling_file, workspace / 'escape-link' / 'link.txt',
            )
            result = ToolAdapters(workspace).run_command({'command': command})
            self.assertEqual(result.sandbox_backend, 'sandbox-exec')
            self.assertTrue((workspace / 'inside.txt').is_file())
            self.assertFalse(sibling_file.exists())
            self.assertFalse((outside / 'link.txt').exists())
            self.assertTrue(result.success, result.output)

    @unittest.skipUnless(sys.platform == 'darwin', 'requires macOS sandbox-exec')
    def test_background_descendants_inherit_macos_filesystem_restriction(self):
        from openkyrozen.tools.processes import SessionProcessManager

        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve()
            workspace = root / 'workspace'
            workspace.mkdir()
            manager = SessionProcessManager('actor', str(workspace), 'session')
            outside = root / 'escaped.txt'
            handle = manager.start(
                f'echo outside > {shlex.quote(str(outside))}; echo completed',
                cwd=str(workspace), env=os.environ.copy(), timeout_seconds=10,
            )
            try:
                handle.process.wait(timeout=5)
                self.assertEqual(handle.sandbox_backend, 'sandbox-exec')
                self.assertFalse(outside.exists())
            finally:
                manager.close()

    @unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('bwrap'),
                         'requires a usable Linux Bubblewrap installation')
    def test_linux_sandbox_allows_workspace_writes_and_denies_sibling_writes(self):
        from openkyrozen.tools import ToolAdapters

        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve()
            workspace = root / 'workspace'
            workspace.mkdir()
            sibling_file = root / 'escaped.txt'
            outside = root / 'outside'
            outside.mkdir()
            (workspace / 'escape-link').symlink_to(outside, target_is_directory=True)
            command = _filesystem_escape_probe(
                workspace, sibling_file, workspace / 'escape-link' / 'link.txt',
            )
            with patch.dict(os.environ, {'KYROZEN_SANDBOX_REQUIRED': '1'}):
                result = ToolAdapters(workspace).run_command({'command': command})
            self.assertEqual(result.sandbox_backend, 'bubblewrap')
            self.assertTrue((workspace / 'inside.txt').is_file())
            self.assertFalse(sibling_file.exists())
            self.assertFalse((outside / 'link.txt').exists())
            self.assertTrue(result.success, result.output)

    @unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('bwrap'),
                         'requires a usable Linux Bubblewrap installation')
    def test_linux_background_descendants_inherit_filesystem_restriction(self):
        from openkyrozen.tools.processes import SessionProcessManager

        with tempfile.TemporaryDirectory(dir=Path.home()) as root:
            root = Path(root).resolve()
            workspace = root / 'workspace'
            workspace.mkdir()
            outside = root / 'escaped.txt'
            manager = SessionProcessManager('actor', str(workspace), 'session')
            with patch.dict(os.environ, {'KYROZEN_SANDBOX_REQUIRED': '1'}):
                handle = manager.start(
                    f'echo outside > {shlex.quote(str(outside))}; echo completed',
                    cwd=str(workspace), env=os.environ.copy(), timeout_seconds=10,
                )
            try:
                handle.process.wait(timeout=5)
                self.assertEqual(handle.sandbox_backend, 'bubblewrap')
                self.assertFalse(outside.exists())
            finally:
                manager.close()


if __name__ == '__main__':
    unittest.main()
