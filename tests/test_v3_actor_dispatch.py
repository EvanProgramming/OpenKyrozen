"""The matched harness must use the privileged actor boundary for both arms."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class ActorDispatchTests(unittest.TestCase):
    def test_shell_adapter_accepts_the_authenticated_actor_client(self):
        from benchmarks import v3_shell_launcher as module
        shell, client = SimpleNamespace(), object()
        with patch.dict(sys.modules, {'openkyrozen.tools': SimpleNamespace(shell=shell)}), patch.object(
                module, '_install_legacy_adapter') as install, patch.dict(os.environ, {}, clear=True):
            self.assertTrue(module.install_worker_adapter(client=client))
            install.assert_called_once_with(client, shell)

    def test_both_arms_dispatch_through_the_parent_and_revoke_on_timeout(self):
        from benchmarks import v3_matched_live as module
        from benchmarks import v3_actor_supervisor as supervisor
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = SimpleNamespace(communicate=unittest.mock.Mock(side_effect=[
                subprocess.TimeoutExpired('worker', 1), (b'', b'')]))
            launch = unittest.mock.MagicMock()
            launch.__enter__.return_value = launch
            launch.popen.return_value = process
            factory, revoke = object(), object()
            with patch.object(supervisor, 'ActorSupervisor', return_value=launch) as create:
                result = module._run_worker(['worker'], cwd=root, env={}, source=root,
                    provider_config=factory, revoke_actor=revoke, timeout=1)
            self.assertEqual(result, (-1, 'timeout'))
            self.assertIs(create.call_args.kwargs['source'], root)
            self.assertIs(create.call_args.kwargs['provider_config'], factory)
            self.assertIs(create.call_args.kwargs['revoke_actor'], revoke)
            launch.close.assert_called_once()
            self.assertEqual(process.communicate.call_count, 2)
