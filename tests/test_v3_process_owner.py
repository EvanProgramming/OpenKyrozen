"""Kernel UID ownership for benchmark actors and daemonized command children."""
import importlib
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.name == 'posix', 'Unix benchmark ownership required')
class ProcessOwnerPolicyTests(unittest.TestCase):
    def owner_module(self):
        self.assertIsNotNone(importlib.util.find_spec('benchmarks.v3_process_owner'),
                             'benchmark process ownership is not implemented')
        return importlib.import_module('benchmarks.v3_process_owner')

    def test_nonprivileged_parent_cannot_claim_distinct_uid_ownership(self):
        module = self.owner_module()
        with patch.object(module.os, 'geteuid', return_value=501):
            with self.assertRaisesRegex(PermissionError, 'privileged'):
                module.UIDOwner()

    def test_allocator_rejects_an_uid_already_present_in_kernel_snapshot(self):
        module = self.owner_module()
        with patch.object(module.os, 'geteuid', return_value=0), patch.object(
                module, 'process_snapshot', return_value={41: (1_234_567_890, 'S')}):
            with self.assertRaisesRegex(ValueError, 'in use'):
                module.UIDOwner(uid=1_234_567_890)

    def test_root_and_ordinary_user_ids_are_never_allocated(self):
        module = self.owner_module()
        with patch.object(module.os, 'geteuid', return_value=0):
            for uid in (0, 501, 65534, -1, True, 2**32):
                with self.subTest(uid=uid), self.assertRaises(ValueError):
                    module.UIDOwner(uid=uid)

    def test_existing_account_without_a_running_process_cannot_be_reused(self):
        import pwd
        module = self.owner_module()
        with patch.object(module.os, 'geteuid', return_value=0), patch.object(
                module, 'process_snapshot', return_value={}), patch.object(
                module, 'reserve_identity', return_value=None), patch.object(pwd, 'getpwuid', return_value=object()):
            with self.assertRaisesRegex(ValueError, 'in use'):
                module.UIDOwner(uid=1_234_567_892)

    def test_cleanup_freezes_and_kills_only_its_owned_uid(self):
        module = self.owner_module()
        snapshots = [{1: (0, 'S'), 20: (1_234_567_890, 'S')},
                     {1: (0, 'S'), 20: (1_234_567_890, 'T'), 21: (1_234_567_890, 'S')},
                     {1: (0, 'S'), 20: (1_234_567_890, 'T'), 21: (1_234_567_890, 'T')},
                     {1: (0, 'S')}]
        with patch.object(module.os, 'geteuid', return_value=0), patch.object(
                module, 'process_snapshot', return_value={}), patch.object(
                module, 'reserve_identity', return_value=None, create=True), patch.object(module.os, 'kill') as kill:
            owner = module.UIDOwner(uid=1_234_567_890)
            with patch.object(module, 'process_snapshot', side_effect=snapshots):
                owner.close()
            self.assertEqual({call.args[0] for call in kill.call_args_list}, {20, 21})
            self.assertTrue(owner.closed)

    def test_identity_reservation_is_exclusive_and_never_follows_links(self):
        module = self.owner_module()
        self.assertTrue(hasattr(module, 'reserve_identity'), 'exclusive UID reservation is missing')
        with tempfile.TemporaryDirectory() as directory:
            marker = module.reserve_identity(1_234_567_891, directory=directory)
            with self.assertRaises(FileExistsError):
                module.reserve_identity(1_234_567_891, directory=directory)
            marker.unlink()
            target = os.path.join(directory, 'target')
            with open(target, 'w') as stream:
                stream.write('unchanged')
            marker.symlink_to(target)
            with self.assertRaises(FileExistsError):
                module.reserve_identity(1_234_567_891, directory=directory)
            with open(target) as stream:
                self.assertEqual(stream.read(), 'unchanged')

    def test_fresh_unix_connection_authenticates_real_sender_pid_and_uid(self):
        module = self.owner_module()
        self.assertTrue(hasattr(module, 'peer_identity'), 'kernel peer authentication is missing')
        with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX) as server:
            address = str(Path(directory) / 'socket')
            server.bind(address)
            server.listen(1)
            server.settimeout(5)
            child = subprocess.Popen([sys.executable, '-I', '-c',
                'import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); '
                's.sendall(b"hello"); s.recv(1)', address])
            try:
                connection, _ = server.accept()
                with connection:
                    connection.settimeout(5)
                    self.assertEqual(connection.recv(5), b'hello')
                    self.assertEqual(module.peer_identity(connection), (child.pid, os.getuid(), os.getgid()))
                    connection.sendall(b'x')
                self.assertEqual(child.wait(timeout=5), 0)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'requires unprivileged fake server')
    def test_actor_client_rejects_unprivileged_server_before_trusting_claims(self):
        module = self.owner_module()
        self.assertTrue(hasattr(module, 'connect_actor_client'), 'actor connection boundary is missing')
        with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX) as server:
            address = str(Path(directory) / 'socket')
            server.bind(address)
            server.listen(1)
            with patch.dict(os.environ, {'KYROZEN_BENCHMARK_ACTOR_SOCKET': address}):
                with self.assertRaisesRegex(PermissionError, 'privileged'):
                    module.connect_actor_client()

    def test_namespaced_parent_requires_kernel_map_and_pinned_socket(self):
        module = self.owner_module()
        self.assertTrue(hasattr(module, '_namespace_parent_pin'))
        identity = 1_234_567_893
        connection = SimpleNamespace(getsockopt=lambda *args: __import__('struct').pack('iII', 0, 65534, 65534))
        socket_metadata = SimpleNamespace(st_dev=7, st_ino=23, st_uid=65534, st_mode=0o140666)
        directory_metadata = SimpleNamespace(st_uid=65534, st_mode=0o40755)
        def metadata(path):
            return socket_metadata if str(path).endswith('/socket') else directory_metadata
        with patch.object(module.socket, 'SO_PEERCRED', 17, create=True), patch.object(module.sys, 'platform', 'linux'), patch.object(module.os, 'geteuid', return_value=identity), patch.object(
                module.os, 'getegid', return_value=identity), patch.object(module.Path, 'read_text',
                return_value=f'{identity} {identity} 1\n'), patch.object(module.Path, 'lstat', metadata):
            module._namespace_parent_pin(connection, '/var/tmp/parent/socket', '[7,23]')
            for pin in ('[7,24]', '[true,23]', 'null', ''):
                with self.subTest(pin=pin), self.assertRaises(PermissionError):
                    module._namespace_parent_pin(connection, '/var/tmp/parent/socket', pin)
            with patch.object(module.Path, 'read_text', return_value='0 0 4294967295\n'), self.assertRaises(PermissionError):
                module._namespace_parent_pin(connection, '/var/tmp/parent/socket', '[7,23]')
            directory_metadata.st_mode = 0o40777
            with self.assertRaises(PermissionError):
                module._namespace_parent_pin(connection, '/var/tmp/parent/socket', '[7,23]')

    def test_linux_bwrap_waits_for_unprivileged_readiness_and_stops_setup_before_uid_sweep(self):
        from unittest.mock import MagicMock
        module = self.owner_module()
        namespace = MagicMock()
        namespace.rewrite.return_value = ['trusted-bwrap']
        namespace.pass_fds = (31, 32)
        leaf = SimpleNamespace(Namespace=MagicMock(return_value=namespace))
        with patch.object(module.os, 'geteuid', return_value=0), patch.object(
                module, 'process_snapshot', return_value={}), patch.object(
                module, 'reserve_identity', return_value=None), patch.object(
                module, '_registered_identity', return_value=False), patch.object(
                module.sys, 'platform', 'linux'), patch.dict(sys.modules, {'benchmarks.v3_linux_namespace': leaf}), patch.object(
                module.subprocess, 'Popen') as popen:
            owner = module.UIDOwner(uid=1_234_567_894)
            process = owner.popen(['/usr/bin/bwrap', '--', 'worker'], cwd='/var/tmp', env={}, group=1_234_567_895)
            leaf.Namespace.assert_called_once_with(owner.uid, 1_234_567_895)
            namespace.attach.assert_called_once_with(process)
            self.assertEqual(popen.call_args.args[0], ['trusted-bwrap'])
            self.assertEqual(popen.call_args.kwargs['pass_fds'], (31, 32))
            self.assertNotIn('user', popen.call_args.kwargs)
            self.assertNotIn('group', popen.call_args.kwargs)
            def drained():
                namespace.close.assert_called_once()
                return {}
            with patch.object(module, 'process_snapshot', side_effect=drained):
                owner.close()
            self.assertTrue(owner.closed)

    def test_cleanup_cannot_certify_empty_while_a_native_spawn_is_still_starting(self):
        import threading
        from unittest.mock import MagicMock
        module = self.owner_module()
        entered, release, closing, finished = (threading.Event() for _ in range(4))
        process, errors = MagicMock(), []
        def spawn(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test spawn was not released')
            return process
        with patch.object(module.os, 'geteuid', return_value=0), patch.object(
                module, 'process_snapshot', return_value={}), patch.object(
                module, 'reserve_identity', return_value=None), patch.object(
                module, '_registered_identity', return_value=False), patch.object(
                module.subprocess, 'Popen', side_effect=spawn):
            owner = module.UIDOwner(uid=1_234_567_896)
            def launch():
                try:
                    owner.popen(['/usr/bin/true'], cwd='/var/tmp', env={})
                except Exception as exc:
                    errors.append(exc)
            def close():
                closing.set()
                owner.close()
                finished.set()
            launcher, closer = threading.Thread(target=launch), threading.Thread(target=close)
            launcher.start()
            try:
                self.assertTrue(entered.wait(2))
                closer.start()
                self.assertTrue(closing.wait(2))
                self.assertFalse(finished.wait(.1), 'cleanup certified before a pending spawn settled')
            finally:
                release.set()
                launcher.join(3)
                if closer.ident is not None:
                    closer.join(3)
                owner.close()
            self.assertFalse(errors)
            self.assertTrue(owner.closed)
            process.wait.assert_called()


@unittest.skipUnless(hasattr(os, 'geteuid') and os.geteuid() == 0,
                     'privileged disposable benchmark runner required')
class PrivilegedProcessOwnershipTests(unittest.TestCase):
    def test_setid_executable_cannot_escape_the_owned_kernel_identity(self):
        import shutil
        from benchmarks.v3_process_owner import UIDOwner
        with tempfile.TemporaryDirectory(dir='/var/tmp') as directory:
            root = Path(directory)
            root.chmod(0o755)
            executable = root / 'id'
            shutil.copyfile('/usr/bin/id', executable)
            executable.chmod(0o4755)
            owner = UIDOwner()
            try:
                process = owner.popen([str(executable), '-u'], cwd='/var/tmp',
                    env={'PATH': os.environ['PATH']}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                output, error = process.communicate(timeout=5)
                self.assertTrue(process.returncode != 0 or output.strip() == str(owner.uid).encode(),
                                'setuid executable escaped the owned UID: ' + repr((output, error)))
            finally:
                owner.close()

    def test_spawn_drops_identity_and_supplementary_groups_before_execution(self):
        from benchmarks.v3_process_owner import UIDOwner
        self.assertTrue(hasattr(UIDOwner, 'popen'), 'UID execution boundary is missing')
        owner = UIDOwner()
        try:
            process = owner.popen([sys.executable, '-I', '-c',
                'import ctypes,json,os,sys\n'
                'if sys.platform == "darwin":\n'
                ' libc=ctypes.CDLL(None,use_errno=True)\n'
                ' count=libc.getgroups(0,None)\n'
                ' if count<0: raise OSError(ctypes.get_errno(),"kernel group count")\n'
                ' buffer=(ctypes.c_uint32*max(1,count))()\n'
                ' count=libc.getgroups(count,buffer)\n'
                ' if count<0: raise OSError(ctypes.get_errno(),"kernel groups")\n'
                ' groups=list(buffer[:count])\n'
                'else: groups=os.getgroups()\n'
                'print(json.dumps([os.getuid(),os.getgid(),groups]))'],
                cwd='/var/tmp', env={'PATH': os.environ['PATH']},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            import json
            identity = json.loads(stdout)
            self.assertEqual(identity[:2], [owner.uid, owner.uid])
            # Modern macOS os.getgroups queries directory membership, not this
            # process's effective kernel groups; libc's legacy symbol checks it.
            self.assertLessEqual(set(identity[2]), {owner.uid})
        finally:
            owner.close()

    def test_cleanup_owns_daemon_after_fork_setsid_and_parent_exit(self):
        import json
        from benchmarks.v3_process_owner import UIDOwner, process_snapshot
        self.assertTrue(hasattr(UIDOwner, 'popen'), 'UID execution boundary is missing')
        owner = UIDOwner()
        child = None
        code = ('import os,time; pid=os.fork(); '
                'os._exit(0) if pid else None; os.setsid(); '
                'print(os.getpid(),flush=True); os.close(1); os.close(2); time.sleep(120)')
        try:
            process = owner.popen([sys.executable, '-I', '-c', code], cwd='/var/tmp',
                                  env={'PATH': os.environ['PATH']},
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output, error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, error)
            child = int(output.strip())
            self.assertEqual(process_snapshot()[child][0], owner.uid)
            owner.close()
            self.assertFalse(any(uid == owner.uid and not state.startswith(('Z', 'X'))
                                 for uid, state in process_snapshot().values()))
        finally:
            if child is not None and process_snapshot().get(child, (None,))[0] == owner.uid:
                try:
                    import signal
                    os.kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            owner.close()


if __name__ == '__main__':
    unittest.main()
