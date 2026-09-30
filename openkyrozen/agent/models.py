"""Runtime state with explicit session and workspace ownership."""
from dataclasses import dataclass, field
from typing import Any
from openkyrozen.tools.ports import ToolPort

@dataclass
class WorkspaceState:
    root: str
    adapters: ToolPort
    launch_context: Any = None
    graph: Any = None
    github: Any = None

@dataclass
class AgentSession:
    session_id: str
    memory: Any
    tasks: Any
    interaction: Any
    workspace: WorkspaceState
    messages: list = field(default_factory=list)
    learning: Any = None
    subagents: Any = None
    last_learning_run: Any = None
    learning_notices: list = field(default_factory=list)
    profile: str = "auto"
    turn_cost_log: list = field(default_factory=list)
    skills: Any = None
    preferences: dict = field(default_factory=dict)
    hydrated_preferences: dict = field(default_factory=dict)
    preference_scope: Any = None
    knowledge_graph: dict = field(default_factory=dict)
    ponytail_level: str = "full"
