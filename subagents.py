"""Specialised sub-agent profiles with independent context and permissions."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from memory import MemoryBank
from tools import allowed_tool_names


@dataclass(frozen=True)
class AgentProfile:
    name: str
    system_prompt: str
    capabilities: str = "readonly"
    memory_scope: str = "session"
    max_steps: int = 12
    metadata: dict[str, Any] = field(default_factory=dict)


class SubAgentManager:
    def __init__(self, memory: MemoryBank, *, runner: Callable[[AgentProfile, str, list[dict[str, Any]], set[str]], str] | None = None):
        self.memory = memory
        self.runner = runner
        self.profiles: dict[str, AgentProfile] = {}
        self.register(AgentProfile("researcher", "Research the task, cite evidence, and separate facts from assumptions.", "readonly"))
        self.register(AgentProfile("coder", "Inspect and modify code only when requested; verify every change.", "workspace"))
        self.register(AgentProfile("reviewer", "Review results critically, find regressions, and report missing evidence.", "readonly"))

    def register(self, profile: AgentProfile) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{2,48}", profile.name):
            raise ValueError("Invalid sub-agent profile name")
        if profile.memory_scope not in {"session", "workspace", "none"}:
            raise ValueError("memory_scope must be session, workspace, or none")
        if profile.max_steps < 1 or profile.max_steps > 100:
            raise ValueError("max_steps must be between 1 and 100")
        self.profiles[profile.name] = profile

    def list_profiles(self) -> list[dict[str, Any]]:
        return [{"name": p.name, "capabilities": p.capabilities, "memory_scope": p.memory_scope,
                 "max_steps": p.max_steps, "metadata": p.metadata} for p in self.profiles.values()]

    def run(self, profile_name: str, task: str, *, workspace_id: str | None = None) -> dict[str, Any]:
        profile = self.profiles.get(profile_name)
        if not profile:
            raise ValueError(f"Unknown sub-agent profile: {profile_name}")
        task = str(task).strip()
        if not task:
            raise ValueError("Task cannot be empty")
        run_id = f"subagent_{uuid.uuid4().hex}"
        workspace_id = workspace_id or self.memory.workspace_id
        scoped_memory = MemoryBank(self.memory.db_path, user_id=self.memory.user_id,
                                   workspace_id=workspace_id,
                                   session_id=run_id if profile.memory_scope == "session" else None)
        context = scoped_memory.recall_records(task, n_results=5) if profile.memory_scope != "none" else []
        tools = allowed_tool_names(capabilities=profile.capabilities)
        self.memory.store.append_event("subagent.started", {"run_id": run_id, "profile": profile.name, "task": task},
                                       user_id=self.memory.user_id, workspace_id=workspace_id, session_id=run_id)
        if self.runner is None:
            result = "Sub-agent runner is not configured."
        else:
            result = self.runner(profile, task, context, tools)
        self.memory.store.append_event("subagent.completed", {"run_id": run_id, "profile": profile.name,
                                                               "result": str(result)[:4000]},
                                       user_id=self.memory.user_id, workspace_id=workspace_id, session_id=run_id)
        scoped_memory.add_log(str(result), kind="episodic", status="active",
                              metadata={"subagent_run_id": run_id, "profile": profile.name})
        return {"run_id": run_id, "profile": profile.name, "result": str(result),
                "tools": sorted(tools), "memory_scope": profile.memory_scope}
