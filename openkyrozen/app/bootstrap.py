"""Composition root: concrete adapters are constructed only on explicit startup."""
from __future__ import annotations

from types import MethodType

from .container import Application


def build_application(*, surface="cli", memory=None, tools=None, user_id="local"):
    from rich.console import Console
    from openkyrozen.agent.runtime import AgentRuntime
    from openkyrozen.agent.modes import InteractionController
    from openkyrozen.agent.subagents import SubAgentManager
    from openkyrozen.learning.engine import LearningEngine
    from openkyrozen.skills.registry import SkillRegistry
    from openkyrozen.tasks.engine import TaskManager
    from openkyrozen.tools import ToolAdapters
    from openkyrozen.tools.github_cli import GitHubCLI
    from openkyrozen.workspace.graph import ProjectGraph
    from openkyrozen.plugins.runtime import get_plugin_runtime
    from openkyrozen.providers.factory import get_provider, get_fallback_provider, detect_provider
    from openkyrozen.routing import system_one
    from openkyrozen.interfaces.cli import commands, rendering, onboarding, slash_commands, chat
    from openkyrozen.providers import configuration
    from openkyrozen.updates import service as updates, learning_setup
    from openkyrozen.workspace import history_service
    from openkyrozen.learning.worker import start_worker, touch_cli_heartbeat
    from .services import bind_services
    from openkyrozen.learning import runtime as learning_runtime, regression, failures, reflection, memory_maintenance, discovery, inspection
    from .state import initialise

    from openkyrozen.agent.models import WorkspaceState
    memory = memory if memory is not None else build_memory(user_id=user_id)
    memory.session_id = f"surface:{surface}"
    memory.decisions = system_one
    adapters = tools if tools is not None else ToolAdapters()
    workspace = WorkspaceState(str(adapters._WORKSPACE_ROOT), adapters)
    tasks = TaskManager(memory.store, user_id=user_id)
    interaction = InteractionController(memory.store, user_id=user_id, workspace_id=memory.workspace_id, session_id=memory.session_id)
    runtime = AgentRuntime(memory, tasks, interaction, workspace)
    bind_services(runtime)
    runtime.workspace_factory = ToolAdapters
    runtime.surface = surface
    runtime.skill_registry = SkillRegistry(runtime.memory_bank.store, workspace_id=runtime.memory_bank.workspace_id)
    runtime.learning_engine = LearningEngine(runtime.memory_bank, registry=runtime.skill_registry)
    runtime.console = Console()
    runtime.bg_console = Console(stderr=True)
    runtime.fast_mode = system_one
    runtime.ProjectGraph = ProjectGraph
    runtime.GitHubCLI = GitHubCLI
    runtime.get_provider = get_provider
    runtime.get_fallback_provider = get_fallback_provider
    runtime.detect_provider = detect_provider
    runtime.get_plugin_runtime = get_plugin_runtime
    runtime.start_learning_worker = start_worker
    runtime.touch_cli_heartbeat = touch_cli_heartbeat
    for module in (commands, rendering, onboarding, slash_commands, chat, configuration, updates, learning_setup, history_service, learning_runtime, regression, failures, reflection, memory_maintenance, discovery, inspection):
        for name, function in vars(module).items():
            if callable(function) and getattr(function, "__module__", "") == module.__name__:
                setattr(runtime, name, MethodType(function, runtime))
    initialise(runtime)
    runtime.subagent_manager = SubAgentManager(
        runtime.memory_bank, runner=runtime._run_subagent_llm,
        learning_engine=runtime.learning_engine, provider_model=runtime._subagent_provider_model,
    )
    return Application(runtime, adapters, runtime.memory_bank.store)


def build_memory(path=None, *, user_id="local", workspace_id="default", session_id=None, file_scope_id=None, store=None, index=None):
    """Construct durable memory and its best-effort derived index at startup."""
    import os
    from pathlib import Path
    from openkyrozen.persistence.store import EventStore
    from openkyrozen.memory.service import MemoryBank
    from openkyrozen.memory.vector import ChromaIndex
    requested = Path(path or (store.path if store is not None else os.environ.get("KYROZEN_DB_PATH", MemoryBank.DEFAULT_PATH))).expanduser()
    if requested.suffix.lower() != ".sqlite3":
        requested.mkdir(parents=True, exist_ok=True)
        requested /= "openkyrozen.sqlite3"
    store = store if store is not None else EventStore(requested)
    error = None
    if index is None and os.environ.get("KYROZEN_DISABLE_VECTOR_INDEX", "").lower() not in {"1", "true", "yes"}:
        try:
            index = ChromaIndex(Path(os.environ.get("KYROZEN_VECTOR_PATH", str(requested.parent / "chroma_index_v2"))).expanduser())
        except Exception as exc:
            error = f"vector index unavailable: {exc}"
    from openkyrozen.routing import system_one
    memory = MemoryBank(requested, user_id=user_id, workspace_id=workspace_id, session_id=session_id, file_scope_id=file_scope_id, store=store, index=index)
    memory.decisions = system_one
    if error:
        memory._last_error = error
    return memory
