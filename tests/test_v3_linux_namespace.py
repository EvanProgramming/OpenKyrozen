"""Pure rewrite checks and root-only, disposable Linux namespace acceptance."""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


def namespace_module(case):
    case.assertIsNotNone(importlib.util.find_spec('benchmarks.v3_linux_namespace'),
                         'private privileged namespace backend is missing')
    return importlib.import_module('benchmarks.v3_linux_namespace')


class RewriteTests(unittest.TestCase):
    def test_environment_removes_privileged_loader_hooks_and_retains_worker_configuration(self):
        module = namespace_module(self)
        namespace = module.Namespace(1073741900, 1073741901)
        environment = {'PATH': '/usr/bin:/bin', 'HOME': '/private/actor', 'KYROZEN_DB_PATH': '/private/db',
                       'LD_PRELOAD': '/workspace/plugin.so', 'LD_AUDIT': '/workspace/audit.so',
                       'DYLD_INSERT_LIBRARIES': '/workspace/plugin.dylib', 'GLIBC_TUNABLES': 'unsafe',
                       'GCONV_PATH': '/workspace', 'LOCPATH': '/workspace',
                       'PYTHONPATH': '/workspace', 'PYTHONHOME': '/workspace', 'PYTHONSTARTUP': '/workspace/start.py'}
        self.assertEqual(namespace.environment(environment),
                         {key: environment[key] for key in ('PATH', 'HOME', 'KYROZEN_DB_PATH')})
        self.assertIn('LD_PRELOAD', environment)

    def test_ready_handshake_handles_fragmented_stream_without_trusting_eof(self):
        module = namespace_module(self)
        connection = mock.Mock()
        connection.recv.side_effect = [b're', b'a', b'dy\n']
        module._read_ready(connection)
        connection.recv.side_effect = [b're', b'']
        with self.assertRaises(PermissionError):
            module._read_ready(connection)

    def test_rewrite_retains_filesystem_and_pid_guards_before_trusted_dropper(self):
        module = namespace_module(self)
        command = ['/usr/bin/bwrap', '--unshare-user', '--disable-userns', '--unshare-pid',
                   '--die-with-parent', '--new-session', '--ro-bind', '/', '/',
                   '--bind', '/fixture', '/fixture', '--ro-bind', '/fixture/test.py', '/fixture/test.py',
                   '--', '/usr/bin/python3', '-c', 'print("model")']
        result = module._rewrite(command, uid=1073741900, gid=1073741901, descriptors=(5, 6),
                                 python='/usr/bin/python3', ready='/run/private/ready')
        separator = result.index('--')
        options, execution = result[:separator], result[separator + 1:]
        self.assertNotIn('--unshare-user', options)
        self.assertNotIn('--disable-userns', options)
        self.assertIn('--assert-userns-disabled', options)
        self.assertIn('--unshare-pid', options)
        self.assertIn('--die-with-parent', options)
        self.assertEqual(options[options.index('--userns') + 1], '6')
        self.assertNotIn('--userns2', options)
        self.assertEqual(options[options.index('--uid') + 1], '1073741900')
        self.assertEqual(options[options.index('--gid') + 1], '1073741901')
        self.assertNotIn('--cap-add', options)
        self.assertEqual(options[options.index('--cap-drop') + 1], 'ALL')
        self.assertEqual(execution[:4], ['/usr/bin/python3', '-I', '-S', '-c'])
        self.assertEqual(execution[-3:], ['/usr/bin/python3', '-c', 'print("model")'])
        self.assertEqual(options[options.index('--ro-bind'):], command[6:command.index('--')])

    def test_rewrite_rejects_missing_guard_and_hidden_or_conflicting_namespace_options(self):
        module = namespace_module(self)
        headers = [[], ['--unshare-user'], ['--disable-userns'],
                   ['--unshare-user', '--disable-userns', '--args', '7'],
                   ['--unshare-user', '--disable-userns', '--userns', '7'],
                   ['--unshare-user', '--disable-userns', '--cap-add', 'ALL'],
                   ['--unshare-user', '--disable-userns', '--uid', '0']]
        for header in headers:
            with self.subTest(header=header), self.assertRaises((ValueError, PermissionError)):
                module._rewrite(['/usr/bin/bwrap', *header, '--', '/usr/bin/true'],
                    uid=1073741900, gid=1073741901, descriptors=(5, 6),
                    python='/usr/bin/python3', ready='/run/private/ready')

    def test_unprivileged_or_nonlinux_context_fails_before_creating_resources(self):
        module = namespace_module(self)
        if sys.platform.startswith('linux') and os.geteuid() == 0:
            self.skipTest('privileged acceptance is covered separately')
        with self.assertRaises(PermissionError):
            with module.Namespace(1073741900, 1073741901):
                self.fail('privileged namespace started without its required OS authority')


@unittest.skipUnless(sys.platform.startswith('linux') and os.geteuid() == 0,
                     'disposable Linux root namespace acceptance required')
class KernelNamespaceTests(unittest.TestCase):
    def setUp(self):
        from benchmarks.v3_process_owner import UIDOwner
        self.owner, self.group = UIDOwner(), UIDOwner()
        self.namespace = None

    def tearDown(self):
        if self.namespace:
            self.namespace.close()
        self.owner.close()
        self.group.close()

    def test_dropper_accepts_an_already_empty_bounding_set_without_privileged_caps(self):
        module = namespace_module(self)
        # Exercise the real pre-handshake drop stage after a launcher already
        # removed every capability, including permission to drop them again.
        dropper = module._BOOTSTRAP.split('resource.setrlimit', 1)[0]
        code = dropper + '\nprint(Path("/proc/self/status").read_text())'
        result = subprocess.run(['/usr/bin/setpriv', '--bounding-set=-all',
            '--reuid', str(self.owner.uid), '--regid', str(self.group.uid), '--clear-groups',
            '--no-new-privs', sys.executable, '-I', '-S', '-c', code],
            cwd='/', env={'PATH': '/usr/bin:/bin'}, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        status = dict(line.split(':', 1) for line in result.stdout.decode().splitlines() if ':' in line)
        for name in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'):
            self.assertEqual(int(status[name].strip(), 16), 0)
        self.assertEqual(status['NoNewPrivs'].strip(), '1')
        denied = subprocess.run(['/usr/bin/setpriv', '--bounding-set=-all,+setpcap',
            '--reuid', str(self.owner.uid), '--regid', str(self.group.uid), '--clear-groups',
            '--no-new-privs', sys.executable, '-I', '-S', '-c', code],
            cwd='/', env={'PATH': '/usr/bin:/bin'}, capture_output=True, timeout=5)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn(b'bounding capability drop failed: 8', denied.stderr)
        self.assertIn(b'CapEff:', denied.stderr)
        self.assertIn(b'CapBnd:', denied.stderr)

    def test_actual_host_identity_caps_namespace_denial_and_original_file_protection(self):
        module = namespace_module(self)
        before = Path('/proc/sys/user/max_user_namespaces').read_text()
        with tempfile.TemporaryDirectory(dir='/var/tmp', prefix='kyrozen-ns-test-') as directory:
            root = Path(directory)
            root.chmod(0o755)
            workspace = root / 'workspace'
            workspace.mkdir()
            os.chown(workspace, self.owner.uid, self.group.uid)
            workspace.chmod(0o770)
            guard, source = workspace / 'test_guard.py', workspace / 'source.py'
            guard.write_text('original')
            source.write_text('before')
            os.chown(source, self.owner.uid, self.group.uid)
            readonly = root / 'scoped-read'
            readonly.mkdir()
            code = r'''import json,os,sys; from pathlib import Path
out={'uids':os.getresuid(),'gids':os.getresgid(),'groups':os.getgroups()}
for key,operation in [('userns',lambda:os.unshare(0x10000000)),('root',lambda:os.setuid(0)),('guard',lambda:Path(sys.argv[1]).write_text('bad'))]:
 try: operation(); out[key]='allowed'
 except OSError as exc: out[key]=exc.errno
Path(sys.argv[2]).write_text('after')
out['scoped_read']=Path(sys.argv[3]).read_text()
out['fds']={}
for fd in os.listdir('/proc/self/fd'):
 try: out['fds'][fd]=os.readlink('/proc/self/fd/'+fd)
 except FileNotFoundError: pass
out['status']={line.split(':',1)[0]:line.split(':',1)[1].strip() for line in Path('/proc/self/status').read_text().splitlines() if line.startswith(('Cap','NoNewPrivs:'))}
print(json.dumps(out))
'''
            with module.Namespace(self.owner.uid, self.group.uid) as namespace:
                self.namespace = namespace
                descriptors = namespace.pass_fds
                command = ['/usr/bin/bwrap', '--unshare-user', '--disable-userns', '--unshare-pid',
                           '--die-with-parent', '--new-session', '--ro-bind', '/', '/',
                           '--bind', str(workspace), str(workspace), '--ro-bind', str(guard), str(guard),
                           '--tmpfs', str(readonly), '--ro-bind', str(guard), str(readonly / guard.name),
                           '--remount-ro', str(readonly),
                           '--proc', '/proc', '--dev-bind', '/dev', '/dev', '--',
                           '/usr/bin/python3', '-I', '-c', code, str(guard), str(source), str(readonly / guard.name)]
                process = subprocess.Popen(namespace.rewrite(command), pass_fds=namespace.pass_fds,
                    cwd='/', env={'PATH': '/usr/bin:/bin'}, extra_groups=[],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    namespace.attach(process)
                except RuntimeError as exc:
                    process.wait(timeout=5)
                    self.fail(f'{exc}: {process.stderr.read(4096).decode(errors="replace")}')
                output, error = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, error)
                result = json.loads(output)
                self.assertEqual(result['uids'], [self.owner.uid] * 3)
                self.assertEqual(result['gids'], [self.group.uid] * 3)
                self.assertEqual(result['groups'], [])
                self.assertEqual(result['userns'], 28)
                self.assertNotEqual(result['root'], 'allowed')
                self.assertEqual(result['guard'], 30)
                for key in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'):
                    self.assertEqual(int(result['status'][key], 16), 0)
                self.assertEqual(result['status']['NoNewPrivs'], '1')
                self.assertEqual(result['scoped_read'], 'original')
                self.assertEqual(set(result['fds']), {'0', '1', '2'})
                self.assertEqual(source.read_text(), 'after')
                self.assertEqual(guard.read_text(), 'original')
            for descriptor in descriptors:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)
        self.assertEqual(Path('/proc/sys/user/max_user_namespaces').read_text(), before)

    def test_attach_timeout_and_dead_keeper_cleanup_close_namespace_descriptors(self):
        module = namespace_module(self)
        with module.Namespace(self.owner.uid, self.group.uid) as namespace:
            self.namespace = namespace
            descriptors = namespace.pass_fds
            process = subprocess.Popen(['/usr/bin/sleep', '30'], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            with self.assertRaisesRegex((TimeoutError, RuntimeError), 'ready|startup'):
                namespace.attach(process, timeout=.05)
            namespace.close()
            self.assertIsNotNone(process.poll())
            process.communicate(timeout=1)
            for descriptor in descriptors:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_failed_startup_before_descriptor_transfer_reaps_keeper_and_closes_channels(self):
        module = namespace_module(self)
        namespace = module.Namespace(self.owner.uid, self.group.uid)
        self.namespace = namespace
        before = set(Path('/proc/self/fd').iterdir())
        with mock.patch.object(module, '_receive', side_effect=TimeoutError('injected startup deadline')):
            with self.assertRaisesRegex(TimeoutError, 'startup deadline'):
                namespace.__enter__()
        self.assertIsNotNone(namespace._keeper.poll())
        self.assertEqual(namespace._fds, [])
        self.assertIsNone(namespace._control)
        self.assertEqual(set(Path('/proc/self/fd').iterdir()), before)

    def test_dead_keeper_reaps_nested_helper_and_closes_namespace_descriptors(self):
        module = namespace_module(self)
        with module.Namespace(self.owner.uid, self.group.uid) as namespace:
            self.namespace = namespace
            descriptors = namespace.pass_fds
            ready = select.poll()
            ready.register(namespace._locked_pidfd, select.POLLIN)
            namespace._keeper.kill()
            namespace._keeper.wait(timeout=5)
            self.assertTrue(ready.poll(1000), 'nested keeper survived its parent-death guard')
            namespace.close()
            for descriptor in descriptors:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_uid_owner_preserves_standard_streams_and_exit_status_without_loader_hooks(self):
        command = ['/usr/bin/bwrap', '--unshare-user', '--disable-userns', '--unshare-pid',
                   '--die-with-parent', '--new-session', '--ro-bind', '/', '/',
                   '--proc', '/proc', '--dev-bind', '/dev', '/dev', '--', '/usr/bin/python3', '-I', '-c',
                   'import sys;print(sys.stdin.read());print("expected",file=sys.stderr);sys.exit(23)']
        process = self.owner.popen(command, cwd='/', env={'PATH': '/usr/bin:/bin', 'LD_PRELOAD': '/absent.so'},
                                   group=self.group.uid, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.namespace = self.owner.namespace
        output, error = process.communicate(b'hello world', timeout=5)
        self.assertEqual(output, b'hello world\n')
        self.assertEqual(error, b'expected\n')
        self.assertEqual(process.returncode, 23)
        self.owner.close()
        self.assertEqual(process.returncode, 23)
