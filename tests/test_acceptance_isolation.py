import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class AcceptanceIsolationTests(unittest.TestCase):
    def test_agent_workflow_does_not_open_the_configured_user_database(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'user-state.sqlite3'
            original = b'User data must never be opened by deterministic acceptance.'
            database.write_bytes(original)
            env = {**os.environ, 'KYROZEN_DB_PATH': str(database),
                   'KYROZEN_DISABLE_VECTOR_INDEX': '1'}
            result = subprocess.run(
                [sys.executable, str(root / 'scripts' / 'agent_workflow_acceptance.py')],
                cwd=directory, env=env, capture_output=True, text=True, timeout=45,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('Agent workflow acceptance passed.', result.stdout)
            self.assertEqual(database.read_bytes(), original)

