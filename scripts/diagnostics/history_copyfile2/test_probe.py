"""Offline boundary tests; fake WinAPI callbacks never count as Windows evidence."""
import ctypes
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def module():
    path = HERE / "probe.py"
    if not path.is_file():
        raise AssertionError("missing native CopyFile2 diagnostic implementation")
    spec = importlib.util.spec_from_file_location("copyfile2_probe", path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "state.sqlite3"
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("CREATE TABLE probe(value INTEGER)")
            db.execute("INSERT INTO probe VALUES(1)")
        self.p = module()


class SQLiteTests(Fixture):
    def test_real_write_probe_rolls_back_and_closes(self):
        result = self.p.sqlite_write_probe(self.db)
        self.assertTrue(result["connect_success"])
        self.assertTrue(result["write_success"])
        self.assertTrue(result["connection_closed"])
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT value FROM probe").fetchall(), [(1,)])


    def test_post_copy_write_commits_visible_data_for_a_fresh_connection(self):
        import inspect
        self.assertIn("commit", inspect.signature(self.p.sqlite_write_probe).parameters)
        result = self.p.sqlite_write_probe(self.db, commit=True)
        self.assertTrue(result["transaction_committed"])
        self.assertTrue(result["connection_closed"])
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT value FROM probe ORDER BY rowid").fetchall(), [(1,), (2,)])

    def test_real_readonly_error_is_retained_not_synthetic_success(self):
        original = sqlite3.connect
        def readonly(path, **kwargs):
            return original(Path(path).as_uri() + "?mode=ro", uri=True, **kwargs)
        with patch.object(self.p.sqlite3, "connect", side_effect=readonly):
            result = self.p.sqlite_write_probe(self.db)
        self.assertTrue(result["connect_success"])
        self.assertFalse(result["write_success"])
        self.assertEqual(result["sqlite_errorcode"], sqlite3.SQLITE_READONLY)
        self.assertTrue(result["connection_closed"])

    def test_real_other_sqlite_error_does_not_qualify_as_readonly(self):
        self.db.unlink()
        with closing(sqlite3.connect(self.db)):
            pass
        result = self.p.sqlite_write_probe(self.db)
        self.assertEqual(result["sqlite_errorcode"], sqlite3.SQLITE_ERROR)
        self.assertTrue(result["connection_closed"])
        self.assertFalse(result["write_success"])


class CallbackTests(Fixture):
    def api(self, *, hresult=0, callback_exception=False, no_stream=False):
        p = self.p
        class API:
            def thread_id(self):
                return 42
            def write_access_probe(self, path):
                return {"last_error": 0, "handle_closed": True}
            def copy(self, src, dst, callback):
                self.flags = 8
                self.paths = [str(src), str(dst)]
                self.callback_action = None
                if not no_stream:
                    message = p.CopyMessage()
                    message.Type = 3
                    message.Info.StreamStarted.hSourceFile = 101
                    message.Info.StreamStarted.hDestinationFile = 202
                    self.callback_action = callback(ctypes.byref(message), None)
                if hresult == 0:
                    shutil.copyfile(src, dst)
                return hresult
        return API()

    def test_sdk_x64_layout_and_callback_integer_width(self):
        self.assertEqual(ctypes.sizeof(self.p.CopyMessage), 80)
        self.assertEqual(self.p.CopyMessage.Info.offset, 8)
        self.assertEqual(ctypes.sizeof(self.p.CopyParameters), 32)
        self.assertEqual(self.p.CopyParameters.pProgressRoutine.offset, 16)
        self.assertEqual(ctypes.sizeof(self.p.HRESULT), 4)

    def test_callback_uses_real_sqlite_and_retains_handles_paths_and_copy_result(self):
        api = self.api()
        src = self.root / "ordinary.txt"
        src.write_bytes(b"ordinary\n")
        dest = self.root / "copied.txt"
        observer = self.p.CopyObserver(api, self.db, src)
        observer(src, dest)
        self.assertEqual(dest.read_bytes(), src.read_bytes())
        row = observer.copies[0]
        self.assertEqual(row["hresult"], 0)
        self.assertEqual(row["flags"], 8)
        self.assertEqual(row["source"], str(src))
        self.assertEqual(row["destination"], str(dest))
        self.assertTrue(row["probe"]["write_success"])
        self.assertEqual(row["source_handle"], 101)
        self.assertEqual(row["destination_handle"], 202)
        self.assertEqual(api.callback_action, 0)

    def test_callback_exception_does_not_escape_ffi_and_blocks_after_native_return(self):
        api = self.api()
        src = self.root / "ordinary.txt"
        src.write_bytes(b"x")
        observer = self.p.CopyObserver(api, self.db, src)
        with patch.object(self.p, "sqlite_write_probe", side_effect=RuntimeError("unit")):
            with self.assertRaisesRegex(RuntimeError, "callback"):
                observer(src, self.root / "copied.txt")
        self.assertEqual(api.callback_action, 1)
        self.assertEqual(observer.copies[0]["callback_error"], "RuntimeError")
        self.assertTrue(observer.copies[0].get("cleanup_uncertified", False))

    def test_native_hresult_failure_and_absent_stream_cannot_qualify(self):
        src = self.root / "ordinary.txt"
        src.write_bytes(b"x")
        for api in [self.api(hresult=-1), self.api(no_stream=True)]:
            with self.subTest(api=api):
                observer = self.p.CopyObserver(api, self.db, src)
                with self.assertRaises(RuntimeError):
                    observer(src, self.root / "copied.txt")
                self.assertFalse(observer.copies[0]["qualified"])

    def test_wrong_source_copy_does_not_run_probe(self):
        src = self.root / "ordinary.txt"
        src.write_bytes(b"x")
        observer = self.p.CopyObserver(self.api(), self.db, self.db)
        with patch.object(self.p, "sqlite_write_probe", side_effect=AssertionError("must not run")):
            observer(src, self.root / "copied.txt")
        self.assertIsNone(observer.copies[0]["probe"])


class SourceAndResultTests(Fixture):
    def test_changed_or_symlinked_source_rejected_before_import(self):
        source = self.root / "source"
        path = source / "x.py"
        source.mkdir()
        path.write_text("one")
        expected = {"x.py": self.p.sha256(path)}
        self.p.verify_files(source, expected)
        path.write_text("two")
        with self.assertRaisesRegex(RuntimeError, "source hash"):
            self.p.verify_files(source, expected)
        path.unlink()
        target = self.root / "target.py"
        target.write_text("one")
        path.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "source file"):
            self.p.verify_files(source, expected)


    def test_exact_git_crlf_identity_is_recorded_but_mixed_or_changed_source_is_rejected(self):
        root = self.root / "source"
        root.mkdir()
        path = root / "x.py"
        path.write_bytes(b"one\ntwo\n")
        expected = {"x.py": self.p.sha256(path)}
        path.write_bytes(b"one\r\ntwo\r\n")
        try:
            observed = self.p.verify_files(root, expected)["x.py"]
        except RuntimeError:
            self.fail("exact Git CRLF physical source must match pinned LF blob")
        self.assertEqual(observed["canonical_sha256"], expected["x.py"])
        self.assertEqual(observed["physical_sha256"], self.p.sha256(path))
        self.assertEqual(observed["newline_style"], "CRLF")
        for raw in [b"one\r\ntwo\n", b"one\r\nchanged\r\n", b"one\rtwo\r"]:
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                with self.assertRaises(RuntimeError):
                    self.p.verify_files(root, expected)

    def test_actual_current_import_in_fresh_child_is_pinned_and_unmodified(self):
        source = Path(os.environ.get("COPYFILE2_CURRENT_SOURCE", os.getcwd())).resolve()
        code = ("import importlib.util,json,pathlib; "
                "s=importlib.util.spec_from_file_location('p'," + repr(str(HERE / "probe.py")) + "); "
                "p=importlib.util.module_from_spec(s);s.loader.exec_module(p); "
                "m,observed=p.load_history(pathlib.Path(" + repr(str(source)) + "),'current'); "
                "print(json.dumps({'count':len(observed),'class':m.HistoryManager.__name__}))")
        done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=5)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"count": 10, "class": "HistoryManager"})


    def test_actual_current_snapshot_excludes_family_and_restores_native_copy_seam(self):
        source = Path(os.environ.get("COPYFILE2_CURRENT_SOURCE", os.getcwd())).resolve()
        code = """
import ctypes,importlib.util,json,pathlib,shutil,tempfile
s=importlib.util.spec_from_file_location('p',PROBE)
p=importlib.util.module_from_spec(s);s.loader.exec_module(p)
m,observed=p.load_history(pathlib.Path(SOURCE),'current')
class API:
 def thread_id(self):return 42
 def write_access_probe(self,path):return {'last_error':0,'handle_closed':True}
 def copy(self,src,dst,callback):
  v=p.CopyMessage();v.Type=3
  v.Info.StreamStarted.hSourceFile=101;v.Info.StreamStarted.hDestinationFile=202
  assert callback(ctypes.byref(v),None)==0
  shutil.copyfile(src,dst);return 0
original=m.shutil.copy2
with tempfile.TemporaryDirectory() as root:
 row=p.run_case(m,API(),pathlib.Path(root)/'case','current-excluded')
 assert m.shutil.copy2 is original
 assert row['condition_valid'],row
 assert not row['database_copied']
 assert row['family_excluded']==[True]*4
 assert len(row['copies'])==1
 class CleanFailure(API):
  def copy(self,src,dst,callback):return -1
 clean_failed=p.run_case(m,CleanFailure(),pathlib.Path(root)/'clean-failure','current-excluded')
 assert not clean_failed['cleanup_uncertified'] and not clean_failed['condition_valid']
 assert clean_failed['post_copy_write']['transaction_committed']
 class FailClose(API):
  def write_access_probe(self,path):
   def forbidden(*args,**kwargs):raise AssertionError('post-copy SQLite after uncertified cleanup')
   p.sqlite_write_probe=forbidden
   return {'last_error':0,'handle_closed':False}
 failed=p.run_case(m,FailClose(),pathlib.Path(root)/'uncertain','current-excluded')
 assert failed['cleanup_uncertified'] and not failed['condition_valid']
 assert failed['post_copy_write']['attempted'] is False
 print(json.dumps({'valid':row['condition_valid'],'cleanup':row['cleanup_uncertified']}))
""".replace("PROBE", repr(str(HERE / "probe.py"))).replace("SOURCE", repr(str(source)))
        done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=5)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"valid": True, "cleanup": False})

    def test_mechanism_assessment_requires_all_three_exact_conditions(self):
        rows = [
            {"name": "old-active-db", "condition_valid": True},
            {"name": "old-ordinary", "condition_valid": True},
            {"name": "current-excluded", "condition_valid": True},
        ]
        self.assertEqual(self.p.assess(rows)["status"], "CAUSAL_CLASS_REPRODUCED")
        self.assertEqual(self.p.assess(rows)["r260_status"], "BLOCKED")
        rows[0]["condition_valid"] = False
        self.assertEqual(self.p.assess(rows)["status"], "BLOCKED")
        self.assertEqual(self.p.assess(rows[:2])["status"], "BLOCKED")


class ChildLoopTests(Fixture):
    def child(self, kind, *, uncertain=False, fail_second=False):
        p = self.p
        calls = []
        def case(module, api, directory, name):
            calls.append(name)
            if fail_second and len(calls) == 2:
                raise RuntimeError("unit second case")
            return {"name": name, "condition_valid": True, "cleanup_uncertified": uncertain}
        output = self.root / kind
        with patch.object(p, "require_windows"), patch.object(p, "load_history", return_value=(object(), {})), \
             patch.object(p, "WindowsAPI", return_value=object()), patch.object(p, "run_case", side_effect=case):
            code = p.run_child(self.root, kind, output)
        return code, calls, json.loads((output / "child.json").read_text())

    def test_actual_child_old_executes_both_clean_rows_without_parent_variables(self):
        code, calls, result = self.child("old")
        self.assertEqual(calls, ["old-active-db", "old-ordinary"])
        self.assertEqual(code, 0)
        self.assertNotIn("error_class", result)
        self.assertEqual(len(result["rows"]), 2)

    def test_actual_child_current_clean_row_has_no_hidden_error(self):
        code, calls, result = self.child("current")
        self.assertEqual(calls, ["current-excluded"])
        self.assertNotIn("error_class", result)
        self.assertEqual(code, 0)

    def test_actual_child_uncertainty_stops_and_never_returns_success(self):
        code, calls, result = self.child("old", uncertain=True)
        self.assertEqual(calls, ["old-active-db"])
        self.assertTrue(result["cleanup_uncertified"])
        self.assertEqual(code, 1)

    def test_actual_child_error_after_first_valid_row_is_not_success(self):
        code, calls, result = self.child("old", fail_second=True)
        self.assertEqual(calls, ["old-active-db", "old-ordinary"])
        self.assertEqual(result["error_class"], "RuntimeError")
        self.assertTrue(result["cleanup_uncertified"])
        self.assertEqual(code, 1)
        self.assertEqual(len(result["rows"]), 1)


class ParentBoundaryTests(Fixture):
    def test_nonwindows_frontdoor_does_not_spawn_or_import_product(self):
        with patch.object(self.p.os, "name", "posix"), patch.object(self.p.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "Windows"):
                self.p.run_parent(self.root, self.root, self.root / "out")
        spawn.assert_not_called()

    def test_timeout_stops_before_current_child_and_retains_uncertainty(self):
        class Child:
            pid = 123
            returncode = None
            def wait(self, timeout):
                raise subprocess.TimeoutExpired("fixture", timeout)
            def poll(self):
                return None
        with patch.object(self.p, "require_windows"), \
             patch.object(self.p, "verify_files", return_value={}), \
             patch.object(self.p.subprocess, "Popen", return_value=Child()) as spawn, \
             patch.object(self.p.subprocess, "run", side_effect=subprocess.TimeoutExpired("taskkill", 1)):
            result = self.p.run_parent(self.root, self.root, self.root / "out")
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertTrue(result["cleanup_uncertified"])
        self.assertEqual(result["children"][0]["phase"], "timeout")
        self.assertTrue((self.root / "out" / "result.json").is_file())

    def test_failed_child_cleanup_uncertainty_and_rows_are_preserved(self):
        p = self.p
        class Child:
            pid = 123
            returncode = 1
            def __init__(self, command, **kwargs):
                output = Path(command[command.index("--output") + 1])
                p.write_json(output / "child.json", {
                    "rows": [{"name": "old-active-db", "condition_valid": False}],
                    "cleanup_uncertified": True, "source_commit": p.PINS["old"]["commit"]})
            def wait(self, timeout):
                return 1
        with patch.object(p, "require_windows"), patch.object(p, "verify_files", return_value={}), \
             patch.object(p.subprocess, "Popen", side_effect=Child) as spawn:
            result = p.run_parent(self.root, self.root, self.root / "out")
        self.assertEqual(spawn.call_count, 1)
        self.assertTrue(result["cleanup_uncertified"])
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["status"], "BLOCKED")


    def test_parent_rejects_nonzero_unknown_exit_error_or_missing_rows_despite_other_valid_rows(self):
        p = self.p
        for index, (exitcode, error, missing) in enumerate([
                (1, False, False), (None, False, False), (False, False, False),
                (0, True, False), (0, False, True)]):
            with self.subTest(exitcode=exitcode, error=error, missing=missing):
                class Child:
                    pid = 123
                    def __init__(self, command, **kwargs):
                        kind = command[command.index("--child") + 1]
                        self.code = exitcode if kind == "current" else 0
                        output = Path(command[command.index("--output") + 1])
                        names = ["old-active-db", "old-ordinary"] if kind == "old" else ["current-excluded"]
                        data = {"rows": [{"name": n, "condition_valid": True} for n in names],
                                "cleanup_uncertified": False, "source_commit": p.PINS[kind]["commit"]}
                        if kind == "current":
                            if error:
                                data["error_class"] = "RuntimeError"
                            if missing:
                                data["rows"] = []
                        p.write_json(output / "child.json", data)
                    def wait(self, timeout):
                        return self.code
                with patch.object(p, "require_windows"), patch.object(p, "verify_files", return_value={}), \
                     patch.object(p.subprocess, "Popen", side_effect=Child):
                    result = p.run_parent(self.root, self.root, self.root / ("out" + str(index)))
                self.assertEqual(result["status"], "BLOCKED")

    def test_parent_reports_actual_elapsed_overrun_without_extending_deadline(self):
        p = self.p
        class Child:
            pid = 123
            def __init__(self, command, **kwargs):
                kind = command[command.index("--child") + 1]
                output = Path(command[command.index("--output") + 1])
                names = ["old-active-db", "old-ordinary"] if kind == "old" else ["current-excluded"]
                p.write_json(output / "child.json", {"rows": [{"name": n, "condition_valid": True} for n in names],
                    "cleanup_uncertified": False, "source_commit": p.PINS[kind]["commit"]})
            def wait(self, timeout):
                return 0
        clock = iter([100.0, 101.0, 102.0, 150.0])
        with patch.object(p, "require_windows"), patch.object(p, "verify_files", return_value={}), \
             patch.object(p.time, "monotonic", side_effect=lambda: next(clock)), \
             patch.object(p.subprocess, "Popen", side_effect=Child):
            result = p.run_parent(self.root, self.root, self.root / "out")
        self.assertEqual(result["elapsed_seconds"], 50)
        self.assertEqual(result["deadline_seconds"], 45)
        self.assertEqual(result["status"], "BLOCKED")

    def test_one_parent_deadline_and_exact_child_commands(self):
        p = self.p
        commands = []
        class Child:
            pid = 123
            returncode = 0
            def __init__(self, command, **kwargs):
                commands.append(command)
                kind = command[command.index("--child") + 1]
                output = Path(command[command.index("--output") + 1])
                names = ["old-active-db", "old-ordinary"] if kind == "old" else ["current-excluded"]
                p.write_json(output / "child.json", {"rows": [{"name": n, "condition_valid": True} for n in names],
                    "cleanup_uncertified": False, "source_commit": p.PINS[kind]["commit"]})
            def wait(self, timeout):
                self.timeout = timeout
                return 0
            def poll(self):
                return 0
        clock = iter([100.0, 101.0, 103.0, 107.0, 108.0, 109.0])
        with patch.object(p, "require_windows"), patch.object(p, "verify_files", return_value={}), \
             patch.object(p.time, "monotonic", side_effect=lambda: next(clock)), \
             patch.object(p.subprocess, "Popen", side_effect=Child):
            result = p.run_parent(self.root, self.root, self.root / "out")
        self.assertEqual(result["deadline_seconds"], 45)
        self.assertEqual(result["status"], "CAUSAL_CLASS_REPRODUCED")
        self.assertEqual(len(commands), 2)
        self.assertTrue(all(c[1] == "-I" for c in commands))
        self.assertEqual([c[c.index("--child") + 1] for c in commands], ["old", "current"])
        self.assertFalse(result["historical_main_error_uniquely_attributed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
