"""Callable inbound boundaries owned by the agent use case."""
from typing import Callable, Any, Protocol

EventSink = Callable[[dict[str, Any]], None]
Approval = Callable[[str, str], bool]

class InteractionStore(Protocol):
    def append_event(self, event_type: str, payload: Any, **scope) -> str: ...
    def list_events(self, event_type: str | None = None, **scope) -> list[dict]: ...
