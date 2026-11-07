"""Internal privileged bwrap setup with unchanged nested-userns restrictions."""
from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time

_NEWUSER = 0x10000000
_TIMEOUT = 10

_BOOTSTRAP = r'''import ctypes,os,resource,socket,sys
from pathlib import Path
libc=ctypes.CDLL(None,use_errno=True)
libc.prctl.argtypes=[ctypes.c_int]+[ctypes.c_ulong]*4
for capability in range(int(Path('/proc/sys/kernel/cap_last_cap').read_text())+1):
 present=libc.prctl(23,capability,0,0,0)
 if present<0: raise OSError(ctypes.get_errno(),'bounding capability inspection failed')
 if present and libc.prctl(24,capability,0,0,0):
  raise OSError(ctypes.get_errno(),'bounding capability drop failed: '+str(capability))
if libc.prctl(47,4,0,0,0): raise OSError(ctypes.get_errno(),'ambient capability drop failed')
class Header(ctypes.Structure): _fields_=[('version',ctypes.c_uint32),('pid',ctypes.c_int)]
class Data(ctypes.Structure): _fields_=[('effective',ctypes.c_uint32),('permitted',ctypes.c_uint32),('inheritable',ctypes.c_uint32)]
header=Header(0x20080522,0); data=(Data*2)()
if libc.capset(ctypes.byref(header),ctypes.byref(data)): raise OSError(ctypes.get_errno(),'capability drop failed')
if libc.prctl(38,1,0,0,0): raise OSError(ctypes.get_errno(),'no_new_privs unavailable')
resource.setrlimit(resource.RLIMIT_NPROC,(256,256))
os.umask(0o002)
if os.getresuid()!=(int(sys.argv[2]),)*3 or os.getresgid()!=(int(sys.argv[3]),)*3 or os.getgroups():
 raise PermissionError('namespace did not preserve the assigned host identity')
for descriptor in os.listdir('/proc/self/fd'):
 if descriptor.isdigit() and int(descriptor)>=3:
  try: os.close(int(descriptor))
  except OSError as exc:
   if exc.errno!=9: raise
connection=socket.socket(socket.AF_UNIX)
connection.settimeout(10)
connection.connect(sys.argv[1]); connection.sendall(b'ready\n')
if connection.recv(1)!=b'X': raise PermissionError('namespace startup was not authorized')
connection.close()
os.execvpe(sys.argv[4],sys.argv[4:],os.environ)
'''


def _rewrite(command, *, uid, gid, descriptors, python, ready):
    if (not isinstance(command, (list, tuple)) or not command
            or Path(command[0]).name != 'bwrap' or '--' not in command or len(descriptors) != 2):
        raise ValueError('namespace requires an explicit bwrap command')
    separator = command.index('--')
    options, target = list(command[1:separator]), list(command[separator + 1:])
    forbidden = {'--args', '--userns', '--userns2', '--uid', '--gid', '--cap-add', '--unshare-user-try'}
    if (options.count('--unshare-user') != 1 or options.count('--disable-userns') != 1
            or forbidden.intersection(options) or not target):
        raise PermissionError('namespace rewrite requires the original strict userns guard')
    options.remove('--unshare-user')
    options.remove('--disable-userns')
    return [command[0], '--userns', str(descriptors[0]), '--userns2', str(descriptors[1]),
            '--uid', str(uid), '--gid', str(gid), '--cap-drop', 'ALL', '--cap-add', 'CAP_SETPCAP',
            '--assert-userns-disabled', *options, '--', python, '-I', '-S', '-c', _BOOTSTRAP,
            ready, str(uid), str(gid), *target]


def _receive(connection):
    value = bytearray()
    while not value.endswith(b'\n'):
        chunk = connection.recv(1)
        if not chunk or len(value) >= 4096:
            raise RuntimeError('namespace keeper startup channel failed')
        value.extend(chunk)
    result = json.loads(value)
    if not isinstance(result, dict):
        raise RuntimeError('namespace keeper startup returned an invalid response')
    if 'error' in result:
        raise RuntimeError('namespace keeper startup failed: ' + str(result['error']))
    return result


def _send(connection, value):
    connection.sendall((json.dumps(value) + '\n').encode())


def _dumpable(value):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    if libc.prctl(4, value, 0, 0, 0):
        raise OSError(ctypes.get_errno(), 'namespace keeper dumpability failed')


def _parent_death(expected):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0):
        raise OSError(ctypes.get_errno(), 'namespace keeper parent-death guard failed')
    if os.getppid() != expected:
        raise RuntimeError('namespace keeper parent exited during startup')


def _read_ready(connection):
    expected = b'ready\n'
    value = bytearray()
    while len(value) < len(expected):
        chunk = connection.recv(len(expected) - len(value))
        if not chunk:
            raise PermissionError('namespace startup disconnected before readiness')
        value.extend(chunk)
    if value != expected:
        raise PermissionError('invalid namespace startup handshake')


def _identity(uid, gid):
    if any(type(value) is not int or not 2**30 <= value < 2**31 for value in (uid, gid)):
        raise ValueError('namespace identities must be reserved ephemeral numeric IDs')


def _keeper(control_fd, uid, gid):
    """Fresh single-thread interpreter only; it never loads selected product code."""
    _identity(uid, gid)
    if not sys.platform.startswith('linux') or os.geteuid() != 0:
        raise PermissionError('namespace keeper requires Linux root')
    control = socket.socket(fileno=control_fd)
    control.settimeout(_TIMEOUT)
    locked_pid = None
    child = None
    try:
        original_parent = os.getppid()
        _parent_death(original_parent)
        original_ns = os.stat('/proc/self/ns/user').st_ino
        os.unshare(_NEWUSER)
        _send(control, {'phase': 'unshared', 'pid': os.getpid()})
        if control.recv(1) != b'M':
            raise RuntimeError('namespace map acknowledgement failed')
        os.setgid(gid)
        os.setuid(uid)
        # Credential changes clear PR_SET_PDEATHSIG. Reinstate it before fork.
        _parent_death(original_parent)
        _dumpable(0)
        if os.stat('/proc/self/ns/user').st_ino == original_ns:
            raise PermissionError('private namespace was not created')
        Path('/proc/sys/user/max_user_namespaces').write_text('1')
        parent, child = socket.socketpair()
        parent.settimeout(_TIMEOUT)
        keeper_pid = os.getpid()
        locked_pid = os.fork()
        if locked_pid == 0:
            parent.close()
            control.close()
            try:
                _parent_death(keeper_pid)
                os.unshare(_NEWUSER)
                _dumpable(1)
                Path('/proc/self/uid_map').write_text(f'{uid} {uid} 1\n')
                Path('/proc/self/setgroups').write_text('deny\n')
                Path('/proc/self/gid_map').write_text(f'{gid} {gid} 1\n')
                _dumpable(0)
                try:
                    os.unshare(_NEWUSER)
                except OSError as exc:
                    if exc.errno != errno.ENOSPC:
                        raise
                else:
                    raise PermissionError('private ancestor userns limit is ineffective')
                _send(child, {'phase': 'ready', 'pid': os.getpid()})
                child.settimeout(None)
                child.recv(1)
            except BaseException as exc:
                try:
                    _send(child, {'error': type(exc).__name__ + ': ' + str(exc)})
                except OSError:
                    pass
            finally:
                os._exit(0)
        child.close()
        child = parent
        result = _receive(child)
        if result.get('phase') != 'ready' or result.get('pid') != locked_pid:
            raise RuntimeError('locked namespace startup failed')
        _send(control, result)
        control.settimeout(None)
        control.recv(1)
    except BaseException as exc:
        try:
            _send(control, {'error': type(exc).__name__ + ': ' + str(exc)})
        except OSError:
            pass
    finally:
        if child is not None:
            child.close()
        if locked_pid:
            try:
                os.kill(locked_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(locked_pid, 0)
        control.close()


class Namespace:
    """One root-created namespace pair; attach verifies identity before model exec."""
    def __init__(self, uid, gid):
        _identity(uid, gid)
        self.uid, self.gid = uid, gid
        self._fds = []
        self._keeper = self._control = self._locked_pidfd = None
        self._process = self._listener = self._directory = None
        self._entered = self._closed = False

    def __enter__(self):
        if not sys.platform.startswith('linux') or os.geteuid() != 0:
            raise PermissionError('private namespace setup requires Linux root')
        if self._entered or self._closed:
            raise RuntimeError('namespace context is already used or closed')
        parent, child = socket.socketpair()
        parent.settimeout(_TIMEOUT)
        self._control = parent
        try:
            self._keeper = subprocess.Popen([str(Path(sys.executable).resolve()), '-I', '-S',
                str(Path(__file__).resolve()), '--keeper', str(child.fileno()), str(self.uid), str(self.gid)],
                cwd='/', env={'PATH': os.defpath, 'LANG': 'C'}, pass_fds=(child.fileno(),),
                extra_groups=[], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            child.close()
            result = _receive(parent)
            if result != {'phase': 'unshared', 'pid': self._keeper.pid}:
                raise RuntimeError('namespace keeper identity changed')
            root = Path('/proc') / str(self._keeper.pid)
            (root / 'uid_map').write_text(f'{self.uid} {self.uid} 1\n')
            (root / 'setgroups').write_text('deny\n')
            (root / 'gid_map').write_text(f'{self.gid} {self.gid} 1\n')
            self._fds.append(os.open(root / 'ns/user', os.O_RDONLY | os.O_CLOEXEC))
            parent.sendall(b'M')
            result = _receive(parent)
            if result.get('phase') != 'ready' or type(result.get('pid')) is not int or result['pid'] <= 0:
                raise RuntimeError('locked namespace identity is unavailable')
            self._locked_pidfd = os.pidfd_open(result['pid'])
            self._fds.append(os.open(Path('/proc') / str(result['pid']) / 'ns/user', os.O_RDONLY | os.O_CLOEXEC))
            if os.fstat(self._fds[0]).st_ino == os.fstat(self._fds[1]).st_ino:
                raise PermissionError('nested user namespace was not created')
            self._directory = Path(tempfile.mkdtemp(prefix='kyrozen-ns-', dir='/var/run')).resolve()
            self._directory.chmod(0o711)
            self._listener = socket.socket(socket.AF_UNIX)
            self._listener.bind(str(self._directory / 'ready'))
            os.chmod(self._directory / 'ready', 0o666)
            self._listener.listen(4)
            self._listener.settimeout(.05)
            self._entered = True
            return self
        except BaseException:
            child.close()
            self.close()
            raise

    @property
    def pass_fds(self):
        if not self._entered or self._closed:
            raise RuntimeError('namespace context is not ready')
        return tuple(self._fds)

    def rewrite(self, command):
        executable = Path(command[0]).resolve(strict=True)
        metadata = executable.stat()
        if metadata.st_uid != 0 or metadata.st_mode & 0o022 or not stat.S_ISREG(metadata.st_mode):
            raise PermissionError('privileged namespace requires a trusted root-owned bwrap')
        command = [str(executable), *command[1:]]
        return _rewrite(command, uid=self.uid, gid=self.gid, descriptors=self.pass_fds,
                        python=str(Path(sys.executable).resolve()), ready=str(self._directory / 'ready'))

    @staticmethod
    def environment(environment):
        # bwrap and the final trusted dropper both start before capabilities are
        # completely removed. Their dynamic loaders must not consult actor input.
        prefixes = ('LD_', 'DYLD_', 'PYTHON', 'MALLOC_')
        names = {'GLIBC_TUNABLES', 'GCONV_PATH', 'LOCPATH', 'LIBC_FATAL_STDERR_'}
        return {key: value for key, value in environment.items()
                if not key.upper().startswith(prefixes) and key.upper() not in names}

    def attach(self, process, *, timeout=_TIMEOUT):
        if not self._entered or self._closed or self._process is not None:
            raise RuntimeError('namespace startup is not ready or is already attached')
        self._process = process
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('namespace process exited before startup readiness')
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            with connection:
                connection.settimeout(min(1, max(.01, deadline - time.monotonic())))
                pid, uid, gid = struct.unpack('iII', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != self.uid or gid != self.gid or pid <= 0:
                    continue
                _read_ready(connection)
                status = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
                if (any(int(value) != self.uid for value in status['Uid'].split())
                        or any(int(value) != self.gid for value in status['Gid'].split())
                        or status['NoNewPrivs'].strip() != '1'
                        or any(int(status[key].strip(), 16) for key in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'))):
                    raise PermissionError('namespace startup retained identity or capability authority')
                connection.sendall(b'X')
                self._listener.close()
                self._listener = None
                return
        raise TimeoutError('namespace startup did not become ready')

    def close(self):
        if self._closed:
            return
        if self._process is not None:
            if self._process.poll() is None:
                self._process.kill()
            self._process.wait(timeout=5)
        if self._control is not None:
            self._control.close()
            self._control = None
        if self._keeper is not None:
            try:
                self._keeper.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._keeper.kill()
                self._keeper.wait(timeout=5)
            if self._keeper.stderr:
                self._keeper.stderr.close()
        if self._locked_pidfd is not None:
            try:
                signal.pidfd_send_signal(self._locked_pidfd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(self._locked_pidfd)
            self._locked_pidfd = None
        for descriptor in self._fds:
            os.close(descriptor)
        self._fds.clear()
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        if self._directory is not None:
            (self._directory / 'ready').unlink(missing_ok=True)
            self._directory.rmdir()
        self._closed = True

    def __exit__(self, *args):
        self.close()


if __name__ == '__main__':
    if len(sys.argv) != 5 or sys.argv[1] != '--keeper':
        raise SystemExit('benchmark namespace keeper only')
    _keeper(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
