import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openkyrozen.app.bootstrap import build_application


class UpdateTransactionTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.home = Path(self.root.name)
        self.runtime = build_application(surface='cli').runtime
        self.revision = 'a' * 40
        self.events = []
        self.commands = []
        self.staged = self.home / 'new-tui'
        self.staged.write_bytes(b'new binary')

    def run_update(self, *, stage_error=None, install_error=None, verified=True, optional_error=False, activation_error=None, optional_callback=None, browser_error=None):
        def stage(*args):
            self.events.append('stage')
            if stage_error:
                raise stage_error
            return self.staged
        def run(command, **kwargs):
            self.events.append('install')
            self.commands.append(command)
            if install_error:
                raise install_error
            return subprocess.CompletedProcess(command, 0, stdout='installed', stderr='')
        def verify(*args):
            self.events.append('verify')
            return verified
        def browser(*args, **kwargs):
            self.events.append('browser')
            if browser_error:
                raise browser_error
        def activate(*args):
            self.events.append('activate')
            if activation_error:
                raise activation_error
            return 'TUI activated'
        with patch('openkyrozen.updates.service.Path.home', return_value=self.home), \
             patch('openkyrozen.updates.service.shutil.which', return_value='/bin/uv'), \
             patch.object(self.runtime, '_resolve_update_revision', return_value=self.revision), \
             patch.object(self.runtime, '_update_tui_binary', return_value=(True, 'fixture TUI')), \
             patch.object(self.runtime, '_stage_update_tui', stage, create=True), \
             patch.object(self.runtime, '_verify_update_package', verify, create=True), \
             patch.object(self.runtime, '_activate_update_tui', activate, create=True), \
             patch.object(self.runtime, '_prepare_update_browser', browser, create=True), \
             patch('openkyrozen.updates.service.subprocess.run', side_effect=run), \
             patch('openkyrozen.tools.github_cli.GitHubCLI.install_managed', side_effect=optional_callback or (OSError('optional setup failed') if optional_error else None), return_value={'message': 'ready'}):
            return self.runtime._self_update()

    def test_tui_failure_does_not_replace_python_or_request_restart(self):
        result = self.run_update(stage_error=RuntimeError('build failed'))
        self.assertEqual(self.events, ['stage'], 'Python must stay unchanged when TUI preparation fails')
        self.assertEqual(result.status, 'failed')
        self.assertFalse(result.restart_ready)
        self.assertEqual(self.events, ['stage'])

    def test_success_verifies_python_before_activating_matching_tui(self):
        result = self.run_update()
        self.assertEqual(result.status, 'success')
        self.assertTrue(result.restart_ready)
        self.assertEqual(result.revision, self.revision)
        self.assertEqual(self.events, ['stage', 'install', 'verify', 'browser', 'activate'])

    def test_install_timeout_reports_uncertainty_and_keeps_tui(self):
        result = self.run_update(install_error=subprocess.TimeoutExpired('uv', 300))
        self.assertEqual(result.status, 'partial')
        self.assertFalse(result.restart_ready)
        self.assertEqual(result.components['python'], 'unknown')
        self.assertNotIn('existing installation was kept', str(result))
        self.assertEqual(self.events, ['stage', 'install'])

    def test_wrong_installed_revision_does_not_activate_tui(self):
        result = self.run_update(verified=False)
        self.assertEqual(result.status, 'partial')
        self.assertFalse(result.restart_ready)
        self.assertEqual(self.events, ['stage', 'install', 'verify'])

    def test_optional_setup_failure_does_not_undo_verified_core_update(self):
        result = self.run_update(optional_error=True)
        self.assertEqual(result.status, 'success')
        self.assertTrue(result.restart_ready)
        self.assertEqual(result.components['github_cli'], 'failed')

    def test_activation_failure_reports_partial_update(self):
        result = self.run_update(activation_error=OSError('locked executable'))
        self.assertEqual(result.status, 'partial')
        self.assertFalse(result.restart_ready)
        self.assertEqual(result.components['python'], 'verified')

    def test_windows_activation_stages_binary_without_overwriting_running_file(self):
        self.runtime._IS_WINDOWS = True
        target = self.home / '.kyrozen/bin/openkyrozen-tui.exe'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'old binary')
        with patch('openkyrozen.updates.service.Path.home', return_value=self.home):
            self.runtime._activate_update_tui(self.staged, self.revision)
        self.assertEqual(target.read_bytes(), b'old binary')
        self.assertEqual(target.with_name(target.name + '.next').read_bytes(), b'new binary')

    def test_interrupted_install_reports_partial_state(self):
        result = self.run_update(install_error=KeyboardInterrupt())
        self.assertEqual(result.status, 'partial')
        self.assertFalse(result.restart_ready)
        self.assertEqual(self.events, ['stage', 'install'])

    def test_missing_uv_returns_failed_without_changing_components(self):
        with patch('openkyrozen.updates.service.shutil.which', return_value=None):
            result = self.runtime._self_update()
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.components['python'], 'unchanged')
        self.assertFalse(result.restart_ready)

    def test_no_verified_source_does_not_install(self):
        with patch('openkyrozen.updates.service.shutil.which', return_value='/bin/uv'), \
             patch.object(self.runtime, '_resolve_update_revision', return_value=None), \
             patch.object(self.runtime, '_release_tui_asset_available', return_value=False):
            result = self.runtime._self_update()
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.components['python'], 'unchanged')

    def test_concurrent_update_is_rejected_before_installation(self):
        with patch('openkyrozen.updates.service.Path.home', return_value=self.home):
            with self.runtime._update_lock():
                result = self.run_update()
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.components['python'], 'unchanged')
        self.assertEqual(self.events, [])

    @unittest.skipIf(os.name == "nt", "POSIX executable fixture; native Windows update is checked separately")
    def test_release_fallback_accepts_legacy_tui_only_after_checksum_and_version(self):
        output = self.home / 'openkyrozen-tui'
        original_run = subprocess.run
        def run(command, **kwargs):
            if command[1] == 'build':
                output.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo "OpenKyrozen 2.0.4"; else exit 2; fi\n')
                output.chmod(0o700)
                return subprocess.CompletedProcess(command, 0, '', '')
            return original_run(command, **kwargs)
        with patch.object(self.runtime, '_ensure_update_go', return_value='go'), \
             patch.object(self.runtime, '_prepare_tui_source', return_value=self.home), \
             patch('openkyrozen.updates.service.subprocess.run', side_effect=run):
            prepared = self.runtime._stage_update_tui(self.home, 'fixture', 'verified-checksum', None)
        self.assertEqual(prepared, output)

    def test_package_probe_rejects_a_different_revision(self):
        import json
        data = {'version': '2.0.4', 'source': {'vcs_info': {'commit_id': 'b' * 40}}}
        results = [subprocess.CompletedProcess([], 0, str(self.home), ''),
                   subprocess.CompletedProcess([], 0, json.dumps(data), '')]
        with patch('openkyrozen.updates.service.subprocess.run', side_effect=results):
            self.assertFalse(self.runtime._verify_update_package('/bin/uv', self.revision))

    def test_update_preserves_optional_provider_sdks(self):
        result = self.run_update()
        command = self.commands[0]
        for package in ('anthropic', 'google-genai', 'perplexityai', 'boto3', 'azure-identity', 'playwright'):
            self.assertIn(package, command)
        self.assertTrue(result.restart_ready)

    def test_optional_setup_remains_inside_update_serialization(self):
        observed=[]
        def setup():
            try:
                with self.runtime._update_lock():
                    observed.append('unlocked')
            except (BlockingIOError, OSError):
                observed.append('locked')
            return {'success':True,'message':'ready'}
        result=self.run_update(optional_callback=setup)
        self.assertEqual(observed,['locked'])
        self.assertTrue(result.restart_ready)

    def test_package_probe_uses_isolated_imports(self):
        commands=[]
        import json
        def run(command,**kwargs):
            commands.append(command)
            data={'version':'2.0.4','source':{'vcs_info':{'commit_id':self.revision}},
                  'paths':[str(self.home/'openkyrozen/lib/site-packages/openkyrozen/__init__.py')]}
            return subprocess.CompletedProcess(command,0,str(self.home) if command[1:]==['tool','dir'] else json.dumps(data),'')
        with patch('openkyrozen.updates.service.subprocess.run',side_effect=run):
            self.assertTrue(self.runtime._verify_update_package('/bin/uv',self.revision))
        self.assertIn('-I',commands[-1])

    def test_checkout_imports_cannot_verify_installed_metadata(self):
        import json
        data={'version':'2.0.4','source':{'vcs_info':{'commit_id':self.revision}},
              'paths':['/unrelated/checkout/openkyrozen/__init__.py']}
        results=[subprocess.CompletedProcess([],0,str(self.home),''),
                 subprocess.CompletedProcess([],0,json.dumps(data),'')]
        with patch('openkyrozen.updates.service.subprocess.run',side_effect=results):
            self.assertFalse(self.runtime._verify_update_package('/bin/uv',self.revision))

    def test_interruption_leaves_durable_restart_block(self):
        import json
        self.run_update(install_error=KeyboardInterrupt())
        state=json.loads((self.home/'.kyrozen/update-state.json').read_text())
        self.assertEqual(state['status'],'installing')
        self.assertEqual(state['revision'],self.revision)

    def test_release_probe_accepts_legacy_installed_entrypoints(self):
        import json
        import sys
        from openkyrozen.updates.models import INSTALL_PROBE
        for name in ('main','server','tui_backend','tui_launcher'):
            (self.home/(name+'.py')).write_text('# installed legacy entrypoint\n')
        metadata=self.home/'openkyrozen-2.0.4.dist-info'
        metadata.mkdir()
        (metadata/'METADATA').write_text('Name: openkyrozen\nVersion: 2.0.4\n')
        (metadata/'direct_url.json').write_text(json.dumps({'url':self.runtime.RELEASE_WHEEL_URL}))
        setup="import sys,builtins;sys.path.insert(0,"+repr(str(self.home))+");original=builtins.__import__\n"
        setup+="def legacy_import(name,*args,**kwargs):\n if name=='openkyrozen':raise ModuleNotFoundError(name='openkyrozen')\n return original(name,*args,**kwargs)\nbuiltins.__import__=legacy_import\n"
        result=subprocess.run([sys.executable,'-I','-c',setup+INSTALL_PROBE],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        data=json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(len(data['paths']),4)
        self.assertTrue(all(Path(path).is_relative_to(self.home) for path in data['paths']))

    def test_browser_preparation_failure_blocks_activation_and_restart(self):
        result = self.run_update(browser_error=RuntimeError('Chromium download failed'))
        self.assertEqual(result.status, 'partial')
        self.assertFalse(result.restart_ready)
        self.assertEqual(result.components['python'], 'verified')
        self.assertEqual(result.components['browser'], 'failed')
        self.assertNotIn('activate', self.events)
        self.assertIn('Chromium download failed', str(result))

    def test_browser_uses_the_new_installed_interpreter(self):
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, '', '')
        with patch('openkyrozen.updates.service.subprocess.run', side_effect=run):
            self.runtime._prepare_update_browser('/bin/uv', root=self.home)
        python = str(self.home / ('Scripts/python.exe' if self.runtime._IS_WINDOWS else 'bin/python'))
        self.assertEqual(commands[0], [python, '-I', '-m', 'playwright', 'install', 'chromium'])
        self.assertEqual(commands[1][:3], [python, '-I', '-c'])
        self.assertIn('launch(', commands[1][3])
