# Shell tools

`run_cmd` and its `execute_terminal_command` alias accept a required `command`
and optional `cwd`, `env`, `timeout_seconds`, and `background` fields. Relative
working directories resolve under the active workspace; absolute paths and
symlinks that leave it are rejected. Environment values overlay the environment
OpenKyrozen uses to launch commands. The default timeout is 60 seconds and the
maximum is 30 minutes.

Foreground results keep `stdout`, `stderr`, `exit_code`, `duration_seconds`, and
`timed_out` as separate structured fields. The rendered text remains available
to CLI and legacy Action clients. A background call returns a session-owned
`process_id`; use `read_process` with `stream` (`stdout` or `stderr`), `offset`,
and `limit` to page its output, and `stop_process` to terminate it. Each page is
limited to 4,000 characters, and each stream retains at most one million
characters. A process handle belongs to its creating actor, workspace, and
session, remains in memory only, expires at its requested timeout (at most 30
minutes), and is stopped when the application closes. Handles cannot be
restored after restart. A session can own up to 32 handles until the application
closes so completed output remains available for paging.

The native structured API exposes all fields directly, including
`sandbox_backend` (`bubblewrap`, `sandbox-exec`, or `unavailable`). The
historical string Action and `run_cmd("...")` APIs remain supported for
foreground commands. Linux uses optional Bubblewrap; macOS uses the experimental
`sandbox-exec`; Windows has no filesystem backend. A missing backend or failed
containment probe is reported and permits execution by default. Set
`KYROZEN_SANDBOX_REQUIRED=1` in OpenKyrozen's environment to block commands when
no working backend is available. Backend startup failures after a successful
probe never fall back to unsandboxed execution. See
[security and permissions](security.md) for the filesystem rules and limitations.
