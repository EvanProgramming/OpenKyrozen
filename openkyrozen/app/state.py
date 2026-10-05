from __future__ import annotations

import importlib.metadata
import os
import re
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
import sys
import threading
import time
import datetime
from pathlib import Path
from typing import Any
from openkyrozen.security.capabilities import issue_capability_token
from openkyrozen.workspace.context import LaunchContext
from openkyrozen.security.tool_policy import resolve_capabilities
from openkyrozen.providers import ProviderConfig, LLMProvider
from openkyrozen.agent.compaction import ContextState


def initialise(self):
    self._PLATFORM = sys.platform

    self._IS_WINDOWS = self._PLATFORM == "win32"

    self._IS_MACOS = self._PLATFORM == "darwin"

    self._IS_LINUX = self._PLATFORM.startswith("linux")

    self._UNICODE_OK = self._terminal_supports_unicode()

    if self._UNICODE_OK:    self._SPINNER_FRAMES = ["◜", "◠", "◝", "◞", "◡", "◟"]; self._BAR_FILL = "█"; self._BAR_EMPTY = "░"; self._CHECK = "✓"; self._CIRCLE = "○"; self._HALF = "◷"; self._BOX_TL = "┌"; self._BOX_H = "─"; self._BOX_BL = "└"; self._BOX_V = "│"; self._DOT = "·"; self._NBHYPHEN = "‑"
    else:               self._SPINNER_FRAMES = ["/", "-", "\\", "|"];             self._BAR_FILL = "#"; self._BAR_EMPTY = "."; self._CHECK = "+"; self._CIRCLE = "o"; self._HALF = ">"; self._BOX_TL = "+"; self._BOX_H = "-"; self._BOX_BL = "+"; self._BOX_V = "|"; self._DOT = "."; self._NBHYPHEN = "-"

    if self._IS_MACOS:
        for _mk in ("MallocStackLogging", "MallocStackLoggingNoCompact"):
            os.environ.pop(_mk, None)

    self.UTC = datetime.timezone.utc

    self.RELEASE_VERSION = "2.0.8"

    self.RELEASE_TAG = f"v{self.RELEASE_VERSION}"

    self.RELEASE_BASE_URL = "https://github.com/EvanProgramming/OpenKyrozen/releases/download/"

    self.RELEASE_WHEEL_URL = f"{self.RELEASE_BASE_URL}{self.RELEASE_TAG}/openkyrozen-{self.RELEASE_VERSION}-py3-none-any.whl"

    self.TUI_SOURCE_URL = f"{self.RELEASE_BASE_URL}{self.RELEASE_TAG}/openkyrozen-tui-{self.RELEASE_VERSION}.tar.gz"

    self.TUI_CHECKSUM_URL = f"{self.TUI_SOURCE_URL}.sha256"

    self.UPDATE_REPOSITORY_URL = "https://github.com/EvanProgramming/OpenKyrozen.git"

    self.GO_VERSION = "1.27.1"

    self.GO_SHA256 = {
        ("darwin", "arm64"): "ee215d57e0ec269c60cc9ceca68e6bda321ba9ee5afe24f4b0988703c2d87d12",
        ("darwin", "amd64"): "8f8f52c6649542cf027bbc9b9c68d1ec042f9f34808a40413f0b8b3b66f3caa4",
        ("linux", "amd64"): "63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445",
        ("linux", "arm64"): "3450b45a3f9ee8568792736a5c5e70a1f2e9b36c35a8f74958c03e51d7d92bec",
        ("windows", "amd64"): "a3911b5e0e1b1053f25ed0675f4c1c6aad1e2bfcf253df2b9be4caabd2edd95d",
        ("windows", "arm64"): "13b69b87bb0e83f96bc68560a8cace7f0343b1e03469f1110ea18d17e3234069",
    }

    self.PROVIDER_UNAVAILABLE_CODE = "provider_unavailable"

    self.PROVIDER_UNAVAILABLE_MESSAGE = (
        "No LLM provider is configured. Set DEEPSEEK_API_KEY or configure a local provider before sending chat."
    )

    try:
        self.__version__ = importlib.metadata.version("openkyrozen")
    except importlib.metadata.PackageNotFoundError:
        self.__version__ = self.RELEASE_VERSION

    self.TOOL_ALIASES: dict[str, str] = {
        "bash": "run_cmd",
        "shell": "run_cmd",
        "sh": "run_cmd",
        "browse_summary": "read_webpage",
        "run_terminal_command": "execute_terminal_command",
        "run_terminal": "execute_terminal_command",
        "terminal": "execute_terminal_command",
        "run_command": "run_cmd",
        "cmd": "run_cmd",
        "exec": "run_cmd",
        "execute": "run_cmd",
        "list_tree": "list_tree",
        "tree": "list_tree",
        "check_memory": "check_stored_data",
        "run_shell_command": "run_cmd",
        "run_shell": "run_cmd",
        "shell_command": "run_cmd",
        "execute_shell": "run_cmd",
        "shell_cmd": "run_cmd",
        "bash_cmd": "run_cmd",
        "command": "run_cmd",
        "run": "run_cmd",
        "run_shell": "run_cmd",
        "write": "write_file",
        # Git aliases
        "status": "git_status",
        "diff": "git_diff",
        "log": "git_log",
        "branch": "git_branch",
        "add": "git_add",
        "commit": "git_commit",
        "push": "git_push",
        "pull": "git_pull",
        "checkout": "git_checkout",
        "stash": "git_stash",
        "clone": "git_clone",
        "reset": "git_reset",
        "show": "git_show",
        "remote": "git_remote",
    }

    self._UNSUPPORTED_ACTION_PROTOCOL_RE = re.compile(
        r"(?is)<\s*ssai_action\b[^>]*>(?:[\s\S]*?</\s*ssai_action\s*>|[\s\S]*\Z)"
        r"|<\s*details\b[^>]*>\s*<\s*summary\b[^>]*>\s*Action\s*:[\s\S]*?(?:</\s*details\s*>|\Z)"
        r"|<\s*(tool_calls|function_calls|invoke)\b[^>]*>[\s\S]*?(?:</\s*\1\s*>|\Z)"
    )

    self._UNSUPPORTED_ACTION_PROTOCOL_MESSAGE = (
        "The model returned an unsupported tool-call wrapper. "
        "No tool was executed; retry with one Action JSON block."
    )

    self._TASK_STATUS_ORDER = ("succeeded", "failed", "blocked", "cancelled", "pending", "running")

    self._ACCENT = "#00f0ff"           # primary brand colour

    self._ACCENT_DIM = "#007788"       # muted variant for secondary elements

    self._ACCENT_BG = "#001a1f"        # dark background tint

    self._SUCCESS = "#00ff88"          # success green

    self._WARNING = "#ffaa00"          # warning amber

    self._ERROR = "#ff4466"            # error red

    self._MUTED = "#445566"            # subtle grey

    self.SHORT_TERM_CAP = 16

    self.MAX_TOOL_RETRIES = 3

    self.MAX_STEPS_PER_TURN = 50          # how many tool-call rounds the LLM may perform in one user turn

    self.MAX_UNKNOWN_TOOL_RETRIES = 3     # how many times to re-prompt when LLM uses an unrecognised action name

    self.CONFIG_PATH = os.path.expanduser("~/.kyrozen_config.json")

    self.IDLE_CONSOLIDATION_TIMEOUT = 60   # 1 minute

    self._EXECUTION_SURFACE = self.surface

    self._dynamic_tools_env = os.environ.get("KYROZEN_ALLOW_DYNAMIC_TOOLS")

    self._surface_capabilities = os.environ.get(
        f"KYROZEN_{self._EXECUTION_SURFACE.upper()}_CAPABILITIES", ""
    ).strip().lower()

    self._execution_capability_token = issue_capability_token(
        f"surface:{self._EXECUTION_SURFACE}",
        resolve_capabilities(
            self._surface_capabilities or ("full" if self._EXECUTION_SURFACE == "cli" else "workspace"),
            default="workspace",
        ),
    )

    self.ALLOW_DYNAMIC_TOOLS = (
        self._dynamic_tools_env.strip().lower() in {"1", "true", "yes"}
        if self._dynamic_tools_env is not None
        else self._EXECUTION_SURFACE == "cli" or self._surface_capabilities == "full"
    )

    self._APPROVAL_REQUIRED_TOOLS = frozenset({
        "git_push", "git_pull", "git_checkout", "git_stash", "git_reset", "git_remote",
        "github_cli", "define_tool",
    })

    self._provider_config: ProviderConfig | None = None

    self.llm_provider: LLMProvider | None = None

    self.DEEPSEEK_MODEL_SIMPLE = "deepseek-flash"   # set at init time from provider

    self.DEEPSEEK_MODEL_COMPLEX = "deepseek-v4-pro"

    self.MODEL_NAME = "deepseek-flash"  # updated at init

    self._LEARNING_FEATURE_ORDER = (
        "auto_learn_conversations",
        "load_project_files_into_memory",
        "age_out_old_coded_entries",
        "auto_debug_tool",
        "consolidate_memories",
        "review_tools",
        "targeted_inquiry",
        "idle_reflection",
        "strategy_distillation",
        "auto_patch_technology",
        "invent_skills",
        "context_compression",
        "outcome_verified_evolution",
        "dynamic_tool_definition",
        "detect_user_preferences",
        "autonomous_inspection",
        "memory_importance_scoring",
        "knowledge_graph_extraction",
        "skill_composition",
        "learning_rollback",
    )

    self._SELF_LEARNING_FLAGS: dict[str, bool] = {name: True for name in self._LEARNING_FEATURE_ORDER}

    self._LEARNING_MODES = {"setup_required", "local", "remote"}

    self._LOCAL_LEARNING_MODEL = "qwen2.5:7b"

    self._REMOTE_LEARNING_FEATURES = {
        "auto_learn_conversations", "auto_debug_tool", "consolidate_memories", "review_tools",
        "targeted_inquiry", "idle_reflection", "strategy_distillation", "auto_patch_technology",
        "invent_skills", "context_compression", "outcome_verified_evolution",
    }

    self._CORE_TESTS = [
        {
            "description": "list_dir returns a non-empty string",
            "action": "list_dir",
            "args": ".",
            "check": "nonempty"
        },
        {
            "description": "read_file returns content of main.py (contains 'Kyrozen')",
            "action": "read_file",
            "args": "main.py",
            "check": "contains",
            "expected": "Kyrozen"
        },
        {
            "description": "find_files finds all .py files in top level",
            "action": "find_files",
            "args": "*.py",
            "check": "nonempty"
        },
        {
            "description": "run_cmd echoes a simple message",
            "action": "run_cmd",
            "args": "echo 'regression_test_ok'",
            "check": "contains",
            "expected": "regression_test_ok"
        },
        {
            "description": "write_file creates a test file and read_file reads it back",
            "action": "write_file",
            "args": "_test_regression_tmp.txt|hello from regression",
            "check": "nonempty"
        },
        {
            "description": "execute_terminal_command works (alias for run_cmd)",
            "action": "execute_terminal_command",
            "args": "echo 'alias_ok'",
            "check": "contains",
            "expected": "alias_ok"
        },
        {
            "description": "list_dir on non‑existent folder returns error",
            "action": "list_dir",
            "args": "_nonexistent_xyz",
            "check": "error"
        },
    ]

    self._BUILTIN_TOOL_NAMES = {
        "write_file","read_file","calculate","run_cmd","search_web","find_files","list_dir",
        "git_clone","git_status","execute_terminal_command","analyze_remote_repo",
        "list_tree","read_webpage","check_stored_data","search_memory",
        "git_diff","git_log","git_branch","git_add","git_commit",
        "git_push","git_pull","git_checkout","git_stash","git_reset",
        "git_show","git_remote",
        "browser_open","browser_snapshot","browser_click","browser_type","browser_close",
        "graph_status","graph_query","graph_explain","graph_path","graph_refresh",
        "github_status","github_read","github_cli",
    }

    self._saved_user_tools: dict[str, Any] = {}

    self._FAILURE_STORE_PREFIX = "FAILURE:"

    self._tool_stats: dict[str, dict] = {}  # {tool_name: {"calls":int,"successes":int,"avg_time":float,"total_time":float}}

    self._total_prompt_tokens: int = 0

    self._total_completion_tokens: int = 0

    self._last_prompt_tokens: int = 0

    self._last_completion_tokens: int = 0

    self._active_usage_run_id: ContextVar[str | None] = ContextVar("active_usage_run_id", default=None)

    self._stream_event_callback: ContextVar[Any] = ContextVar("stream_event_callback", default=None)

    self._approval_callback: ContextVar[Any] = ContextVar("approval_callback", default=None)

    self._active_interaction_mode: ContextVar[str] = ContextVar("active_interaction_mode", default="agent")

    self._interaction_mode_override: ContextVar[str | None] = ContextVar("interaction_mode_override", default=None)

    self._interaction_controls_enabled: ContextVar[bool] = ContextVar("interaction_controls_enabled", default=True)

    self._fast_used_backend: ContextVar[str] = ContextVar("fast_used_backend", default="")

    self._turn_cost_log: list[dict] = []  # {"tokens":int, "time":float, "tool_calls":int}

    self._last_task_end = time.time()

    self._last_user_interaction = time.time()

    self._last_code_scan_time = 0

    self._last_inquiry_time = time.time()

    self._inquired_functions: set[str] = set()

    self._known_libraries = set()

    self._technology_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kyrozen-learning")

    self._technology_in_flight: set[str] = set()

    self._technology_lock = threading.Lock()

    self._SPINNER_STOP = threading.Event()

    self._SPINNER_THREAD: threading.Thread | None = None

    self._active_context_state: ContextVar[ContextState | None] = ContextVar("active_context_state", default=None)

    self._in_context_compaction: ContextVar[bool] = ContextVar("in_context_compaction", default=False)

    self._context_status_lock = threading.Lock()

    self._context_status_by_scope: dict[tuple[str, str | None], dict[str, Any]] = {}

    self._provider_token_counts_by_scope: dict[tuple[str, str | None], dict[str, int]] = {}

    self.DEEPSEEK_MODEL: str = self.DEEPSEEK_MODEL_SIMPLE

    self._last_project_scan_time = 0.0

    self._last_project_scan_root: Path | None = None

    self._project_graph: self.ProjectGraph | None = None

    self._github_cli: self.GitHubCLI | None = None

    self._fix_outcomes: list[dict] = []  # [{error_sig, fix_desc, success, timestamp}]

    self._FIX_WORKFLOW_STAGES = (
        "reported", "reproduced", "diagnosed", "hypothesized", "fixed", "verified", "explained",
    )

    self._FIX_TERMINAL_STAGES = {"explained", "blocked"}

    self._FIX_EVIDENCE_STAGES = {"reproduced", "fixed", "verified"}

    self._FIX_MAX_STEPS = 12

    self._FIX_REPRODUCTION_ACTIONS = {"read_file", "run_cmd", "execute_terminal_command"}

    self._FIX_VERIFICATION_ACTIONS = {"run_cmd", "execute_terminal_command"}

    self._FIX_MUTATION_ACTIONS = {"write_file", "git_apply", "git_commit"}

    self._FIX_SUCCESS_FEEDBACK = (
        "thanks", "thank you", "that works", "it works", "worked", "fixed", "solved",
        "great", "perfect", "awesome", "谢", "谢谢", "好了", "可以了", "搞定", "没问题",
    )

    self._FIX_FAILURE_FEEDBACK = (
        "not working", "still broken", "didn't work", "doesn't work", "wrong", "incorrect",
        "not correct", "not fixed", "still fails", "还不行", "还是错", "没好", "没解决", "不对",
    )

    self._user_preferences: dict[str, Any] = {
        "language": "",           # preferred programming language
        "naming_style": "",       # snake_case, camelCase, etc.
        "comment_style": "",      # verbose, minimal, docstring-only
        "indent": "",             # spaces-4, tabs, spaces-2
        "formatter": "",          # black, ruff, none
        "verbosity": "",          # concise, detailed, balanced
        "prefers_tables": False,  # user likes table-format output
        "code_first": False,      # user prefers code before explanation
    }

    self._hydrated_preferences: dict[str, Any] = {}

    self._preference_scope: tuple[str, str, str | None, str | None] | None = None

    self._last_inspection_time: float = 0

    self._inspection_interval = 1800  # 30 minutes between inspections

    self._knowledge_graph: dict[str, list[str]] = {}

    self._set_workspace_root(os.environ.get(
        "KYROZEN_WORKSPACE_ROOT",
        str(Path.home() / ".kyrozen" / "workspace"),
    ))

    self._launch_context: LaunchContext | None = None

    self._restore_self_learning_flags()

    self._agent_profile_mode = "auto"

    self._last_learning_run: dict[str, Any] | None = None

    self._learning_notices: list[str] = []

    self._ponytail_level = "full"

    self._ACCEPTANCE_COMMAND_RE = re.compile(
        r"(?:^|\s)(?:pytest|python\s+-m\s+unittest|make\s+(?:test|check|lint)|npm\s+test|cargo\s+test|go\s+test)(?:\s|$)",
        re.IGNORECASE,
    )

    self.AVAILABLE_TOOLS["check_stored_data"] = self._check_stored_data

    self.AVAILABLE_TOOLS["search_memory"] = self._search_memory
    self.AVAILABLE_TOOLS["discover_tools"] = self._discover_tools

    self.TOOLS_LIST = self._build_tools_list()


    self.short_term_memory: list[dict[str, str]] = [
        {"role": "user", "content": "Hello, are you ready to help me?"},
        {"role": "assistant", "content": "Yes. I can use the tools permitted by the active interaction mode. How can I help?"},
    ]

    self._PROMPT_INJECTION_PATTERNS = [
        r"ignore\s+(all\s+)?(previous|above|prior)\s+instructions",
        r"you\s+are\s+now\s+(a\s+)?\w+\s+(bot|assistant|agent)",
        r"system\s*:\s*new\s+(prompt|instruction)",
        r"\[system\]\s*\(override\)",
        r"<\|im_start\|>",
        r"<\|system\|>",
        r"forget\s+everything\s+(you\s+know|above)",
        r"pretend\s+you\s+are",
        r"act\s+as\s+if",
    ]

    self._ACTION_MARKER_NAMES = tuple(sorted(set(self.AVAILABLE_TOOLS) | set(self.TOOL_ALIASES), key=len, reverse=True))

    self._ACTION_MARKER_RE = re.compile(
        r"(?i)(?<![\w])(?P<name>(?:" + "|".join(map(re.escape, self._ACTION_MARKER_NAMES)) + r"))\s*:"
    )

    self._ACTION_SENTENCE_ENDS = (".", "!", "?", "…", "。", "！", "？", ")", "]", "}", "`", '"', "'")

    self._ACTION_PROSE_START_RE = re.compile(
        r"(?i)^(?:a|an|and|are|accepts|can|does|for|from|is|means|must|not|or|returns|the|that|this|to|used|use|will|which|with)\b"
    )

    self._LEARNING_FEATURE_REGISTRY: dict[str, dict[str, Any]] = {
        "auto_learn_conversations": {
            "description": "Extract durable facts and preferences from recent conversations",
            "executor": lambda _context: (self._auto_learn_conversations() or self._learning_result()),
        },
        "load_project_files_into_memory": {
            "description": "Incrementally refresh the private local Graphify code index",
            "executor": self._run_learning_project_graph,
        },
        "age_out_old_coded_entries": {
            "description": "Remove legacy FILE snapshots for deleted project files",
            "executor": lambda _context: (self._age_out_old_coded_entries() or self._learning_result()),
        },
        "auto_debug_tool": {
            "description": "Analyse repeated tool failures and record bounded findings",
            "executor": lambda _context: (self._auto_debug_tool() or self._learning_result()),
        },
        "consolidate_memories": {
            "description": "Deduplicate and consolidate non-trivial memory entries",
            "executor": lambda _context: (self._consolidate_memories() or self._learning_result()),
        },
        "review_tools": {
            "description": "Review tool performance and record improvement proposals",
            "executor": lambda _context: (self._review_tools() or self._learning_result()),
        },
        "targeted_inquiry": {
            "description": "Inspect one undocumented project function per bounded cycle",
            "executor": lambda _context: (self._targeted_inquiry() or self._learning_result()),
        },
        "idle_reflection": {
            "description": "Reflect on recent multi-step work after an idle interval",
            "executor": lambda _context: (self._maybe_trigger_reflection() or self._learning_result()),
        },
        "strategy_distillation": {
            "description": "Distil efficiency strategies from sufficiently large recent runs",
            "executor": lambda _context: (self._maybe_strategy_distillation() or self._learning_result()),
        },
        "auto_patch_technology": {
            "description": "Discover unfamiliar libraries and fetch bounded background context",
            "executor": self._run_learning_technology,
        },
        "invent_skills": {
            "description": "Create candidate reusable workflows from repeated conversations",
            "executor": lambda _context: (self._invent_skills() or self._learning_result()),
        },
        "context_compression": {
            "description": "Report foreground model-window context compaction",
            "executor": self._run_learning_context_compression,
        },
        "outcome_verified_evolution": {
            "description": "Review verified trajectories and create at most one bounded canary",
            "executor": lambda _context: (self._review_evolution_runs() or self._learning_result()),
        },
        "dynamic_tool_definition": {
            "description": "Observe the dynamic-tool inventory without granting new capability",
            "executor": self._run_learning_dynamic_tools,
        },
        "detect_user_preferences": {
            "description": "Detect explicit preference signals during a user turn",
            "executor": self._run_learning_preferences,
        },
        "autonomous_inspection": {
            "description": "Run one bounded project health inspection during idle time",
            "executor": lambda _context: (self._autonomous_inspection() or self._learning_result()),
        },
        "memory_importance_scoring": {
            "description": "Score a bounded recent memory window and persist the result",
            "executor": self._run_learning_memory_scoring,
        },
        "knowledge_graph_extraction": {
            "description": "Extract bounded entity relationships from stored facts",
            "executor": self._run_learning_graph,
        },
        "skill_composition": {
            "description": "Compose matching learned skills into a recorded workflow",
            "executor": self._run_learning_skill_composition,
        },
        "learning_rollback": {
            "description": "Keep automatic learning rollback safe and user-directed",
            "executor": self._run_learning_rollback,
        },
    }

    self._learning_dispatch_lock = threading.RLock()

    self._learning_dispatch_cursor = 0
