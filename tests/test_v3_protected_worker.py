import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks import v3_matched_live as live


class ProtectedWorkerTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'darwin' or sys.platform.startswith('linux'), 'Unix sandbox required')
    def test_original_test_file_rejects_write_delete_replace_links_and_ancestor_moves(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            workspace = base / 'workspace'
            nested = workspace / 'nested'
            nested.mkdir(parents=True)
            protected = nested / 'test_guard.py'
            protected.write_text('original')
            sibling = nested / 'source.py'
            sibling.write_text('before')
            script = '''
import json, os, pathlib, sys
base, workspace, nested, protected, sibling = map(pathlib.Path, sys.argv[1:])
out = {}
def attempt(name, fn):
    try: fn(); out[name] = 'allowed'
    except OSError: out[name] = 'denied'
attempt('write', lambda: protected.write_text('changed'))
attempt('delete', lambda: protected.unlink())
attempt('replace', lambda: os.replace(sibling, protected))
attempt('hardlink', lambda: os.link(protected, nested / 'alias'))
attempt('symlink_write', lambda: ((nested / 'sym').symlink_to(protected), (nested / 'sym').write_text('changed')))
attempt('nested_move', lambda: os.rename(nested, base / 'moved-nested'))
attempt('workspace_move', lambda: os.rename(workspace, base / 'moved-workspace'))
attempt('parent_move', lambda: os.rename(base, base.with_name(base.name + '-moved')))
attempt('sibling_write', lambda: sibling.write_text('after'))
print(json.dumps(out))
'''
            command = [sys.executable, '-c', script, str(base), str(workspace), str(nested), str(protected), str(sibling)]
            wrapped, env = live._protect_worker_command(command, cwd=workspace, env=dict(os.environ),
                                                        protected_files=[protected])
            result = subprocess.run(wrapped, cwd=workspace, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            values = json.loads(result.stdout)
            self.assertEqual(values['sibling_write'], 'allowed')
            for name in ('write', 'delete', 'replace', 'hardlink', 'symlink_write', 'nested_move', 'workspace_move', 'parent_move'):
                self.assertEqual(values[name], 'denied', name)
            self.assertEqual(protected.read_text(), 'original')
            self.assertEqual(sibling.read_text(), 'after')

    @unittest.skipUnless(sys.platform == 'darwin' or sys.platform.startswith('linux'), 'Unix sandbox required')
    def test_worker_launch_protects_test_file_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / 'workspace'
            root.mkdir()
            protected = root / 'test_guard.py'
            protected.write_text('original')
            command = [sys.executable, '-c', 'from pathlib import Path; Path("test_guard.py").write_text("changed")']
            code, error = live._run_worker(command, cwd=root, env=dict(os.environ), protected_files=[protected])
            self.assertNotEqual(code, 0)
            self.assertEqual(protected.read_text(), 'original')

    def test_symlink_and_outside_test_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            workspace = base / 'workspace'
            workspace.mkdir()
            outside = base / 'outside.py'
            outside.write_text('outside')
            with self.assertRaisesRegex(ValueError, 'protection'):
                live._protect_worker_command(['true'], cwd=workspace, env={}, protected_files=[outside])
            link = workspace / 'test_link.py'
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest('symlinks unavailable')
            with self.assertRaisesRegex(ValueError, 'protection'):
                live._protect_worker_command(['true'], cwd=workspace, env={}, protected_files=[link])

    def test_preflight_rejects_a_backend_that_does_not_enforce_protection(self):
        def unprotected(command, *, cwd, env, protected_files):
            return command, env
        with patch.object(live, '_protect_worker_command', side_effect=unprotected):
            with self.assertRaisesRegex(ValueError, 'protection'):
                live._verify_worker_protection()

    def test_missing_backend_rejects_protection(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(live.shutil, 'which', return_value=None):
            root = Path(directory).resolve()
            protected = root / 'test_guard.py'
            protected.write_text('original')
            with self.assertRaisesRegex(ValueError, 'protection'):
                live._protect_worker_command(['true'], cwd=root, env={}, protected_files=[protected])


if __name__ == '__main__':
    unittest.main()
