"""
Kyrozen: self-learning AI Agent powered by DeepSeek API + tools.
(Run `python main.py` to launch the agent)
"""

import pathlib
import shutil
import sys
import warnings

# Suppress all DeprecationWarnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Ensure the package directory is on sys.path (needed when installed via pip)
import os as _os
_PKG_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

# Guard: Python 3.14 has an import-system deadlock with openai 2.x
if sys.version_info >= (3, 14):
    sys.exit(
        "OpenKyrozen does not support Python 3.14+ due to a known import deadlock "
        "with the OpenAI SDK. Use Python 3.12 or 3.13 instead.\n"
        "Rebuild the venv with:  make reinstall PYTHON=python3.12"
    )

# Platform detection for cross-platform startup info
_PLATFORM = sys.platform
_IS_WINDOWS = _PLATFORM == "win32"
_IS_MACOS = _PLATFORM == "darwin"
_IS_LINUX = _PLATFORM.startswith("linux")

# Terminal capability detection (Windows cmd.exe has limited Unicode/ANSI support)
def _terminal_supports_unicode() -> bool:
    """Return True if the terminal can render Unicode box-drawing and special chars."""
    if not _IS_WINDOWS:
        return True
    # Windows Terminal, VS Code terminal, and ConEmu all set WT_SESSION
    if _os.environ.get("WT_SESSION") or _os.environ.get("TERM_PROGRAM") == "vscode":
        return True
    # PowerShell 6+ generally supports UTF-8
    if "pwsh" in _os.environ.get("TERM_PROGRAM", "").lower():
        return True
    # Check codepage — 65001 is UTF-8
    try:
        import ctypes
        if ctypes.windll.kernel32.GetConsoleOutputCP() == 65001:
            return True
    except Exception:
        pass
    return False

_UNICODE_OK = _terminal_supports_unicode()

# Dual character sets: Unicode preferred, ASCII fallback for legacy terminals
if _UNICODE_OK:    _SPINNER_FRAMES = ["◜", "◠", "◝", "◞", "◡", "◟"]; _BAR_FILL = "█"; _BAR_EMPTY = "░"; _CHECK = "✓"; _CIRCLE = "○"; _HALF = "◷"; _BOX_TL = "┌"; _BOX_H = "─"; _BOX_BL = "└"; _BOX_V = "│"; _DOT = "·"; _NBHYPHEN = "‑"
else:               _SPINNER_FRAMES = ["/", "-", "\\", "|"];             _BAR_FILL = "#"; _BAR_EMPTY = "."; _CHECK = "+"; _CIRCLE = "o"; _HALF = ">"; _BOX_TL = "+"; _BOX_H = "-"; _BOX_BL = "+"; _BOX_V = "|"; _DOT = "."; _NBHYPHEN = "-"

# Delete stale __pycache__ before any imports to avoid loading deprecated bytecode
for p in pathlib.Path(__file__).parent.rglob("__pycache__"):
    shutil.rmtree(p, ignore_errors=True)

import ast
import json
import os
import re
import subprocess

# macOS: suppress "MallocStackLogging: can't turn off malloc stack logging"
# warnings from child Python processes spawned during self-learning
if _IS_MACOS:
    for _mk in ("MallocStackLogging", "MallocStackLoggingNoCompact"):
        os.environ.pop(_mk, None)
import sys
import traceback
import threading
import time
import datetime
# Patch datetime.UTC for Python <3.11
if not hasattr(datetime, 'UTC'):
    datetime.UTC = datetime.timezone.utc
UTC = datetime.timezone.utc
from pathlib import Path
from typing import Any

from openai import OpenAI
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich import print as rprint

from memory import MemoryBank
from tools import AVAILABLE_TOOLS, set_workspace_root as _set_tools_workspace_root
from providers import (
    ProviderConfig, LLMProvider, get_provider, detect_provider,
    save_provider_config, PROVIDER_DEFAULT_MODELS, PROVIDER_ENV_VARS,
    get_fallback_provider, get_cost_summary, reset_cost_tracker,
    save_provider_config_encrypted, encrypt_api_key, decrypt_api_key,
)

# aliases for flexible action recognition
TOOL_ALIASES: dict[str, str] = {
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

def _is_valid_action(name: str | None) -> bool:
    if not name:
        return False
    return name in AVAILABLE_TOOLS or name in TOOL_ALIASES


def _detect_unknown_action(text: str) -> str | None:
    """Return the first action name in the text that is not a valid tool."""
    for obj in _extract_json_objects(text):
        action = obj.get("action")
        if action and not _is_valid_action(action):
            return str(action)
    return None


def _workspace_info() -> str:
    """Return a message describing the current working location,
    adjusting when the agent is inside an OpenKyrozen folder."""
    cwd = os.getcwd()
    parent = os.path.dirname(cwd)
    if os.path.basename(cwd) == "OpenKyrozen":
        return (
            f"OpenKyrozen is installed inside `{cwd}`.\n"
            f"Your workspace (the folder you are working on) is located one level above:\n"
            f"{parent}\n\n"
            "You should treat all file operations relative to your workspace.\n"
            "For example, to list files in your workspace use `list_dir('.')`.\n"
            "To read a file that is in your workspace (not inside OpenKyrozen), "
            "use an absolute or relative path as usual – the agent is currently running "
            "inside the OpenKyrozen folder, but your project files are in your workspace."
        )
    else:
        return (
            f"You are currently working inside the directory:\n{cwd}\n\n"
            "You can use relative paths like '.' or 'main.py' directly.\n"
            "Do not ask the user to supply a local path or a remote URL unless you intend to "
            "use the `analyze_remote_repo` tool to clone an external repository."
        )


console = Console()
bg_console = Console(stderr=True)

# ---- Task status panel (rendered at bottom via Rich, no scroll regions) ----

def _tasks_panel_height() -> int:
    if not tasks.tasks:
        return 0
    return min(len(tasks.tasks) + 3, 12)


def _tasks_panel_content() -> str:
    """Build task panel as Rich-markup string (no raw ANSI)."""
    if not tasks.tasks:
        return ""
    total = len(tasks.tasks)
    done = sum(1 for t in tasks.tasks if t["status"] == "done")
    bar_w = 20
    filled = int(bar_w * done / max(total, 1))
    bar = _BAR_FILL * filled + _BAR_EMPTY * (bar_w - filled)
    lines = [f"[bold white on {_ACCENT_BG}] {_BOX_TL}{_BOX_H}{_BOX_H} TASKS [{bar}] {done}/{total} [/]"]
    for i, t in enumerate(tasks.tasks):
        icon = _CHECK if t["status"] == "done" else _CIRCLE if t["status"] == "pending" else _HALF
        color = _SUCCESS if t["status"] == "done" else _WARNING if t["status"] == "pending" else _ACCENT
        desc = t["description"][:55]
        lines.append(f"[white on {_ACCENT_BG}] {_BOX_V} [{color}]{icon}[/{color}] [{_MUTED}]{i}[/{_MUTED}] {desc} [/]")
    pending = total - done
    if pending > 0:
        lines.append(f"[bold white on {_ACCENT_BG}] {_BOX_BL}{_BOX_H}{_BOX_H} {pending} remaining — DO NOT STOP [/]")
    else:
        lines.append(f"[bold white on {_ACCENT_BG}] {_BOX_BL}{_BOX_H}{_BOX_H} All tasks complete {_CHECK} [/]")
    return "\n".join(lines)


def _update_tasks_panel() -> None:
    """Render task panel at current cursor position via Rich."""
    content = _tasks_panel_content()
    if not content:
        return
    try:
        console.print(Panel(content, title="Tasks", border_style=_ACCENT))
    except Exception:
        pass


def _clear_tasks_panel() -> None:
    """No‑op — panel is inline, cleared naturally by new output."""
    pass


# ---- Theme ----
_ACCENT = "#00f0ff"           # primary brand colour
_ACCENT_DIM = "#007788"       # muted variant for secondary elements
_ACCENT_BG = "#001a1f"        # dark background tint
_SUCCESS = "#00ff88"          # success green
_WARNING = "#ffaa00"          # warning amber
_ERROR = "#ff4466"            # error red
_MUTED = "#445566"            # subtle grey

# ---- Constants ----
SHORT_TERM_CAP = 16
MAX_TOOL_RETRIES = 3
MAX_STEPS_PER_TURN = 50          # how many tool-call rounds the LLM may perform in one user turn
MAX_UNKNOWN_TOOL_RETRIES = 3     # how many times to re-prompt when LLM uses an unrecognised action name
CONFIG_PATH = os.path.expanduser("~/.kyrozen_config.json")
IDLE_CONSOLIDATION_TIMEOUT = 60   # 1 minute
_EXECUTION_SURFACE = os.environ.get("KYROZEN_EXECUTION_SURFACE", "cli").strip().lower()
_dynamic_tools_env = os.environ.get("KYROZEN_ALLOW_DYNAMIC_TOOLS")
_surface_capabilities = os.environ.get(
    f"KYROZEN_{_EXECUTION_SURFACE.upper()}_CAPABILITIES", ""
).strip().lower()
ALLOW_DYNAMIC_TOOLS = (
    _dynamic_tools_env.strip().lower() in {"1", "true", "yes"}
    if _dynamic_tools_env is not None
    else _EXECUTION_SURFACE == "cli" or _surface_capabilities == "full"
)

# ---- Provider (multi-LLM support) ----
_provider_config: ProviderConfig | None = None
llm_provider: LLMProvider | None = None

# Backward-compatible aliases (used throughout the codebase)
DEEPSEEK_MODEL_SIMPLE = "deepseek-chat"   # set at init time from provider
DEEPSEEK_MODEL_COMPLEX = "deepseek-reasoner"
MODEL_NAME = "deepseek-chat (V4 auto-select)"  # updated at init
# -------- Self-learning feature flags (toggled via /self-learning) --------
_SELF_LEARNING_FLAGS: dict[str, bool] = {
    "auto_learn_conversations": True,
    "load_project_files_into_memory": True,
    "age_out_old_coded_entries": True,
    "auto_debug_tool": True,
    "consolidate_memories": True,
    "review_tools": True,
    "targeted_inquiry": True,
    "maybe_trigger_reflection": True,
    "maybe_strategy_distillation": True,
    "auto_patch_new_technology": True,
    "invent_skills": True,
}


# -------- Task Manager for multi-step tasks --------
class TaskManager:
    """Manage a list of tasks with states: pending, in_progress, done."""
    def __init__(self) -> None:
        self.tasks: list[dict] = []

    def add_task(self, description: str) -> int:
        idx = len(self.tasks)
        self.tasks.append({"description": description, "status": "pending"})
        return idx

    def set_status(self, idx: int, status: str) -> None:
        if 0 <= idx < len(self.tasks):
            self.tasks[idx]["status"] = status

    def mark_done(self, idx: int) -> None:
        self.set_status(idx, "done")

    def mark_first_pending_done(self) -> None:
        """Mark the first task whose status is pending as done."""
        for t in self.tasks:
            if t["status"] == "pending":
                t["status"] = "done"
                break

    def format(self) -> str:
        if not self.tasks:
            return "No tasks."
        lines = []
        for i, t in enumerate(self.tasks):
            status_icon = {
                "pending": "○",
                "in_progress": "◷",
                "done": "✓",
            }.get(t["status"], "?")
            desc = t["description"]
            # Defensive: escape any curly braces in description
            desc_safe = desc.replace('{', '{{').replace('}', '}}')
            lines.append(f"  {status_icon}  {desc_safe}")
        return "\n".join(lines)

    def from_llm_block(self, text: str) -> None:
        """Parse a TaskList block and populate tasks."""
        pattern = r"TaskList:\s*```(?:json)?\s*([\s\S]*?)\s*```"
        match = re.search(pattern, text)
        if not match:
            return
        raw = match.group(1).strip()
        try:
            raw_tasks = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(raw_tasks, list):
            return
        self.tasks = []
        for item in raw_tasks:
            if isinstance(item, str):
                self.add_task(item)
            elif isinstance(item, dict) and "description" in item:
                self.add_task(item["description"])

    def mark_done_from_text(self, text: str) -> None:
        """Parse TaskDone: index blocks."""
        pattern = r"TaskDone:\s*(\d+)"
        for match in re.finditer(pattern, text):
            idx = int(match.group(1))
            self.mark_done(idx)


tasks = TaskManager()

# -------- Regression Testing --------
# Core test suite covering the most important built-in tools
_CORE_TESTS = [
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

# Snapshot management for user‑defined tools
_BUILTIN_TOOL_NAMES = {
    "write_file","read_file","run_cmd","search_web","find_files","list_dir",
    "git_clone","git_status","execute_terminal_command","analyze_remote_repo",
    "list_tree","read_webpage","check_stored_data","search_memory",
    "git_diff","git_log","git_branch","git_add","git_commit",
    "git_push","git_pull","git_checkout","git_stash","git_reset",
    "git_show","git_remote",
}
_saved_user_tools: dict[str, Any] = {}

def _take_tool_snapshot() -> None:
    global _saved_user_tools
    _saved_user_tools = {k: v for k, v in AVAILABLE_TOOLS.items() if k not in _BUILTIN_TOOL_NAMES}


def _restore_tool_snapshot() -> None:
    global _saved_user_tools
    # Remove current user‑defined tools
    keys_to_remove = [k for k in AVAILABLE_TOOLS if k not in _BUILTIN_TOOL_NAMES]
    for k in keys_to_remove:
        del AVAILABLE_TOOLS[k]
    # Restore from snapshot
    AVAILABLE_TOOLS.update(_saved_user_tools)


def _run_regression_tests() -> bool:
    """Execute the core test suite and return True iff all tests pass."""
    all_pass = True
    for test in _CORE_TESTS:
        action = test["action"]
        args = test["args"]
        expected = test.get("expected", "")
        check_type = test["check"]

        fn = AVAILABLE_TOOLS.get(action)
        if fn is None:
            all_pass = False
            continue
        try:
            result = fn(args)
        except Exception as e:
            result = f"Error: {e}"

        passed = False
        if check_type == "nonempty":
            passed = bool(result.strip())
        elif check_type == "contains":
            passed = expected in result
        elif check_type == "error":
            passed = result.strip().lower().startswith("error")

        if not passed:
            all_pass = False
        else:
            pass
    return all_pass


# -------- Error‑Driven Learning (Failure Memory) --------
_FAILURE_STORE_PREFIX = "FAILURE:"
def _store_failure(original_request: str, attempted_action: str, error_info: str, resolution: str) -> None:
    """Store a failure incident in long‑term memory for later retrieval."""
    entry = (
        f"{_FAILURE_STORE_PREFIX}\n"
        f"Request: {original_request}\n"
        f"Attempted: {attempted_action}\n"
        f"Error: {error_info}\n"
        f"Resolution: {resolution}\n"
    )
    memory_bank.add_log(entry)


def _retrieve_failure(query: str, n: int = 3) -> list[str]:
    """Retrieve relevant failure records from memory."""
    results = memory_bank.recall(query, n_results=n)
    return [r for r in results if r.startswith(_FAILURE_STORE_PREFIX)]


# -------- Tool Performance Tracking --------
_tool_stats: dict[str, dict] = {}  # {tool_name: {"calls":int,"successes":int,"avg_time":float,"total_time":float}}
_total_prompt_tokens: int = 0
_total_completion_tokens: int = 0
_last_prompt_tokens: int = 0
_last_completion_tokens: int = 0
_turn_cost_log: list[dict] = []  # {"tokens":int, "time":float, "tool_calls":int}

def _track_tool_performance(action: str, result: str, elapsed: float) -> None:
    stats = _tool_stats.setdefault(action, {"calls":0,"successes":0,"total_time":0.0})
    stats["calls"] += 1
    stats["total_time"] += elapsed
    if not _is_tool_error(result):
        stats["successes"] += 1
    # persist stats to memory periodically (handled in consolidation)


# -------- Post‑Task Reflection --------
_last_task_end = time.time()
def _maybe_trigger_reflection_after_complex_task(num_tool_calls: int) -> None:
    """Trigger reflection after a multi‑tool task ends (not idle)."""
    global _last_task_end
    now = time.time()
    if now - _last_task_end < 60:
        return  # at most once per minute
    _last_task_end = now
    # Only reflect on tasks that required several tool calls
    if num_tool_calls < 2:
        return
    recent = memory_bank.get_recent(20)
    if not recent:
        return
    recent_tokens = sum(entry.get("tokens", 0) for entry in _turn_cost_log[-5:])
    recent_time = sum(entry.get("time", 0) for entry in _turn_cost_log[-5:])
    cost_summary = f"Recent token count: {recent_tokens}, recent runtime: {recent_time:.1f}s" if _turn_cost_log else ""
    reflect_prompt = (
        "You are Kyrozen's reflection module. The last task used {num_tool_calls} tool calls. "
        "Analyse whether a more efficient approach exists. "
        f"{cost_summary}\n"
        "Output the optimised strategy as a numbered list. If nothing to improve, output '—'.\n\n"
        + "\n".join(recent[-10:])
    )
    try:
        messages = [{"role": "system", "content": reflect_prompt}]
        answer = _get_llm_response(messages).strip()
        if answer and answer not in ("—", ""):
            memory_bank.add_log(f"REFLECTION:\n{answer}")
    except Exception:
        pass


def _maybe_trigger_reflection() -> None:
    """If at least 3 messages have been exchanged since last reflection and idle, reflect."""
    global _last_task_end
    now = time.time()
    if now - _last_task_end < 300:
        return  # not idle long enough
    _last_task_end = now
    recent = memory_bank.get_recent(20)
    if not recent:
        return
    # Compute token cost of recent turns
    recent_tokens = sum(
        entry.get("tokens", 0) for entry in _turn_cost_log[-5:]
    )
    recent_time = sum(
        entry.get("time", 0) for entry in _turn_cost_log[-5:]
    )
    cost_summary = f"Recent token count: {recent_tokens}, recent runtime: {recent_time:.1f}s" if _turn_cost_log else ""
    reflect_prompt = (
        "You are Kyrozen's reflection module. Read the recent interactions and find a non‑trivial task "
        "that took several steps. Analyse whether a more efficient approach exists. "
        f"{cost_summary}\n"
        "Output the optimised strategy as a numbered list. If nothing to improve, output '—'.\n\n"
        + "\n".join(recent[-10:])
    )
    try:
        messages = [{"role": "system", "content": reflect_prompt}]
        answer = _get_llm_response(messages).strip()
        if answer and answer not in ("—", ""):
            memory_bank.add_log(f"REFLECTION:\n{answer}")
    except Exception:
        pass


def _maybe_strategy_distillation() -> None:
    """If recent turns consumed many tokens, distill an efficient strategy."""
    if len(_turn_cost_log) < 3:
        return
    recent_total = sum(entry.get("tokens", 0) for entry in _turn_cost_log[-5:])
    if recent_total < 5000:
        return

    # Gather recent conversation context for analysis
    recent_logs = memory_bank.get_recent(30)
    if not recent_logs:
        return
    # Filter out system‑internal entries
    user_logs = [r for r in recent_logs if r and not r.startswith(("FILE:", "FACT:", "LEARNED:", "SKILL:", "DEBUG:", "TOOL_REVIEW:"))]
    if len(user_logs) < 5:
        return

    distill_prompt = (
        "You are Kyrozen's strategy distillation module. Recent turns consumed "
        f"{recent_total} tokens. Analyse the conversation patterns below and "
        "distill 1‑3 concise strategies the agent should adopt to work more "
        "efficiently (fewer tool calls, less token waste, faster execution).\n\n"
        "Output each strategy on a new line prefixed with 'STRATEGY:'.\n"
        "If no clear improvement, output '—'.\n\n"
        + "\n".join(user_logs[-15:])
    )
    try:
        messages = [{"role": "system", "content": distill_prompt}]
        answer = _get_llm_response(messages).strip()
        if answer and answer not in ("—", ""):
            for line in answer.split("\n"):
                line = line.strip()
                if line.startswith("STRATEGY:"):
                    memory_bank.add_log(f"STRATEGY: {line}")
    except Exception:
        pass


# -------- Sleep & Consolidation (Dream Cycle) --------
_last_user_interaction = time.time()
_last_code_scan_time = 0

def _age_out_old_coded_entries() -> None:
    """Remove file snapshots for .py files that no longer exist on disk."""
    global _last_code_scan_time
    now = time.time()
    if now - _last_code_scan_time < 3600:  # once per hour
        return
    _last_code_scan_time = now
    project_root = Path(__file__).parent.resolve()
    skip_dirs = {".venv", "venv", "chroma_memory", "__pycache__", ".git"}
    valid: set[str] = set()
    for py_file in project_root.rglob("*.py"):
        if not any(part in py_file.parts for part in skip_dirs):
            valid.add(str(py_file.relative_to(project_root)))
    memory_bank.remove_stale_files(valid)

def _consolidate_memories() -> None:
    global _saved_user_tools
    _take_tool_snapshot()
    """Cluster, deduplicate and summarise recent facts and logs."""
    recent = memory_bank.get_recent(50)
    if not recent:
        return

    # Keep only non‑trivial logs
    non_trivial = [r for r in recent if r and not r.startswith("FILE:")]
    if len(non_trivial) < 3:
        return

    consolidate_prompt = (
        "You are Kyrozen's memory consolidation module. Read the following recent logs "
        "and merge duplicates, remove contradictions (keeping the most recent), "
        "and extract a minimal set of important facts. "
        "Output each consolidated fact on a new line prefixed with 'FACT:'. "
        "If nothing important, output only '—'.\n\n"
        + "\n".join(non_trivial)
    )
    try:
        messages = [{"role": "system", "content": consolidate_prompt}]
        answer = _get_llm_response(messages).strip()
        if answer and not answer.startswith("—"):
            # Store consolidated facts
            for line in answer.split("\n"):
                line = line.strip()
                if line.startswith("FACT:"):
                    memory_bank.add_log(line)
            # Remove the original logs that were just consolidated (to avoid duplication)
            # We can delete them via ChromaDB if available
            _remove_consolidated_entries(non_trivial)
    except Exception:
        pass


def _remove_consolidated_entries(logs: list[str]) -> None:
    """Try to delete exact matching logs from ChromaDB."""
    if memory_bank._collection is None:
        return
    try:
        # Get all documents, find those that match exactly any of logs
        all_logs = memory_bank._collection.get()
        ids = all_logs.get("ids", [])
        docs = all_logs.get("documents", [])
        to_delete = []
        for i, doc in enumerate(docs):
            if doc in logs:
                to_delete.append(ids[i])
        if to_delete:
            memory_bank._collection.delete(ids=to_delete)
    except Exception:
        pass

def _register_tool(name: str, code: str, description: str = "") -> bool:
    return False

# -------- Tool Refactoring --------
def _review_tools() -> None:
    """Analyse tool usage patterns and suggest merges or improvements."""
    if len(_tool_stats) < 3:
        return  # not enough data

    # Build a summary of tool usage
    summary_lines = []
    for name, stats in sorted(_tool_stats.items(),
                              key=lambda x: x[1].get("calls", 0), reverse=True)[:10]:
        calls = stats.get("calls", 0)
        successes = stats.get("successes", 0)
        avg_time = stats.get("total_time", 0) / max(calls, 1)
        summary_lines.append(
            f"  {name}: {calls} calls, {successes} successes, "
            f"avg {avg_time:.2f}s per call"
        )

    if not summary_lines:
        return

    review_prompt = (
        "You are Kyrozen's tool review module. Review the following tool usage "
        "statistics and suggest improvements:\n"
        "- Are there tools that could be merged (similar functionality)?\n"
        "- Are any tools under‑used and candidates for removal?\n"
        "- Could any tool be made faster or more robust?\n"
        "Output each suggestion on a new line prefixed with 'TOOL_REVIEW:'.\n"
        "If no improvements needed, output '—'.\n\n"
        + "\n".join(summary_lines)
    )
    try:
        messages = [{"role": "system", "content": review_prompt}]
        answer = _get_llm_response(messages).strip()
        if answer and answer not in ("—", ""):
            for line in answer.split("\n"):
                line = line.strip()
                if line.startswith("TOOL_REVIEW:"):
                    memory_bank.add_log(f"TOOL_REVIEW: {line}")
    except Exception:
        pass


# -------- Active Exploration (Self‑Learning Code Analysis) --------
_last_inquiry_time = time.time()

def _targeted_inquiry() -> None:
    """Scan project files for undocumented functions and infer their purpose via LLM.
    This is self-learning: the agent analyses code itself, never asks the user."""
    global _last_inquiry_time
    now = time.time()
    if now - _last_user_interaction < 300:
        return
    if now - _last_inquiry_time < 600:  # once per 10 min
        return
    _last_inquiry_time = now

    project_root = Path(__file__).parent.resolve()
    for py_file in project_root.rglob("*.py"):
        if "__pycache__" in str(py_file) or py_file.name.startswith("test_"):
            continue
        content = py_file.read_text(encoding="utf-8", errors="ignore")
        # Find function definitions without docstrings
        for match in re.finditer(
            r"def\s+(\w+)\s*\(([^)]*)\)\s*(?:->\s*\S+\s*)?:\s*\n(\s+)(\S.*)",
            content
        ):
            func_name = match.group(1)
            func_params = match.group(2)
            indent = match.group(3)
            first_line = match.group(4).strip()

            # Skip if it already has a docstring
            if first_line.startswith('"""') or first_line.startswith("'''"):
                continue

            # Extract function body (up to ~30 lines for context)
            func_start = match.start()
            body_start = content.index("\n", match.end(3)) + 1
            lines = content[body_start:].split("\n")
            body_lines = []
            for line in lines:
                if line.strip() and not line.startswith(indent):
                    break
                body_lines.append(line)
                if len(body_lines) >= 30:
                    break
            body_text = "\n".join(body_lines)

            # Use LLM to infer what this function does
            analyze_prompt = (
                "You are Kyrozen's code analysis module. Examine this Python "
                f"function from {py_file.name} and infer its purpose.\n\n"
                f"```python\ndef {func_name}({func_params}):\n{body_text}\n```\n\n"
                "Output a single line starting with 'PURPOSE: ' followed by "
                "a concise description of what this function does, its inputs, "
                "and its outputs. If unclear, output 'PURPOSE: unclear'."
            )
            try:
                messages = [{"role": "system", "content": analyze_prompt}]
                answer = _get_llm_response(messages).strip()
                if answer.startswith("PURPOSE: "):
                    purpose = answer[len("PURPOSE: "):].strip()
                    if purpose.lower() != "unclear":
                        memory_bank.add_log(
                            f"CODE_DOC: Function '{func_name}' in {py_file.name} "
                            f"({func_params}) — {purpose}"
                        )
            except Exception:
                pass
            return  # one function per cycle


def _invent_skills() -> None:
    """Examine recent conversation logs and create a reusable skill
    (workflow) that the agent can later call via memory retrieval."""
    recent = memory_bank.get_recent(40)
    if not recent:
        return
    # Exclude system‑internal logs (FILE, FACT, SKILL, LEARNED)
    logs = [
        r for r in recent
        if not r.startswith("FILE:")
        and not r.startswith("FACT:")
        and not r.startswith("LEARNED:")
        and not r.startswith("SKILL:")
    ]
    if len(logs) < 5:
        return

    prompt = (
        "You are Kyrozen's skill invention module. Read the following recent "
        "conversation logs and extract a reusable skill (workflow) that the "
        "agent could follow in the future to accomplish similar tasks more "
        "efficiently.\n\n"
        "Output exactly in this format, nothing else:\n\n"
        "Skill Name: <short name>\n"
        "Description: <short description>\n"
        "Steps:\n"
        "1. <step>\n"
        "2. <step>\n"
        "...\n\n"
        "If you cannot identify a useful skill, output only the single character '—'.\n\n"
        + "\n".join(logs[-20:])
    )
    try:
        messages = [{"role": "system", "content": prompt}]
        answer = _get_llm_response(messages).strip()
        if answer in ("—", ""):
            return
        # Parse the answer
        name_match = re.search(r"Skill Name:\s*(.+)", answer, re.IGNORECASE)
        desc_match = re.search(r"Description:\s*(.+)", answer, re.IGNORECASE)
        steps_match = re.search(r"Steps:\s*(.+)", answer, re.DOTALL | re.IGNORECASE)
        skill_name = name_match.group(1).strip() if name_match else "unknown"
        description = desc_match.group(1).strip() if desc_match else ""
        steps_text = steps_match.group(1).strip() if steps_match else ""
        lines = steps_text.split("\n")
        steps_clean = [line.strip() for line in lines if line.strip() and line.strip()[:1].isdigit()]
        steps_str = "\n".join(steps_clean)
        stored = f"SKILL: {skill_name} | {description}\nSteps:\n{steps_str}"
        memory_bank.add_log(stored)
        # Also log a short FACT so the agent sees it quickly
        memory_bank.add_log(f"FACT: Learned a reusable skill called '{skill_name}' "
                            f"( {description} )")
    except Exception:
        pass


# -------- Auto‑Patching (Background Knowledge) --------
_known_libraries = set()
def _auto_patch_new_technology(user_input: str) -> None:
    """If user mentions an unknown library, fetch its docs in background."""
    detected: set[str] = set()

    def _extract_words(text: str) -> list[str]:
        """Split text into words, handling CJK by treating each CJK char as a word boundary."""
        result: list[str] = []
        buf: list[str] = []
        for ch in text:
            cp = ord(ch)
            if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
                0x20000 <= cp <= 0x2A6DF or 0x3040 <= cp <= 0x30FF or
                0xAC00 <= cp <= 0xD7AF):
                if buf:
                    result.extend(''.join(buf).split())
                    buf = []
            elif ch.isspace() or ch in '"\'`,.;:!?()[]{}':
                if buf:
                    result.extend(''.join(buf).split())
                    buf = []
            else:
                buf.append(ch)
        if buf:
            result.extend(''.join(buf).split())
        return result or text.split()

    words = _extract_words(user_input)
    for w in words:
        # Pattern 1: suffix based detection (lib, py, framework, package)
        if w.endswith(("lib","py","framework","package")) and w not in _known_libraries:
            detected.add(w)

    # Pattern 2: detect "use <Library>" or "using <Library>" phrases
    for match in re.finditer(r"(?:use|using)\s+([A-Za-z_]\w*)", user_input, re.IGNORECASE | re.UNICODE):
        lib = match.group(1)
        if lib.lower() not in ("a","an","the","this","that","it","my","our","your"):
            detected.add(lib)

    # Pattern 3: import statements in code or conversation
    for match in re.finditer(r"(?:import|from)\s+([A-Za-z_]\w*)", user_input, re.UNICODE):
        lib = match.group(1)
        if lib.lower() not in ("a","an","the","os","sys","re","json","time","math"):
            detected.add(lib)

    # Pattern 4: "pip install <lib>" or "install <lib>"
    for match in re.finditer(r"(?:pip\s+install|install)\s+([A-Za-z_][\w.-]*)", user_input, re.IGNORECASE | re.UNICODE):
        lib = match.group(1)
        detected.add(lib)

    # Pattern 5: well‑known library heuristics (capitalised or compound names)
    well_known = {
        "numpy","pandas","scipy","matplotlib","seaborn","plotly",
        "sklearn","scikit-learn","tensorflow","keras","pytorch","torch",
        "flask","django","fastapi","starlette","aiohttp","httpx","requests",
        "sqlalchemy","alembic","pydantic","celery","redis","rabbitmq",
        "pytest","unittest","mypy","ruff","black","isort","pre-commit",
        "docker","kubernetes","nginx","postgresql","mysql","mongodb",
        "react","vue","angular","svelte","next.js","tailwind","bootstrap",
        "graphql","grpc","protobuf","websocket","openapi","swagger",
    }
    for w in words:
        clean = w.strip('"\'`,.;:!?()[]{}').lower()
        if clean in well_known and clean not in _known_libraries:
            detected.add(clean)

    # Spawn background fetches for new discoveries
    for lib in detected:
        _known_libraries.add(lib)
        threading.Thread(target=_fetch_library_info, args=(lib,), daemon=True).start()


def _fetch_library_info(lib_name: str) -> None:
    """Search web for core concepts and store in memory."""
    from tools import search_web
    try:
        result = search_web(f"{lib_name} documentation overview")
        if result and "Search" not in result:
            memory_bank.add_log(f"LIBRARY_INFO: {lib_name}\n{result[:2000]}")
    except Exception:
        pass


# ---- Spinner for LLM waiting ----
_SPINNER_STOP = threading.Event()
_SPINNER_THREAD: threading.Thread | None = None

# _SPINNER_FRAMES defined at module level (dual-set Unicode/ASCII)

def _spinner_worker(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        for frame in _SPINNER_FRAMES:
            if stop_event.is_set():
                break
            sys.stdout.write("\r" + frame + " ")
            sys.stdout.flush()
            time.sleep(0.25)

def _call_llm_with_spinner(messages: list[dict], model: str | None = None) -> str:
    global _SPINNER_STOP, _SPINNER_THREAD
    _SPINNER_STOP.clear()
    _SPINNER_THREAD = threading.Thread(target=_spinner_worker, args=(_SPINNER_STOP,), daemon=True)
    _SPINNER_THREAD.start()
    try:
        result = _get_llm_response(messages, model=model)
    finally:
        _SPINNER_STOP.set()
        if _SPINNER_THREAD:
            _SPINNER_THREAD.join(timeout=2)
        sys.stdout.write("\r" + " " * 70 + "\r")
        sys.stdout.flush()
    return result

# ---- Existing functions (unchanged) ----
def _load_config_key() -> str | None:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                data = json.load(f)
            key = data.get("api_key")
            if data.get("encrypted") and isinstance(key, str):
                key = decrypt_api_key(key)
            if key and isinstance(key, str) and key.strip():
                os.environ["DEEPSEEK_API_KEY"] = key.strip()
                return key.strip()
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _save_config_key(key: str) -> None:
    try:
        existing = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r") as f:
                existing = json.load(f)
        existing["api_key"] = encrypt_api_key(key.strip())
        existing["encrypted"] = True
        existing["encryption"] = "fernet"
        with open(CONFIG_PATH, "w") as f:
            json.dump(existing, f, indent=2)
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        console.print(f"[{_WARNING}]Warning: could not save API key to config file.[/{_WARNING}]")


def _prompt_and_init_deepseek() -> None:
    """Detect provider, prompt for API key if needed, initialise the LLM client."""
    global _provider_config, llm_provider, DEEPSEEK_MODEL, DEEPSEEK_MODEL_SIMPLE, DEEPSEEK_MODEL_COMPLEX, MODEL_NAME

    _provider_config = detect_provider()

    # If no API key is stored, prompt the user
    if not _provider_config.api_key:
        console.print(f"\n{_provider_config.provider.title()} API key not set.")
        env_var = PROVIDER_ENV_VARS.get(_provider_config.provider, "")
        hint = f" (set {env_var})" if env_var else ""
        try:
            key = console.input(
                f"[bold yellow]Enter your {_provider_config.provider.title()} API key{hint}: [/bold yellow]"
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nCancelled.")
            llm_provider = None
            sys.exit(0)
        if not key:
            console.print("No API key entered – use /quit to exit.")
            llm_provider = None
            sys.exit(0)
        _provider_config.api_key = key
        save_provider_config_encrypted(_provider_config)

    # Set provider-specific env var for subprocesses / SDK auto-detection
    env_var = PROVIDER_ENV_VARS.get(_provider_config.provider, "")
    if env_var:
        os.environ[env_var] = _provider_config.api_key

    # Set the model names from provider config
    DEEPSEEK_MODEL_SIMPLE = _provider_config.model_simple
    DEEPSEEK_MODEL_COMPLEX = _provider_config.model_complex
    DEEPSEEK_MODEL = DEEPSEEK_MODEL_SIMPLE
    MODEL_NAME = f"{_provider_config.provider} ({DEEPSEEK_MODEL_SIMPLE})"

    # Create the provider instance (with fallback chain)
    llm_provider = get_fallback_provider(_provider_config)

    # Validate configuration
    issues = _provider_config.validate()
    if issues:
        for issue in issues:
            console.print(f"[{_WARNING}]Config: {issue}[/{_WARNING}]")


DEEPSEEK_MODEL: str = DEEPSEEK_MODEL_SIMPLE


def _switch_provider() -> None:
    """Interactive menu to switch LLM provider at runtime."""
    global _provider_config, llm_provider, DEEPSEEK_MODEL, DEEPSEEK_MODEL_SIMPLE, DEEPSEEK_MODEL_COMPLEX, MODEL_NAME

    providers_list = list(PROVIDER_DEFAULT_MODELS.keys())
    console.print(f"\n[bold {_ACCENT}]═══ Switch LLM Provider ═══[/bold {_ACCENT}]")
    for i, p in enumerate(providers_list):
        marker = " ●" if _provider_config.provider == p else "  "
        console.print(f"  [{_MUTED}]{i+1}.[/{_MUTED}]{marker} {p.title()}")
    console.print()

    choice = console.input("[bold cyan]Choose provider (number) or 'cancel': [/bold cyan]").strip().lower()
    if choice == "cancel":
        return
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(providers_list):
            new_provider = providers_list[idx]
            if new_provider == _provider_config.provider:
                console.print(f"[{_WARNING}]Already using {new_provider.title()}.[/{_WARNING}]")
                return
            _provider_config.provider = new_provider
            _provider_config.model_simple = PROVIDER_DEFAULT_MODELS[new_provider][0]
            _provider_config.model_complex = PROVIDER_DEFAULT_MODELS[new_provider][1]

            # Prompt for API key if needed
            env_var = PROVIDER_ENV_VARS.get(new_provider, "")
            if env_var:
                key = console.input(
                    f"[bold yellow]Enter {new_provider.title()} API key "
                    f"(or press Enter to use {env_var}): [/bold yellow]"
                ).strip()
                if key:
                    _provider_config.api_key = key

            save_provider_config_encrypted(_provider_config)

            # Re-initialize the provider
            env_var = PROVIDER_ENV_VARS.get(new_provider, "")
            if env_var and _provider_config.api_key:
                os.environ[env_var] = _provider_config.api_key

            DEEPSEEK_MODEL_SIMPLE = _provider_config.model_simple
            DEEPSEEK_MODEL_COMPLEX = _provider_config.model_complex
            DEEPSEEK_MODEL = DEEPSEEK_MODEL_SIMPLE
            MODEL_NAME = f"{new_provider} ({DEEPSEEK_MODEL_SIMPLE})"
            llm_provider = get_provider(_provider_config)

            console.print(f"[{_SUCCESS}]Switched to {new_provider.title()} ({DEEPSEEK_MODEL_SIMPLE}).[/{_SUCCESS}]")
        else:
            console.print(f"[{_ERROR}]Invalid number.[/{_ERROR}]")
    except ValueError:
        console.print(f"[{_ERROR}]Please enter a number or 'cancel'.[/{_ERROR}]")


def _load_project_files_into_memory() -> None:
    project_root = Path(__file__).parent.resolve()
    # Collect valid relative paths for stale‑file cleanup
    skip_dirs = {".venv", "venv", "chroma_memory", "__pycache__", ".git"}
    valid_paths: set[str] = set()
    for py_file in project_root.rglob("*.py"):
        if any(part in py_file.parts for part in skip_dirs):
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
            rel_path = str(py_file.relative_to(project_root))
            valid_paths.add(rel_path)
            memory_bank.add_file(rel_path, content)
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception:
            pass
    # Remove FILE snapshots for paths that no longer exist on disk
    memory_bank.remove_stale_files(valid_paths)


def _self_update() -> str:
    """Pull the latest version from the git remote."""
    try:
        result = subprocess.run(
            ["git", "pull"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = result.stdout.strip() or ""
        err = result.stderr.strip() or ""
        if result.returncode != 0:
            return f"Update failed:\n{err}"
        return f"Updated successfully:\n{out}"
    except subprocess.TimeoutExpired:
        return "Update timed out."
    except FileNotFoundError:
        return "Error: git not installed or not inside a git repository."
    except Exception as e:
        return f"Error during update: {e}"


# ================================================================
# Feature 1: Context Compression
# ================================================================

_context_compression_size = 30000  # chars — above this, compress old turns
_context_compression_target = 8000   # chars — compress down to this

def _summarize_old_turns() -> None:
    """Compress old conversation turns when short_term_memory grows too large.
    Keeps the most recent messages and summarizes older ones into a compact note."""
    global short_term_memory

    total_chars = sum(len(msg.get("content", "")) for msg in short_term_memory)
    if total_chars < _context_compression_size:
        return

    # Keep the last 4 messages (2 turns) as-is, summarize everything before
    keep_count = min(4, len(short_term_memory))
    to_summarize = short_term_memory[:-keep_count]

    if len(to_summarize) < 4:
        return  # not enough to compress meaningfully

    # Build the text to summarize
    summary_input = []
    for msg in to_summarize:
        role = msg.get("role", "?")
        content = msg.get("content", "")[:500]  # truncate per-message
        summary_input.append(f"[{role}]: {content}")

    compress_prompt = (
        "Summarize this conversation history into a tight bullet list. "
        "Include: key decisions, files changed, bugs fixed, facts learned. "
        "Omit greetings and filler. Output as plain text, max 800 chars.\n\n"
        + "\n".join(summary_input[-20:])  # last 20 messages at most
    )

    try:
        summary = _get_llm_response(
            [{"role": "system", "content": compress_prompt}]
        ).strip()
    except Exception:
        summary = "(conversation compressed)"

    if not summary or len(summary) < 10:
        summary = "(conversation compressed)"

    # Replace old messages with a single summary message
    compressed_msg = {
        "role": "system",
        "content": f"[Compressed history — {len(to_summarize)} earlier messages]:\n{summary}"
    }
    short_term_memory = [compressed_msg] + short_term_memory[-keep_count:]

# ================================================================
# Feature 2: Fix Verification Loop
# ================================================================

_fix_outcomes: list[dict] = []  # [{error_sig, fix_desc, success, timestamp}]

def _track_fix_outcome(user_input: str, agent_reply: str) -> None:
    """Track whether a bug fix was successful based on user feedback.
    Call this after each turn to build a fix-outcome database."""
    low_input = user_input.lower()
    low_reply = agent_reply.lower()

    # Detect positive feedback
    positive = any(kw in low_input for kw in [
        "thanks", "thank you", "that works", "it works", "worked",
        "fixed", "solved", "great", "perfect", "awesome", "谢", "谢谢",
        "好了", "可以了", "搞定", "没问题"
    ])
    # Detect negative feedback
    negative = any(kw in low_input for kw in [
        "not working", "still broken", "didn't work", "doesn't work",
        "wrong", "incorrect", "not correct", "not fixed", "still fails",
        "还不行", "还是错", "没好", "没解决", "不对"
    ])

    if not positive and not negative:
        return  # no clear feedback signal

    # Find the most recent bug-fix attempt from memory
    recent = memory_bank.get_recent(5)
    fix_context = ""
    for r in recent:
        if "FAILURE:" in r or "bug" in r.lower() or "fix" in r.lower() or "error" in r.lower():
            fix_context = r[:500]
            break

    outcome = {
        "timestamp": datetime.datetime.now(UTC).isoformat(),
        "success": positive,
        "user_feedback": user_input[:200],
        "fix_context": fix_context[:300],
    }
    _fix_outcomes.append(outcome)
    # Keep only last 20
    if len(_fix_outcomes) > 20:
        _fix_outcomes.pop(0)

    # Store the outcome in long-term memory
    verdict = "SUCCESS" if positive else "FAILURE"
    memory_bank.add_log(
        f"FIX_OUTCOME: {verdict} | User said: {user_input[:100]}\n"
        f"Fix context: {fix_context[:300]}"
    )

    # If failure detected, trigger auto-debug
    if negative:
        _store_failure(
            user_input,
            "bug-fix-attempt",
            f"User indicated fix did not work: {user_input[:200]}",
            f"Agent replied: {agent_reply[:200]}"
        )

def _get_fix_success_rate() -> float:
    """Return the success rate of recent bug fixes."""
    if not _fix_outcomes:
        return 1.0  # no data, assume success
    successes = sum(1 for o in _fix_outcomes if o["success"])
    return successes / len(_fix_outcomes)

# ================================================================
# Feature 3: DefineTool — Dynamic Tool Creation from SKILLs
# ================================================================

def _register_tool(name: str, code: str, description: str = "") -> bool:
    """Register a new callable tool dynamically. Returns True on success."""
    if not ALLOW_DYNAMIC_TOOLS:
        return False
    if not name or not code:
        return False
    # Validate: name must be a valid identifier
    if not re.match(r"^[a-zA-Z_]\w*$", name):
        return False

    # Don't overwrite built-in tools
    if name in _BUILTIN_TOOL_NAMES:
        return False

    # Compile the tool function
    try:
        local_ns: dict[str, Any] = {}
        exec(code, {"__builtins__": __builtins__}, local_ns)
        fn = local_ns.get(name)
        if fn is None or not callable(fn):
            # Try to find any top-level function
            for v in local_ns.values():
                if callable(v) and hasattr(v, "__name__"):
                    fn = v
                    break
            if fn is None:
                return False
    except Exception:
        return False

    # Apply description
    if description and hasattr(fn, "__doc__"):
        pass  # uses its own docstring
    elif description:
        fn.__doc__ = description

    # Register
    AVAILABLE_TOOLS[name] = fn
    # Rebuild tools list for system prompt
    global TOOLS_LIST
    TOOLS_LIST = _build_tools_list()

    # Log the new tool
    memory_bank.add_log(f"TOOL_CREATED: {name} — {description}")
    return True


def _attempt_define_tool(text: str) -> bool:
    r"""Parse a DefineTool block from LLM output and register the tool.
    Format:
    DefineTool:
    ```python
    def tool_name(args: str) -> str:
        '''Description'''
        ...
    ```
    """
    if not ALLOW_DYNAMIC_TOOLS:
        return False

    pattern = r"DefineTool:\s*```(?:python)?\s*([\s\S]*?)\s*```"
    match = re.search(pattern, text)
    if not match:
        return False

    code = match.group(1).strip()
    if not code:
        return False

    # Extract function name and description
    name_match = re.search(r"def\s+(\w+)\s*\(", code)
    if not name_match:
        return False
    name = name_match.group(1)

    desc_match = re.search(r'"""([^"]*)"""', code) or re.search(r"'''([^']*)'''", code)
    description = desc_match.group(1).strip() if desc_match else ""

    return _register_tool(name, code, description)

# ================================================================
# Feature 4: User Preference Model
# ================================================================

_user_preferences: dict[str, Any] = {
    "language": "",           # preferred programming language
    "naming_style": "",       # snake_case, camelCase, etc.
    "comment_style": "",      # verbose, minimal, docstring-only
    "indent": "",             # spaces-4, tabs, spaces-2
    "formatter": "",          # black, ruff, none
    "verbosity": "",          # concise, detailed, balanced
    "prefers_tables": False,  # user likes table-format output
    "code_first": False,      # user prefers code before explanation
}

def _detect_user_preferences(user_input: str) -> None:
    """Detect implicit user preferences from their messages."""
    low = user_input.lower()

    # Language preference
    lang_signals = {
        "python": "python", "py": "python",
        "javascript": "javascript", "js": "javascript",
        "typescript": "typescript", "ts": "typescript",
        "rust": "rust", "go": "go", "golang": "go",
        "java": "java", "c++": "c++", "cpp": "c++",
        "ruby": "ruby", "swift": "swift",
    }
    for signal, lang in lang_signals.items():
        if re.search(rf"\b{signal}\b", low):
            _user_preferences["language"] = lang
            break

    # Naming style
    if re.search(r"\b[a-z]+_[a-z]+\b", user_input) and "rust" not in low:
        _user_preferences["naming_style"] = "snake_case"
    elif re.search(r"\b[a-z]+[A-Z][a-z]+\b", user_input):
        _user_preferences["naming_style"] = "camelCase"

    # Verbosity
    if any(w in low for w in ["brief", "short", "concise", "quick"]):
        _user_preferences["verbosity"] = "concise"
    elif any(w in low for w in ["detailed", "explain fully", "in depth", "comprehensive"]):
        _user_preferences["verbosity"] = "detailed"

    # Code-first
    if any(w in low for w in ["show me the code", "just the code", "code only"]):
        _user_preferences["code_first"] = True

    # Tables
    if any(w in low for w in ["table", "comparison table", "tabular"]):
        _user_preferences["prefers_tables"] = True

    # Store detected preferences
    detected = {k: v for k, v in _user_preferences.items() if v}
    if detected:
        pref_str = "; ".join(f"{k}={v}" for k, v in detected.items())
        # Only store if new or changed
        recent = memory_bank.get_recent(3)
        already = any(f"PREF: {pref_str}" in r for r in recent)
        if not already:
            memory_bank.add_log(f"PREF: {pref_str}")


def _build_preference_context() -> str:
    """Build a system message describing known user preferences."""
    active = [f"{k}={v}" for k, v in _user_preferences.items()
              if v and v not in ("", False)]
    if not active:
        return ""
    return (
        "## Known user preferences (apply unless overridden)\n"
        + "\n".join(f"- {p}" for p in active)
    )

# ================================================================
# Feature 5: Autonomous Inspection
# ================================================================

_last_inspection_time: float = 0
_inspection_interval = 1800  # 30 minutes between inspections

def _autonomous_inspection() -> None:
    """Proactive codebase health check: outdated packages, code smells, security issues."""
    global _last_inspection_time
    now = time.time()
    if now - _last_inspection_time < _inspection_interval:
        return
    if now - _last_user_interaction < 300:  # user active, don't disturb
        return
    _last_inspection_time = now

    findings: list[str] = []

    # 1. Check for outdated pip packages
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--outdated", "--format=columns"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().split("\n")
            if len(lines) > 2:  # header + at least one package
                outdated_pkgs = lines[2:6]  # top 3 outdated
                findings.append("Outdated packages:\n  " + "\n  ".join(outdated_pkgs[:3]))
    except Exception:
        pass

    # 2. Check for common code smells (bare except, print debugging, TODO markers)
    project_root = Path(__file__).parent.resolve()
    bare_excepts = 0
    print_debugs = 0
    todo_markers = 0
    skip_dirs = {".venv", "venv", "chroma_memory", "__pycache__", ".git"}

    for py_file in project_root.rglob("*.py"):
        if any(part in py_file.parts for part in skip_dirs):
            continue
        try:
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            rel = str(py_file.relative_to(project_root))

            # Count bare excepts
            bare = len(re.findall(r"except\s*:", content))
            if bare > 0:
                bare_excepts += bare

            # Count print debugging
            prints = len(re.findall(r"^\s*print\(", content, re.MULTILINE))
            if prints > 2:  # more than 2 prints in a file
                print_debugs += prints

            # Count TODOs
            todos = len(re.findall(r"#\s*TODO", content))
            if todos > 0:
                todo_markers += todos
        except Exception:
            pass

    if bare_excepts > 0:
        findings.append(f"Code smell: {bare_excepts} bare 'except:' clauses (should specify exception type)")
    if print_debugs > 0:
        findings.append(f"Code smell: {print_debugs} print() statements may be leftover debugging")
    if todo_markers > 0:
        findings.append(f"Code hygiene: {todo_markers} TODO markers found in codebase")

    # 3. Check .gitignore for common missing entries
    gitignore_path = project_root / ".gitignore"
    if gitignore_path.exists():
        try:
            gi_content = gitignore_path.read_text(encoding="utf-8")
            missing = []
            for entry in ["*.log", "*.swp", ".env", "dist/", "build/"]:
                if entry not in gi_content:
                    missing.append(entry)
            if missing:
                findings.append(f"Gitignore missing entries: {', '.join(missing)}")
        except Exception:
            pass

    if not findings:
        return

    # Store findings in memory for the agent to act on next time user asks
    report = "INSPECTION: Autonomous codebase health check\n" + "\n".join(f"- {f}" for f in findings)
    memory_bank.add_log(report)

    # If there are security-critical findings, also log as a FACT for immediate visibility
    if bare_excepts > 5:
        memory_bank.add_log(f"FACT: Codebase has {bare_excepts} bare except clauses — potential bug masking")

# ================================================================
# End of new features
# ================================================================


# ================================================================
# Feature 6: Memory Importance Scoring
# ================================================================

def _score_memory_importance(entry: str) -> int:
    """Score a memory entry 0-10 based on its apparent importance."""
    score = 1
    if entry.startswith("FACT:"): score += 3
    if entry.startswith("FAILURE:"): score += 5
    if entry.startswith("FIX_OUTCOME:"): score += 4
    if entry.startswith("STRATEGY:"): score += 4
    if entry.startswith("PREF:"): score += 3
    if entry.startswith("SKILL:"): score += 3
    if entry.startswith("FILE:"): score -= 2
    if len(entry) > 200: score += 1
    if len(entry) > 500: score += 1
    if any(kw in entry.lower() for kw in ["bug", "fix", "error", "critical", "important"]): score += 2
    return max(0, min(10, score))

# ================================================================
# Feature 7: Bad Learning Rollback — /forget command
# ================================================================

def _forget_recent(prefix: str = "", count: int = 5) -> str:
    """Show recent learnable entries and allow the user to delete them."""
    recent = memory_bank.get_recent(100)
    learnable = [r for r in recent if r and not r.startswith("FILE:")]
    if prefix:
        learnable = [r for r in learnable if prefix.lower() in r.lower()]
    learnable = learnable[:count]
    if not learnable:
        return "No matching memories found."
    lines = ["Recent learnings (most recent first):"]
    for i, entry in enumerate(learnable):
        score = _score_memory_importance(entry)
        snippet = entry[:120].replace("\n", " ")
        lines.append(f"  [{i}] (score:{score}) {snippet}")
    return "\n".join(lines)

# ================================================================
# Feature 8: Knowledge Graph Extraction
# ================================================================

_knowledge_graph: dict[str, list[str]] = {}

def _extract_knowledge_graph() -> None:
    """Extract entities and relationships from memory for structured knowledge."""
    recent = memory_bank.get_recent(50)
    facts = [r for r in recent if r and r.startswith("FACT:")][:10]
    if len(facts) < 3:
        return
    prompt = (
        "Extract key entities and relationships from these facts. "
        "Output in format 'entity -> related_entity' (one per line).\n\n"
        + "\n".join(f"  - {f[:200]}" for f in facts)
    )
    try:
        answer = _get_llm_response([{"role": "system", "content": prompt}]).strip()
        for line in answer.split("\n"):
            if "->" in line:
                parts = line.split("->", 1)
                src = parts[0].strip().lower()
                dst = parts[1].strip().lower()
                if src and dst:
                    _knowledge_graph.setdefault(src, []).append(dst)
                    memory_bank.add_log(f"GRAPH: {src} -> {dst}")
    except Exception:
        pass

# ================================================================
# Feature 9: Sandbox — Restrict file operations to workspace
# ================================================================

_workspace_root: str = ""

def _set_workspace_root(path: str) -> None:
    global _workspace_root
    _workspace_root = os.path.abspath(os.path.expanduser(path))
    _set_tools_workspace_root(_workspace_root)

def _is_path_safe(path: str) -> bool:
    if not _workspace_root:
        return True
    try:
        root = os.path.realpath(_workspace_root)
        resolved = os.path.realpath(os.path.expanduser(path))
        return os.path.commonpath([root, resolved]) == root
    except ValueError:
        return False
    except Exception:
        return False

# ================================================================
# Feature 10: Skill Composition — Chain SKILLs into workflows
# ================================================================

def _compose_skills(task_description: str) -> str | None:
    """Find relevant SKILLs from memory and build a composed workflow."""
    recent = memory_bank.get_recent(100)
    skills = [r for r in recent if r and r.startswith("SKILL:")]
    if not skills:
        return None
    skills_text = "\n".join(s[:300] for s in skills[-10:])
    prompt = (
        "Given this task and these available skills, compose a workflow. "
        "Output steps as '1. <SkillName>: <what to do>'. "
        "If no skills match, output '—'.\n\n"
        f"Task: {task_description}\n\nSkills:\n{skills_text}"
    )
    try:
        answer = _get_llm_response([{"role": "system", "content": prompt}]).strip()
        if answer and answer != "—":
            return answer
    except Exception:
        pass
    return None


def _build_tools_list() -> str:
    lines = []
    for name, fn in AVAILABLE_TOOLS.items():
        doc = getattr(fn, "__doc__", None) or ""
        desc = doc.strip().replace("\n", " ").strip()
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


TOOLS_LIST = _build_tools_list()


def _system_prompt(tools_list: str) -> str:
    cwd = os.getcwd()
    parent = os.path.dirname(cwd)
    if os.path.basename(cwd) == "OpenKyrozen":
        cwd_note = (
            "## Current working directory\n"
            f"OpenKyrozen is installed inside `{cwd}`.\n"
            f"Your workspace (the folder you are working on) is located one level above:\n"
            f"{parent}\n\n"
            "All file operations should be relative to your workspace, not to the OpenKyrozen folder.\n"
            "For example, `list_dir('.')` will list your workspace contents, and `read_file('main.py')` will refer to a file in your workspace.\n"
            "Do not ask the user to supply a local path or a remote URL unless you intend to use the `analyze_remote_repo` tool to clone an external repository."
        )
    else:
        cwd_note = (
            "## Current working directory\n"
            "You are currently inside the project root directory of the repository the user is working in.  Relative paths (like \"README.md\" or \"main.py\") will be resolved correctly.  You can use `read_file`, `write_file`, `list_dir`, `find_files`, `run_cmd`, etc. **without needing the user to provide a path**.  Do **not** ask the user to supply a local path or a remote URL unless you intend to use the `analyze_remote_repo` tool to clone an external repository."
        )
    # Build system prompt via safe concatenation (no f‑string to avoid format‑spec collisions)
    return (
        "You are Kyrozen, an intelligent, self-learning AI assistant with file access, "
        "shell commands, and web search. Use tools when needed; converse naturally otherwise.\n\n"
        "## Available Tools\n"
        + tools_list + "\n\n"
        "## Tool invocation format\n"
        "Output exactly one Action per response:\n\n"
        "Thought: <brief reasoning>\n"
        "Action:\n"
        "```json\n"
        "{\"action\": \"tool_name\", \"args\": \"arguments\"}\n"
        "```\n\n"
        "**Args rules:**\n"
        "- `args` is always a **plain string**, never a JSON object.\n"
        "- For `write_file`: use `\"path|content\"` (pipe‑separated).\n"
        "- For `run_cmd`: the full shell command as one string.\n"
        "- Use only the action names listed above. Aliases like `bash`→`run_cmd` are accepted.\n"
        "- **CRITICAL**: Use SINGLE braces `{` and `}` in JSON, NOT double `{{` or `}}`. "
        "Correct: `{\"action\": \"read_file\", \"args\": \"main.py\"}`. "
        "Wrong: `{{\"action\": \"read_file\", \"args\": \"main.py\"}}`.\n\n"
        "## Task complexity routing\n"
        "Classify every user request and follow the corresponding protocol:\n\n"
        "**SIMPLE** (greeting, factual Q&A, single tool call): "
        "Reply directly. No Plan or TaskList needed. Example: \"hi\" → just greet back.\n\n"
        "**MEDIUM** (2‑3 related tool calls): "
        "Output a short **Plan** block (numbered list), then execute Actions one by one. "
        "No TaskList required. Example: \"list files and read README\" → Plan→list_dir→read_file→summarise.\n\n"
        "**COMPLEX** (4+ tools, multi‑step analysis, code generation): "
        "Output **Plan** → **TaskList** (JSON array) → Actions with **TaskDone: N** after each. "
        "Complete ALL tasks. Never stop early. Example: \"audit this repo\" → full workflow.\n\n"
        "## Planning format (medium/complex tasks only)\n"
        "Plan:\n"
        "1. First step – what and why\n"
        "2. Second step – what and why\n"
        "...\n\n"
        "## TaskList format (complex tasks only)\n"
        "Each task must be a **concrete, verifiable action** — specify which tool "
        "and what outcome. Bad: \"start working\", \"continue\", \"finish up\". "
        "Good: \"Use list_dir to explore project structure\", "
        "\"Use read_file to read main.py\", \"Use write_file to save report.md\".\n\n"
        "TaskList:\n"
        "```json\n"
        "[\"Use list_dir to explore the directory\", \"Use read_file on README.md\", ...]\n"
        "```\n"
        "After completing a task, output `TaskDone: <index>` on its own line before the next Action.\n\n"
        "## General rules\n"
        "- Never ask the user for permission to continue — decide and act.\n"
        "- When analysing a repo, start with `list_dir('.')`.\n"
        "- After `write_file`, use the absolute path returned in subsequent commands.\n"
        "- For stored knowledge, use `search_memory` or `check_stored_data`.\n"
        "- Do not invent tool names; use exactly the ones listed above.\n\n"
        "## Bug‑fixing workflow\n"
        "When the user reports a bug, error, or unexpected behaviour, follow this protocol:\n"
        "1. **REPRODUCE**: Read relevant code, run the failing command, capture error output.\n"
        "2. **DIAGNOSE**: Analyse the error traceback / log. Identify the root cause — do NOT guess.\n"
        "3. **HYPOTHESISE**: State what you believe is wrong and how to fix it BEFORE editing.\n"
        "4. **FIX**: Apply the minimal code change. Use `write_file` only for the necessary lines.\n"
        "5. **VERIFY**: Re‑run the original failing command. Confirm the fix works. If not, go to step 2.\n"
        "6. **EXPLAIN**: Tell the user what was wrong, what you changed, and why.\n\n"
        "## Git operations workflow\n"
        "You have full git capabilities: `git_status`, `git_diff`, `git_log`, `git_branch`,\n"
        "`git_add`, `git_commit`, `git_push`, `git_pull`, `git_checkout`, `git_stash`,\n"
        "`git_reset`, `git_show`, `git_remote`, `git_clone`.\n"
        "When performing git operations:\n"
        "- Always check `git_status` first to understand current state.\n"
        "- Before committing, run `git_diff` to review exactly what changed.\n"
        "- Write meaningful commit messages (under 72 chars, imperative mood).\n"
        "- NEVER force‑push (`--force`) unless the user explicitly requests it.\n"
        "- NEVER use `git reset --hard` without first stashing or confirming with the user.\n"
        "- Before switching branches with uncommitted changes, use `git_stash`.\n"
        "- After `git_push`, confirm success. After `git_pull`, report what was merged.\n"
        "- When creating a commit for a bug fix, prefix with \"fix: \".\n"
        "- When implementing a feature, prefix with \"feat: \".\n\n"
        "## Complex task decomposition\n"
        "For complex multi‑step tasks (COMPLEX level):\n"
        "1. **UNDERSTAND**: Read the full request. Identify all subtasks and dependencies.\n"
        "2. **PLAN**: Output a numbered Plan with 3‑10 concrete steps. Each step must be verifiable.\n"
        "3. **TASKLIST**: Create a JSON TaskList where each task maps to one Plan step.\n"
        "4. **EXECUTE**: Work through tasks in order. After each: `TaskDone: N`.\n"
        "5. **TRACK**: If a step fails, create a recovery sub‑task before continuing.\n"
        "6. **SUMMARISE**: When all tasks complete, produce a concise summary of what was done.\n"
        "CRITICAL: Never skip tasks, never stop early, never mark tasks done without executing them.\n"
        "If you get stuck on a step, try an alternative approach — do NOT abandon the task.\n"
        + cwd_note
    )


memory_bank = MemoryBank()

# ---- Tool to let agent examine its own memory ----
def _check_stored_data(args: str) -> str:
    """Return a categorized summary of stored memories.
    Usage: empty args for overview, or a category prefix like 'FACT', 'SKILL', 'STRATEGY'
    to see more entries of that type.
    Source code copies (FILE:) and raw conversation logs (User:) are excluded from the
    learning overview — FILE entries are being migrated to a separate agent_files collection."""
    # Known learning categories and their display labels
    _LEARNING_CATEGORIES: dict[str, str] = {
        "FACT:":          "Learned Facts",
        "SKILL:":         "Invented Skills",
        "STRATEGY:":      "Distilled Strategies",
        "REFLECTION:":    "Idle Reflections",
        "PREF:":          "User Preferences",
        "FAILURE:":       "Failure Records",
        "FIX_OUTCOME:":   "Bug Fix Outcomes",
        "GRAPH:":         "Knowledge Graph",
        "LEARNED:":       "Learning Logs",
        "TOOL_REVIEW:":   "Tool Reviews",
        "DEBUG_FINDING:": "Debug Findings",
        "INSPECTION:":    "Codebase Inspections",
        "CODE_DOC:":      "Code Documentation",
        "LIBRARY_INFO:":  "Library Research",
    }
    # Prefixes that are stored in agent_logs but are NOT learning artifacts
    _EXCLUDED_PREFIXES = {"FILE:", "User:"}

    try:
        total = memory_bank.count_logs()
        recent_all = memory_bank.get_recent(500)

        categorized: dict[str, list[str]] = {}
        excluded: dict[str, list[str]] = {}  # FILE:, User:, etc.
        uncategorized: list[str] = []

        for log in recent_all:
            matched = False
            # Check excluded first
            for xpfx in _EXCLUDED_PREFIXES:
                if log.startswith(xpfx):
                    excluded.setdefault(xpfx, []).append(log)
                    matched = True
                    break
            if matched:
                continue
            # Check learning categories
            for prefix in _LEARNING_CATEGORIES:
                if log.startswith(prefix):
                    categorized.setdefault(prefix, []).append(log)
                    matched = True
                    break
            if not matched:
                uncategorized.append(log)

        # Build output
        lines = [f"Total stored logs: {total}"]
        learning_count = sum(len(v) for v in categorized.values())
        excluded_count = sum(len(v) for v in excluded.values())
        lines.append(
            f"Recent window scanned: {len(recent_all)} entries "
            f"(learning: {learning_count}, excluded: {excluded_count}, "
            f"uncategorised: {len(uncategorized)})"
        )

        # If user specified a category filter
        filter_arg = args.strip().upper() if args and args.strip() else ""
        if filter_arg:
            filter_prefix = filter_arg + ":" if not filter_arg.endswith(":") else filter_arg
            if filter_prefix in _LEARNING_CATEGORIES:
                entries = categorized.get(filter_prefix, [])
                label = _LEARNING_CATEGORIES[filter_prefix]
                lines.append(f"\n## {label} ({len(entries)} recent)")
                for i, entry in enumerate(entries[:10]):
                    snippet = _safe_fstring(entry[:400].replace("\n", " "))
                    lines.append(f"  [{i+1}] {snippet}")
                if len(entries) > 10:
                    lines.append(f"  ... and {len(entries) - 10} more in the recent window.")
            elif filter_prefix in _EXCLUDED_PREFIXES:
                lines.append(f"\n'{filter_arg}' entries are excluded from the learning overview (source code or raw conversations).")
            else:
                lines.append(f"\nUnknown category '{filter_arg}'. Known learning categories: {', '.join(k.rstrip(':') for k in _LEARNING_CATEGORIES)}")
            return "\n".join(lines)

        # ---- Overview ----
        lines.append("\n## Learning Categories Overview")
        for prefix, label in _LEARNING_CATEGORIES.items():
            entries = categorized.get(prefix, [])
            count = len(entries)
            marker = "▌" if count > 0 else "·"
            lines.append(f"  {marker} {label}: {count}")
            if entries:
                sample = entries[0][:200].replace("\n", " ")
                lines.append(f"      Latest: {_safe_fstring(sample)}")

        # Excluded section
        if excluded:
            lines.append("\n## Excluded from Learning Overview")
            for xpfx, entries in excluded.items():
                label = "Legacy source code (→ agent_files)" if xpfx == "FILE:" else "Raw conversation logs"
                lines.append(f"  · {label}: {len(entries)} entries")

        if uncategorized:
            lines.append(f"\n  · Other uncategorised: {len(uncategorized)} entries")
            # Show a sample of uncategorized
            for u in uncategorized[:2]:
                snippet = u[:150].replace("\n", " ")
                lines.append(f"      {_safe_fstring(snippet)}")

        # Quick summary
        if learning_count > 0:
            lines.append(f"\nTotal self‑learning artifacts in recent window: {learning_count}")
            cat_names = [label for prefix, label in _LEARNING_CATEGORIES.items() if prefix in categorized]
            lines.append(f"Categories present: {', '.join(cat_names)}")
        else:
            lines.append("\n(No self‑learning artifacts found in the recent window. They may be deeper in storage — try `search_memory`.)")

        lines.append(f"\nTip: use `check_stored_data FACT` to drill into a category, or `search_memory <query>` for semantic search.")
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading memory: {e}"


# ---- CJK (Chinese/Japanese/Korean) compatibility helpers ----

def _has_cjk(text: str) -> bool:
    """Return True if text contains any CJK character."""
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF or   # CJK Unified Ideographs
            0x3400 <= cp <= 0x4DBF or    # CJK Ext-A
            0x20000 <= cp <= 0x2A6DF or  # CJK Ext-B
            0x3040 <= cp <= 0x309F or    # Hiragana
            0x30A0 <= cp <= 0x30FF or    # Katakana
            0xAC00 <= cp <= 0xD7AF):     # Hangul Syllables
            return True
    return False


def _cjk_approximate_words(text: str) -> int:
    """Return approximate semantic-unit count for text.
    CJK characters count 1:1 as semantic units.
    Non-CJK text is split on whitespace."""
    count = 0
    cjk_ranges = (
        range(0x4E00, 0xA000), range(0x3400, 0x4DC0),
        range(0x20000, 0x2A6E0), range(0x3040, 0x3100),
        range(0xAC00, 0xD7B0),
    )
    buf: list[str] = []
    for ch in text:
        cp = ord(ch)
        if any(cp in r for r in cjk_ranges):
            if buf:
                count += len(''.join(buf).split())
                buf = []
            count += 1
        elif ch.isspace():
            if buf:
                count += len(''.join(buf).split())
                buf = []
        else:
            buf.append(ch)
    if buf:
        count += len(''.join(buf).split())
    return max(count, 1)


def _search_memory(args: str) -> str:
    """Search stored memories for facts relevant to the query. Args: "query" """
    try:
        query = args.strip() if args else ""
        if not query:
            return "Search Error: provide a query."
        # First attempt: semantic search via ChromaDB/memory recall
        results = memory_bank.recall(query, n_results=5)
        if not results:
            # Fallback: keyword match on recent entries (FILE entries live in a
            # separate collection, so the main collection is learning-only)
            recent = memory_bank.get_recent(200)
            # CJK-aware keyword splitting: use character bigrams for CJK queries
            if _has_cjk(query):
                keywords = []
                norm = query.lower().strip()
                for i in range(len(norm)):
                    if i + 1 < len(norm):
                        keywords.append(norm[i:i+2])
                if not keywords:
                    keywords = [norm]
            else:
                keywords = query.lower().split()
            matched = []
            for doc in recent:
                if any(kw in doc.lower() for kw in keywords):
                    matched.append(doc)
            if matched:
                results = matched[:5]
            else:
                # No results even after fallback
                return "No relevant memories found."
        lines = [f"Relevant memories ({len(results)}):"]
        for i, doc in enumerate(results):
            snippet = _safe_fstring(doc[:500].replace("\n", " "))
            lines.append(f"\n--- Result {i+1} ---\n{snippet}")
        ret = "\n".join(lines)
        if ret.strip() == f"Relevant memories ({len(results)}):":
            return "No relevant memories found for that query."
        return ret
    except Exception as e:
        return f"Error searching memory: {e}"


def _build_memory_context(query: str, n: int = 3) -> str:
    """Return a formatted string of the top n relevant memories for the given query."""
    recalled = memory_bank.recall(query, n_results=n)
    if not recalled:
        return ""
    lines = ["Remembered facts that may be relevant:"]
    for r in recalled:
        snippet = r[:300].replace("\n", " ")
        # Defensively escape any curly braces to prevent format spec injection
        snippet = snippet.replace('{', '{{').replace('}', '}}')
        lines.append("- " + snippet)
    return "\n".join(lines)


AVAILABLE_TOOLS["check_stored_data"] = _check_stored_data
AVAILABLE_TOOLS["search_memory"] = _search_memory
TOOLS_LIST = _build_tools_list()

_logs_count_at_last_learn = 0
short_term_memory: list[dict[str, str]] = [
    {"role": "user", "content": "Hello, are you ready to help me?"},
    {"role": "assistant", "content": "Yes! I can use tools like search_web and write_file. How can I help?"},
]


def _build_messages(user_input: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []

    messages.append({"role": "system", "content": _system_prompt(TOOLS_LIST)})

    messages.append({
        "role": "system",
        "content": _workspace_info()
    })

    # Retrieve failure records if relevant
    failures = _retrieve_failure(user_input)
    if failures:
        failure_block = "Past failures to avoid:\n" + "\n".join(failures[:2])
        messages.append({"role": "system", "content": failure_block})

    # Inject bug-fix workflow guidance when user reports a bug
    if _is_bug_report(user_input):
        bug_guidance = (
            "BUG-FIX WORKFLOW ACTIVE: The user is reporting a bug or error. "
            "Follow this protocol EXACTLY:\n"
            "1. REPRODUCE: Read the relevant code and run the failing command. Capture error output.\n"
            "2. DIAGNOSE: Analyse the traceback/error log. Identify the ROOT CAUSE — do not guess.\n"
            "3. HYPOTHESISE: State what you believe is wrong and how to fix it BEFORE making any changes.\n"
            "4. FIX: Apply the MINIMAL code change needed. Do not refactor unrelated code.\n"
            "5. VERIFY: Re-run the original failing command. Confirm the fix works. If not, go back to step 2.\n"
            "6. EXPLAIN: Tell the user what was wrong, what you changed, and why.\n"
            "Output each step as a separate TaskList item. Do NOT skip any step."
        )
        messages.append({"role": "system", "content": bug_guidance})

    # Inject git workflow guidance when user is doing git operations
    git_indicators = ["git commit", "git push", "git pull", "git merge", "git rebase",
                       "git branch", "git checkout", "commit this", "push this",
                       "merge branch", "create a branch", "switch branch",
                       "提交代码", "推送代码", "创建分支", "合并分支"]
    if any(ind in user_input.lower() for ind in git_indicators):
        git_guidance = (
            "GIT WORKFLOW ACTIVE: The user is requesting git operations. "
            "Follow this protocol:\n"
            "1. Always run git_status first to understand current state.\n"
            "2. Before committing, run git_diff to review exactly what changed.\n"
            "3. Write meaningful commit messages (under 72 chars, imperative mood: 'fix: ...' or 'feat: ...').\n"
            "4. NEVER force-push (--force) unless the user explicitly requests it.\n"
            "5. NEVER use git reset --hard without first stashing or confirming with user.\n"
            "6. Before switching branches with uncommitted changes, use git_stash.\n"
            "7. After git_push, confirm success. After git_pull, report what was merged."
        )
        messages.append({"role": "system", "content": git_guidance})

    # Inject user preferences
    pref_ctx = _build_preference_context()
    if pref_ctx:
        messages.append({"role": "system", "content": pref_ctx})

    mem_ctx = _build_memory_context(user_input)
    if mem_ctx:
        messages.append({"role": "system", "content": mem_ctx})

    for msg in short_term_memory[-SHORT_TERM_CAP * 2 :]:
        messages.append(msg)

    messages.append({"role": "user", "content": user_input})

    return messages


def parse_json_from_response(text: str) -> dict | None:
    text = (text or "").strip()
    for pattern in (
        r"Action:\s*```(?:json)?\s*([\s\S]*?)\s*```",
        r"Action:\s*(\{[\s\S]*?\})\s*(?:```|$)",
        r"```(?:json)?\s*([\s\S]*?)\s*```",
    ):
        for match in re.finditer(pattern, text):
            raw = match.group(1).strip()
            raw = raw.rstrip("`").strip()
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and "action" in data and _is_valid_action(data.get("action")):
                    return data
            except json.JSONDecodeError:
                continue

    try:
        start = text.find("{")
        if start != -1:
            end = text.rfind("}")
            if end != -1 and end > start:
                raw = text[start:end + 1]
                data = json.loads(raw)
                if isinstance(data, dict) and "action" in data and _is_valid_action(data.get("action")):
                    return data
    except (json.JSONDecodeError, ValueError):
        pass

    return None


def _extract_json_objects(text: str) -> list[dict]:
    objects: list[dict] = []
    i = 0
    while True:
        start = text.find("{", i)
        if start == -1:
            break
        depth = 0
        for pos in range(start, len(text)):
            char = text[pos]
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[start:pos + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            objects.append(obj)
                    except json.JSONDecodeError:
                        pass
                    i = pos + 1
                    break
        else:
            i = start + 1
    return objects


def _collect_tool_calls(text: str) -> list[dict]:
    """Extract tool-call dicts from LLM response. Deduplicated by (action, args)."""
    calls: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(data: dict) -> None:
        key = (str(data.get("action", "")), str(data.get("args", "")))
        if key not in seen:
            seen.add(key)
            calls.append(data)

    # 1. standard triple‑backtick block: Action:\n```json\n{...}\n```
    pattern = r"Action:\s*```(?:json)?\s*([\s\S]*?)\s*```"
    for match in re.finditer(pattern, text, re.DOTALL):
        raw = match.group(1).strip().rstrip("`").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "action" in data and _is_valid_action(data.get("action")):
            _add(data)

    # 2. plain "Action: { … }" without backticks
    plain_pattern = r"Action:\s*(?:\n)?\s*(\{[\s\S]*?\})"
    for match in re.finditer(plain_pattern, text, re.DOTALL):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "action" in data and _is_valid_action(data.get("action")):
            _add(data)

    # 3. extract any JSON dict from anywhere (only if it matches tool format exactly)
    for obj in _extract_json_objects(text):
        if (isinstance(obj, dict) and "action" in obj and "args" in obj
                and _is_valid_action(obj.get("action"))
                and len(obj) <= 3):  # action, args + optionally one more key
            _add(obj)

    return calls


def _run_tool(action: str, args: str) -> str:
    # Map aliases
    action = TOOL_ALIASES.get(action, action)
    # Handle dict arguments for tools that expect a simple string
    if isinstance(args, dict):
        if action in ("run_cmd", "execute_terminal_command"):
            cmd = args.get("cmd") or args.get("command") or ""
            args = cmd
        elif action == "write_file":
            path = args.get("file_path") or args.get("path") or ""
            content = args.get("content") or args.get("text") or ""
            args = f"{path}|{content}"
        else:
            args = json.dumps(args)
    # also try to parse a string that looks like a Python dict literal
    elif isinstance(args, str) and args.startswith("{"):
        try:
            parsed = ast.literal_eval(args)
            if isinstance(parsed, dict):
                if action in ("run_cmd", "execute_terminal_command"):
                    cmd = parsed.get("cmd") or parsed.get("command") or ""
                    args = cmd
                elif action == "write_file":
                    path = parsed.get("file_path") or parsed.get("path") or ""
                    content = parsed.get("content") or parsed.get("text") or ""
                    args = f"{path}|{content}"
                else:
                    args = json.dumps(parsed)
        except (ValueError, SyntaxError):
            pass
    fn = AVAILABLE_TOOLS.get(action)
    if not fn:
        return f"Error: unknown tool '{action}'"
    start = time.time()
    try:
        result = str(fn(args))
    except KeyboardInterrupt:
        result = "Tool execution interrupted by user (Ctrl+C)."
    except Exception as e:
        result = f"Error: {e}"
    elapsed = time.time() - start
    _track_tool_performance(action, result, elapsed)
    return result


def _select_model(user_input: str) -> str:
    """Auto-select the best DeepSeek model based on task complexity.
    Simple queries → fast model (deepseek-chat / V4).
    Complex tasks → reasoning model (deepseek-reasoner / R1).
    """
    text = user_input.lower().strip()

    # Complexity signals: length, multi-step, technical depth
    complexity_score = 0

    # Long inputs suggest complex tasks
    if len(user_input) > 200:
        complexity_score += 2
    elif len(user_input) > 100:
        complexity_score += 1

    # CJK: if input is CJK and longer than 50 chars, it's likely substantive
    if _has_cjk(user_input) and len(user_input) > 50:
        complexity_score += 1

    # Multi-step / numbered instructions (English)
    if re.search(r"\b(step|first|then|next|finally|after that)\b", text):
        complexity_score += 2
    # CJK multi-step indicators
    _cjk_step_markers = ["第一步", "第二步", "首先", "然后", "接着", "最后", "最終",
                          "最初", "次に", "最後に", "まず", "다음", "마지막"]
    if any(marker in user_input for marker in _cjk_step_markers):
        complexity_score += 2
    if re.search(r"\d+[.)]\s", text):
        complexity_score += 1

    # Code-related tasks (English)
    code_keywords = [
        "code", "debug", "fix", "refactor", "implement", "build a",
        "write a program", "write a script", "function", "class",
        "architecture", "design pattern", "optimize", "performance",
        "bug", "error", "traceback", "exception", "crash", "broken",
        "doesn't work", "not working", "fails", "failing"
    ]
    if any(kw in text for kw in code_keywords):
        complexity_score += 3  # code tasks are inherently complex
    # CJK code-related tasks
    _cjk_code_keywords = ["代码", "调试", "修复", "重构", "实现", "写一个程序",
                           "写脚本", "函数", "架构", "优化", "性能", "编程",
                           "bug", "报错", "错误", "出错了", "崩溃", "不行"]
    if any(kw in user_input for kw in _cjk_code_keywords):
        complexity_score += 3

    # Deep analysis / reasoning (English)
    analysis_keywords = [
        "analyze", "explain how", "explain why", "compare", "evaluate",
        "review this", "understand", "deep dive", "audit", "diagnose"
    ]
    if any(kw in text for kw in analysis_keywords):
        complexity_score += 2
    # CJK analysis keywords
    _cjk_analysis_keywords = ["分析", "解释", "比较", "评估", "审查", "理解",
                               "深度", "审计", "诊断", "分析一下", "帮我分析"]
    if any(kw in user_input for kw in _cjk_analysis_keywords):
        complexity_score += 2

    # Git-related tasks (English) — may need careful reasoning
    git_keywords = [
        "git commit", "git push", "git pull", "git merge", "git rebase",
        "git branch", "git checkout", "commit this", "push this",
        "merge branch", "create a branch", "switch branch"
    ]
    if any(kw in text for kw in git_keywords):
        complexity_score += 2
    # CJK git keywords
    _cjk_git_keywords = ["git提交", "git推送", "git合并", "提交代码", "推送代码",
                          "创建分支", "合并分支", "切换分支"]
    if any(kw in user_input for kw in _cjk_git_keywords):
        complexity_score += 2

    # Research / multi-source tasks (English)
    research_keywords = [
        "research", "find all", "gather", "summarize", "comprehensive",
        "report", "overview of", "investigate"
    ]
    if any(kw in text for kw in research_keywords):
        complexity_score += 1
    # CJK research keywords
    _cjk_research_keywords = ["研究", "搜索", "总结", "汇总", "综合", "报告",
                               "调查", "全面", "概述", "搜集"]
    if any(kw in user_input for kw in _cjk_research_keywords):
        complexity_score += 1

    if complexity_score >= 3:
        return DEEPSEEK_MODEL_COMPLEX
    return DEEPSEEK_MODEL_SIMPLE


def _get_llm_response(messages: list[dict[str, str]], model: str | None = None, stream: bool = False) -> str:
    global _last_prompt_tokens, _last_completion_tokens, _total_prompt_tokens, _total_completion_tokens
    if llm_provider is None:
        return "[Error] LLM provider not initialised"
    try:
        if stream and hasattr(llm_provider, 'chat_stream'):
            # Streaming mode: collect chunks and print in real-time
            collected: list[str] = []
            for chunk in llm_provider.chat_stream(messages, model or DEEPSEEK_MODEL):
                collected.append(chunk)
                sys.stdout.write(chunk)
                sys.stdout.flush()
            text = "".join(collected).strip()
            _last_prompt_tokens = 0
            _last_completion_tokens = len(text) // 4  # rough estimate
        else:
            text, usage_dict = llm_provider.chat(messages, model or DEEPSEEK_MODEL)
            if usage_dict:
                _last_prompt_tokens = usage_dict.get("prompt_tokens", 0)
                _last_completion_tokens = usage_dict.get("completion_tokens", 0)
                _total_prompt_tokens += _last_prompt_tokens
                _total_completion_tokens += _last_completion_tokens
            else:
                _last_prompt_tokens = 0
                _last_completion_tokens = 0
        return text
    except Exception as e:
        return f"[LLM Error] {e}"


def _requires_tool_action(text: str) -> bool:
    """Return True if the user text looks like a request that needs tool execution."""
    text_lower = text.lower()
    # Use CJK-aware word count instead of naive split
    if _has_cjk(text):
        if _cjk_approximate_words(text) <= 3:
            return False
    elif len(text.split()) <= 2:
        return False
    # Action verbs that strongly imply tool usage (word‑boundary match)
    action_indicators = [
        " search ", " research ", " find ", " compare ", " write ", " save ",
        " run ", " execute ", " build ", " generate ", " download ",
        " clone ", " edit ", " modify ", " move ", " copy ", " delete ", " remove ",
        " fetch ", " pull ",
        "create a", "create the", "make a", "make the",
        "list all", "list the", "show me", "check the",
        "table of", "chart", "graph", "visualize",
    ]
    # Add CJK action indicators
    _cjk_action_indicators = [
        "搜索", "查找", "找到", "写", "写入", "保存", "运行", "执行",
        "构建", "生成", "下载", "克隆", "编辑", "修改", "移动", "复制",
        "删除", "创建", "列出", "显示", "查看", "读取", "安装", "分析",
        "审查", "总结", "获取", "更新", "编译", "调试",
        "調べる", "検索", "実行", "作成", "書く", "読む", "削除",
        "검색", "찾기", "실행", "생성", "쓰기", "읽기", "삭제",
    ]
    if any(ind in text for ind in _cjk_action_indicators):
        return True
    if any(ind in " " + text_lower + " " for ind in action_indicators):
        return True
    # Context nouns that suggest tool-requiring tasks
    context_indicators = [
        "file", "folder", "directory", "repo", "repository", "github",
        "web", "internet", "online", "price", "spec", "specification",
        "report", "summary of", "overview of",
        "git", "commit", "branch", "merge", "push", "pull",
        "bug", "error", "traceback", "exception", "fix",
    ]
    if any(ind in text_lower for ind in context_indicators):
        return True
    return False


def _is_tool_error(result: str) -> bool:
    low = result.strip().lower()
    return (
        low.startswith("error") or
        low.startswith("exit code") or
        "no such file" in low or
        "syntax error" in low or
        "not found" in low or
        "no search results" in low or
        "search temporarily unavailable" in low or
        "traceback" in low or
        "traceback (most recent call last)" in low or
        "typeerror" in low or
        "valueerror" in low or
        "attributeerror" in low or
        "importerror" in low or
        "modulenotfounderror" in low or
        "keyerror" in low or
        "indexerror" in low or
        "git" in low and ("failed" in low or "fatal" in low or "error" in low)
    )


# ---- Prompt injection protection ----

_PROMPT_INJECTION_PATTERNS = [
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

def _detect_prompt_injection(text: str) -> str | None:
    """Return the matched injection pattern if detected, None otherwise."""
    for pattern in _PROMPT_INJECTION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return pattern
    return None

def _sanitize_input(text: str) -> tuple[str, bool]:
    """Sanitize user input for prompt injection. Returns (sanitized_text, was_flagged)."""
    injection = _detect_prompt_injection(text)
    if injection:
        # Neutralize the injection by wrapping the suspicious part
        sanitized = re.sub(injection, "[filtered]", text, flags=re.IGNORECASE)
        return sanitized, True
    return text, False


def _is_bug_report(text: str) -> bool:
    """Detect if user input is reporting a bug, error, or unexpected behaviour."""
    low = text.strip().lower()
    bug_indicators = [
        "bug", "error", "traceback", "exception", "crash", "broken",
        "doesn't work", "not working", "fails", "failing", "didn't work",
        "stack trace", "typeerror", "valueerror", "attributeerror",
        "keyerror", "indexerror", "importerror", "modulenotfounderror",
        "syntax error", "segfault", "segmentation fault", "null pointer",
        "undefined is not", "cannot read", "unexpected",
    ]
    if any(ind in low for ind in bug_indicators):
        return True
    # CJK bug report indicators
    _cjk_bug = ["报错", "错误", "出错了", "bug", "崩溃", "不行", "失败", "异常",
                 "不工作", "没反应", "闪退", "卡住", "死机"]
    if any(ind in text for ind in _cjk_bug):
        return True
    # Check for traceback patterns
    if re.search(r"File\s+\".+?\",\s+line\s+\d+", text):
        return True
    if re.search(r"^\s*Traceback\s", text, re.MULTILINE):
        return True
    return False


def _is_question(text: str) -> bool:
    low = text.strip().lower()
    if "?" in low:
        return True
    # CJK question markers (Chinese, Japanese, Korean)
    _cjk_question_markers = [
        "吗", "？", "呢", "吧", "么", "否", "如何", "怎么", "怎样",
        "什么", "何时", "何地", "为何", "か", "？", "까", "니", "냐",
    ]
    if any(marker in text for marker in _cjk_question_markers):
        return True
    question_phrases = [
        "shall i", "should i", "do you want me", "would you like me", "continue",
        "proceed", "next step",
    ]
    return any(phrase in low for phrase in question_phrases)


def _remove_task_blocks(text: str) -> str:
    """Remove TaskList and TaskDone blocks from a string."""
    return re.sub(
        r"(?:TaskList:\s*```(?:json)?[\s\S]*?```|TaskDone:\s*\d+)",
        "",
        text
    ).strip()


def _safe_fstring(s: str) -> str:
    """Escape curly braces so they do not interfere with f-string parsing."""
    return s.replace('{', '{{').replace('}', '}}')


def _tasks_from_plan(text: str) -> None:
    """Parse a Plan block and create pending tasks."""
    plan_match = re.search(
        r"Plan:\s*\n(.*?)(?=\n\s*(?:Action|TaskList|$))",
        text,
        re.DOTALL | re.IGNORECASE
    )
    if not plan_match:
        return
    plan_body = plan_match.group(1).strip()
    for line in plan_body.splitlines():
        line = line.strip()
        if not line:
            continue
        # Remove leading numbering "1." or "1)" etc.
        cleaned = re.sub(r"^\s*\d+[.)]?\s*", "", line).strip()
        if cleaned:
            tasks.add_task(cleaned)
        else:
            tasks.add_task(line)


def _build_task_progress_hint() -> str:
    """Build a prominent task progress summary for the LLM feedback.
    Placed at the TOP of feedback so the LLM never loses track."""
    if not tasks.tasks:
        return ""
    total = len(tasks.tasks)
    done = sum(1 for t in tasks.tasks if t["status"] == "done")
    pending_tasks = [t for t in tasks.tasks if t["status"] == "pending"]
    lines = [
        "=" * 40,
        f"📋 YOUR TASK LIST ({done}/{total} done):",
    ]
    for i, t in enumerate(tasks.tasks):
        icon = "✓" if t["status"] == "done" else "○" if t["status"] == "pending" else "◷"
        desc = t["description"][:80]
        lines.append(f"  [{i}] {icon} {desc}")
    if pending_tasks:
        next_task = pending_tasks[0]["description"][:80]
        lines.append(f"▶ NEXT TASK TO EXECUTE: \"{next_task}\"")
        lines.append(f"⚠️  {len(pending_tasks)} tasks remain. DO NOT STOP. Output the next Action NOW.")
    lines.append("=" * 40)
    return "\n".join(lines) + "\n"


def _classify_complexity(user_input: str) -> str:
    """Classify the user request as simple, medium, or complex.
    Returns 'simple', 'medium', or 'complex'."""
    text = user_input.lower().strip()

    # Simple: short greetings, trivial questions, single actions (English)
    simple_patterns = [
        r"^(hi|hey|hello|yo|sup|good morning|good evening)\b",
        r"^(thanks|thank you|ok|okay|got it|bye|goodbye)\b",
        r"^(what is|who is|when is|where is|how are you|what can you)\b",
        r"^(yes|no|maybe|sure|yep|nope)$",
    ]
    if any(re.search(p, text) for p in simple_patterns):
        return "simple"

    # CJK simple greetings
    _cjk_simple_patterns = [
        r"^(你好|您好|嗨|早|哈喽|哈啰|喂|谢谢|再见|拜拜|好的|嗯|哦|知道了)",
        r"^(こんにちは|もしもし|おはよう|こんばんは|さようなら|ありがとう|はい|いいえ)",
        r"^(안녕|안녕하세요|감사합니다|네|아니요|잘가)",
    ]
    if any(re.search(p, user_input) for p in _cjk_simple_patterns):
        return "simple"

    # Short inputs without tool verbs are simple (CJK-aware)
    _tool_verbs = {"read","list","find","search","write","run","create","clone","fetch","open","edit","delete","move","copy"}
    _cjk_tool_verbs = {"读","读取","查","查找","搜索","写","运行","创建","克隆","获取","编辑","删除","移动","复制","列出","安装","读文件","写文件","搜索网页","执行"}
    if _has_cjk(user_input):
        words = _cjk_approximate_words(user_input)
        if words <= 4 and not any(tv in user_input for tv in _cjk_tool_verbs):
            return "simple"
    else:
        words = set(user_input.lower().split())
        if len(user_input.split()) <= 3 and not (words & _tool_verbs):
            return "simple"

    # Complex: multi-step, code generation, deep analysis, bug fixing, git operations
    complex_indicators = [
        r"\b(step|first|then|next|finally|after that)\b",
        r"\d+[.)]\s+\w",  # numbered steps
        r"\b(implement|refactor|migrate|audit|comprehensive)\b",
        r"\b(write\s+(a|the)\s+(program|script|app|application|function|class))\b",
        r"\b(analy[sz]e\s+(the|this|my|our)\s+(codebase|repo|project|architecture))\b",
        r"\b(debug|fix\s+(the|this|a|my)\s+(bug|error|issue|problem|crash))\b",
        r"\b(traceback|stack\s*trace|exception|TypeError|ValueError|AttributeError)\b",
        r"\b(git\s+(merge|rebase|cherry-pick|bisect))\b",
    ]
    if any(re.search(p, text) for p in complex_indicators):
        return "complex"
    # CJK complex indicators
    _cjk_complex = ["实现", "重构", "迁移", "审计", "审查代码", "写一个程序", "写脚本",
                     "分析代码", "分析架构", "第一步", "第二步", "全面的",
                     "调试", "修复bug", "修bug", "出错了", "报错", "错误",
                     "git操作", "git合并", "提交代码", "推送", "拉取"]
    if any(ind in user_input for ind in _cjk_complex):
        return "complex"
    # Long inputs with multiple sentences suggest complexity
    if len(user_input) > 250:
        return "complex"

    # Default: medium (most tool-using requests)
    return "medium"


def _chat_turn(user_input: str, clear_tasks: bool = False) -> str:
    """One user turn: build context, get LLM reply, execute tool calls
    with automatic retries and failure memory."""

    global _last_user_interaction, DEEPSEEK_MODEL
    _last_user_interaction = time.time()

    # Compress old turns if context is growing too large
    _summarize_old_turns()

    if clear_tasks:
        tasks.tasks.clear()

    # Auto-select the best model for this turn based on task complexity
    DEEPSEEK_MODEL = _select_model(user_input)
    complexity = _classify_complexity(user_input)

    turn_start = time.time()
    turn_prompt_total = 0
    turn_completion_total = 0

    MAX_RETRIES = 3
    messages = _build_messages(user_input)
    response_text = _call_llm_with_spinner(messages).strip()
    turn_prompt_total += _last_prompt_tokens
    turn_completion_total += _last_completion_tokens

    if not response_text or not response_text.strip():
        messages.append({
            "role": "user",
            "content": "System: You returned nothing. Please output your Thought and JSON Action now.",
        })
        response_text = _call_llm_with_spinner(messages).strip()
        turn_prompt_total += _last_prompt_tokens
        turn_completion_total += _last_completion_tokens

    # Process task blocks from the LLM
    tasks.from_llm_block(response_text)
    tasks.mark_done_from_text(response_text)
    response_text_clean = re.sub(r'(?:TaskList:\s*```(?:json)?[\s\S]*?```|TaskDone:\s*\d+)', '', response_text).strip()
    if not response_text_clean:
        response_text_clean = response_text
    response_text = response_text_clean

    # Check for DefineTool blocks and register new tools
    if "DefineTool:" in response_text:
        _attempt_define_tool(response_text)

    tool_calls = _collect_tool_calls(response_text)

    # ---- Unknown action detection (all complexity levels) ----
    _unknown_retries = 0
    while _unknown_retries < MAX_UNKNOWN_TOOL_RETRIES:
        unknown_action = _detect_unknown_action(response_text)
        if not unknown_action:
            break
        _unknown_retries += 1
        msg = (
            f"System: Action '{unknown_action}' is not recognized. "
            "You must use one of the following actions: "
            + ", ".join(sorted(AVAILABLE_TOOLS.keys())) + ".\n"
            "Do not invent new action names. Pick from the list and output an Action block."
        )
        messages.append({"role": "user", "content": msg})
        response_text = _call_llm_with_spinner(messages).strip()
        turn_prompt_total += _last_prompt_tokens
        turn_completion_total += _last_completion_tokens
        tasks.from_llm_block(response_text)
        tasks.mark_done_from_text(response_text)
        tool_calls = _collect_tool_calls(response_text)

    # ---- Plan enforcement: MEDIUM and COMPLEX only ----
    _llm_has_plan = bool(re.search(r"Plan:", response_text))
    if complexity in ("medium", "complex"):
        plan_attempts = 0
        while not _llm_has_plan and tool_calls and plan_attempts < 2:
            plan_attempts += 1
            plan_hint = (
                "System: This is a {0} task. Output a Plan block first:\n"
                "Plan:\n1. <first step – what and why>\n2. <second step>\n...".format(complexity)
            )
            messages.append({"role": "user", "content": plan_hint})
            response_text = _call_llm_with_spinner(messages).strip()
            turn_prompt_total += _last_prompt_tokens
            turn_completion_total += _last_completion_tokens
            tasks.from_llm_block(response_text)
            tasks.mark_done_from_text(response_text)
            tool_calls = _collect_tool_calls(response_text)
            _llm_has_plan = bool(re.search(r"Plan:", response_text))
        if not _llm_has_plan and tool_calls and plan_attempts >= 2:
            # Auto‑generate a minimal plan from tool calls
            plan_lines = ["Plan:"]
            for i, tc in enumerate(tool_calls):
                plan_lines.append(f"{i+1}. Execute {tc.get('action','?')}")
            # Don't inject — just let it proceed without plan this time

    # ---- TaskList enforcement: COMPLEX only ----
    _llm_has_tasklist = bool(re.search(r"TaskList:", response_text))
    if complexity == "complex" and tool_calls:
        if not _llm_has_tasklist and _llm_has_plan:
            _tasks_from_plan(response_text)
            if tasks.tasks:
                _llm_has_tasklist = True
                _update_tasks_panel()
        if not _llm_has_tasklist:
            # Auto‑generate TaskList from plan or tool calls
            tasks.tasks.clear()
            if _llm_has_plan:
                _tasks_from_plan(response_text)
            if not tasks.tasks:
                for tc in tool_calls:
                    safe_args = str(tc.get('args') or '')[:50]
                    tasks.add_task(f"Execute {tc.get('action','?')}: {safe_args}")
            if tasks.tasks:
                _update_tasks_panel()

    # ---- Missing action recovery (up to 3 attempts) ----
    if not tool_calls:
        if _requires_tool_action(user_input) or _llm_has_plan or _llm_has_tasklist:
            action_retries = 0
            while not tool_calls and action_retries < 3:
                action_retries += 1
                if action_retries == 1:
                    reminder = (
                        "System: You output a Plan but no Action block. "
                        "You **must** now output a JSON Action block to perform the work. "
                        "Do not repeat the Plan — output ONLY: Thought + Action JSON."
                    )
                elif action_retries == 2:
                    reminder = (
                        "System: STILL no Action block. Output ONLY this now:\n\n"
                        "Action:\n```json\n{\"action\": \"write_file\", \"args\": \"...\"}\n```\n\n"
                        "Pick the first step from your Plan and execute it. No Plan, no TaskList."
                    )
                else:
                    reminder = (
                        "System: FINAL attempt. You have a Plan. Execute step 1. "
                        "Output a single Action block. Nothing else.\n"
                        "Action:\n```json\n"
                    )
                messages.append({"role": "user", "content": reminder})
                response_text = _call_llm_with_spinner(messages).strip()
                turn_prompt_total += _last_prompt_tokens
                turn_completion_total += _last_completion_tokens
                if not response_text:
                    continue
                tool_calls = _collect_tool_calls(response_text)
                if tool_calls:
                    tasks.from_llm_block(response_text)
                    tasks.mark_done_from_text(response_text)
        if not tool_calls:
            elapsed = time.time() - turn_start
            _turn_cost_log.append({
                "tokens": turn_prompt_total + turn_completion_total,
                "time": elapsed,
                "tool_calls": 0
            })
            return response_text


    results: list[str] = []
    for tc in tool_calls:
        action = tc.get("action", "")
        args = tc.get("args", "")
        if isinstance(args, dict):
            result = (
                f"Error: `{action}` requires a plain string as the `args` field.\n"
                "You passed a JSON object. Convert to a plain string.\n"
                "Example: `\"args\": \"python3 process_logs.py\"`\n"
                "Do NOT use `\"args\": {\"cmd\": ...}`.\n"
            )
        else:
            if not isinstance(args, str):
                args = str(args)
            result = _run_tool(action, args)
        safe_result = str(result)[:2000]
        console.print(Panel(rich_escape(safe_result), title=f"Tool: {action}", border_style=_ACCENT_DIM, title_align="left"))
        results.append(f"- `{action}({args!r})` returned:\n{_safe_fstring(safe_result)}")
        if tasks.tasks:
            _update_tasks_panel()
    tool_results_text = "\n".join(results)

    # Unified multi-step loop - always runs, can handle early errors
    round_limit = MAX_STEPS_PER_TURN
    round_count = 0
    incomplete_prompt_attempts = 0
    final_answer: str | None = None
    current_reply = response_text
    has_errors = any(_is_tool_error(r) for r in results)
    consecutive_search_failures = 0  # track failed search_web calls to prevent loops
    total_search_calls = len([r for r in results if "search_web" in r])
    rounds_without_taskdone = 0  # auto-mark safety net if LLM forgets TaskDone
    # check for missing arguments errors
    _args_missing_errors = [
        "requires a command",
        "requires args in format path|content",
    ]
    _has_args_missing = any(
        any(pat in r for pat in _args_missing_errors) for r in results
    )
    # Remember the first tool-call block for the context
    initial_response = response_text

    while round_count < round_limit:
        # Build the summary messages. If the last tool call failed, include guidance.
        error_hint = ""
        if has_errors:
            error_hint = (
                "The tool returned an error. "
            )
            if _has_args_missing:
                error_hint += (
                    "You probably forgot to provide the argument string. "
                    "For `write_file` the argument must be `path|content` (with a pipe separator). "
                    "For `run_cmd` or `execute_terminal_command` the argument must be the command string. "
                    "Do not leave the argument empty.\n"
                )
            else:
                error_hint += "The action name may be wrong. "
            error_hint += (
                "If you used a self-created tool, try using a built-in tool instead. "
                "Use one of the following actions: "
                + ", ".join(sorted(AVAILABLE_TOOLS.keys())) + ".\n"
            )

        # Search throttle: prevent infinite search loops WITHOUT stopping the task
        search_throttle = ""
        if consecutive_search_failures >= 3:
            search_throttle = (
                f"\n⚠️  **Search unavailable:** {consecutive_search_failures} consecutive "
                "search_web calls failed (rate‑limited or backend down).\n"
                "**Do NOT abort the task.** Continue by other means:\n"
                "- Use `read_webpage` on URLs you already know.\n"
                "- Work with partial/earlier search results you already have.\n"
                "- Use `run_cmd` with curl to fetch known API endpoints.\n"
                "- Write a script, read a file, or use any other tool.\n"
                "- If truly stuck, explain what you could find and what's missing.\n"
                "But **keep going** with the task — do not give up.\n"
            )
        elif total_search_calls >= 5:
            search_throttle = (
                f"\n⚠️  **Search rate warning:** {total_search_calls} search_web calls "
                "made this turn. At most 1 more search is allowed. Prefer other "
                "tools (read_webpage, run_cmd with curl, file operations) to "
                "continue the task. **Do not stop** — complete the user's request.\n"
            )
        summary_messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "You are Kyrozen, an intelligent AI assistant. "
                    "You have just obtained the following information by running tools. "
                    "If more steps are needed to satisfy the user request, output the next Action block. "
                    "Otherwise, output only a plain final answer."
                )
            },
            {"role": "system", "content": _workspace_info()},
            {
                "role": "system",
                "content": _build_memory_context(user_input),
            },
            {
                "role": "system",
                "content": (
                    "Here are the tools you can use. "
                    "Make sure to pick an action name exactly as listed:\n"
                    + TOOLS_LIST
                )
            },
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": current_reply},
            {
                "role": "system",
                "content": (
                    "REMINDER: The user's original request was:\n"
                    f"---\n{user_input[:500]}\n---\n"
                    "Complete ALL parts of this request before stopping."
                )
            },
            {
                "role": "user",
                "content": (
                    _build_task_progress_hint() +
                    f"The tools returned:\n{tool_results_text}\n\n"
                    + error_hint + search_throttle +
                    "Please continue if there are remaining steps, or respond with the final answer.\n"
                    "If you have completed a task, you **must** output `TaskDone: <index>` "
                    "(replace index with the zero-based index) **before** the next Action block. "
                    "Do not omit the `TaskDone:` line.\n"
                    "Never ask the user for permission to continue. Automatically decide."
                )
            }
        ]
        step_reply = _call_llm_with_spinner(summary_messages).strip()
        turn_prompt_total += _last_prompt_tokens
        turn_completion_total += _last_completion_tokens
        if not step_reply:
            break

        # let the tasks manager know about any TaskDone/TaskList in this reply
        tasks.from_llm_block(step_reply)
        tasks.mark_done_from_text(step_reply)

        # Safety net: if LLM forgets TaskDone for 3 rounds, auto-mark one pending task
        has_taskdone = bool(re.search(r"TaskDone:\s*\d+", step_reply))
        if has_taskdone:
            rounds_without_taskdone = 0
        elif tasks.tasks and any(t["status"] == "pending" for t in tasks.tasks):
            rounds_without_taskdone += 1
            if rounds_without_taskdone >= 3:
                tasks.mark_first_pending_done()
                rounds_without_taskdone = 0

        # extract tool calls for the next step
        next_tool_calls = _collect_tool_calls(step_reply)

        # ----- reject unknown action names and force re-prompting -----
        _unknown_tool_retries = 0
        while _unknown_tool_retries < 3:
            unknown_action = _detect_unknown_action(step_reply)
            if not unknown_action:
                break
            _unknown_tool_retries += 1
            msg = (
                f"System: Action '{unknown_action}' is not recognized.\n"
                "You **must** use one of the following action names exactly:\n"
                + ", ".join(sorted(AVAILABLE_TOOLS.keys())) + "\n"
                "Do not invent new names. Output an Action block now."
            )
            # re-prompt the LLM
            step_reply = _call_llm_with_spinner(
                summary_messages + [{"role": "user", "content": msg}]
            ).strip()
            turn_prompt_total += _last_prompt_tokens
            turn_completion_total += _last_completion_tokens
            if not step_reply:
                break
            # re‑parse tasks and tool calls
            tasks.from_llm_block(step_reply)
            tasks.mark_done_from_text(step_reply)
            next_tool_calls = _collect_tool_calls(step_reply)

        # if after 3 retries the action is still unknown, clear the list to avoid a crash
        if _unknown_tool_retries >= 3:
            next_tool_calls = []

        # if there are no more tool calls, the LLM might be giving a natural reply
        if not next_tool_calls:
            # If all tasks are already done (or none were set), accept this as final answer
            all_done = not tasks.tasks or all(t["status"] != "pending" for t in tasks.tasks)
            if all_done:
                final_answer = step_reply
                break

            # Find the next pending task to tell the LLM what to do
            next_pending_desc = ""
            for t in tasks.tasks:
                if t["status"] == "pending":
                    next_pending_desc = t["description"]
                    break

            if tasks.tasks and any(t["status"] != "done" for t in tasks.tasks):
                incomplete_prompt_attempts += 1
                if incomplete_prompt_attempts >= 12:
                    for t in tasks.tasks:
                        if t["status"] == "pending":
                            t["status"] = "done"
                    console.print(f"[{_WARNING}]Auto‑completed remaining tasks after {incomplete_prompt_attempts} nudges.[/{_WARNING}]")
                    break

                # Build a specific nudge mentioning the exact next task
                nudge = (
                    f"System: You have {sum(1 for t in tasks.tasks if t['status'] == 'pending')} "
                    "incomplete tasks remaining. "
                )
                if next_pending_desc:
                    nudge += f"The next task is: \"{next_pending_desc}\". "
                nudge += "Output the Action block for this task NOW. Do not explain or summarise — just act."
                summary_messages.append({"role": "user", "content": nudge})
                step_reply = _call_llm_with_spinner(summary_messages).strip()
                turn_prompt_total += _last_prompt_tokens
                turn_completion_total += _last_completion_tokens
                if not step_reply:
                    break
                tasks.from_llm_block(step_reply)
                tasks.mark_done_from_text(step_reply)
                next_tool_calls = _collect_tool_calls(step_reply)
                if not next_tool_calls:
                    # Still no action — don't give up yet, loop will try again
                    # (incomplete_prompt_attempts will eventually trigger the 12-nudge limit)
                    pass
            elif _is_question(step_reply):
                summary_messages.append({
                    "role": "user",
                    "content": "System: Do not ask the user. Output the next Action block now."
                })
                step_reply = _call_llm_with_spinner(summary_messages).strip()
                turn_prompt_total += _last_prompt_tokens
                turn_completion_total += _last_completion_tokens
                if not step_reply:
                    break
                tasks.from_llm_block(step_reply)
                tasks.mark_done_from_text(step_reply)
                next_tool_calls = _collect_tool_calls(step_reply)
                if not next_tool_calls:
                    final_answer = step_reply
                    break
            else:
                summary_messages.append({
                    "role": "user",
                    "content": "System: You have not output an Action block. "
                               "Output the next JSON Action block now to continue."
                })
                step_reply = _call_llm_with_spinner(summary_messages).strip()
                turn_prompt_total += _last_prompt_tokens
                turn_completion_total += _last_completion_tokens
                if not step_reply:
                    break
                tasks.from_llm_block(step_reply)
                tasks.mark_done_from_text(step_reply)
                next_tool_calls = _collect_tool_calls(step_reply)
                if not next_tool_calls:
                    final_answer = step_reply
                    break

        # execute all new tool calls
        round_count += 1
        next_results: list[str] = []
        has_errors = False
        for tc2 in next_tool_calls:
            action = tc2.get("action", "")
            args = tc2.get("args", "")
            if isinstance(args, dict):
                result2 = (
                    f"Error: `{action}` requires a plain string as the `args` field.\n"
                    "You passed a JSON object. Convert to a plain string.\n"
                    "Example: `\"args\": \"python3 process_logs.py\"`\n"
                    "Do NOT use `\"args\": {\"cmd\": ...}`.\n"
                )
            else:
                if not isinstance(args, str):
                    args = str(args)
                try:
                    result2 = _run_tool(action, args)
                except KeyboardInterrupt:
                    result2 = "Tool execution interrupted by user (Ctrl+C)."
            safe_result2 = str(result2)[:2000]
            console.print(Panel(rich_escape(safe_result2), title=f"Tool: {action}", border_style=_ACCENT_DIM, title_align="left"))
            next_results.append(f"- `{action}({args!r})` returned:\n{_safe_fstring(safe_result2)}")
            if tasks.tasks:
                _update_tasks_panel()
            if _is_tool_error(result2):
                has_errors = True
            # Track search_web failures to prevent infinite search loops
            if action == "search_web":
                total_search_calls += 1
                if _is_tool_error(result2) or "Search temporarily unavailable" in str(result2):
                    consecutive_search_failures += 1
                else:
                    consecutive_search_failures = 0  # reset on success
        tool_results_text = "\n".join(next_results)
        _has_args_missing = any(
            any(pat in r for pat in [
                "requires a command",
                "requires args in format path|content",
            ]) for r in next_results
        )
        # Hard limit: too many search_web calls → one last chance to complete
        if total_search_calls >= 7 and consecutive_search_failures >= 3:
            final_msg = (
                "System: Search is unavailable (7+ calls, {0} consecutive failures). "
                "Stop searching now. Give your **best final answer** using whatever "
                "information you already gathered from earlier tool results. "
                "Do NOT output another Action block — just give the final answer "
                "directly. Acknowledge any gaps honestly."
            ).format(consecutive_search_failures)
            step_reply = _call_llm_with_spinner(
                summary_messages + [{"role": "user", "content": final_msg}]
            ).strip()
            final_answer = step_reply if step_reply else (
                "Search limit reached. Here's what I gathered: " + tool_results_text[:500]
            )
            console.print(f"[{_WARNING}]Search limit reached — synthesizing final answer.[/{_WARNING}]")
            break
        # Check if all pending tasks are done; if so, stop
        # (also stops if no tasks were set — medium complexity just runs tools sequentially)
        if not tasks.tasks or all(t["status"] != "pending" for t in tasks.tasks):
            # If the LLM's latest reply was a natural-language answer without Action,
            # use it as the final answer. Otherwise, synthesise from results.
            if step_reply and not _collect_tool_calls(step_reply):
                final_answer = step_reply
            else:
                final_answer = current_reply
            break
        # Update the LLM's previous output so the next iteration sees the new results (remove task blocks)
        current_reply = _remove_task_blocks(step_reply)

    if final_answer is None:
        # Fallback if the loop exited without a final answer
        final_answer = f"I executed your request. Here is the information I gathered:\n\n{_safe_fstring(tool_results_text)}"
    elif len(final_answer.strip()) < 10:
        final_answer = f"I executed your request. Here is the information I gathered:\n\n{_safe_fstring(tool_results_text)}"

    # Also detect user correction (e.g., "not correct", "you misunderstood")
    correction_keywords = ["not correct", "you misunderstood", "wrong", "incorrect", "fix it"]
    if any(kw in user_input.lower() for kw in correction_keywords):
        _store_failure(
            user_input,
            current_reply[:200],
            "User indicated previous answer was wrong",
            final_answer[:200] + "..."
        )
    elapsed = time.time() - turn_start
    _turn_cost_log.append({
        "tokens": turn_prompt_total + turn_completion_total,
        "time": elapsed,
        "tool_calls": len(tool_calls)
    })
    _maybe_trigger_reflection_after_complex_task(len(tool_calls))
    final_answer = _remove_task_blocks(final_answer)

    # If tasks were completed, generate a summary so the user knows what happened
    total_tools_executed = len(tool_calls) + len(results)
    if total_tools_executed >= 2:
        summary_prompt = (
            "You just completed a multi-step task. Summarise your work below.\n\n"
            "## What was accomplished\n"
            "- (2-4 bullet points: tools used, files created, key results)\n\n"
            "Output only the completed summary in plain text (no Action blocks).\n\n"
            f"Tool results:\n{tool_results_text[:1500]}"
        )
        try:
            summary = _get_llm_response(
                [{"role": "system", "content": summary_prompt}]
            ).strip()
            if summary and len(summary) > 30:
                if len(final_answer.strip()) < 60:
                    final_answer = summary
                else:
                    final_answer = final_answer + "\n\n---\n\n" + summary
        except Exception:
            pass

    return final_answer


def _split_reply(text: str) -> tuple[str, str]:
    text = text.strip()
    thought_match = re.search(r"^Thought:\s*(.*?)(?=\n(?:Action:|(?:\n|$)))", text, re.DOTALL | re.MULTILINE)
    if thought_match:
        thinking = thought_match.group(1).strip()
        answer = re.sub(r"^Thought:\s*.*?(?=\n(?:Action:|(?:\n|$))|\n?$)", "", text, count=1, flags=re.DOTALL | re.MULTILINE).strip()
        answer = re.sub(r"Action:\s*```(?:json)?[\s\S]*?```", "", answer).strip()
        answer = re.sub(r"\{[\s\S]*?\}", "", answer).strip()
        answer = answer.lstrip('"').lstrip("'").strip()
        if not answer:
            answer = text
        return thinking, answer
    else:
        return "", text


def _auto_learn_conversations() -> None:
    global _logs_count_at_last_learn
    new_count = memory_bank.count_logs()
    if new_count == _logs_count_at_last_learn:
        return  # no new logs to process
    num_new = new_count - _logs_count_at_last_learn
    recent = memory_bank.get_recent(num_new)
    if not recent:
        _logs_count_at_last_learn = new_count
        return
    # Remove system‑internal logs that shouldn't be learned
    recent = [
        log for log in recent if log
        if not log.startswith("FACT:")
        and not log.startswith("LEARNED:")
        and not log.startswith("FILE:")
    ]
    if not recent:
        _logs_count_at_last_learn = new_count
        return
    learn_prompt = (
        "You are Kyrozen's self-learning module. Read the following recent conversation logs "
        "and extract any important facts, user preferences, or new skills that should be "
        "remembered for future interactions. Output a bullet list of facts. "
        "If nothing important, output only '—'.\n\n"
        + "\n".join(recent)
    )
    messages = [{"role": "system", "content": learn_prompt}]
    try:
        fact_text = _get_llm_response(messages).strip()
        if fact_text and fact_text not in ("—", ""):
            for line in fact_text.split("\n"):
                line = line.strip().lstrip("-* ").strip()
                if line:
                    memory_bank.add_log(f"FACT: {line}")
    except Exception:
        pass
    finally:
        _logs_count_at_last_learn = new_count


def _auto_debug_tool() -> None:
    """Analyse tool failures and error patterns, then log debugging insights."""
    # Collect failing tools from performance stats
    failing_tools = []
    for tool_name, stats in _tool_stats.items():
        if stats.get("calls", 0) > 2:
            success_rate = stats.get("successes", 0) / max(stats["calls"], 1)
            if success_rate < 0.5:
                failing_tools.append((tool_name, stats))

    # Collect recent failure entries from memory
    recent = memory_bank.get_recent(30)
    failures = [r for r in recent if r and r.startswith(_FAILURE_STORE_PREFIX)]

    if not failing_tools and not failures:
        return

    # Build analysis prompt
    parts = []
    if failing_tools:
        parts.append("Low‑success tools detected:")
        for name, stats in failing_tools:
            parts.append(
                f"  - {name}: {stats['calls']} calls, "
                f"{stats['successes']} successes "
                f"({stats['successes']/max(stats['calls'],1)*100:.0f}% success rate)"
            )
    if failures:
        parts.append("\nRecent failure records:")
        for f in failures[-3:]:
            parts.append(f"  {f[:300]}")

    debug_prompt = (
        "You are Kyrozen's auto‑debug module. Analyse the following tool "
        "performance data and failure records. Identify the root cause of "
        "failures and suggest concrete fixes. Output each finding on a new "
        "line prefixed with 'DEBUG_FINDING:'.\n\n"
        + "\n".join(parts)
    )
    try:
        messages = [{"role": "system", "content": debug_prompt}]
        answer = _get_llm_response(messages).strip()
        if answer:
            for line in answer.split("\n"):
                line = line.strip()
                if line.startswith("DEBUG_FINDING:") or line.startswith("FIX:"):
                    memory_bank.add_log(f"DEBUG: {line}")
    except Exception:
        pass


def _background_learning_loop() -> None:
    """Enhanced loop with consolidation, reflection, tool review, auto‑inquiry."""
    global _last_user_interaction
    while True:
        time.sleep(30)
        try:
            now = time.time()
            idle_duration = now - _last_user_interaction

            # Skip heavy background work if the user has been active within the last 1 minute
            if idle_duration < 60:
                continue

            # silently learning (respect self-learning flags)
            if _SELF_LEARNING_FLAGS["auto_learn_conversations"]:
                _auto_learn_conversations()
            if _SELF_LEARNING_FLAGS["load_project_files_into_memory"]:
                _load_project_files_into_memory()
            if _SELF_LEARNING_FLAGS["age_out_old_coded_entries"]:
                _age_out_old_coded_entries()
            if _SELF_LEARNING_FLAGS["auto_debug_tool"]:
                _auto_debug_tool()

            if idle_duration > IDLE_CONSOLIDATION_TIMEOUT:
                if _SELF_LEARNING_FLAGS["consolidate_memories"]:
                    _consolidate_memories()
                if not _run_regression_tests():
                    _restore_tool_snapshot()
                if _SELF_LEARNING_FLAGS["review_tools"]:
                    _review_tools()
                if not _run_regression_tests():
                    _restore_tool_snapshot()
                if _SELF_LEARNING_FLAGS["targeted_inquiry"]:
                    _targeted_inquiry()
                if _SELF_LEARNING_FLAGS["maybe_trigger_reflection"]:
                    _maybe_trigger_reflection()
                if _SELF_LEARNING_FLAGS["maybe_strategy_distillation"]:
                    _maybe_strategy_distillation()
                if _SELF_LEARNING_FLAGS["invent_skills"]:
                    _invent_skills()
                # Autonomous codebase health inspection
                _autonomous_inspection()
                # Knowledge graph extraction (once per idle cycle)
                _extract_knowledge_graph()
        except Exception:
            pass


def _show_self_learning_menu() -> None:
    """Display an interactive menu to toggle self-learning features."""
    flag_names = [
        ("auto_learn_conversations",      "Auto-learn from conversations"),
        ("load_project_files_into_memory","Scan project files into memory"),
        ("age_out_old_coded_entries",     "Age out stale code entries"),
        ("auto_debug_tool",               "Auto-debug tools"),
        ("consolidate_memories",          "Consolidate memories"),
        ("review_tools",                  "Review and merge tools"),
        ("targeted_inquiry",              "Targeted inquiry (ask about undocumented code)"),
        ("maybe_trigger_reflection",      "Idle reflection"),
        ("maybe_strategy_distillation",   "Strategy distillation"),
        ("auto_patch_new_technology",     "Auto‑patch new technology info"),
        ("invent_skills",                 "Invent reusable skills from past conversations"),
    ]
    while True:
        console.print(f"\n[bold {_ACCENT}]═══ Self‑Learning Features ═══[/bold {_ACCENT}]")
        console.print(f"[{_MUTED}]Enter a number to toggle, or 'done' to exit.[/{_MUTED}]\n")
        for i, (key, desc) in enumerate(flag_names):
            enabled = _SELF_LEARNING_FLAGS[key]
            icon = f"[{_SUCCESS}]●[/{_SUCCESS}]" if enabled else f"[{_MUTED}]○[/{_MUTED}]"
            console.print(f"  [{_MUTED}]{i+1}.[/{_MUTED}] {icon} {desc}")
        console.print()
        choice = console.input("[bold cyan]Toggle (number) or 'done': [/bold cyan]").strip().lower()
        if choice == "done":
            console.print(f"[{_SUCCESS}]Self‑learning settings updated.[/{_SUCCESS}]")
            break
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(flag_names):
                key = flag_names[idx][0]
                _SELF_LEARNING_FLAGS[key] = not _SELF_LEARNING_FLAGS[key]
                state = "enabled" if _SELF_LEARNING_FLAGS[key] else "disabled"
                console.print(f"[{_SUCCESS}]Toggled {flag_names[idx][1]} → {state}.[/{_SUCCESS}]")
            else:
                console.print(f"[{_ERROR}]Invalid number.[/{_ERROR}]")
        except ValueError:
            console.print(f"[{_ERROR}]Please enter a number or 'done'.[/{_ERROR}]")


def _print_banner() -> None:
    """Print the OpenKyrozen ASCII-art banner in accent colour."""
    _ascii = [
        r"  ____  _____  ______ _   _   _  ____     _______   ____ ____________ _   _ ",
        r" / __ \|  __ \|  ____| \ | | | |/ /\ \   / /  __ \ / __ \___  /  ____| \ | |",
        r"| |  | | |__) | |__  |  \| | | ' /  \ \_/ /| |__) | |  | | / /| |__  |  \| |",
        r"| |  | |  ___/|  __| | . ` | |  <    \   / |  _  /| |  | |/ / |  __| | . ` |",
        r"| |__| | |    | |____| |\  | | . \    | |  | | \ \| |__| / /__| |____| |\  |",
        r" \____/|_|    |______|_| \_| |_|\_\   |_|  |_|  \_\\____/_____|______|_| \_|",
    ]
    for ln in _ascii:
        console.print(f"  [{_ACCENT}]{ln}[/{_ACCENT}]")
    console.print(f"  [{_MUTED}]self{_NBHYPHEN}learning AI agent  {_DOT}  DeepSeek V4[/{_MUTED}]")
    # Platform tag
    if _IS_WINDOWS:
        platform_tag = "Windows"
    elif _IS_MACOS:
        platform_tag = "macOS"
    elif _IS_LINUX:
        platform_tag = "Linux"
    else:
        platform_tag = _PLATFORM
    console.print(f"  [{_MUTED}]Platform: {platform_tag} {_DOT} Python {sys.version_info.major}.{sys.version_info.minor}[/{_MUTED}]")


def main() -> None:
    # Bytecode cache cleared already at module level (see top of file)
    global _provider_config, llm_provider, DEEPSEEK_MODEL, DEEPSEEK_MODEL_SIMPLE, DEEPSEEK_MODEL_COMPLEX, MODEL_NAME

    if "--init" in sys.argv:
        console.print(f"[{_ACCENT}]OpenKyrozen initialisation[/{_ACCENT}]")
        try:
            import openai  # noqa: F401
        except ImportError:
            console.print(f"[{_ERROR}]openai not installed. Run: pip install -r requirements.txt[/{_ERROR}]")
            sys.exit(1)
        _prompt_and_init_deepseek()
        _load_project_files_into_memory()
        if llm_provider is not None:
            console.print(f"[{_SUCCESS}]{_provider_config.provider.title()} API key configured and saved to ~/.kyrozen_config.json[/{_SUCCESS}]")
        console.print(f"[{_SUCCESS}]Initialisation finished. Run `python main.py` to start the agent.[/{_SUCCESS}]")
        sys.exit(0)

    # ASCII-art banner, 41 chars wide — fits 60-col terminals
    _print_banner()
    provider_name = _provider_config.provider.title() if _provider_config else "DeepSeek"
    model_name = DEEPSEEK_MODEL_SIMPLE
    console.print(f"[{_ACCENT}]Kyrozen[/{_ACCENT}] [{_MUTED}]{_DOT} Provider: {provider_name} {_DOT} Model: {model_name}[/{_MUTED}]")
    console.print(f"[{_MUTED}]Chat:[/{_MUTED}] [{_ACCENT_DIM}] /quit /exit /provider /api_key /learn /update /self-learning[/{_ACCENT_DIM}]")

    # Compact self-learning summary
    enabled_count = sum(1 for v in _SELF_LEARNING_FLAGS.values() if v)
    total_count = len(_SELF_LEARNING_FLAGS)
    console.print(f"[{_MUTED}]Self-learning:[/{_MUTED}] [{_ACCENT_DIM}]{enabled_count}/{total_count} features active (toggle with /self-learning)[/{_ACCENT_DIM}]")
    console.print(f"[{_MUTED}]Memory:[/{_MUTED}] [{_ACCENT_DIM}]ChromaDB (`chroma_memory/`) — ask me what I remember[/{_ACCENT_DIM}]")
    console.print(f"[{_MUTED}]Cost:[/{_MUTED}] [{_ACCENT_DIM}]{get_cost_summary()}[/{_ACCENT_DIM}]")
    # Horizontal rule
    console.print(f"[{_ACCENT_DIM}]{_BOX_H * 50}[/{_ACCENT_DIM}]")

    _prompt_and_init_deepseek()
    if llm_provider is None:
        console.print(f"[{_ERROR}]Cannot start without an API key.[/{_ERROR}]")
        sys.exit(1)
    # Set workspace root for sandbox
    _set_workspace_root(os.getcwd())
    # Load project files synchronously to avoid ChromaDB thread conflicts
    _load_project_files_into_memory()
    console.print(f"[{_MUTED}]Project files loaded into memory.[/{_MUTED}]")

    # Start background learning daemon
    threading.Thread(target=_background_learning_loop, daemon=True).start()

    while True:
        try:
            user_input = console.input(f"[bold {_ACCENT}]You:[/bold {_ACCENT}] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print(f"\n[{_ERROR}]Goodbye.[/{_ERROR}]")
            sys.exit(0)

        if not user_input:
            continue

        _is_auto_continue = False

        # clear tasks for a new user request (skip during auto‑continue)
        if not _is_auto_continue and not user_input.startswith("/"):
            tasks.tasks.clear()
            _clear_tasks_panel()

        if user_input.lower() in ("/quit", "/exit"):
            _clear_tasks_panel()
            console.print(f"[{_ERROR}]Goodbye.[/{_ERROR}]")
            break
        if user_input.lower() == "/learn":
            _load_project_files_into_memory()
            console.print(f"[{_SUCCESS}]Project files re‑learned and stored in memory.[/{_SUCCESS}]")
            continue
        if user_input.lower() == "/api_key":
            new_key = console.input(f"[bold yellow]Enter new {_provider_config.provider.title()} API key: [/bold yellow]").strip()
            if new_key:
                _provider_config.api_key = new_key
                save_provider_config_encrypted(_provider_config)
                env_var = PROVIDER_ENV_VARS.get(_provider_config.provider, "")
                if env_var:
                    os.environ[env_var] = new_key
                llm_provider = get_provider(_provider_config)
                console.print(f"[{_SUCCESS}]API key updated and saved for future sessions.[/{_SUCCESS}]")
            else:
                console.print(f"[{_ERROR}]No key provided – key unchanged.[/{_ERROR}]")
            continue
        if user_input.lower() == "/provider":
            _switch_provider()
            continue
        if user_input.lower() == "/update":
            console.print(f"[{_ACCENT}]Updating OpenKyrozen from git...[/{_ACCENT}]")
            update_result = _self_update()
            console.print(Panel(update_result, title="Update", border_style=_ACCENT))
            continue

        if user_input.lower() == "/self-learning":
            _show_self_learning_menu()
            continue

        # /forget — show and optionally delete recent learnings
        if user_input.lower().startswith("/forget"):
            parts = user_input.split(maxsplit=1)
            prefix = parts[1] if len(parts) > 1 else ""
            console.print(Panel(_forget_recent(prefix), title="Forget", border_style=_WARNING))
            if prefix:
                # Auto-delete entries matching prefix (lowest score first)
                recent = memory_bank.get_recent(100)
                matched = [r for r in recent if r and prefix.lower() in r.lower() and not r.startswith("FILE:")]
                if matched:
                    memory_bank.delete_logs(matched[:3])
                    console.print(f"[{_SUCCESS}]Deleted {min(3, len(matched))} entries matching '{prefix}'.[/{_SUCCESS}]")
            continue

        # Auto‑patching: detect new technology mentions (if enabled)
        if _SELF_LEARNING_FLAGS["auto_patch_new_technology"]:
            _auto_patch_new_technology(user_input)

        # Prompt injection check
        sanitized, flagged = _sanitize_input(user_input)
        if flagged:
            console.print(f"[{_WARNING}]Prompt injection detected and filtered.[/{_WARNING}]")

        try:
            reply = _chat_turn(sanitized, clear_tasks=True)
        except Exception as e:
            import traceback as _tb
            console.print(f"[{_ERROR}]=== EXCEPTION IN _chat_turn ===[/{_ERROR}]")
            _tb.print_exc(file=sys.stderr)
            console.print(f"[{_ERROR}]Exception type: {type(e).__name__}[/{_ERROR}]")
            console.print(f"[{_ERROR}]Exception message: {e}[/{_ERROR}]")
            reply = f"Error: {e}"

        if len(reply.strip()) < 5:
            console.print(f"[{_ERROR}][Error] Received empty response from LLM[/{_ERROR}]")
            continue

        short_term_memory.append({"role": "user", "content": user_input})
        cleaned_reply = _remove_task_blocks(reply)
        short_term_memory.append({"role": "assistant", "content": cleaned_reply})
        memory_bank.add_log(f"User: {user_input}\nAssistant: {reply}")

        # Track fix outcomes and detect preferences
        if _is_bug_report(user_input) or any(kw in user_input.lower() for kw in ["fix", "bug", "error", "报错"]):
            _track_fix_outcome(user_input, reply)
        _detect_user_preferences(user_input)

        thinking, answer = _split_reply(reply)

        if thinking:
            console.print(Panel(thinking, title="Thinking", border_style=_ACCENT_DIM))
        if answer:
            console.print(Panel(Markdown(answer), title="Kyrozen", border_style=_ACCENT, title_align="left"))
        else:
            console.print(Panel(Markdown(answer or "(no content)"), title="Kyrozen", border_style=_ACCENT, title_align="left"))
        print()

        # Show tasks panel if any tasks exist
        if tasks.tasks:
            _update_tasks_panel()
            print()

        # auto‑continue: process further Action blocks without user input
        _is_auto_continue = True
        _auto_continue_limit = 5
        _original_user_input = user_input
        while _auto_continue_limit > 0 and not _original_user_input.startswith("/"):
            potential_actions = _collect_tool_calls(reply)
            if not potential_actions:
                break
            # treat reply as new user input for the next LLM call
            user_input = reply
            _auto_continue_limit -= 1
            try:
                reply = _chat_turn(user_input)
            except Exception as e:
                console.print(f"[{_ERROR}]Error: {e}[/{_ERROR}]")
                reply = f"Error: {e}"
                break
            if len(reply.strip()) < 5:
                console.print(f"[{_ERROR}][Error] Received empty response from LLM[/{_ERROR}]")
                break
            short_term_memory.append({"role": "user", "content": user_input})
            short_term_memory.append({"role": "assistant", "content": reply})
            memory_bank.add_log(f"User: {user_input}\nAssistant: {reply}")
            thinking, answer = _split_reply(reply)
            if thinking:
                console.print(Panel(thinking, title="Thinking", border_style=_ACCENT_DIM))
            if answer:
                console.print(Panel(Markdown(answer), title="Kyrozen", border_style=_ACCENT, title_align="left"))
            else:
                console.print(Panel(Markdown(answer or "(no content)"), title="Kyrozen", border_style=_ACCENT, title_align="left"))
            print()
            if tasks.tasks:
                _update_tasks_panel()
                print()

        _is_auto_continue = False

if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("=== CRITICAL SYSTEM TRACE ===", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
