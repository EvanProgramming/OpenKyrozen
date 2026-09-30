"""Explicit application ownership and lifecycle."""
from dataclasses import dataclass
from typing import Any


@dataclass
class Application:
    runtime: Any
    tools: Any
    store: Any

    def close(self):
        self.runtime._technology_executor.shutdown(wait=False, cancel_futures=True)
        for workspace in self.runtime._workspaces.values():
            workspace.adapters._BROWSER.close_all()
