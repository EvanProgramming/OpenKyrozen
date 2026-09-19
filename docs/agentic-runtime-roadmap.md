# Agentic runtime roadmap

OpenKyrozen now has one durable interaction state machine across the classic
CLI, Bubble Tea TUI, web UI, REST, and SSE. Ask and Plan are read-only; accepted
plans become evidence-gated `TaskManager` work in Agent mode. The following work
is intentionally deferred rather than hidden behind speculative abstractions:

1. **Active-run steering, cancellation, and queuing** — add when a turn can be
   safely interrupted between receipts without leaving a tool or durable task in
   an ambiguous state.
2. **Provider-native typed tool calls** — add per provider after parity tests
   prove the native schema preserves the current fail-closed control protocol.
3. **OS-level sandboxing** — add platform-specific process/filesystem isolation
   beneath capability tokens; capability labels are policy, not an OS sandbox.
4. **Asynchronous subagent orchestration** — add only with scoped budgets,
   independent receipts, cancellation, and deterministic parent reconciliation.
5. **Long-run checkpoints and context compaction** — add resumable checkpoints
   that retain plan/question versions, evidence, and outstanding approvals.
6. **Additional delivery channels** — reuse the interaction envelope only where
   a channel can render questions/plans and correlate responses safely.

MCP remains intentionally non-interactive. Tool approvals remain distinct from
clarification questions on every future surface.
