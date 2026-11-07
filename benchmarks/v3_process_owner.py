"""Benchmark-only kernel UID ownership, including detached descendants."""
from __future__ import annotations

import os
import json
from pathlib import Path
import secrets
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

_LOCK = threading.Lock()
_ALLOCATED = set()


def connect_actor_client():
    """Connect only to a kernel-authenticated privileged benchmark parent."""
    address = os.environ.pop('KYROZEN_BENCHMARK_ACTOR_SOCKET', None)
    if not address:
        raise PermissionError('benchmark actor socket is missing')
    connection = socket.socket(socket.AF_UNIX)
    try:
        connection.settimeout(10)
        connection.connect(address)
        if peer_identity(connection)[1] != 0:
            raise PermissionError('benchmark actor requires a privileged parent')
        connection.sendall(b'{"op":"hello"}\n')
        with connection.makefile('rb', buffering=0) as stream:
            response = stream.readline(4097)
        if len(response) > 4096 or not response.endswith(b'\n'):
            raise PermissionError('invalid parent handshake')
        response = json.loads(response)
        identity = response.get('value') if response.get('ok') is True else None
        if (not isinstance(identity, dict)
                or set(identity) != {'run_id', 'actor_id', 'session_id', 'revision'}
                or any(not isinstance(identity[k], str) or not identity[k] for k in ('run_id', 'actor_id', 'session_id'))
                or type(identity['revision']) is not int or identity['revision'] < 0):
            raise PermissionError('invalid parent actor identity')
        connection.settimeout(None)
        try:
            from v3_shell_launcher import BrokerClient
        except ModuleNotFoundError:
            from benchmarks.v3_shell_launcher import BrokerClient
        return BrokerClient(connection.detach(), identity)
    except BaseException:
        connection.close()
        raise


def peer_identity(connection):
    """Kernel identity of the connecting/sending actor, never JSON claims."""
    if connection.family != socket.AF_UNIX:
        raise PermissionError('actor authentication requires a Unix socket')
    if sys.platform.startswith('linux'):
        pid, uid, gid = struct.unpack('iII', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    elif sys.platform == 'darwin':
        import ctypes
        library = ctypes.CDLL(None, use_errno=True)
        uid_value, gid_value = ctypes.c_uint32(), ctypes.c_uint32()
        if library.getpeereid(connection.fileno(), ctypes.byref(uid_value), ctypes.byref(gid_value)):
            raise OSError(ctypes.get_errno(), 'kernel peer credential lookup failed')
        pid = struct.unpack('i', connection.getsockopt(0, 2, 4))[0]  # SOL_LOCAL, LOCAL_PEERPID
        uid, gid = uid_value.value, gid_value.value
    else:
        raise PermissionError('kernel actor authentication is unavailable')
    if pid <= 0 or uid == 2**32 - 1 or gid == 2**32 - 1:
        raise PermissionError('kernel actor identity is unavailable')
    return pid, uid, gid


def reserve_identity(uid, *, directory='/var/tmp'):
    """Exclusive cross-parent claim; stale claims deliberately prevent reuse."""
    directory = Path(directory)
    metadata = directory.lstat()
    if (not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022 and not metadata.st_mode & stat.S_ISVTX):
        raise PermissionError('UID reservation directory must be owned and protected')
    path = directory / f'.openkyrozen-benchmark-uid-{uid}'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, str(os.getpid()).encode())
    finally:
        os.close(descriptor)
    return path


def process_snapshot():
    """Read numeric kernel identities only, never commands or environments."""
    result = subprocess.run(['/bin/ps', '-axo', 'uid=,pid=,stat='],
                            capture_output=True, text=True, timeout=3, check=True)
    return {int(pid): (int(uid), state) for uid, pid, state in
            (line.split() for line in result.stdout.splitlines() if line.strip())}


def _registered_identity(uid):
    import pwd
    import grp
    for lookup in (pwd.getpwuid, grp.getgrgid):
        try:
            lookup(uid)
            return True
        except KeyError:
            pass
    return False


class UIDOwner:
    """One fresh UID per invocation; no persistent account or service is created.

    The trusted parent must be privileged. Unprivileged code cannot certify UID
    ownership or silently fall back to ancestry/process-group cleanup.
    """
    def __init__(self, *, uid=None):
        if os.geteuid() != 0:
            raise PermissionError('benchmark UID ownership requires a privileged parent')
        if uid is not None and (type(uid) is not int or not 2**30 <= uid < 2**31):
            raise ValueError('benchmark UID must be a distinct ephemeral numeric identity')
        with _LOCK:
            used = {value[0] for value in process_snapshot().values()} | _ALLOCATED
            if uid is None:
                for _ in range(128):
                    candidate = 2**30 + secrets.randbelow(2**29)
                    if candidate not in used and not _registered_identity(candidate):
                        try:
                            reservation = reserve_identity(candidate)
                        except FileExistsError:
                            continue
                        uid = candidate
                        break
                else:
                    raise RuntimeError('no unused benchmark actor UID')
            else:
                if uid in used or _registered_identity(uid):
                    raise ValueError('benchmark UID is already in use')
                try:
                    reservation = reserve_identity(uid)
                except FileExistsError:
                    raise ValueError('benchmark UID is already in use') from None
            _ALLOCATED.add(uid)
        self.uid = uid
        self.reservation = reservation
        self.closed = False
        self.process = None

    def popen(self, command, *, cwd, env, group=None, stdin=None, stdout=None, stderr=None):
        """Drop credentials in subprocess' native exec path, never preexec_fn."""
        if self.closed or self.process is not None:
            raise RuntimeError('benchmark UID invocation is already used or closed')
        if (not isinstance(command, (list, tuple)) or not command
                or any(not isinstance(arg, str) or '\0' in arg for arg in command)):
            raise ValueError('benchmark command must be an argument vector')
        group = self.uid if group is None else group
        if type(group) is not int or not 2**30 <= group < 2**31:
            raise ValueError('benchmark group must be an ephemeral numeric identity')
        bootstrap = 'import os,resource,sys\n'
        if sys.platform.startswith('linux'):
            bootstrap += ('import ctypes\nlibc=ctypes.CDLL(None,use_errno=True)\n'
                          'libc.prctl.argtypes=[ctypes.c_int]+[ctypes.c_ulong]*4\n'
                          'if libc.prctl(38,1,0,0,0): raise PermissionError("no_new_privs unavailable")\n')
        elif sys.platform == 'darwin':
            denial = '(deny process-exec (require-any (file-mode #o4000) (file-mode #o2000)))'
            command = list(command)
            if command[0] == '/usr/bin/sandbox-exec':
                if len(command) < 4 or command[1] != '-p':
                    raise ValueError('benchmark sandbox must use an explicit parent profile')
                command[2] += denial
            else:
                command = ['/usr/bin/sandbox-exec', '-p', '(version 1)(allow default)' + denial, *command]
        else:
            raise PermissionError('benchmark UID execution has no supported privilege boundary')
        bootstrap += ('resource.setrlimit(resource.RLIMIT_NPROC,(256,256))\n'
                      'os.umask(0o002)\nos.execvpe(sys.argv[1],sys.argv[1:],os.environ)')
        self.process = subprocess.Popen([sys.executable, '-I', '-S', '-c', bootstrap, *command],
            cwd=cwd, env=env, user=self.uid, group=group, extra_groups=[],
            stdin=stdin, stdout=stdout, stderr=stderr, close_fds=True, start_new_session=True)
        return self.process

    def close(self):
        if self.closed:
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            owned = {pid: state for pid, (uid, state) in process_snapshot().items()
                     if uid == self.uid and not state.startswith(('Z', 'X'))}
            if not owned:
                if self.process is not None:
                    self.process.wait(timeout=1)
                self.closed = True
                if self.reservation is not None:
                    self.reservation.unlink()
                # Never recycle an identity in this parent, even after cleanup.
                return
            stopped = all(state.startswith('T') for state in owned.values())
            for pid, state in owned.items():
                if stopped or not state.startswith('T'):
                    try:
                        os.kill(pid, signal.SIGKILL if stopped else signal.SIGSTOP)
                    except ProcessLookupError:
                        pass
            time.sleep(.01)
        raise RuntimeError('benchmark UID descendants could not be certified stopped')
