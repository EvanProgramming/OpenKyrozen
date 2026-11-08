# CopyFile2/SQLite causal-class regression — managed v2

Scope: diagnostic artifacts only; product8c595f909859b0fda10ff4fdb6e25f4ccb25c96e remains unchanged. No Windows/VM execution or workflow dispatch has occurred. Original native42513800271 and R260 remain BLOCKED.

## Revision boundary

This v2 preserves the complete v1 freeze in the sibling history-copyfile2-regression namespace. Peer review demonstrated that v1's real run_child loop referenced parent-only variables, hid a NameError after its first row, and could return0 despite the error. Its18 offline tests did not cover that loop; they are not native-child proof. Peer review also found a post-copy commit after uncertified callback cleanup and clipped elapsed reporting.

V2 adds direct actual-child-loop tests, strict parent exit/error/row admission, cleanup-before-any-post-copy-work, and unclipped elapsed/overrun rejection. The original source93ba9720… and all original RED/GREEN records remain unchanged. No native Windows execution has occurred in either version.

## Interpretation fixed before native execution

This diagnostic has exactly three counterfactuals: historical active-primary-database copy, historical ordinary-file copy control, and current exclusion. It can establish a native CopyFile2/SQLite primary-database sharing mechanism. It cannot uniquely attribute the original final main-thread journal error: the highlighted primary fallback is workerTID7064, whereas all67 mainTID1364 primary opens succeeded; main-thread SHM/WAL sharing failures are separately retained. Exact final per-connection mode/extended subcode is unknown. No family-axis extension, fake WAL, forced-readonly native flag, changed selected source, success waiver or historical-trigger claim is authorized here.

Even CAUSAL_CLASS_REPRODUCED keeps r260_status=BLOCKED and historical_main_error_uniquely_attributed=false. The original all-six-historical-reviewers-exit0 criterion remains unchanged. This tool is not a release gate.

## Concrete invocation and source binding

After Root/peer review only, the proposed diagnostic-repository locations are scripts/diagnostics/history_copyfile2/{probe.py,test_probe.py,PROTOCOL.md}. Main is not modified.

Windows PowerShell, with public exact source checkouts:
~~~powershell
$old = (Resolve-Path historical-product).Path
$current = (Resolve-Path product).Path
$out = Join-Path $PWD 'history-copyfile2-evidence'
python -I scripts/diagnostics/history_copyfile2/probe.py --old-source $old --current-source $current --output $out
~~~

The output path must be new. The parent uses two fresh isolated interpreters under one45-second parent deadline, including source validation, import, fixture and native operations, with two seconds reserved for bounded timeout cleanup. It does not mix historical/current modules. Child logs and JSON stay under this fresh output.

Old source is0f172f9fe692ff8fafcc0a15a72fe51eaf67e977; current is8c595f909859b0fda10ff4fdb6e25f4ccb25c96e. Before import, ten exact source files are SHA256-checked against their LF blobs, accepting only the exact LF bytes or the exact uniform Git CRLF conversion. Physical and canonical hashes/newline style are retained; mixed/newline-only transformations or altered content are rejected. After import every loaded OpenKyrozen module must belong to that same pinned ten-module closure and keep the same physical identity. A fresh empty bytecode-cache prefix prevents stale pyc reads; bytecode writing is disabled, leaving the selected checkout unchanged. The source identity receipt explicitly covers imported code; the workflow must additionally checkout/verify the declared whole commits. No HistoryManager code is extracted, rewritten or compiled into a substitute. The snapshot-only path port supplies a real private database path without an unrelated persistent connection owner. The actual selected HistoryManager._snapshot and _excluded implementations run.

Production requires Windows x64, Python3.12.10 and SQLite3.49.1. Root's proposed dedicated job must set COPYFILE2_CURRENT_SOURCE to the absolute product checkout for its unit-test caller, install the unchanged145 historical dependency pins and keep all original3/10/5,45/300-second and512MiB limits unchanged. This tool does not run WPR, xperf, six fixtures, a provider, an application or any ledger. Its fixed fixtures are a small real SQLite database and16-byte ordinary file per case; no unbounded copy input is accepted. No new storage quota or larger sampling allowance is introduced. Native job/outer artifact accounting remains Root-owned.

## Native operations

The real native CopyFile2 call receives the original source/destination paths and flag8 (COPY_FILE_ALLOW_DECRYPTED_DESTINATION), matching CPython3.12.10 copy2's normal path. A synchronous progress callback on the first STREAM_STARTED message executes the probe while native source/destination handles are present. There is no sleep, helper thread, held synthetic interference handle, large-file timing trick or general execution service. If the callback is unavailable or native behavior does not match, the condition is false/BLOCKED.

The x64 SDK layout uses fixed32-bit DWORD/HRESULT/enum, pointer-sized HANDLE, and64-bit ULARGE_INTEGER. COPYFILE2_MESSAGE has8-byte header plus72-byte largest union member, total80bytes; StreamStarted's handles begin at union offsets8/16. COPYFILE2_EXTENDED_PARAMETERS is32bytes with callback at offset16. Only the documented StreamStarted member is read; the remaining union is opaque reserved storage. ctypes.WINFUNCTYPE is used on Windows. The offline ctypes layout check is not a Windows ABI execution claim.

The callback creates an ordinary default sqlite3 connection (CPython READWRITE|CREATE), reads the seed value, attempts BEGIN IMMEDIATE/INSERT, and rolls back. Actual sqlite_errorcode/name, connection closure, paths, native TID, source/destination handles, callback ordering, HRESULT and performance-counter timestamps are retained. An additional short-lived GENERIC_WRITE/share7/OPEN_EXISTING CreateFileW probe records actual error32 or success; it does not hold a manufactured conflict. Any valid probe-owned handle is closed immediately.

- old-active-db must actually copy state.sqlite3. During its callback, the default SQLite connect must succeed but the write must produce base SQLITE_READONLY, and the write-access probe must return32. A fresh post-copy transaction must commit.
- old-ordinary uses the same native API/flag/callback/SQLite probe while copying ordinary.txt. The callback write must succeed; the historical snapshot still copies its database outside that probe.
- current-excluded must never copy the database, must exclude its four canonical family paths, must retain/copy ordinary.txt, and must successfully perform the ordinary-file callback write and a fresh post-copy commit. Existing source tests cover actual sidecar fixtures; this native primary test does not create bogus WAL/SHM bytes.

CopyFile2-owned handles are observed, never CloseHandle'd by this diagnostic. All SQLite connections and write-probe-owned handles are closed. A Python exception never unwinds through a ctypes callback: it is retained, returns CANCEL, then causes a normal Python failure after native return. Callback/handle cleanup uncertainty remains explicit and propagates to parent even on child exit1. No later child starts after an uncertain cleanup or timeout. A clean condition failure inside the old child may still collect its second safe counterfactual. Before launching the current child, the parent requires exact integer exit0, no child error and complete exact row membership/validity. Missing, contradictory or error-bearing receipts remain BLOCKED even when their available rows claim success. No failed data is relabeled valid.

If a native child stalls, the existing parent deadline triggers bounded taskkill /PID OWN_CHILD /T /F and bounded wait, retaining actual return status. The receipt distinguishes child exit observation from uncertainty and does not certify unrelated processes. It never sends a guessed PID as a process-group ID or issues a global stop. No nested session is created. A timeout/interrupt always leaves mechanism status BLOCKED even if process cleanup succeeds. Elapsed time is the actual monotonic interval without a45-second clamp; an observed overrun is retained and BLOCKED without extending the limit.

## Offline validation and retained evidence

Private-HOME test command, with cwd set to current exact public source checkout:
~~~text
<python3.12> -I <managed-or-staged-path>/test_probe.py
~~~
The optional COPYFILE2_CURRENT_SOURCE environment variable supplies that same public current source root when cwd differs. On the proposed Windows workflow set it to the absolute product checkout before running these tests.

The v2 suite has24 methods. It uses actual SQLite for transaction/readonly-error handling, real current-source imports and a real current snapshot with a fake WinAPI shim. Fake callbacks are explicitly offline boundary tests, never native evidence. The forced-readonly URI exists only in a unit test for error projection; the native implementation never passes it.

The sibling v1 namespace retains initial missing-implementation/cleanup/commit/CRLF RED records and its18-test GREEN. V2 retains the complete7-method/10-assertion peer-regression RED, a separate unknown-partial-case-cleanup RED, and the final24-test GREEN. The direct child tests exercise the actual run_child loop with controlled case/API dependencies, not a replacement child function. The real-source snapshot test injects an uncertified native write-handle cleanup and asserts no later SQLite call; an ordinary clean native failure still permits a safe post-copy commit. The new real-source snapshot test additionally confirms family exclusion, ordinary copying, and restoration of the temporary copy seam. No broad product suite was run for these managed-only artifacts.

## Primary API/source references

- [CPython3.12.10 copy2](https://raw.githubusercontent.com/python/cpython/v3.12.10/Lib/shutil.py)
- [CPython3.12.10 SQLite open flags](https://raw.githubusercontent.com/python/cpython/v3.12.10/Modules/_sqlite/connection.c)
- [SQLite3.49.1 Windows VFS](https://raw.githubusercontent.com/sqlite/sqlite/version-3.49.1/src/os_win.c)
- [CopyFile2 parameters](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-copyfile2_extended_parameters)
- [CopyFile2 callback message](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-copyfile2_message)
- [CopyFile2 callback contract](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nc-winbase-pcopyfile2_progress_routine)
- [CreateFileW sharing contract](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)

Retained raw original and forensic analyses remain immutable. See r260-resume/native-offline-gzip-3ae391e-42515358773/REPORT.md and native-8c595f9-family-hypothesis-review/REPORT.md for the unresolved actual main-family edge. This diagnostic contains no secret/config/chat lookup and performs no provider/model/network call.
