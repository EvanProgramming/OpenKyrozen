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

# Delete stale __pycache__ before any imports to avoid loading deprecated bytecode
for p in pathlib.Path(__file__).parent.rglob("__pycache__"):
    shutil.rmtree(p, ignore_errors=True)

import ast
import json
import os
import re
import subprocess
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
from tools import AVAILABLE_TOOLS

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


# ---- Constants (optimized for DeepSeek V4) ----
MODEL_NAME = "deepseek-chat (V4 auto-select)"
DEEPSEEK_MODEL_SIMPLE = os.environ.get("DEEPSEEK_MODEL_SIMPLE", "deepseek-chat")      # fast, general-purpose (V4-level)
DEEPSEEK_MODEL_COMPLEX = os.environ.get("DEEPSEEK_MODEL_COMPLEX", "deepseek-reasoner")  # deep reasoning for complex tasks
SHORT_TERM_CAP = 16
MAX_TOOL_RETRIES = 3
MAX_STEPS_PER_TURN = 50          # how many tool-call rounds the LLM may perform in one user turn
MAX_UNKNOWN_TOOL_RETRIES = 3     # how many times to re-prompt when LLM uses an unrecognised action name
CONFIG_PATH = os.path.expanduser("~/.kyrozen_config.json")
IDLE_CONSOLIDATION_TIMEOUT = 60   # 1 minute

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
    "list_tree","read_webpage","check_stored_data","search_memory"
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
    """Remove or mark obsolete code facts when code files change."""
    global _last_code_scan_time
    now = time.time()
    if now - _last_code_scan_time < 3600:  # once per hour
        return
    _last_code_scan_time = now
    _remove_stale_file_entries(Path(__file__).parent.resolve())

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


# -------- Active Exploration (Targeted Inquiry) --------
_last_inquiry_time = time.time()
_pending_inquiry: str | None = None  # stores the question, cleared after user answers

def _targeted_inquiry() -> None:
    """Scan project files for undocumented functions and print a prompt."""
    global _last_inquiry_time, _pending_inquiry
    now = time.time()
    if now - _last_inquiry_time < 600:  # once per 10 min
        return
    _last_inquiry_time = now
    project_root = Path(__file__).parent.resolve()
    for py_file in project_root.rglob("*.py"):
        if "__pycache__" in str(py_file):
            continue
        content = py_file.read_text(encoding="utf-8", errors="ignore")
        # naive detection: function definition without docstring
        matches = re.findall(r"def (\w+)\(.*\):\s*(?:\"\"\"|''')?", content)
        for m in matches:
            func_def = f"def {m}("
            # check if first non‑comment line after func_def is docstring
            after = content.split(func_def,1)[-1].split("\n",1)[-1]
            if not after.lstrip().startswith('"""') and not after.lstrip().startswith("'''"):
                question = f"I noticed function '{m}' in {py_file.name} has no docstring – what does it do? (You can tell me and I'll remember.)"
                bg_console.print(f"\n[bold yellow]🔍 {question}[/bold yellow]\n")
                _pending_inquiry = question
                return  # one inquiry per cycle


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

    words = user_input.split()
    for w in words:
        # Pattern 1: suffix based detection (lib, py, framework, package)
        if w.endswith(("lib","py","framework","package")) and w not in _known_libraries:
            detected.add(w)

    # Pattern 2: detect "use <Library>" or "using <Library>" phrases
    for match in re.finditer(r"(?:use|using)\s+([A-Za-z_]\w*)", user_input, re.IGNORECASE):
        lib = match.group(1)
        if lib.lower() not in ("a","an","the","this","that","it","my","our","your"):
            detected.add(lib)

    # Pattern 3: import statements in code or conversation
    for match in re.finditer(r"(?:import|from)\s+([A-Za-z_]\w*)", user_input):
        lib = match.group(1)
        if lib.lower() not in ("a","an","the","os","sys","re","json","time","math"):
            detected.add(lib)

    # Pattern 4: "pip install <lib>" or "install <lib>"
    for match in re.finditer(r"(?:pip\s+install|install)\s+([A-Za-z_][\w.-]*)", user_input, re.IGNORECASE):
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

_SPINNER_FRAMES = [
    "[ . . . ===> \\ / ===> @ @ @ ]",
    "[ : . . . ===> / \\ ===> # @ @ ]",
    "[ : : . . ===> \\ / ===> ## @ ]",
    "[ : : : . ===> / \\ ===> @@@ ]",
    "[ . . . . ===> \\ / ===> @@@ ]",
]

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
        existing["api_key"] = key.strip()
        with open(CONFIG_PATH, "w") as f:
            json.dump(existing, f, indent=2)
    except OSError:
        console.print("[yellow]Warning: could not save API key to config file.[/yellow]")


def _prompt_and_init_deepseek() -> None:
    global deepseek_client, DEEPSEEK_MODEL
    key = _load_config_key()
    if not key:
        key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        print("\nDeepSeek API key not set. Enter your API key: ", end="", flush=True)
        try:
            key = input().strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nCancelled.")
            deepseek_client = None
            DEEPSEEK_MODEL = DEEPSEEK_MODEL_SIMPLE
            sys.exit(0)
        if not key:
            console.print("No API key entered – use /quit to exit.")
            deepseek_client = None
            DEEPSEEK_MODEL = DEEPSEEK_MODEL_SIMPLE
            sys.exit(0)
        _save_config_key(key)
    os.environ["DEEPSEEK_API_KEY"] = key
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", DEEPSEEK_MODEL_SIMPLE)
    deepseek_client = OpenAI(api_key=key, base_url=base_url)


DEEPSEEK_MODEL: str = DEEPSEEK_MODEL_SIMPLE
deepseek_client = None


def _load_project_files_into_memory() -> None:
    project_root = Path(__file__).parent.resolve()
    # Before adding new versions, remove any stale FILE entries from ChromaDB
    _remove_stale_file_entries(project_root)
    # Skip directories that can contain many files (e.g., virtual environment)
    skip_dirs = {".venv", "venv", "chroma_memory", "__pycache__", ".git"}
    for py_file in project_root.rglob("*.py"):
        # Skip files inside skipped directories
        if any(part in py_file.parts for part in skip_dirs):
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
            rel_path = str(py_file.relative_to(project_root))
            log_text = f"FILE: {rel_path}\n```python\n{content}\n```"
            memory_bank.add_log(log_text)
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception:
            pass


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


def _remove_stale_file_entries(project_root: Path) -> None:
    """Delete old FILE entries that no longer correspond to existing .py files."""
    if memory_bank._collection is None:
        return
    skip_dirs = {".venv", "venv", "chroma_memory", "__pycache__", ".git"}
    existing_py_files = set()
    for py_file in project_root.rglob("*.py"):
        # Skip files inside skipped directories
        if any(part in py_file.parts for part in skip_dirs):
            continue
        existing_py_files.add(str(py_file.relative_to(project_root)))
    try:
        all_logs = memory_bank._collection.get()
        ids = all_logs.get("ids", [])
        documents = all_logs.get("documents", [])
        to_delete = []
        for i, doc in enumerate(documents):
            if doc and doc.startswith("FILE:"):
                lines = doc.split("\n")
                if len(lines) >= 1:
                    file_rel = lines[0].replace("FILE: ", "").strip()
                    if file_rel not in existing_py_files:
                        to_delete.append(ids[i])
        if to_delete:
            memory_bank._collection.delete(ids=to_delete)
    except Exception:
        pass


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
        "{{\"action\": \"tool_name\", \"args\": \"arguments\"}}\n"
        "```\n\n"
        "**Args rules:**\n"
        "- `args` is always a **plain string**, never a JSON object.\n"
        "- For `write_file`: use `\"path|content\"` (pipe‑separated).\n"
        "- For `run_cmd`: the full shell command as one string.\n"
        "- Use only the action names listed above. Aliases like `bash`→`run_cmd` are accepted.\n\n"
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
        "TaskList:\n"
        "```json\n"
        "[\"Task description 1\", \"Task description 2\", ...]\n"
        "```\n"
        "After completing a task, output `TaskDone: <index>` on its own line before the next Action.\n\n"
        "## General rules\n"
        "- Never ask the user for permission to continue — decide and act.\n"
        "- When analysing a repo, start with `list_dir('.')`.\n"
        "- After `write_file`, use the absolute path returned in subsequent commands.\n"
        "- For stored knowledge, use `search_memory` or `check_stored_data`.\n"
        "- Do not invent tool names; use exactly the ones listed above.\n"
        + cwd_note
    )


memory_bank = MemoryBank()

# ---- Tool to let agent examine its own memory ----
def _check_stored_data(args: str) -> str:
    """Return a summary of stored memories from ChromaDB (or in‑memory fallback).
    Only facts (non‑file entries) are shown; source code copies are hidden to reduce noise."""
    try:
        total = memory_bank.count_logs()
        # Retrieve enough logs to find non‑FILE entries.
        recent_all = memory_bank.get_recent(200)
        # Filter out FILE entries
        fact_entries = [log for log in recent_all if not log.startswith("FILE:")]
        # Show only the most recent 5 non‑FILE logs
        displayed = fact_entries[:5]
        lines = [f"Total stored logs: {total}"]
        if displayed:
            for i, log in enumerate(displayed):
                snippet = _safe_fstring(log[:300])
                lines.append(f"\n--- Log {i+1} (FACT) ---\n{snippet}")
        else:
            lines.append("\n(No non‑file facts have been learned yet.)")
        lines.append(f"\nTotal non‑file fact entries: {len(fact_entries)}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading memory: {e}"


def _search_memory(args: str) -> str:
    """Search stored memories for facts relevant to the query. Args: "query" """
    try:
        query = args.strip() if args else ""
        if not query:
            return "Search Error: provide a query."
        # First attempt: semantic search via ChromaDB/memory recall
        results = memory_bank.recall(query, n_results=5)
        if not results:
            # Fallback: scan recent non‑FILE entries for keyword matches
            recent = memory_bank.get_recent(200)
            keywords = query.lower().split()
            matched = []
            for doc in recent:
                if doc.startswith("FILE:"):
                    continue
                if any(kw in doc.lower() for kw in keywords):
                    matched.append(doc)
            if matched:
                results = matched[:5]
            else:
                # No results even after fallback
                return "No relevant memories found."
        lines = [f"Relevant memories ({len(results)}):"]
        for i, doc in enumerate(results):
            # Don't show file entries in this search either
            if doc.startswith("FILE:"):
                continue
            snippet = _safe_fstring(doc[:500].replace("\n", " "))
            lines.append(f"\n--- Result {i+1} ---\n{snippet}")
        ret = "\n".join(lines)
        if ret.strip() == f"Relevant memories ({len(results)}):":
            return "No non‑file facts found for that query."
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


def _attempt_define_tool(text: str) -> bool:
    return False


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

    # Multi-step / numbered instructions
    if re.search(r"\b(step|first|then|next|finally|after that)\b", text):
        complexity_score += 2
    if re.search(r"\d+[.)]\s", text):
        complexity_score += 1

    # Code-related tasks
    code_keywords = [
        "code", "debug", "fix", "refactor", "implement", "build a",
        "write a program", "write a script", "function", "class",
        "architecture", "design pattern", "optimize", "performance"
    ]
    if any(kw in text for kw in code_keywords):
        complexity_score += 3  # code tasks are inherently complex

    # Deep analysis / reasoning
    analysis_keywords = [
        "analyze", "explain how", "explain why", "compare", "evaluate",
        "review this", "understand", "deep dive", "audit", "diagnose"
    ]
    if any(kw in text for kw in analysis_keywords):
        complexity_score += 2

    # Research / multi-source tasks
    research_keywords = [
        "research", "find all", "gather", "summarize", "comprehensive",
        "report", "overview of", "investigate"
    ]
    if any(kw in text for kw in research_keywords):
        complexity_score += 1

    if complexity_score >= 3:
        return DEEPSEEK_MODEL_COMPLEX
    return DEEPSEEK_MODEL_SIMPLE


def _get_llm_response(messages: list[dict[str, str]], model: str | None = None) -> str:
    global _last_prompt_tokens, _last_completion_tokens, _total_prompt_tokens, _total_completion_tokens
    if deepseek_client is None:
        return "[DeepSeek Error] DEEPSEEK_API_KEY environment variable not set"
    try:
        response = deepseek_client.chat.completions.create(
            model=model or DEEPSEEK_MODEL,
            messages=messages,
        )
        text = response.choices[0].message.content or ""
        # Track token usage if available
        usage = getattr(response, "usage", None)
        if usage is not None:
            _last_prompt_tokens = usage.prompt_tokens or 0
            _last_completion_tokens = usage.completion_tokens or 0
            _total_prompt_tokens += _last_prompt_tokens
            _total_completion_tokens += _last_completion_tokens
        else:
            _last_prompt_tokens = 0
            _last_completion_tokens = 0
        return text.strip()
    except Exception as e:
        return f"[DeepSeek Error] {e}"


def _requires_tool_action(text: str) -> bool:
    """Return True if the user text looks like a request that needs tool execution."""
    text_lower = text.lower()
    if len(text.split()) <= 2:
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
    if any(ind in " " + text_lower + " " for ind in action_indicators):
        return True
    # Context nouns that suggest tool-requiring tasks
    context_indicators = [
        "file", "folder", "directory", "repo", "repository", "github",
        "web", "internet", "online", "price", "spec", "specification",
        "report", "summary of", "overview of",
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
        "search temporarily unavailable" in low
    )


def _is_question(text: str) -> bool:
    low = text.strip().lower()
    if "?" in low:
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


def _classify_complexity(user_input: str) -> str:
    """Classify the user request as simple, medium, or complex.
    Returns 'simple', 'medium', or 'complex'."""
    text = user_input.lower().strip()

    # Simple: short greetings, trivial questions, single actions
    simple_patterns = [
        r"^(hi|hey|hello|yo|sup|good morning|good evening)\b",
        r"^(thanks|thank you|ok|okay|got it|bye|goodbye)\b",
        r"^(what is|who is|when is|where is|how are you|what can you)\b",
        r"^(yes|no|maybe|sure|yep|nope)$",
    ]
    if any(re.search(p, text) for p in simple_patterns):
        return "simple"
    # Short inputs (≤3 words) without tool verbs are simple
    _tool_verbs = {"read","list","find","search","write","run","create","clone","fetch","open","edit","delete","move","copy"}
    words = set(user_input.lower().split())
    if len(user_input.split()) <= 3 and not (words & _tool_verbs):
        return "simple"

    # Complex: multi-step, code generation, deep analysis
    complex_indicators = [
        r"\b(step|first|then|next|finally|after that)\b",
        r"\d+[.)]\s+\w",  # numbered steps
        r"\b(implement|refactor|migrate|audit|comprehensive)\b",
        r"\b(write\s+(a|the)\s+(program|script|app|application|function|class))\b",
        r"\b(analy[sz]e\s+(the|this|my|our)\s+(codebase|repo|project|architecture))\b",
    ]
    if any(re.search(p, text) for p in complex_indicators):
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
                console.print(Panel(tasks.format(), title="Tasks", border_style="blue"))
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
                console.print(Panel(tasks.format(), title="Tasks (auto)", border_style="blue"))

    # ---- Missing action recovery ----
    if not tool_calls:
        if _requires_tool_action(user_input) or _llm_has_plan or _llm_has_tasklist:
            reminder_msg = (
                "System: You did not output an Action block. "
                "You **must** now output a JSON Action block to perform the actual work. "
                "Do not explain; just output the Action block."
            )
            messages.append({"role": "user", "content": reminder_msg})
            response_text = _call_llm_with_spinner(messages).strip()
            turn_prompt_total += _last_prompt_tokens
            turn_completion_total += _last_completion_tokens
            if not response_text or not response_text.strip():
                messages.append({
                    "role": "user",
                    "content": "System: You returned nothing. Please output a JSON Action block now."
                })
                response_text = _call_llm_with_spinner(messages).strip()
                turn_prompt_total += _last_prompt_tokens
                turn_completion_total += _last_completion_tokens
            tasks.from_llm_block(response_text)
            tasks.mark_done_from_text(response_text)
            tool_calls = _collect_tool_calls(response_text)
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
        console.print(Panel(rich_escape(safe_result), title=f"Result of {action}", border_style="yellow"))
        results.append(f"- `{action}({args!r})` returned:\n{_safe_fstring(safe_result)}")
        tasks.mark_first_pending_done()
        if tasks.tasks:
            console.print(Panel(tasks.format(), title="Tasks", border_style="blue"))
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

        # Search throttle: prevent infinite search loops
        search_throttle = ""
        if consecutive_search_failures >= 3:
            search_throttle = (
                f"\n⚠️  **Search throttle active:** {consecutive_search_failures} consecutive "
                "search_web calls failed. The search backend is likely rate‑limited or "
                "unavailable right now. **Stop using search_web.** Instead:\n"
                "- If you have partial results from earlier searches, work with those.\n"
                "- Use `read_webpage` on a known URL instead.\n"
                "- Acknowledge the limitation and give the best answer you can with "
                "available information.\n"
            )
        elif total_search_calls >= 5:
            search_throttle = (
                f"\n⚠️  **Search limit warning:** {total_search_calls} search_web calls "
                "made this turn. Limit further searches to at most 1 more. Prefer "
                "`read_webpage` on known URLs instead.\n"
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
                "role": "user",
                "content": (
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

        # if there are no more tool calls, the LLM might still be planning
        if not next_tool_calls:
            # If there are still pending tasks, do NOT finish — keep asking
            if tasks.tasks and any(t["status"] != "done" for t in tasks.tasks):
                incomplete_prompt_attempts += 1
                if incomplete_prompt_attempts >= 15:
                    # Force complete remaining tasks to avoid infinite loop
                    for t in tasks.tasks:
                        if t["status"] == "pending":
                            t["status"] = "done"
                    console.print(f"[yellow]Auto‑completed remaining tasks after {incomplete_prompt_attempts} prompts.[/yellow]")
                    break
                summary_messages.append({
                    "role": "user",
                    "content": "System: There are still incomplete tasks. Output the next Action block now."
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
                    # After retry still no action, mark incomplete tasks as done and exit
                    for t in tasks.tasks:
                        if t["status"] == "pending":
                            t["status"] = "done"
                    console.print("[yellow]Auto-completed remaining tasks (no action after retry).[/yellow]")
                    break
            elif _is_question(step_reply):
                # re-prompt if the LLM asked a question instead of acting
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
                # The LLM may have announced intent without outputting an Action block.
                # Prompt it to issue the next Action block.
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
            console.print(Panel(rich_escape(safe_result2), title=f"Result of {action}", border_style="yellow"))
            next_results.append(f"- `{action}({args!r})` returned:\n{_safe_fstring(safe_result2)}")
            tasks.mark_first_pending_done()
            if tasks.tasks:
                console.print(Panel(tasks.format(), title="Tasks", border_style="blue"))
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
        # Hard limit: too many search_web calls → force stop
        if total_search_calls >= 7:
            final_answer = (
                "Search limit reached (7+ search_web calls this turn). "
                "The search backend appears to be unavailable or rate‑limited. "
                "Here's what I found from the earlier searches: " + tool_results_text[:500]
            )
            console.print("[yellow]Search limit reached — forcing task completion.[/yellow]")
            break
        # Check if all pending tasks are done; if so, stop (no need to ask LLM for more steps)
        if tasks.tasks and all(t["status"] != "pending" for t in tasks.tasks):
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

            console.print("[dim]Learning...[/dim]")

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
        console.print("[bold cyan]Self‑Learning Features[/bold cyan]")
        console.print("Enter the number of a feature to toggle it on/off, or 'done' to exit.\n")
        for i, (key, desc) in enumerate(flag_names):
            status = "✓" if _SELF_LEARNING_FLAGS[key] else "✗"
            console.print(f"  {i+1}. [{status}] {desc}")
        console.print()
        choice = console.input("[bold yellow]Toggle (number) or 'done': [/bold yellow]").strip().lower()
        if choice == "done":
            console.print("[green]Self‑learning settings updated.[/green]")
            break
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(flag_names):
                key = flag_names[idx][0]
                _SELF_LEARNING_FLAGS[key] = not _SELF_LEARNING_FLAGS[key]
                console.print(f"[green]Toggled {flag_names[idx][1]} to {'enabled' if _SELF_LEARNING_FLAGS[key] else 'disabled'}.[/green]")
            else:
                console.print("[red]Invalid number.[/red]")
        except ValueError:
            console.print("[red]Please enter a number or 'done'.[/red]")


def main() -> None:
    # Bytecode cache cleared already at module level (see top of file)
    global deepseek_client, DEEPSEEK_MODEL, _pending_inquiry

    if "--init" in sys.argv:
        console.print("[cyan]OpenKyrozen initialisation[/cyan]")
        try:
            import openai  # noqa: F401
        except ImportError:
            console.print("[red]openai not installed. Run: pip install -r requirements.txt[/red]")
            sys.exit(1)
        _prompt_and_init_deepseek()
        _load_project_files_into_memory()
        if deepseek_client is not None:
            console.print("[green]API key configured and saved to ~/.kyrozen_config.json[/green]")
        console.print("[green]Initialisation finished. Run `python main.py` to start the agent.[/green]")
        sys.exit(0)

    banner_text = r"""   ██████╗ ██████╗ ███████╗███╗   ██╗    ██╗  ██╗██╗   ██╗██████╗  ██████╗ ███████╗███████╗███╗   ██╗
  ██╔═══██╗██╔══██╗██╔════╝████╗  ██║    ██║ ██╔╝╚██╗ ██╔╝██╔══██╗██╔═══██╗╚══███╔╝╚══███╔╝████╗  ██║
  ██║   ██║██████╔╝█████╗  ██╔██╗ ██║    █████╔╝  ╚████╔╝ ██████╔╝██║   ██║  ███╔╝   ███╔╝ ██╔██╗ ██║
  ██║   ██║██╔═══╝ ██╔══╝  ██║╚██╗██║    ██╔═██╗   ╚██╔╝  ██╔══██╗██║   ██║ ███╔╝   ███╔╝  ██║╚██╗██║
  ╚██████╔╝██║     ███████╗██║ ╚████║    ██║  ██╗   ██║   ██║  ██║╚██████╔╝███████╗███████╗██║ ╚████║
   ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚═══╝    ╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚══════╝╚═╝  ╚═══╝"""
    console.print(Panel.fit(banner_text, title="OPEN KYROZEN", subtitle="self-learning AI agent", border_style="cyan"))
    console.print(f"Kyrozen (DeepSeek + Tools). Model: {MODEL_NAME}", style="cyan")
    console.print("Commands: /quit or /exit to exit, /update to pull latest version, /learn to reload project files, /api_key to change API key, /self-learning to toggle self-learning features.\n", style="yellow")
    # Show which self-learning features are enabled
    enabled = [name for name, val in _SELF_LEARNING_FLAGS.items() if val]
    if enabled:
        console.print(f"[dim]Self-learning features enabled: {rich_escape(', '.join(enabled))}.[/dim]")
    else:
        console.print("[dim]All self-learning features are disabled (use /self-learning to enable).[/dim]")
    console.print("[dim]Learning results are stored in `chroma_memory/` (ChromaDB persistent storage). You can also see them by asking the agent what it remembers.[/dim]")

    _prompt_and_init_deepseek()
    if deepseek_client is None:
        console.print("Cannot start without an API key.", style="red")
        sys.exit(1)
    threading.Thread(target=_load_project_files_into_memory, daemon=True).start()
    console.print("[yellow]Loading project files in background...[/yellow]")

    # Start background learning daemon
    threading.Thread(target=_background_learning_loop, daemon=True).start()

    while True:
        try:
            user_input = console.input("[bold cyan]You: [/bold cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[red]Goodbye.[/red]")
            sys.exit(0)

        if not user_input:
            continue

        _is_auto_continue = False

        # clear tasks for a new user request (skip during auto‑continue)
        if not _is_auto_continue and not user_input.startswith("/"):
            tasks.tasks.clear()

        if user_input.lower() in ("/quit", "/exit"):
            console.print("[red]Goodbye.[/red]")
            break
        if user_input.lower() == "/learn":
            _load_project_files_into_memory()
            console.print("[green]Project files re‑learned and stored in memory.[/green]")
            continue
        if user_input.lower() == "/api_key":
            new_key = console.input("[bold yellow]Enter new DeepSeek API key: [/bold yellow]").strip()
            if new_key:
                _save_config_key(new_key)
                os.environ["DEEPSEEK_API_KEY"] = new_key
                base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
                DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", DEEPSEEK_MODEL_SIMPLE)
                deepseek_client = OpenAI(api_key=new_key, base_url=base_url)
                console.print("[green]API key updated and saved for future sessions.[/green]")
            else:
                console.print("[red]No key provided – key unchanged.[/red]")
            continue
        if user_input.lower() == "/update":
            console.print("[yellow]Updating OpenKyrozen from git...[/yellow]")
            update_result = _self_update()
            console.print(Panel(update_result, title="Update", border_style="blue"))
            continue

        if user_input.lower() == "/self-learning":
            _show_self_learning_menu()
            continue

        # Auto‑patching: detect new technology mentions (if enabled)
        if _SELF_LEARNING_FLAGS["auto_patch_new_technology"]:
            _auto_patch_new_technology(user_input)

        # If there was a pending inquiry and the user answered, store the answer immediately
        if _pending_inquiry is not None and not user_input.startswith("/"):
            # Only treat non‑command input as a genuine answer
            if len(user_input.strip()) > 2:
                memory_bank.add_log(f"FACT: User answered inquiry '{_pending_inquiry[:80]}...' with: {user_input}")
            _pending_inquiry = None

        try:
            reply = _chat_turn(user_input, clear_tasks=True)
        except Exception as e:
            import traceback as _tb
            console.print("[red]=== EXCEPTION IN _chat_turn ===[/red]")
            _tb.print_exc(file=sys.stderr)
            console.print(f"[red]Exception type: {type(e).__name__}[/red]")
            console.print(f"[red]Exception message: {e}[/red]")
            reply = f"Error: {e}"

        if len(reply.strip()) < 5:
            console.print("[red][Error] Received empty response from LLM[/red]")
            continue

        short_term_memory.append({"role": "user", "content": user_input})
        cleaned_reply = _remove_task_blocks(reply)
        short_term_memory.append({"role": "assistant", "content": cleaned_reply})
        memory_bank.add_log(f"User: {user_input}\nAssistant: {reply}")

        thinking, answer = _split_reply(reply)

        if thinking:
            console.print(Panel(thinking, title="Thinking", border_style="dim white"))
        if answer:
            console.print(Panel(Markdown(rich_escape(answer)), title="Agent", border_style="green"))
        else:
            console.print(Panel(Markdown(rich_escape(answer or "(no content)")), title="Agent", border_style="green"))
        print()

        # Show tasks panel if any tasks exist
        if tasks.tasks:
            console.print(Panel(tasks.format(), title="Tasks", border_style="blue"))
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
                console.print(f"[red]Error: {e}[/red]")
                reply = f"Error: {e}"
                break
            if len(reply.strip()) < 5:
                console.print("[red][Error] Received empty response from LLM[/red]")
                break
            short_term_memory.append({"role": "user", "content": user_input})
            short_term_memory.append({"role": "assistant", "content": reply})
            memory_bank.add_log(f"User: {user_input}\nAssistant: {reply}")
            thinking, answer = _split_reply(reply)
            if thinking:
                console.print(Panel(thinking, title="Thinking", border_style="dim white"))
            if answer:
                console.print(Panel(Markdown(rich_escape(answer)), title="Agent", border_style="green"))
            else:
                console.print(Panel(Markdown(rich_escape(answer or "(no content)")), title="Agent", border_style="green"))
            print()
            if tasks.tasks:
                console.print(Panel(tasks.format(), title="Tasks", border_style="blue"))
                print()

        _is_auto_continue = False

if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("=== CRITICAL SYSTEM TRACE ===", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
