"""Repository operations consumed by HistoryStore."""
from __future__ import annotations
from typing import Protocol, Any
from dataclasses import dataclass

class HistoryError(RuntimeError):
    """Raised when a history operation cannot be completed safely."""


@dataclass(frozen=True)
class TurnToken:
    parent_id: str



class HistoryStore(Protocol):
    def append_event(self, event_type: str, payload: Any, *, user_id: str='local', workspace_id: str='default', session_id: str | None=None, task_id: str | None=None):
        ...
    def history_head(self, *, user_id: str='local', workspace_id: str='default', session_id: str):
        ...
    def history_node(self, node_id: str, *, user_id: str='local', workspace_id: str='default', session_id: str | None=None):
        ...
    def insert_history_node(self, node: dict[str, Any], *, set_head: bool=False, expected_head: str | None=None):
        ...
    def list_history_nodes(self, *, user_id: str='local', workspace_id: str='default', session_id: str, include_recovery: bool=False, limit: int=10000):
        ...
    def replace_tasks(self, tasks: list[dict[str, Any]], *, user_id: str='local', workspace_id: str='default', session_id: str | None=None):
        ...
    def set_history_head(self, node_id: str, *, user_id: str='local', workspace_id: str='default', session_id: str, expected_head: str | None=None):
        ...
