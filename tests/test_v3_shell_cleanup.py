"""Bounded legacy cleanup races; these do not certify detached UID ownership."""
import os
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from benchmarks import v3_shell_launcher as launcher


class LegacyCleanupRaceTests(unittest.TestCase):
    def process(self):
        return SimpleNamespace(pid=10, returncode=-9, wait=lambda timeout: -9)

    def snapshot(self, rows):
        return SimpleNamespace(stdout=rows)

    def test_group_is_signalled_before_its_leader_and_detached_descendants(self):
        signals = []
        snapshot = self.snapshot('10 1 10 S\n11 10 10 S\n12 10 12 S\n13 12 12 S\n')
        with patch.object(launcher.subprocess, 'run', return_value=snapshot), patch.object(
                launcher.os, 'killpg', side_effect=lambda pid, sig: signals.append(('group', pid))), patch.object(
                launcher.os, 'kill', side_effect=lambda pid, sig: signals.append(('pid', pid))):
            launcher._kill_tree(self.process())
        self.assertEqual(signals[0], ('group', 10))
        self.assertEqual(set(signals[1:]), {('pid', 11), ('pid', 12), ('pid', 13)})
        self.assertLess(signals.index(('pid', 13)), signals.index(('pid', 12)))

    def test_group_permission_race_is_benign_only_after_no_live_members_remain(self):
        initial = self.snapshot('10 1 10 S\n11 10 10 S\n')
        for fresh in ('', '10 1 10 Z\n11 1 10 Z\n'):
            with self.subTest(fresh=fresh), patch.object(launcher.subprocess, 'run',
                    side_effect=[initial, self.snapshot(fresh)]), patch.object(
                    launcher.os, 'killpg', side_effect=PermissionError('group already exiting')), patch.object(
                    launcher.os, 'kill') as kill:
                launcher._kill_tree(self.process())
            kill.assert_not_called()

    def test_group_permission_error_with_a_live_member_is_never_swallowed(self):
        with patch.object(launcher.subprocess, 'run', side_effect=[
                self.snapshot('10 1 10 S\n'), self.snapshot('11 1 10 S\n')]), patch.object(
                launcher.os, 'killpg', side_effect=PermissionError('live group')), patch.object(launcher.os, 'kill'):
            with self.assertRaisesRegex(PermissionError, 'live group'):
                launcher._kill_tree(self.process())

    def test_failed_fresh_snapshot_cannot_certify_a_permission_race_as_clean(self):
        with patch.object(launcher.subprocess, 'run', side_effect=[
                self.snapshot('10 1 10 S\n'), subprocess.TimeoutExpired('ps', 3)]), patch.object(
                launcher.os, 'killpg', side_effect=PermissionError('not certified')), patch.object(launcher.os, 'kill'):
            with self.assertRaisesRegex(PermissionError, 'not certified'):
                launcher._kill_tree(self.process())

    def test_successful_group_signal_does_not_hide_an_unsignalable_live_child(self):
        with patch.object(launcher.subprocess, 'run', side_effect=[
                self.snapshot('10 1 10 S\n11 10 10 S\n'), self.snapshot('11 1 10 S\n')]), patch.object(
                launcher.os, 'killpg'), patch.object(launcher.os, 'kill', side_effect=PermissionError('live child')):
            with self.assertRaisesRegex(PermissionError, 'live child'):
                launcher._kill_tree(self.process())

    def test_detached_pid_permission_race_requires_that_pid_to_have_exited(self):
        initial = self.snapshot('10 1 10 S\n12 10 12 S\n')
        with patch.object(launcher.subprocess, 'run', side_effect=[initial, self.snapshot('12 1 12 Z\n')]), patch.object(
                launcher.os, 'killpg'), patch.object(launcher.os, 'kill', side_effect=PermissionError('exiting child')):
            launcher._kill_tree(self.process())
        with patch.object(launcher.subprocess, 'run', side_effect=[initial, self.snapshot('12 1 12 S\n')]), patch.object(
                launcher.os, 'killpg'), patch.object(launcher.os, 'kill', side_effect=PermissionError('live child')):
            with self.assertRaisesRegex(PermissionError, 'live child'):
                launcher._kill_tree(self.process())

    def test_broker_close_finishes_other_cleanup_before_surfacing_a_live_group_failure(self):
        broker = launcher.ShellBroker.__new__(launcher.ShellBroker)
        broker.closed, broker.close_lock = threading.Event(), threading.RLock()
        broker.connection = SimpleNamespace(shutdown=lambda how: None, close=lambda: None)
        broker.processes = {'first': SimpleNamespace(pid=10), 'second': SimpleNamespace(pid=20)}
        closed = []
        broker.records = {name: {'invocation': SimpleNamespace(close=lambda name=name: closed.append(name)),
                                'lock': threading.Lock(), 'eof': set()}
                          for name in ('first', 'second')}
        attempted = []
        def cleanup(process):
            attempted.append(process.pid)
            if process.pid == 10:
                raise PermissionError('live group')
        with patch.object(launcher, '_kill_tree', side_effect=cleanup):
            with self.assertRaisesRegex(PermissionError, 'live group'):
                broker.close()
        self.assertEqual(attempted, [10, 20])
        self.assertEqual(closed, ['first', 'second'])


@unittest.skipUnless(os.name == 'posix', 'Unix process groups required')
class LegacyCleanupProcessTests(unittest.TestCase):
    def test_live_detached_descendant_in_snapshot_is_stopped_with_its_leader(self):
        code = '''import os,time
child=os.fork()
if child == 0:
    os.setsid()
    time.sleep(120)
else:
    print(child,flush=True)
    time.sleep(120)
'''
        process = subprocess.Popen([sys.executable, '-I', '-c', code], start_new_session=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        child = None
        try:
            child = int(process.stdout.readline())
            time.sleep(.05)
            launcher._kill_tree(process)
            self.assertIsNotNone(process.returncode)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                result = subprocess.run(['/bin/ps', '-p', str(child), '-o', 'stat='],
                                        capture_output=True, text=True, check=False)
                if not result.stdout.strip() or result.stdout.lstrip().startswith(('Z', 'X')):
                    break
                time.sleep(.01)
            else:
                self.fail('the observed detached child remained live')
        finally:
            for pid in (child, process.pid):
                if pid:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
