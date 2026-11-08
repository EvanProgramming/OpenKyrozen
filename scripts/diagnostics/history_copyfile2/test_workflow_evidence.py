import json, os, sys, tempfile, types, unittest, shutil, subprocess
from pathlib import Path
from unittest.mock import patch
import yaml
WORKFLOW = Path(__file__).resolve().parents[3] / '.github/workflows/v3-windows-reliability.yml'
CODE = next((s['run'] for s in yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)['jobs']['history-copyfile2-probe']['steps'] if s.get('name') == 'Run bounded private forensic probe and its boundary tests'))

class EvidenceTests(unittest.TestCase):

    def exercise(self, live, cleanup_error=False):
        with tempfile.TemporaryDirectory(prefix='copyfile2-workflow-regression-') as directory:
            base = Path(directory)
            root = base / 'diagnostic-source'
            root.mkdir()
            out = base / 'history-copyfile2-evidence'
            out.mkdir()
            home = base / 'home'
            home.mkdir()
            seen = []
            calls = []

            class Home:

                def __init__(self, **kw):
                    pass

                def __enter__(self):
                    return str(home)

                def __exit__(self, *args):
                    seen.append((out / 'process-receipt.json').exists())
                    if cleanup_error:
                        error = OSError('synthetic cleanup failure')
                        error.winerror = 32
                        raise error

            def run_process(command, log, env, timeout):
                calls.append((command, timeout, dict(env)))
                log.write_text('synthetic boundary fixture\n')
                return {'returncode': 0, 'timeout_cleanup': None} if len(calls) == 1 else dict(live)
            module = types.ModuleType('scripts.v3_windows_reliability')
            module.run_process = run_process
            module.isolated_environment = lambda path: {'HOME': str(path)}
            package = types.ModuleType('scripts')
            package.__path__ = []
            cwd = Path.cwd()
            paths = list(sys.path)
            failure = None
            try:
                os.chdir(root)
                with patch.dict(sys.modules, {'scripts': package, 'scripts.v3_windows_reliability': module}), patch.object(tempfile, 'TemporaryDirectory', Home), patch.dict(os.environ, {'GITHUB_SHA': '0' * 40}, clear=True):
                    try:
                        exec(compile(CODE, str(WORKFLOW), 'exec'), {})
                    except BaseException as exc:
                        failure = exc
            finally:
                os.chdir(cwd)
                sys.path[:] = paths
            receipt = json.loads((out / 'process-receipt.json').read_text()) if (out / 'process-receipt.json').exists() else None
            return (receipt, seen, failure, calls)

    def test_uncertified_timeout_saved_before_home_cleanup_failure(self):
        live = {'returncode': 124, 'timeout_cleanup': {'descendant_cleanup_certified': False, 'sentinel': 'original'}}
        receipt, seen, failure, calls = self.exercise(live, True)
        self.assertIsNotNone(receipt)
        self.assertEqual(seen, [True])
        self.assertEqual(receipt['returncode'], 124)
        self.assertEqual(receipt['timeout_cleanup'], live['timeout_cleanup'])
        self.assertTrue(receipt['cleanup_uncertified'])
        self.assertEqual(receipt['failure_phase'], 'private_home_cleanup')
        self.assertEqual(receipt['step_error']['winerror'], 32)
        self.assertIsNotNone(failure)
        self.assertEqual([v[1] for v in calls], [45, 45])

    def test_successful_child_does_not_hide_failed_home_cleanup(self):
        receipt, seen, failure, _ = self.exercise({'returncode': 0, 'timeout_cleanup': None}, True)
        self.assertIsNotNone(receipt)
        self.assertEqual(seen, [True])
        self.assertEqual(receipt['returncode'], 0)
        self.assertTrue(receipt['cleanup_uncertified'])
        self.assertTrue(receipt['step_failed'])
        self.assertIsNotNone(failure)

    def test_uncertified_timeout_retains_original_evidence_on_normal_cleanup(self):
        receipt, seen, failure, _ = self.exercise({'returncode': 124, 'timeout_cleanup': {'descendant_cleanup_certified': False}})
        self.assertEqual(seen, [True])
        self.assertTrue(receipt['cleanup_uncertified'])
        self.assertIsInstance(failure, SystemExit)
        self.assertEqual(failure.code, 124)

    def test_normal_success_keeps_exact_result(self):
        receipt, seen, failure, _ = self.exercise({'returncode': 0, 'timeout_cleanup': None})
        self.assertEqual(seen, [True])
        self.assertFalse(receipt['cleanup_uncertified'])
        self.assertFalse(receipt['step_failed'])
        self.assertIsInstance(failure, SystemExit)
        self.assertEqual(failure.code, 0)

    def test_missing_timeout_cleanup_is_not_certified(self):
        receipt, seen, failure, _ = self.exercise({'returncode': 124})
        self.assertEqual(seen, [True])
        self.assertTrue(receipt['cleanup_uncertified'])
        self.assertTrue(receipt['step_failed'])
        self.assertEqual(failure.code, 124)

    def test_success_exit_cannot_override_explicit_uncertified_cleanup(self):
        receipt, seen, failure, _ = self.exercise({'returncode': 0, 'timeout_cleanup': {'descendant_cleanup_certified': False}})
        self.assertEqual(seen, [True])
        self.assertTrue(receipt['cleanup_uncertified'])
        self.assertTrue(receipt['step_failed'])
        self.assertEqual(receipt['returncode'], 0)
        self.assertEqual(failure.code, 1)

    def test_boundary_tests_use_same_absolute_current_source_as_native_probe(self):
        _, _, _, calls = self.exercise({'returncode': 0, 'timeout_cleanup': None})
        source = calls[0][2]['COPYFILE2_CURRENT_SOURCE']
        self.assertTrue(Path(source).is_absolute())
        self.assertEqual(Path(source).name, 'product')
        self.assertEqual(source, calls[1][0][calls[1][0].index('--current-source') + 1])

    def test_unknown_or_boolean_exit_status_cannot_pass(self):
        for code in (None, False):
            with self.subTest(code=code):
                receipt, seen, failure, _ = self.exercise({'returncode': code, 'timeout_cleanup': None})
                self.assertEqual(seen, [True])
                self.assertTrue(receipt['step_failed'])
                self.assertTrue(receipt['cleanup_uncertified'])
                self.assertEqual(failure.code, 1)

    @unittest.skipUnless(shutil.which('pwsh'), 'PowerShell executable is required for the Git exit-code regression')
    def test_failed_git_status_cannot_be_hidden_by_later_success(self):
        document = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
        identity = next((step['run'] for step in document['jobs']['history-copyfile2-probe']['steps'] if step.get('name') == 'Verify exact checked-out probe sources'))
        fake = "$ErrorActionPreference = 'Stop'\nfunction git {\n    if ($args -contains 'rev-parse') {\n        $global:LASTEXITCODE = 0\n        switch ($args[1]) {\n            'diagnostic-source' { Write-Output $env:GITHUB_SHA }\n            'product' { Write-Output '8c595f909859b0fda10ff4fdb6e25f4ccb25c96e' }\n            'historical-product' { Write-Output '0f172f9fe692ff8fafcc0a15a72fe51eaf67e977' }\n        }\n    } elseif ($args[1] -eq 'diagnostic-source') {\n        $global:LASTEXITCODE = 1\n    } else {\n        $global:LASTEXITCODE = 0\n    }\n}\n"
        with tempfile.TemporaryDirectory(prefix='git-status-receipt-regression-') as directory:
            root = Path(directory)
            script = root / 'check.ps1'
            script.write_text(fake + identity)
            result = subprocess.run([shutil.which('pwsh'), '-NoProfile', '-NonInteractive', '-File', str(script)], cwd=root, env=dict(os.environ, GITHUB_SHA='0' * 40), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
            self.assertNotEqual(result.returncode, 0, result.stdout.decode(errors='replace'))
            receipt = json.loads((root / 'history-copyfile2-evidence/source-identities.json').read_text(encoding='utf-8-sig'))
            self.assertEqual(receipt[0]['head_exitcode'], 0)
            self.assertEqual(receipt[0]['status_exitcode'], 1)
            self.assertIsNone(receipt[0]['tracked_edits'])
            self.assertEqual(receipt[-1]['status_exitcode'], 0)
if __name__ == '__main__':
    unittest.main(verbosity=2)
