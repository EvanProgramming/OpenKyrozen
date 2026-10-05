# Conversation history and rollback

History preserves conversation snapshots as a tree. Each node is tied to a user actor, workspace, session, and parent version. The current head represents the active line. Creating a new change after a rollback can leave the old branch available for inspection rather than discarding it.

## Inspect history

Use `/history` in the terminal UI or the session-history routes listed in [tool-inventory.md](tool-inventory.md). The web response exposes public node summaries and snapshot metadata; it does not promise that an arbitrary filesystem state can be reconstructed from a conversation transcript alone. History records capture the state that the runtime is designed to restore, including the files it snapshotted, and the UI shows a preview before the choice is applied.

## Preview and confirm

In the TUI, choose a numbered history node to preview the target. Explicitly confirm with `/rollback NUMBER confirm` after reviewing it. The session head is updated only through the rollback operation. A stale head or a mismatched session is rejected instead of overwriting a newer branch. In the web API, the rollback endpoint is scoped to the requested session and node; authenticate and inspect its preview/result before continuing work.

Rollback restores the recorded files in scope and rewinds the selected conversation history. It does not reverse external effects such as messages, network requests, deployments, provider charges, or database changes outside the captured workspace. It also does not imply that shared memory claims, agent credentials, or another session's state have been erased. Check those stores separately.

## Recovery points and safety

Keep source-control commits and independent backups for important work. A conversation snapshot is not a replacement for Git history, off-device backup, or a migration plan. If a rollback is interrupted, inspect the workspace and current history head before retrying. Do not try to recover by editing private SQLite rows manually.

## Related state

Durable task receipts are persisted separately from conversation nodes. An interrupted task may therefore still require reconciliation even when the conversation is rolled back. See [tasks and automation](tasks-and-automation.md) and [memory and storage](memory-and-storage.md).
