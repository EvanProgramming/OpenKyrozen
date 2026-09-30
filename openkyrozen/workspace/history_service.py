"""Bind workspace snapshots and history operations to the active session."""
from __future__ import annotations
from typing import Any
from .context import source_scope_id
from .history import HistoryError, HistoryManager

def history_manager(self, session_id: str | None = None) -> HistoryManager:
    """Return the history service bound to the active workspace and session."""
    active_session = session_id or self.memory_bank.session_id or f"surface:{self._EXECUTION_SURFACE}"
    return HistoryManager(
        self.memory_bank.store, self._get_workspace_root(), self._state_root(),
        source_scope_id=source_scope_id(self._get_workspace_root()), user_id=self.memory_bank.user_id,
        workspace_id=self.interaction_workspace_id(), session_id=active_session,
    )


def history_text(self, session_id: str | None = None) -> str:
    return self.history_manager(session_id).tree_text()


def restore_history(self, node_id: str, *, confirm: str, expected_head: str | None = None,
                    session_id: str | None = None) -> dict[str, Any]:
    """Restore one history node and rebind the local runtime to its state."""
    manager = self.history_manager(session_id)
    current = manager.current()
    if current is None:
        raise HistoryError("no history has been recorded for this conversation")
    target, recovery = manager.rollback(
        node_id, confirm=confirm, expected_head=expected_head,
        current_conversation=list(getattr(self, "short_term_memory", [])),
        current_interaction=self._interaction_controller.state(),
        current_tasks=list(self.tasks.tasks),
    )
    self.bind_interaction_scope(manager.session_id, user_id=self.memory_bank.user_id)
    if self._project_graph is not None:
        try:
            self._project_graph.refresh_async(callback=None)
        except Exception:
            pass
    return {"target": target, "recovery": recovery}
