"""Callable inbound boundaries owned by the agent use case."""
from typing import Callable, Any, Protocol
from openkyrozen.agent.events import RuntimeEvent

EventSink = Callable[[dict[str, Any]], None]
RuntimeEventSink = Callable[[RuntimeEvent], None]
Approval = Callable[[str, str], bool]

from openkyrozen.tasks.ports import InteractionStore
