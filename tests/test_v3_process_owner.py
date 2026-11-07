"""Kernel UID ownership for benchmark actors and daemonized command children."""
import importlib
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
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
