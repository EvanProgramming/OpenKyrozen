import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from openkyrozen.updates.windows import install_windows_package, activate_windows_package


class WindowsUpdateTests(unittest.TestCase):
    def test_install_keeps_generation_at_its_original_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = ['uv', 'tool', 'install', '--force', 'candidate']
            with patch('openkyrozen.updates.windows.subprocess.run', return_value=subprocess.CompletedProcess(command, 0, '', '')) as run:
                stage = install_windows_package('uv', command, root)
            self.assertEqual(run.call_args.kwargs['env']['UV_TOOL_DIR'], str(stage.root.parent))
            self.assertEqual(run.call_args.kwargs['env']['UV_TOOL_BIN_DIR'], str(stage.bin_dir))
            self.assertNotEqual(stage.root, root / 'tools' / 'openkyrozen')
            self.assertTrue(stage.root.parent.is_dir())

    def test_activation_probes_recovery_cli_and_keeps_old_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / 'source', root / 'bin'
            source.mkdir(); target.mkdir()
            (source / 'kyrozen.exe').write_bytes(b'new')
            (target / 'kyrozen.exe').write_bytes(b'old')
            tui = root / 'tui.next'; tui.write_bytes(b'new tui')
            destination = root / 'tui.exe'; destination.write_bytes(b'old tui')
            with patch('openkyrozen.updates.windows.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
                activate_windows_package(source, target, tui, destination)
            self.assertEqual((target / 'kyrozen.exe').read_bytes(), b'new')
            self.assertEqual(run.call_args.args[0], [str(target / 'kyrozen.exe'), '--help'])
            self.assertEqual(destination.read_bytes(), b'new tui')

    def test_failed_recovery_cli_rolls_back_all_components(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / 'source', root / 'bin'
            source.mkdir(); target.mkdir()
            (source / 'kyrozen.exe').write_bytes(b'new')
            (target / 'kyrozen.exe').write_bytes(b'old')
            tui = root / 'tui.next'; tui.write_bytes(b'new tui')
            destination = root / 'tui.exe'; destination.write_bytes(b'old tui')
            with patch('openkyrozen.updates.windows.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'bad')):
                with self.assertRaises(RuntimeError):
                    activate_windows_package(source, target, tui, destination)
            self.assertEqual((target / 'kyrozen.exe').read_bytes(), b'old')
            self.assertEqual(destination.read_bytes(), b'old tui')
