from __future__ import annotations

from pathlib import Path
from openkyrozen.app.resources import plugin_directory
from typing import Any


def _record_plugin_event(self, event_type: str, payload: dict[str, Any]) -> None:
    """Persist plugin runtime diagnostics without allowing them to break work."""
    try:
        self.memory_bank.store.append_event(
            event_type, payload, user_id=self.memory_bank.user_id,
            workspace_id=self.memory_bank.workspace_id, session_id=self.memory_bank.session_id,
        )
    except Exception:
        pass


def _plugin_runtime_for_surface(self, surface: str | None = None):
    active_plugins = self._get_workspace_root() / "plugins"
    packaged_plugins = plugin_directory()
    return self.get_plugin_runtime(
        surface or self._EXECUTION_SURFACE, event_recorder=self._record_plugin_event,
        plugin_dirs=(active_plugins, packaged_plugins),
    )
