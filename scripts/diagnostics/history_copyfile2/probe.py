#!/usr/bin/env python3
"""Bounded Windows-only CopyFile2/SQLite causal-class diagnostic. Never a release gate."""
from __future__ import annotations
import argparse
import ctypes as C
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

PINS = {
    "old": {
        "commit": "0f172f9fe692ff8fafcc0a15a72fe51eaf67e977",
        "files": {
            "openkyrozen/__init__.py": "10192dc3bc79f0053def6ba6ab5580672d1fe25e34594f5742156f0645278730",
            "openkyrozen/workspace/__init__.py": "dc7f1316a38624204fea8cf9cf1db8e6c18fe0c5ee11c0d82d76672f6ffa5342",
            "openkyrozen/workspace/history.py": "9af7b209620a196d8599b9454787ae041dd6cef386a0a883edb8bcbda71b544f",
            "openkyrozen/workspace/history_port.py": "4cd2246ff3fe38649969d515f9c3fb2347ede031f3113a82c7caa989ecd4ead8",
            "openkyrozen/agent/__init__.py": "cdbf8920f2980bad0f60126e905f1dce53913f4c5e93b554cc50d1abb487d4c0",
            "openkyrozen/agent/events.py": "17c66f79cad0f7daba85243ed7c1bd5726399f52dea640efe59292460ef0923e",
            "openkyrozen/agent/conversation.py": "1296780b2ea6191f20f4c2a2734e3a842a3f7e18817983a6892deabf01daac8a",
            "openkyrozen/agent/compaction.py": "edca21600ab5c8e74c58ba86d93672b2a30798655daa51a4d56ea383d52496de",
            "openkyrozen/persistence/__init__.py": "ac990d9d3ef7acbc0c327f430fd9c76309f50a4ca4cfaef0abd6c231c6e7923a",
            "openkyrozen/persistence/models.py": "22269c3bd9b5d16850f290f2efcc47efbff7634f1604535f02a419b677e049ec"
        }
    },
    "current": {
        "commit": "8c595f909859b0fda10ff4fdb6e25f4ccb25c96e",
        "files": {
            "openkyrozen/__init__.py": "d7a41b901d9a356596af49604d7a24dceb812e01871ead0b777fb4b3da239f72",
            "openkyrozen/workspace/__init__.py": "dc7f1316a38624204fea8cf9cf1db8e6c18fe0c5ee11c0d82d76672f6ffa5342",
            "openkyrozen/workspace/history.py": "d61996a2ae0768dc297adfc30b4c8c4dc90069e6078db3ff313168baea8518db",
            "openkyrozen/workspace/history_port.py": "f9b0c78229c52a4ede68b544c66892f48d9b9d2e1358927038efe87fdb9547ac",
            "openkyrozen/agent/__init__.py": "cdbf8920f2980bad0f60126e905f1dce53913f4c5e93b554cc50d1abb487d4c0",
            "openkyrozen/agent/events.py": "17c66f79cad0f7daba85243ed7c1bd5726399f52dea640efe59292460ef0923e",
            "openkyrozen/agent/conversation.py": "1296780b2ea6191f20f4c2a2734e3a842a3f7e18817983a6892deabf01daac8a",
            "openkyrozen/agent/compaction.py": "bb1633a2198990583966f3a99b86e72f89c34203e94f0b7c2e40a976f2b5035b",
            "openkyrozen/persistence/__init__.py": "ac990d9d3ef7acbc0c327f430fd9c76309f50a4ca4cfaef0abd6c231c6e7923a",
            "openkyrozen/persistence/models.py": "22269c3bd9b5d16850f290f2efcc47efbff7634f1604535f02a419b677e049ec"
        }
    }
}
DEADLINE_SECONDS = 45
HRESULT = C.c_int32
DWORD = C.c_uint32
HANDLE = C.c_void_p
U64 = C.c_uint64
CALLBACK = getattr(C, "WINFUNCTYPE", C.CFUNCTYPE)(C.c_int32, C.c_void_p, C.c_void_p)


class StreamStarted(C.Structure):
    _fields_ = [("dwStreamNumber", DWORD), ("dwReserved", DWORD),
                ("hSourceFile", HANDLE), ("hDestinationFile", HANDLE),
                ("uliStreamSize", U64), ("uliTotalFileSize", U64)]


class CopyInfo(C.Union):
    # SDK's largest union member is ChunkFinished: 2 DWORD, 2 HANDLE, 6 ULARGE_INTEGER.
    # Only StreamStarted is inspected. The full 72-byte union is still reserved.
    _fields_ = [("StreamStarted", StreamStarted), ("full_union", C.c_ubyte * 72)]


class CopyMessage(C.Structure):
    _fields_ = [("Type", C.c_int32), ("dwPadding", DWORD), ("Info", CopyInfo)]


class CopyParameters(C.Structure):
    _fields_ = [("dwSize", DWORD), ("dwCopyFlags", DWORD), ("pfCancel", C.POINTER(C.c_int32)),
                ("pProgressRoutine", CALLBACK), ("pvCallbackContext", C.c_void_p)]


def sha256(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def verify_files(root, expected):
    root = Path(root).resolve(strict=True)
    observed = {}
    for relative, digest in expected.items():
        path = root / relative
        if path.is_symlink() or not path.is_file() or path.resolve().relative_to(root).as_posix() != relative:
            raise RuntimeError("invalid source file: " + relative)
        raw = path.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        style = "LF"
        if actual != digest:
            canonical = raw.replace(b"\r\n", b"\n")
            if (b"\r" in canonical or raw != canonical.replace(b"\n", b"\r\n")
                    or hashlib.sha256(canonical).hexdigest() != digest):
                raise RuntimeError("source hash/newline mismatch: " + relative)
            style = "CRLF"
        observed[relative] = {"physical_sha256": actual, "canonical_sha256": digest,
                              "newline_style": style}
    return observed


def load_history(source, kind):
    if any(name == "openkyrozen" or name.startswith("openkyrozen.") for name in sys.modules):
        raise RuntimeError("source modules already imported; fresh interpreter required")
    source = Path(source).resolve(strict=True)
    expected = PINS[kind]["files"]
    observed = verify_files(source, expected)
    sys.path.insert(0, str(source))
    # Do not read stale source-tree bytecode or write into the selected checkout.
    sys.dont_write_bytecode = True
    previous_cache_prefix = sys.pycache_prefix
    with tempfile.TemporaryDirectory(prefix="copyfile2-import-") as cache:
        sys.pycache_prefix = cache
        try:
            module = importlib.import_module("openkyrozen.workspace.history")
        finally:
            sys.pycache_prefix = previous_cache_prefix
    imported = {}
    for name, value in tuple(sys.modules.items()):
        if name == "openkyrozen" or name.startswith("openkyrozen."):
            path = Path(value.__file__).resolve(strict=True)
            relative = path.relative_to(source).as_posix()
            if relative not in expected:
                raise RuntimeError("unexpected selected-source import: " + name)
            if verify_files(source, {relative: expected[relative]})[relative] != observed[relative]:
                raise RuntimeError("selected source changed during import: " + name)
            imported[relative] = observed[relative]
    if set(imported) != set(expected):
        raise RuntimeError("selected-source import closure changed")
    return module, imported


def require_windows():
    if os.name != "nt" or C.sizeof(HANDLE) != 8:
        raise RuntimeError("Windows x64 is required")
    if platform.python_version() != "3.12.10" or sqlite3.sqlite_version != "3.49.1":
        raise RuntimeError("pinned Python3.12.10/SQLite3.49.1 required")


class WindowsAPI:
    def __init__(self):
        require_windows()
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.kernel.CopyFile2.argtypes = [C.c_wchar_p, C.c_wchar_p, C.POINTER(CopyParameters)]
        self.kernel.CopyFile2.restype = HRESULT
        self.kernel.GetCurrentThreadId.argtypes = []
        self.kernel.GetCurrentThreadId.restype = DWORD
        self.kernel.CreateFileW.argtypes = [
            C.c_wchar_p, DWORD, DWORD, C.c_void_p, DWORD, DWORD, HANDLE]
        self.kernel.CreateFileW.restype = HANDLE
        self.kernel.CloseHandle.argtypes = [HANDLE]
        self.kernel.CloseHandle.restype = C.c_int32

    def thread_id(self):
        return int(self.kernel.GetCurrentThreadId())

    def copy(self, source, destination, callback):
        params = CopyParameters(C.sizeof(CopyParameters), 8, None, callback, None)
        return int(self.kernel.CopyFile2(str(source), str(destination), C.byref(params)))

    def write_access_probe(self, path):
        # A short-lived WRITE open, not a held/fabricated interference handle.
        C.set_last_error(0)
        handle = self.kernel.CreateFileW(str(path), 0x40000000, 7, None, 3, 0x80, None)
        if handle == C.c_void_p(-1).value:
            return {"last_error": int(C.get_last_error()), "handle_closed": True}
        closed = bool(self.kernel.CloseHandle(handle))
        return {"last_error": 0, "handle_closed": closed,
                "close_error": 0 if closed else int(C.get_last_error())}


def sqlite_write_probe(path, *, commit=False):
    result = {"attempted": True, "connect_success": False, "write_success": False, "connection_closed": False,
              "sqlite_errorcode": None, "sqlite_errorname": None, "transaction_committed": False,
              "start_performance_counter_ns": time.perf_counter_ns()}
    connection = None
    try:
        # Default CPython READWRITE|CREATE. No readonly URI/flags in the native probe.
        connection = sqlite3.connect(str(path), isolation_level=None)
        result["connect_success"] = True
        result["read_value"] = connection.execute("SELECT value FROM probe").fetchone()[0]
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO probe VALUES(2)")
        if commit:
            connection.commit()
            result["transaction_committed"] = True
        else:
            connection.rollback()
        result["write_success"] = True
    except sqlite3.Error as error:
        result.update(sqlite_errorcode=getattr(error, "sqlite_errorcode", None),
                      sqlite_errorname=getattr(error, "sqlite_errorname", None),
                      error_class=type(error).__name__, error_message=str(error)[:256])
    finally:
        try:
            if connection is not None:
                connection.close()
            result["connection_closed"] = True
        except BaseException as error:
            result["close_error_class"] = type(error).__name__
        result["end_performance_counter_ns"] = time.perf_counter_ns()
    return result


class CopyObserver:
    def __init__(self, api, database, target):
        self.api, self.database = api, Path(database)
        self.target = Path(target).resolve()
        self.copies = []

    def __call__(self, source, destination, *, follow_symlinks=True):
        if not follow_symlinks:
            raise RuntimeError("fixture unexpectedly contains a symlink")
        source, destination = Path(source), Path(destination)
        selected = source.resolve() == self.target
        row = {"source": str(source), "destination": str(destination), "flags": 8,
               "probe": None, "hresult": None, "callback_error": None, "qualified": False,
               "callback_count": 0, "cleanup_uncertified": False, "start_performance_counter_ns": time.perf_counter_ns()}
        self.copies.append(row)

        @CALLBACK
        def callback(pointer, context):
            try:
                message = C.cast(pointer, C.POINTER(CopyMessage)).contents
                row["callback_count"] += 1
                if selected and message.Type == 3 and row["probe"] is None:
                    stream = message.Info.StreamStarted
                    if not stream.hSourceFile or not stream.hDestinationFile:
                        raise RuntimeError("native stream handles unavailable")
                    row.update(source_handle=int(stream.hSourceFile),
                               destination_handle=int(stream.hDestinationFile),
                               native_tid=self.api.thread_id(),
                               callback_performance_counter_ns=time.perf_counter_ns())
                    row["write_access"] = self.api.write_access_probe(self.database)
                    if not row["write_access"]["handle_closed"]:
                        raise RuntimeError("owned write probe handle cleanup failed")
                    row["probe"] = sqlite_write_probe(self.database)
                    if not row["probe"]["connection_closed"]:
                        raise RuntimeError("probe SQLite connection cleanup failed")
                return 0
            except BaseException as error:
                # Never unwind a Python exception across the FFI boundary.
                row["callback_error"] = type(error).__name__
                row["cleanup_uncertified"] = True
                return 1

        try:
            row["hresult"] = self.api.copy(source, destination, callback)
        except BaseException as error:
            row["native_error_class"] = type(error).__name__
            row["cleanup_uncertified"] = True
            raise
        finally:
            row["end_performance_counter_ns"] = time.perf_counter_ns()
        row["qualified"] = (row["hresult"] == 0 and row["callback_error"] is None
                            and (not selected or row["probe"] is not None))
        if not row["qualified"]:
            raise RuntimeError("CopyFile2 callback/native completion did not qualify")
        return str(destination)


def run_case(module, api, directory, name):
    directory = Path(directory)
    workspace, state = directory / "workspace", directory / "history-state"
    workspace.mkdir(parents=True)
    database = workspace / "state.sqlite3"
    ordinary = workspace / "ordinary.txt"
    ordinary.write_bytes(b"ordinary source\n")
    connection = sqlite3.connect(database, isolation_level=None)
    try:
        connection.execute("CREATE TABLE probe(value INTEGER)")
        connection.execute("INSERT INTO probe VALUES(1)")
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    if journal_mode != "delete":
        raise RuntimeError("clean rollback-journal fixture required")
    # Snapshot-only port: real source implementation consumes path, no fake SQLite.
    manager = module.HistoryManager(SimpleNamespace(path=database), workspace, state,
                                   source_scope_id="source-" + "a" * 32, session_id="fixture")
    target = database if name == "old-active-db" else ordinary
    observer = CopyObserver(api, database, target)
    original = module.shutil.copy2
    row = {"name": name, "database": str(database), "ordinary": str(ordinary),
           "copies": observer.copies, "condition_valid": False, "cleanup_uncertified": False,
           "fixture_journal_mode": journal_mode}
    try:
        module.shutil.copy2 = observer
        snapshot, manifest, summary = manager._snapshot("hist_" + "b" * 32)
        row.update(snapshot=snapshot, manifest=manifest, summary=summary)
    except BaseException as error:
        row.update(error_class=type(error).__name__, error_message=str(error)[:256])
    finally:
        module.shutil.copy2 = original
    probes = [copy for copy in observer.copies if copy["probe"] is not None]
    row["cleanup_uncertified"] = any(
        copy["cleanup_uncertified"] or (copy.get("write_access") is not None
                                       and not copy["write_access"]["handle_closed"])
        or (copy["probe"] is not None and not copy["probe"]["connection_closed"])
        for copy in observer.copies)
    if row["cleanup_uncertified"]:
        row["post_copy_write"] = {"attempted": False, "reason": "cleanup_uncertified",
                                 "transaction_committed": False}
    else:
        row["post_copy_write"] = sqlite_write_probe(database, commit=True)
        row["cleanup_uncertified"] = not row["post_copy_write"]["connection_closed"]
    family = [Path(str(database) + suffix) for suffix in ("", "-wal", "-shm", "-journal")]
    row["family_excluded"] = [bool(manager._excluded(path)) for path in family]
    database_copied = any(Path(copy["source"]) == database for copy in observer.copies)
    row["database_copied"] = database_copied
    if len(probes) == 1 and "manifest" in row and row["post_copy_write"]["transaction_committed"]:
        observed = probes[0]
        if name == "old-active-db":
            row["condition_valid"] = (
                database_copied and observed["probe"]["connect_success"]
                and observed["probe"]["sqlite_errorcode"] == sqlite3.SQLITE_READONLY
                and not observed["probe"]["write_success"]
                and observed["write_access"]["last_error"] == 32)
        elif name == "old-ordinary":
            row["condition_valid"] = database_copied and observed["probe"]["write_success"]
        elif name == "current-excluded":
            row["condition_valid"] = (
                not database_copied and all(row["family_excluded"])
                and all("state.sqlite3" + suffix not in row["manifest"]
                        for suffix in ("", "-wal", "-shm", "-journal"))
                and row["manifest"].get("ordinary.txt", {}).get("kind") == "file"
                and observed["probe"]["write_success"])
    row["condition_valid"] = bool(row["condition_valid"] and not row["cleanup_uncertified"])
    return row


def assess(rows):
    expected = ["old-active-db", "old-ordinary", "current-excluded"]
    valid = [row.get("name") for row in rows] == expected and all(
        row.get("condition_valid") is True for row in rows)
    return {"status": "CAUSAL_CLASS_REPRODUCED" if valid else "BLOCKED",
            "r260_status": "BLOCKED", "historical_main_error_uniquely_attributed": False,
            "all_six_historical_reviewers_must_exit_zero": True}


def run_child(source, kind, output):
    require_windows()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    result = {"source_commit": PINS[kind]["commit"], "rows": [], "cleanup_uncertified": False}
    names = ["old-active-db", "old-ordinary"] if kind == "old" else ["current-excluded"]
    try:
        module, observed = load_history(source, kind)
        result["imported_source_hashes"] = observed
        result["source_identity_scope"] = "all imported OpenKyrozen modules"
        result["pid"] = os.getpid()
        result["python"] = platform.python_version()
        result["sqlite"] = sqlite3.sqlite_version
        api = WindowsAPI()
        for name in names:
            try:
                row = run_case(module, api, output / name, name)
            except BaseException:
                result["cleanup_uncertified"] = True
                raise
            result["rows"].append(row)
            result["cleanup_uncertified"] |= row["cleanup_uncertified"]
            if result["cleanup_uncertified"]:
                break
    except BaseException as error:
        result.update(error_class=type(error).__name__, error_message=str(error)[:256])
    write_json(output / "child.json", result)
    complete = ("error_class" not in result and not result["cleanup_uncertified"]
                and [row["name"] for row in result["rows"]] == names
                and all(row["condition_valid"] is True for row in result["rows"]))
    return 0 if complete else 1


def child_environment(home):
    # No inherited provider/config/credential variables are transported.
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP"}}
    env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "AppData"),
               LOCALAPPDATA=str(home / "LocalAppData"), PYTHONDONTWRITEBYTECODE="1")
    return env


def run_parent(old_source, current_source, output):
    deadline = time.monotonic() + DEADLINE_SECONDS
    require_windows()
    output = Path(output).absolute()
    output.mkdir(parents=True, exist_ok=False)
    result = dict(assess([]), deadline_seconds=DEADLINE_SECONDS, children=[],
                  rows=[], cleanup_uncertified=False, probe_sha256=sha256(__file__))
    try:
        for kind, source in [("old", old_source), ("current", current_source)]:
            source = Path(source).resolve(strict=True)
            verify_files(source, PINS[kind]["files"])
            work = output / kind
            work.mkdir()
            home = work / "private-home"
            home.mkdir()
            command = [sys.executable, "-I", str(Path(__file__).resolve()), "--child", kind,
                       "--source", str(source), "--output", str(work)]
            record = {"kind": kind, "source": str(source), "phase": "starting"}
            result["children"].append(record)
            with (work / "output.log").open("xb") as log:
                child = subprocess.Popen(command, cwd=work, env=child_environment(home),
                                         stdout=log, stderr=subprocess.STDOUT)
                record["pid"] = child.pid
                try:
                    remaining = deadline - time.monotonic() - 2
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, 0)
                    record["returncode"] = child.wait(timeout=remaining)
                    record["owned_child_exit_observed"] = True
                    record["phase"] = "exited"
                except BaseException as error:
                    record.update(phase="timeout" if isinstance(error, subprocess.TimeoutExpired)
                                  else "interrupted", error_class=type(error).__name__)
                    record["cleanup_uncertified"] = True
                    try:
                        killed = subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                            stdout=log, stderr=subprocess.STDOUT,
                            timeout=max(0.001, min(1, deadline - time.monotonic())))
                        record["taskkill_returncode"] = killed.returncode
                        record["returncode"] = child.wait(
                            timeout=max(0.001, min(1, deadline - time.monotonic())))
                        record["owned_child_exit_observed"] = child.poll() is not None
                        record["cleanup_uncertified"] = not (
                            killed.returncode == 0 and record["owned_child_exit_observed"])
                    except BaseException as cleanup_error:
                        record["cleanup_error_class"] = type(cleanup_error).__name__
                    result["cleanup_uncertified"] |= record["cleanup_uncertified"]
                    break
            child_result = json.loads((work / "child.json").read_text(encoding="utf-8"))
            if child_result["source_commit"] != PINS[kind]["commit"]:
                raise RuntimeError("child source identity mismatch")
            rows = child_result.get("rows")
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise RuntimeError("child rows missing or malformed")
            result["rows"].extend(rows)
            result["cleanup_uncertified"] |= child_result["cleanup_uncertified"]
            names = ["old-active-db", "old-ordinary"] if kind == "old" else ["current-excluded"]
            record["receipt_admitted"] = (
                type(record["returncode"]) is int and record["returncode"] == 0
                and "error_class" not in child_result
                and [row.get("name") for row in rows] == names
                and all(row.get("condition_valid") is True for row in rows))
            if result["cleanup_uncertified"] or not record["receipt_admitted"]:
                break
    except BaseException as error:
        result.update(error_class=type(error).__name__, error_message=str(error)[:256])
    result.update(assess(result["rows"]))
    result["elapsed_seconds"] = DEADLINE_SECONDS - (deadline - time.monotonic())
    result["deadline_exceeded"] = result["elapsed_seconds"] > DEADLINE_SECONDS
    if (result["cleanup_uncertified"] or result["deadline_exceeded"] or "error_class" in result
            or len(result["children"]) != 2
            or not all(child.get("receipt_admitted") is True for child in result["children"])):
        result["status"] = "BLOCKED"
    write_json(output / "result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-source", type=Path)
    parser.add_argument("--current-source", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--child", choices=("old", "current"), help=argparse.SUPPRESS)
    parser.add_argument("--source", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        if args.source is None:
            parser.error("child source required")
        return run_child(args.source, args.child, args.output)
    if args.old_source is None or args.current_source is None:
        parser.error("both pinned source roots required")
    result = run_parent(args.old_source, args.current_source, args.output)
    print(json.dumps({key: result[key] for key in ("status", "r260_status", "cleanup_uncertified")}))
    return 0 if result["status"] == "CAUSAL_CLASS_REPRODUCED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
