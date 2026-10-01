"""Explicit application ownership and lifecycle."""
from dataclasses import dataclass
from typing import Any


@dataclass
class Application:
    runtime: Any
    tools: Any
    store: Any

    def close(self):
        sessions = [self.runtime._default_session, *self.runtime._sessions.values()]
        for session in sessions:
            coordinator = getattr(session.subagents, "coordinator", None)
            if coordinator:
                coordinator.close()
        self.runtime._technology_executor.shutdown(wait=False, cancel_futures=True)
        for workspace in self.runtime._workspaces.values():
            workspace.adapters._BROWSER.close_all()
