# Durable tasks and scheduled work

This guide describes resumable work recorded by the local runtime. A normal chat turn and a durable task have different lifecycle rules: accepting a plan or requesting queued work creates a task with explicit progress and evidence rather than treating an unfinished reply as success.

## Task lifecycle

1. **Prepare:** Ask/Plan gathers context and proposes a bounded task. The user accepts the plan or explicitly requests Agent work.
2. **Run:** The task manager claims work under its scope, records tool calls and other evidence, and returns control between actions for approvals and cancellation.
3. **Verify:** A task is complete only when its completion contract has observable evidence. Coding work needs an acceptance command or test result; research needs its requested sources and output. A tool call that returns successfully is not by itself proof that the goal succeeded.
4. **Recover:** Pending tasks can be resumed after restart. Interrupted sub-agent assignments are reported and mutations are not replayed automatically. Reconcile current files and receipts before retrying any interrupted side effect.

Task IDs and records are actor/workspace/session scoped. The web task API exposes list/create/resume operations; the [runtime inventory](tool-inventory.md) gives exact routes. In the TUI use `/tasks` to inspect progress and `/agents` for delegated jobs. Cancellation stops subsequent sub-agent tools between tool calls; it cannot undo an external effect that already occurred.

## Sub-agents

For a suitable multi-part request, the coordinator can assign independent work to specialist sessions, preserve parent scope, collect structured results, and conduct a read-only peer review. Concurrency is a number of parallel execution slots: the default is four; additional assignments queue. Per-role provider and model overrides do not inherit the main provider's generic key. Provider setup, authorization and failure handling are described in [sub-agents](subagents.md).

Sub-agents share the parent's intended work context only through the declared assignment and permitted workspace. Their conclusions are evidence to evaluate, not an approval or permission token. Check `/agents` or the run detail route to distinguish complete, failed, cancelled and still-running assignments.

## Scheduled jobs

The web runtime owns a scheduler for the supported named jobs, including learning cycles, chat jobs, and task-worker processing. Schedule metadata is stored locally and scoped to actor/workspace. Use `/api/v2/schedules` to inspect and create schedules; the matching disable route disables an existing schedule. The route list is generated in [tool-inventory.md](tool-inventory.md).

Scheduling needs the web process to be running. A durable schedule record does not mean the host is a guaranteed background service, and a stopped machine cannot run a job at its scheduled time. Check job state and application logs after a restart before interpreting a delayed run as a successful execution. Avoid creating duplicate external jobs when a prior run's outcome is uncertain.

## Webhooks

The webhook routes let an authenticated server operator register a public HTTP(S) destination for allowed events. The current implementation allows `chat.completed` and `test`, caps registrations at 32, rejects URL credentials and obvious local/private IP or host names, and sets a five-second request timeout. A successful chat is not made to fail just because webhook delivery failed; failures are audited locally. The `chat.completed` body contains a bounded redacted reply summary, reply length, actor, session ID, profile, and streaming flag.

IP hostname checks cannot fully prevent DNS rebinding. Run with outbound firewall controls or an egress proxy when an attacker can control webhook registrations. Keep endpoints public and intentionally operated; do not register internal administration URLs. The registration API currently keeps its webhook list in the web service process, so treat registrations as process-local runtime state and re-check them after restart.

## Inspect and recover

Use the web UI's task and agent panels, `/api/v2/tasks`, `/api/v2/agents/runs`, and `/api/v2/agents/runs/{run_id}` to inspect status. Correct an incomplete goal or replay a failed request only after reading tool receipts and checking whether its earlier actions took effect. See [history and rollback](history-and-rollback.md) for conversation restore and [memory and storage](memory-and-storage.md) for durable state.
