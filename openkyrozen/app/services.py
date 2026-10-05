"""Concrete feature services supplied by the composition root."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from openai import OpenAI
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich import print as rprint
from openkyrozen.memory.service import MemoryBank
from openkyrozen.tasks.engine import TaskManager, TaskWorker, canonical_status, is_complete, is_terminal
from openkyrozen.persistence.models import stable_hash, utc_now
from openkyrozen.agent.modes import InteractionController, InteractionError, is_plan_acceptance, mode_capabilities, normalize_provider_control, parse_control_block, render_plan, render_question, validate_plan_proposal, split_inline_command, validate_question_request
from openkyrozen.learning.engine import LearningEngine
from openkyrozen.skills.registry import SkillRegistry
from openkyrozen.tools.github_cli import GH_VERSION
from openkyrozen.skills.instructions import format_instructions
from openkyrozen.app.config import AgentConfigError, effective_capabilities, load_agent_config
from openkyrozen.agent.subagents import AgentProfile, SubAgentManager
from openkyrozen.security.capabilities import issue_capability_token
from openkyrozen.security.dynamic_tools import SAFE_BUILTINS, validate_tool_source
from openkyrozen.workspace.context import LaunchContext, resolve_launch_context, source_scope_id
from openkyrozen.workspace.history import HistoryError, HistoryManager, TurnToken
from openkyrozen.tools import CommandResult, resolve_capabilities, tool_capability
from openkyrozen.providers import ProviderConfig, LLMProvider, save_provider_config, PROVIDER_DEFAULT_MODELS, PROVIDER_ENV_VARS, PROVIDER_FALLBACKS, get_cost_summary, reset_cost_tracker, usage_scope, save_provider_config_encrypted, encrypt_api_key, decrypt_api_key, model_for_complexity, provider_is_configured, resolve_ollama_models, discover_ollama_models, PROVIDER_DISPLAY_NAMES
from openkyrozen.agent.compaction import ContextState, compact_for_pressure, message_fingerprint, retain_context_digests
from openkyrozen.agent.types import ProviderUnavailableError, ContextOverflowError, ContextTooLargeError, ExecutionReceipt, DeepSeekDSMLFilter


def bind_services(runtime):
    runtime.ThreadPoolExecutor = ThreadPoolExecutor
    runtime.ContextVar = ContextVar
    runtime.copy_context = copy_context
    runtime.dataclass = dataclass
    runtime.Path = Path
    runtime.Any = Any
    runtime.OpenAI = OpenAI
    runtime.Console = Console
    runtime.Markdown = Markdown
    runtime.rich_escape = rich_escape
    runtime.Panel = Panel
    runtime.rprint = rprint
    runtime.MemoryBank = MemoryBank
    runtime.TaskManager = TaskManager
    runtime.TaskWorker = TaskWorker
    runtime.canonical_status = canonical_status
    runtime.is_complete = is_complete
    runtime.is_terminal = is_terminal
    runtime.stable_hash = stable_hash
    runtime.utc_now = utc_now
    runtime.InteractionController = InteractionController
    runtime.InteractionError = InteractionError
    runtime.is_plan_acceptance = is_plan_acceptance
    runtime.mode_capabilities = mode_capabilities
    runtime.normalize_provider_control = normalize_provider_control
    runtime.parse_control_block = parse_control_block
    runtime.render_plan = render_plan
    runtime.render_question = render_question
    runtime.validate_plan_proposal = validate_plan_proposal
    runtime.split_inline_command = split_inline_command
    runtime.validate_question_request = validate_question_request
    runtime.LearningEngine = LearningEngine
    runtime.SkillRegistry = SkillRegistry
    runtime.GH_VERSION = GH_VERSION
    runtime.format_instructions = format_instructions
    runtime.AgentConfigError = AgentConfigError
    runtime.effective_capabilities = effective_capabilities
    runtime.load_agent_config = load_agent_config
    runtime.AgentProfile = AgentProfile
    runtime.SubAgentManager = SubAgentManager
    runtime.issue_capability_token = issue_capability_token
    runtime.SAFE_BUILTINS = SAFE_BUILTINS
    runtime.validate_tool_source = validate_tool_source
    runtime.LaunchContext = LaunchContext
    runtime.resolve_launch_context = resolve_launch_context
    runtime.source_scope_id = source_scope_id
    runtime.HistoryError = HistoryError
    runtime.HistoryManager = HistoryManager
    runtime.TurnToken = TurnToken
    runtime.CommandResult = CommandResult
    runtime.resolve_capabilities = resolve_capabilities
    runtime.tool_capability = tool_capability
    runtime.ProviderConfig = ProviderConfig
    runtime.LLMProvider = LLMProvider
    runtime.save_provider_config = save_provider_config
    runtime.PROVIDER_DEFAULT_MODELS = PROVIDER_DEFAULT_MODELS
    runtime.PROVIDER_ENV_VARS = PROVIDER_ENV_VARS
    runtime.PROVIDER_FALLBACKS = PROVIDER_FALLBACKS
    runtime.get_cost_summary = get_cost_summary
    runtime.reset_cost_tracker = reset_cost_tracker
    runtime.usage_scope = usage_scope
    runtime.save_provider_config_encrypted = save_provider_config_encrypted
    runtime.encrypt_api_key = encrypt_api_key
    runtime.decrypt_api_key = decrypt_api_key
    runtime.model_for_complexity = model_for_complexity
    runtime.provider_is_configured = provider_is_configured
    runtime.resolve_ollama_models = resolve_ollama_models
    runtime.discover_ollama_models = discover_ollama_models
    runtime.PROVIDER_DISPLAY_NAMES = PROVIDER_DISPLAY_NAMES
    runtime.ContextState = ContextState
    runtime.compact_for_pressure = compact_for_pressure
    runtime.message_fingerprint = message_fingerprint
    runtime.retain_context_digests = retain_context_digests
    runtime.ProviderUnavailableError = ProviderUnavailableError
    runtime.ContextOverflowError = ContextOverflowError
    runtime.ContextTooLargeError = ContextTooLargeError
    runtime.ExecutionReceipt = ExecutionReceipt
