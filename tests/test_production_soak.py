import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import production_soak


class ProductionSoakProcessTests(unittest.TestCase):
    def windows_table(self, rows, probe=99):
        with patch('scripts.production_soak.platform.system',return_value='Windows'), \
             patch('scripts.production_soak.shutil.which',return_value='powershell'), \
             patch('scripts.production_soak._process_output',return_value=(json.dumps(rows),probe)):
            return production_soak._process_table()

    @staticmethod
    def row(pid, ppid, second, **extra):
        return dict(pid=pid,ppid=ppid,CreationDate=(f'2026-11-07T08:00:{second:02d}.0000000Z'
                    if second is not None else None),Name='python.exe',**extra)

    def test_windows_stale_parent_pid_cannot_adopt_an_older_process_or_its_children(self):
        table=self.windows_table([self.row(10,1,20),self.row(11,10,10),self.row(12,11,11),
                                  self.row(13,10,21),self.row(14,13,22)])
        self.assertEqual(production_soak._descendants(10,table),{13,14})

    def test_windows_reused_tracked_pid_is_not_the_same_surviving_process(self):
        before=self.windows_table([self.row(10,1,10),self.row(11,10,11)])
        after=self.windows_table([self.row(11,500,30)])
        with tempfile.TemporaryDirectory() as directory:
            checkpoint=Path(directory)
            with patch('scripts.production_soak._process_table',return_value=before):
                tracked=production_soak._capture_process_tree(10,checkpoint/'before.json')
            with patch('scripts.production_soak._process_table',return_value=after):
                production_soak._assert_processes_stopped(tracked,checkpoint/'after.json')
            report=json.loads((checkpoint/'after.json').read_text())
            self.assertEqual(report['offending'],[])
            self.assertEqual(report['reused_pids'],[11])

    def test_windows_same_identity_orphan_still_fails_after_reparenting_with_evidence(self):
        before=self.windows_table([self.row(10,1,10),self.row(11,10,11)])
        after=self.windows_table([self.row(11,500,11)])
        with tempfile.TemporaryDirectory() as directory:
            checkpoint=Path(directory)
            with patch('scripts.production_soak._process_table',return_value=before):
                tracked=production_soak._capture_process_tree(10,checkpoint/'before.json')
            with patch('scripts.production_soak._process_table',return_value=after), \
                 self.assertRaisesRegex(AssertionError,'orphan gateway child'):
                production_soak._assert_processes_stopped(tracked,checkpoint/'after.json')
            report=json.loads((checkpoint/'after.json').read_text())
            self.assertEqual(report['offending'],[self.row(11,500,11)])

    def test_windows_missing_relevant_creation_identity_fails_closed_and_is_retained(self):
        for rows in ([self.row(10,1,None),self.row(11,10,11)],
                     [self.row(10,1,10),self.row(11,10,None)]):
            with self.subTest(rows=rows),tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'before.json'
                with patch('scripts.production_soak._process_table',return_value=self.windows_table(rows)), \
                     self.assertRaisesRegex(AssertionError,'creation identity'):
                    production_soak._capture_process_tree(10,path)
                self.assertIn('error',json.loads(path.read_text()))
        before=self.windows_table([self.row(10,1,10),self.row(11,10,11)])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            with patch('scripts.production_soak._process_table',return_value=before):
                tracked=production_soak._capture_process_tree(10,path/'before.json')
            with patch('scripts.production_soak._process_table',return_value=self.windows_table([self.row(11,500,None)])), \
                 self.assertRaisesRegex(AssertionError,'creation identity'):
                production_soak._assert_processes_stopped(tracked,path/'after.json')
            self.assertIn('error',json.loads((path/'after.json').read_text()))

    def test_posix_stopped_descendant_check_preserves_pid_membership_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            with patch('scripts.production_soak._process_table',return_value={10:1,11:10}):
                tracked=production_soak._capture_process_tree(10,path/'before.json')
            with patch('scripts.production_soak._process_table',return_value={11:500}), \
                 self.assertRaisesRegex(AssertionError,'orphan gateway child'):
                production_soak._assert_processes_stopped(tracked,path/'after.json')

    def test_audit_baseline_does_not_hide_a_new_child_reusing_a_baseline_pid(self):
        baseline=self.windows_table([self.row(10,1,10),self.row(11,10,11)])
        current=self.windows_table([self.row(10,1,10),self.row(11,10,30)])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            with patch('scripts.production_soak._process_table',return_value=baseline):
                tracked=production_soak._capture_process_tree(10,path/'baseline.json')
            with patch('scripts.production_soak._process_table',return_value=current), \
                 self.assertRaisesRegex(AssertionError,'orphan audit child'):
                production_soak._assert_no_new_audit_children(10,tracked,path/'after.json')
            self.assertEqual(json.loads((path/'after.json').read_text())['offending_pids'],[11])

    def test_audit_orphan_identity_outside_excerpt_preserves_capture_metadata(self):
        rows=[self.row(10,1,10),self.row(11,10,9),self.row(99,10,12)]
        rows.extend(self.row(pid,10,11) for pid in range(100,700))
        baseline=self.windows_table(rows)
        orphan=self.row(900,10,12)
        current=self.windows_table(rows+[orphan])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            with patch('scripts.production_soak._process_table',return_value=baseline):
                tracked=production_soak._capture_process_tree(10,path/'baseline.json')
            with patch('scripts.production_soak._process_table',return_value=current), \
                 self.assertRaisesRegex(AssertionError,'orphan audit child'):
                production_soak._assert_no_new_audit_children(10,tracked,path/'after.json')
            report=json.loads((path/'after.json').read_text())
            self.assertEqual(report['offending'],[orphan])
            self.assertEqual(report['offending_count'],1)
            self.assertEqual(report['offending_pids'],[900])
            self.assertEqual(report['root_pid'],10)
            self.assertEqual(report['tracked_count'],601)
            self.assertEqual(len(report['tracked_pids']),512)
            self.assertEqual(report['excluded_stale_parent_pids'],[11])
            self.assertEqual(report['excluded_stale_parent_count'],1)
            self.assertEqual(report['excluded_probe_pid'],99)
            self.assertEqual(report['excluded_probe'],self.row(99,10,12))
            self.assertTrue(report['rows_truncated'])
            self.assertEqual(report['row_count'],603)
            self.assertNotIn(900,[row['pid'] for row in report['rows']])

    def test_process_capture_failure_is_explicit_in_checkpoint_instead_of_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'before.json'
            with patch('scripts.production_soak._process_table',side_effect=OSError('offline probe failed')), \
                 self.assertRaises(OSError):
                production_soak._capture_process_tree(10,path)
            self.assertTrue(path.is_file())
            self.assertEqual(json.loads(path.read_text())['status'],'capture_failed')

    def test_missing_unrelated_identity_is_ignored_and_one_tick_reuse_is_distinct(self):
        old=self.row(11,10,11)
        new=dict(old,CreationDate='2026-11-07T08:00:11.0000001Z')
        before=self.windows_table([self.row(10,1,10),old,self.row(20,1,None)])
        after=self.windows_table([new,self.row(20,1,None)])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            with patch('scripts.production_soak._process_table',return_value=before):
                tracked=production_soak._capture_process_tree(10,path/'before.json')
            with patch('scripts.production_soak._process_table',return_value=after):
                production_soak._assert_processes_stopped(tracked,path/'after.json')
            self.assertEqual(json.loads((path/'after.json').read_text())['reused_pids'],[11])

    def test_checkpoint_bounds_do_not_hide_real_orphans_outside_the_logged_excerpt(self):
        before=self.windows_table([self.row(10,1,10)]+[self.row(pid,10,11) for pid in range(100,700)])
        after=self.windows_table([self.row(pid,5000,11) for pid in range(100,700)])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            with patch('scripts.production_soak._process_table',return_value=before):
                tracked=production_soak._capture_process_tree(10,path/'before.json')
            report=json.loads((path/'before.json').read_text())
            self.assertEqual(report['row_count'],601);self.assertTrue(report['rows_truncated'])
            self.assertLessEqual(len(report['rows']),512)
            self.assertLessEqual(len(report['tracked_pids']),512)
            with patch('scripts.production_soak._process_table',return_value=after), \
                 self.assertRaisesRegex(AssertionError,'orphan gateway child'):
                production_soak._assert_processes_stopped(tracked,path/'after.json')
            report=json.loads((path/'after.json').read_text())
            self.assertEqual(report['offending_count'],600)
            self.assertEqual(len(report['offending']),512)

    @unittest.skipUnless(os.name=='posix','local POSIX identity-observer smoke')
    def test_native_observer_detects_live_owned_child_then_certifies_its_exit(self):
        import select,signal,subprocess,sys
        hold='import sys;sys.stdin.buffer.read()'
        family_code='''
import subprocess,sys
child=subprocess.Popen([sys.executable,'-I','-c',sys.argv[1]],stdin=subprocess.PIPE,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
try:
    print(child.pid,flush=True)
    sys.stdin.buffer.read()
finally:
    try:child.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate(timeout=5)
'''
        def drain(process):
            try:process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                if process.poll() is None:
                    try:os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                process.communicate(timeout=5)
        # Other tests may own live siblings beneath the unittest process.
        unrelated=subprocess.Popen([sys.executable,'-I','-c',hold],stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        self.addCleanup(drain,unrelated)
        family=subprocess.Popen([sys.executable,'-I','-c',family_code,hold],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        self.addCleanup(drain,family)
        self.assertTrue(select.select([family.stdout],[],[],5)[0],'child family did not become ready')
        child_pid=int(family.stdout.readline())
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            tracked=production_soak._capture_process_tree(family.pid,path/'before.json')
            self.assertEqual(tracked['tracked'],{child_pid})
            self.assertNotIn(unrelated.pid,tracked['tracked'])
            with self.assertRaisesRegex(AssertionError,'orphan gateway child'):
                production_soak._assert_processes_stopped(tracked,path/'live.json')
            _,error=family.communicate(timeout=5)
            self.assertEqual(family.returncode,0,error.decode(errors='replace'))
            production_soak._assert_processes_stopped(tracked,path/'exited.json')
            self.assertEqual(json.loads((path/'exited.json').read_text())['status'],'stopped')
            self.assertIsNone(unrelated.poll())

    def test_candidate_dependency_inventory_does_not_require_psutil(self):
        dependencies = production_soak.candidate_identity()["dependencies"]

        self.assertNotIn("psutil", dependencies)

    def test_process_table_tracks_descendants_across_multiple_generations(self):
        parents = production_soak._parse_process_table("10 1\n11 10\n12 11\n20 1\n")

        self.assertEqual(production_soak._descendants(10, parents), {11, 12})
        self.assertEqual(production_soak._descendants(20, parents), set())

    def test_windows_process_table_excludes_its_own_probe(self):
        rows=[self.row(10,1,10),self.row(11,10,11),self.row(99,10,12)]
        output=json.dumps(rows)
        with patch("scripts.production_soak.platform.system", return_value="Windows"), \
             patch("scripts.production_soak.shutil.which", return_value="powershell"), \
             patch("scripts.production_soak._process_output", return_value=(output, 99)) as probe:
            table=production_soak._process_table()
            self.assertEqual(table,{10:rows[0],11:rows[1]})
            self.assertEqual(table.probe_pid,99)
            self.assertEqual(table.probe,rows[2])
            query=probe.call_args.args[0][-1]
            self.assertIn('CreationDate.ToUniversalTime()',query)
            self.assertNotIn('CommandLine',query);self.assertNotIn('ExecutablePath',query)

    def test_windows_process_snapshot_maps_native_resource_counters(self):
        with patch("scripts.production_soak.platform.system", return_value="Windows"), \
             patch("scripts.production_soak._powershell", return_value=
                   '{"rssBytes":4096,"threads":3,"openHandles":8}'):
            self.assertEqual(production_soak._process_snapshot(10),
                             {"rss_bytes": 4096, "threads": 3, "open_handles": 8})

    def test_handle_bound_uses_windows_growth_and_preserves_posix_cap(self):
        with patch("scripts.production_soak.platform.system", return_value="Windows"):
            self.assertEqual(production_soak._handle_limit(125), 225)
        with patch("scripts.production_soak.platform.system", return_value="Linux"):
            self.assertEqual(production_soak._handle_limit(125), 99)

    def test_durable_task_wait_allows_sqlite_busy_timeout_under_load(self):
        class Gateway:
            statuses = iter(("running", "succeeded"))

            def _request(self, *_args, **_kwargs):
                return {"tasks": [{"id": "task-1", "session_id": "soak-a",
                                   "status": next(self.statuses)}]}

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "soak-a-29.txt").write_text("soak-a:29")
            with patch("scripts.production_soak.time.monotonic", side_effect=(0, 16, 17)), \
                 patch("scripts.production_soak.time.sleep"):
                production_soak._wait_for_task_completion(
                    Gateway(), "http://localhost", workspace, "soak-a", 29,
                    "soak-a-29.txt", "task-1",
                )

    def test_current_platform_process_probe_returns_bounded_resource_counts(self):
        snapshot = production_soak._process_snapshot(os.getpid())

        self.assertGreater(snapshot["rss_bytes"], 0)
        self.assertGreaterEqual(snapshot["threads"], 1)
        self.assertGreater(snapshot["open_handles"], 0)


if __name__ == "__main__":
    unittest.main()
