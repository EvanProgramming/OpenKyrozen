# Legacy protocol compatibility

OpenKyrozen keeps textual `Action`, `Plan`, `TaskList`, `TaskDone`, `AskUser`, and `PlanProposal` handling inside `LegacyProtocolAdapter`. It supports older providers and existing callers while native-capable providers use structured tool calls. The native AgentEngine does not parse these text controls. Read-only Plan mode remains on the adapter until its approval controls have a typed contract.

When a legacy control is actually recognized, the runtime emits a `legacy.protocol_used` event containing only marker names. It never includes the model response or tool arguments. Operators can use these events to determine whether legacy behavior remains in use.

Review legacy usage during each release cycle. Keep the adapter while supported providers or clients still depend on it. Before removal, require two consecutive release cycles with no observed use, publish a migration guide at least one release in advance, and remove the adapter only in a major release. This policy keeps compatibility changes tied to measured usage rather than a calendar deadline.
