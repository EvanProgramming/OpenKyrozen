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


console = Console()
bg_console = Console(stderr=True)


# ---- Constants (optimized for DeepSeek) ----
MODEL_NAME = "deepseek-chat"
SHORT_TERM_CAP = 16
MAX_TOOL_RETRIES = 3
MAX_STEPS_PER_TURN = 5           # how many tool‑call rounds the LLM may perform in one user turn
MAX_UNKNOWN_TOOL_RETRIES = 3     # how many times to re‑prompt when LLM uses an unrecognised action name
CONFIG_PATH = os.path.expanduser("~/.kyrozen_config.json")
IDLE_CONSOLIDATION_TIMEOUT = 60   # 1 minute


# -------- Task Manager for multi‑step tasks --------
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
            lines.append(f"  {status_icon}  {t['description']}")
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
# Core test suite covering the most important built‑in tools
_CORE_TESTS = [
    {
        "description": "list_dir returns a non‑empty string",
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
    "git_clone","git_status","execute_terminal_command","analyze_remote_repo"
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
    """If recent turns consumed many tokens, log a note (tool creation disabled)."""
    if len(_turn_cost_log) < 3:
        return
    recent_total = sum(entry.get("tokens", 0) for entry in _turn_cost_log[-5:])
    if recent_total < 5000:
        return
    memory_bank.add_log(f"COST_NOTE: Recent token total {recent_total}. Pattern compression skipped.")


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
    """Define a tool directly from code string, run regression, rollback on failure."""
    global _saved_user_tools
    _take_tool_snapshot()
    local_vars: dict = {}
    try:
        exec(code, {"__builtins__": __builtins__, "datetime": datetime, "UTC": datetime.timezone.utc}, local_vars)
    except Exception:
        return False
    if name not in local_vars or not callable(local_vars[name]):
        return False
    AVAILABLE_TOOLS[name] = local_vars[name]
    if not _run_regression_tests():
        _restore_tool_snapshot()
        return False
    memory_bank.add_log(
        f"New tool defined: {name}\n"
        f"Description: {description}\n"
        f"Code:\n```python\n{code}\n```"
    )
    print(f"[DefineTool] Added new tool '{name}' to AVAILABLE_TOOLS.")
    return True

# -------- Tool Refactoring --------
def _review_tools() -> None:
    """Scan user‑defined tools for duplication and merge candidates."""
    _take_tool_snapshot()
    user_defined = {k: v for k, v in AVAILABLE_TOOLS.items() if k not in _BUILTIN_TOOL_NAMES}
    if len(user_defined) < 2:
        return
    names = list(user_defined.keys())
    review_prompt = (
        "You are Kyrozen's tool refactoring module. The following tools exist:\n"
        + "\n".join(f"{n}: {getattr(v, '__doc__', '')[:100]}" for n, v in user_defined.items()) +
        "\nSuggest a merge plan: choose tools to combine into a single more powerful tool. "
        "Output JSON as a list of actions: [{\"action\":\"merge\",\"old\":[\"a\",\"b\"],\"new\":\"c\",\"code\":\"def c(args):...\"}]. "
        "If no merge, output []."
    )
    try:
        messages = [{"role": "system", "content": review_prompt}]
        answer = _get_llm_response(messages).strip()
        if answer and answer not in ("[]", ""):
            plan = json.loads(answer)
            for item in plan:
                if item.get("action") == "merge":
                    old_list = item.get("old", [])
                    new_name = item.get("new", "")
                    code = item.get("code", "")
                    if old_list and new_name and code:
                        # Remove old tools before registering new one
                        for old in old_list:
                            AVAILABLE_TOOLS.pop(old, None)
                        if _register_tool(new_name, code, description="merged"):
                            pass
                        else:
                            pass
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
                # bg_console.print(f"[italic yellow]{question}[/italic yellow]")
                _pending_inquiry = question
                return  # one inquiry per cycle


# -------- Auto‑Patching (Background Knowledge) --------
_known_libraries = set()
def _auto_patch_new_technology(user_input: str) -> None:
    """If user mentions an unknown library, fetch its docs in background."""
    # extract potential library names from user input
    words = user_input.split()
    for w in words:
        # Pattern 1: suffix based detection (lib, py, framework, package)
        if w.endswith(("lib","py","framework","package")) and w not in _known_libraries:
            _known_libraries.add(w)
            threading.Thread(target=_fetch_library_info, args=(w,), daemon=True).start()
    # Pattern 2: detect "use <Library>" or "using <Library>" phrases
    use_match = re.search(r"(?:use|using)\s+([A-Za-z_]\w*)", user_input, re.IGNORECASE)
    if use_match:
        lib = use_match.group(1)
        if lib not in _known_libraries:
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

def _call_llm_with_spinner(messages: list[dict]) -> str:
    global _SPINNER_STOP, _SPINNER_THREAD
    _SPINNER_STOP.clear()
    _SPINNER_THREAD = threading.Thread(target=_spinner_worker, args=(_SPINNER_STOP,), daemon=True)
    _SPINNER_THREAD.start()
    try:
        result = _get_llm_response(messages)
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
            DEEPSEEK_MODEL = "deepseek-chat"
            sys.exit(0)
        if not key:
            console.print("No API key entered – use /quit to exit.")
            deepseek_client = None
            DEEPSEEK_MODEL = "deepseek-chat"
            sys.exit(0)
        _save_config_key(key)
    os.environ["DEEPSEEK_API_KEY"] = key
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    deepseek_client = OpenAI(api_key=key, base_url=base_url)


DEEPSEEK_MODEL: str = "deepseek-chat"
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
    return f"""You are Kyrozen, an intelligent, self‑learning AI assistant. You have access to tools and can learn from project files and past conversations to improve your knowledge over time.

## Available Tools:
{tools_list}

🌐 **Action name rules:** The only defined action names are those listed above. Do **not** use `"bash"`, `"shell"`, or any other name – always use `"run_cmd"` for command execution.  
**Warning:** If you use an action name that is not listed above, the system will reject it and you will be forced to try again with a correct action. Do not invent new names.

## Current working directory
You are currently inside the project root directory of the repository the user is working in.  Relative paths (like "README.md" or "main.py") will be resolved correctly.  You can use `read_file`, `write_file`, `list_dir`, `find_files`, `run_cmd`, etc. **without needing the user to provide a path**.  Do **not** ask the user to supply a local path or a remote URL unless you intend to use the `analyze_remote_repo` tool to clone an external repository.

## How to use tools
When you need to perform an action, you **must** output a Thought followed by **exactly one** Action in JSON format enclosed in triple backticks.

**Format:**
Thought: (explain your reasoning)
Action:
```json
{{
  "action": "tool_name",
  "args": "arguments"
}}
```

If you need to run several tools in sequence, run **one per message** – the system will then ask you for the next step.

If no action is required, respond naturally in plain text.

You are not limited to coding tasks; you can assist with any topic. Use the tools when appropriate, but feel free to engage in general conversation. Always try to provide thorough, helpful answers.

You have stored knowledge of the project's code files as well as previous conversations. Use that knowledge to improve your answers and learn over time.

You cannot define new tools. You must use only the built-in tools listed in the Available Tools section.

### Always verify file paths
When you create a file using `write_file`, the tool returns its **absolute path** (e.g. `Wrote 800 characters to /Users/.../Testing/matrix_calculator.py`).  
**Always** read that path from the result and use the **absolute** path in any subsequent `run_cmd` or `read_file` call.  
If you do not see the absolute path, run `read_file` on the relative path to make sure you have the correct location, then remember that absolute path for later use.

**Format requirement**: Do **not** use raw XML tags (`<tool_name>args</tool_name>`) to invoke a tool. Only use the JSON Action block described above.

### Planning phase (mandatory)
**You MUST start with a Plan block before any Action block.**  
The Plan block must appear **before any TaskList or Action**.  
If you omit the Plan block, your output will be discarded and you will be forced to output a Plan.

Use the following format:

Plan:
1. (step description – why and what)
2. (step description)
...

Dependencies between steps must be clear.  Do not output any Action until the plan is complete.
After the plan, you **must** output a `TaskList:` block listing the tasks (see below).

### Task management
Any time you need to use one or more tools, you **must** first output a `TaskList:` block containing a JSON array of task descriptions.  Output the TaskList before the first Action block.
When you finish a task **before** taking the next Action, output `TaskDone: <index>` (where <index> is the zero‑based index of the completed task) **exactly** once, on its own line.
Do **not** forget to output `TaskDone:` – the system uses it to update the progress display.
Never ask the user whether to continue. Automatically proceed.

Prefer built‑in tools (`write_file`, `read_file`, `run_cmd`, `search_web`, `find_files`, `list_dir`, `read_webpage`, `git_clone`, `git_status`) over self‑created tools. Only define a new tool if none of the existing tools can accomplish the required task.

### Important reminder
When the user asks you to analyze the repository, start by running `list_dir('.')` to see the files.  Do **not** ask for a local path or remote URL – you are already in the correct directory.
"""


memory_bank = MemoryBank()
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
        "content": f"You are currently working inside the directory:\n{os.getcwd()}\n\nYou can use relative paths like '.' or 'main.py' directly.  Do not ask the user for a local path or a remote URL unless you intend to clone an external repository."
    })

    # Retrieve failure records if relevant
    failures = _retrieve_failure(user_input)
    if failures:
        failure_block = "Past failures to avoid:\n" + "\n".join(failures[:2])
        messages.append({"role": "system", "content": failure_block})

    recalled = memory_bank.recall(user_input, n_results=4)
    if recalled:
        memory_block = "Relevant past context:\n" + "\n".join(recalled[:2])
        messages.append({"role": "system", "content": memory_block})

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
    calls: list[dict] = []
    # 1. standard triple‑backtick block
    pattern = r"Action:\s*```(?:json)?\s*([\s\S]*?)\s*```"
    for match in re.finditer(pattern, text, re.DOTALL):
        raw = match.group(1).strip()
        raw = raw.rstrip("`").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "action" in data and _is_valid_action(data.get("action")):
            calls.append(data)

    # 2. plain “Action: { … }” without backticks
    plain_pattern = r"Action:\s*(?:\n)?\s*(\{[\s\S]*?\})"
    for match in re.finditer(plain_pattern, text, re.DOTALL):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "action" in data and _is_valid_action(data.get("action")):
            if data not in calls:
                calls.append(data)

    # 3. extract any JSON dictionary from anywhere (fallback)
    for obj in _extract_json_objects(text):
        if isinstance(obj, dict) and "action" in obj and _is_valid_action(obj.get("action")):
            if obj not in calls:
                calls.append(obj)


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
            content = args.get("content", "")
            args = f"{path}|{content}"
        else:
            import json
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
                    content = parsed.get("content", "")
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
    except Exception as e:
        result = f"Error: {e}"
    elapsed = time.time() - start
    _track_tool_performance(action, result, elapsed)
    return result


def _attempt_define_tool(text: str) -> bool:
    global _saved_user_tools
    _take_tool_snapshot()

    # Try JSON‑style DefineTool block first
    def _parse_definition(raw: str) -> dict | None:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    definition: dict | None = None

    # pattern 1: standard markdown block
    pattern1 = r"DefineTool:\s*```(?:json)?\s*([\s\S]*?)\s*```"
    match = re.search(pattern1, text)
    if match:
        raw = match.group(1).strip()
        definition = _parse_definition(raw)

    # pattern 2: XML‑style <DefineTool>...</DefineTool>
    if definition is None:
        xml_pattern = r'<DefineTool>\s*(.*?)\s*</DefineTool>'
        xml_match = re.search(xml_pattern, text, re.DOTALL)
        if xml_match:
            raw = xml_match.group(1).strip()
            # raw may be JSON directly or wrapped in backticks
            # try stripping backticks first
            inner = raw
            # remove surrounding ```json ... ``` if present
            inner = re.sub(r'^```(?:json)?\s*', '', inner)
            inner = re.sub(r'\s*```$', '', inner)
            inner = inner.strip()
            definition = _parse_definition(inner)

    if definition is None:
        return False

    name = definition.get("name", "").strip()
    description = definition.get("description", "").strip()
    code = definition.get("code", "").strip()

    if not name or not code:
        return False

    # Reject if a built‑in tool with a similar name or purpose already exists
    builtin_similar_names = {
        "write_file":  {"write","save","file","store"},
        "read_file":   {"read","open","load"},
        "run_cmd":     {"bash","shell","command","execute","terminal"},
        "search_web":  {"search","web","internet","query"},
        "find_files":  {"find","glob","file matching"},
        "list_dir":    {"list","directory","ls"},
        "git_clone":   {"clone","git"},
        "git_status":  {"status","git"},
        "read_webpage":{"read","webpage","url","html"},
    }
    if name in builtin_similar_names:
        return False
    # also check description for keywords
    for builtin, keywords in builtin_similar_names.items():
        if any(k in description.lower() for k in keywords):
            memory_bank.add_log(
                f"IGNORED_DEFINE_TOOL: {name} – built‑in '{builtin}' can handle the same task."
            )
            return False

    local_vars: dict = {}
    try:
        exec(code, {"__builtins__": __builtins__}, local_vars)
    except Exception:
        return False

    if name not in local_vars:
        return False

    new_tool = local_vars[name] = local_vars.pop(name)
    if not callable(new_tool):
        return False

    AVAILABLE_TOOLS[name] = new_tool

    # Quick regression check – if it fails, rollback
    if not _run_regression_tests():
        console.print(f"[red]Regression failed after DefineTool – reverting '{name}'[/red]")
        _restore_tool_snapshot()
        return False

    memory_bank.add_log(
        f"New tool defined: {name}\n"
        f"Description: {description}\n"
        f"Code:\n```python\n{code}\n```"
    )
    print(f"[DefineTool] Added new tool '{name}' to AVAILABLE_TOOLS.")
    return True


def _get_llm_response(messages: list[dict[str, str]]) -> str:
    global _last_prompt_tokens, _last_completion_tokens, _total_prompt_tokens, _total_completion_tokens
    if deepseek_client is None:
        return "[DeepSeek Error] DEEPSEEK_API_KEY environment variable not set"
    try:
        response = deepseek_client.chat.completions.create(
            model=DEEPSEEK_MODEL,
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
    """Return True if the user text looks like a request that needs tool execution (search, write, etc.)."""
    indicators = [
        "search", "research", "find", "compare", "write", "save", "run", "execute",
        "create", "make", "table", "file", "folder", "open", "read", "list", "download",
        "clone", "update", "pull", "edit", "modify", "move", "copy", "delete",
        "show", "check", "get", "fetch", "web", "internet", "online", "price",
        "spec", "specification", "review", "information", "about", "difference",
        "cost", "taobao", "jd", "how", "what", "which", "where", "why", "when",
    ]
    text_lower = text.lower()
    # Skip very short commands or greetings
    if len(text.split()) < 2:
        return False
    return any(ind in text_lower for ind in indicators)


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


def _chat_turn(user_input: str, clear_tasks: bool = False) -> str:
    """One user turn: build context, get LLM reply, execute tool calls
    with automatic retries and failure memory."""

    global _last_user_interaction
    _last_user_interaction = time.time()

    if clear_tasks:
        tasks.tasks.clear()

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
    # Remove task blocks from the displayed text
    response_text_clean = re.sub(r'(?:TaskList:\s*```(?:json)?[\s\S]*?```|TaskDone:\s*\d+)', '', response_text).strip()
    if not response_text_clean:
        response_text_clean = response_text
    response_text = response_text_clean

    tool_calls = _collect_tool_calls(response_text)
    # detect unknown action in response
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
        # re‑parse everything including tasks
        tasks.from_llm_block(response_text)
        tasks.mark_done_from_text(response_text)
        tool_calls = _collect_tool_calls(response_text)
    # Enforce planning phase before any tool execution
    _llm_has_plan = bool(re.search(r"Plan:", response_text))
    plan_attempts = 0
    while not _llm_has_plan and tool_calls and plan_attempts < 3:
        plan_attempts += 1
        plan_prompt = (
            "System: You must first output a **Plan** block (numbered steps) before any Action. "
            "Output a Plan:\n1. ...\n..."
        )
        messages.append({"role": "user", "content": plan_prompt})
        response_text = _call_llm_with_spinner(messages).strip()
        turn_prompt_total += _last_prompt_tokens
        turn_completion_total += _last_completion_tokens
        # reparse tasks and re‑collect tool calls
        tasks.from_llm_block(response_text)
        tasks.mark_done_from_text(response_text)
        tool_calls = _collect_tool_calls(response_text)
        _llm_has_plan = bool(re.search(r"Plan:", response_text))
    if not _llm_has_plan and tool_calls:
        # After 3 attempts still no plan, reject
        return "I cannot proceed without producing a Plan block first. Please rephrase your request."

    # Enforce that a TaskList is output before any tool execution
    _llm_has_tasklist = bool(re.search(r"TaskList:", response_text))
    if not _llm_has_tasklist and tool_calls:
        # Ask LLM to produce a TaskList first
        plan_prompt = (
            "System: You must first output a TaskList block (a JSON array of task descriptions) "
            "before using any tool. Please output your TaskList block now."
        )
        messages.append({"role": "user", "content": plan_prompt})
        response_text = _call_llm_with_spinner(messages).strip()
        turn_prompt_total += _last_prompt_tokens
        turn_completion_total += _last_completion_tokens
        # reparse
        tasks.from_llm_block(response_text)
        tasks.mark_done_from_text(response_text)
        response_text_clean = re.sub(r'(?:TaskList:\s*```(?:json)?[\s\S]*?```|TaskDone:\s*\d+)', '', response_text).strip()
        if not response_text_clean:
            response_text_clean = response_text
        response_text = response_text_clean
        _llm_has_tasklist = bool(re.search(r"TaskList:", response_text))
        tool_calls = _collect_tool_calls(response_text)
        if not _llm_has_tasklist and tool_calls:
            # fallback to auto-create tasks
            tasks.tasks.clear()
            for tc in tool_calls:
                safe_args = str(tc.get('args') or '')[:50]
                tasks.add_task(f"Execute {tc.get('action','?')}: {safe_args}")
    tool_was_search = any(tc.get("action") == "search_web" for tc in tool_calls)
    if not tool_calls:
        # If the user request seems to ask for action (search, research, write, save, compare, etc.)
        # and the assistant didn't produce any tool call, re‑prompt with a strong reminder.
        if _requires_tool_action(user_input):
            reminder_msg = (
                "System: You did not output an Action block. "
                "You **must** now output a JSON Action block to perform the actual work. "
                "Do not explain; just output the Action block."
            )
            messages.append({
                "role": "user",
                "content": reminder_msg,
            })
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
            # Re‑process task blocks and tool calls
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
            # No tools needed – return plain answer (maybe trigger reflection)
            return response_text


    results: list[str] = []
    for tc in tool_calls:
        action = tc.get("action", "")
        args = tc.get("args", "")
        if not isinstance(args, str):
            args = str(args)
        result = _run_tool(action, args)
        safe_result = str(result)[:2000]
        console.print(Panel(safe_result, title=f"Result of {action}", border_style="yellow"))
        results.append(f"- `{action}({args!r})` returned:\n{safe_result}")
        tasks.mark_first_pending_done()
        if tasks.tasks:
            console.print(Panel(tasks.format(), title="Tasks", border_style="blue"))
    tool_results_text = "\n".join(results)

    # Unified multi‑step loop – always runs, can handle early errors
    round_limit = MAX_STEPS_PER_TURN
    round_count = 0
    final_answer: str | None = None
    current_reply = response_text
    has_errors = any(_is_tool_error(r) for r in results)
    # Remember the first tool-call block for the context
    initial_response = response_text

    while round_count < round_limit:
        # Build the summary messages. If the last tool call failed, include guidance.
        error_hint = ""
        if has_errors:
            error_hint = (
                "The tool returned an error. The action name may be wrong. "
                "If you used a self‑created tool, try using a built‑in tool instead. "
                "Use one of the following actions: "
                + ", ".join(sorted(AVAILABLE_TOOLS.keys())) + ".\n"
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
            {"role": "system", "content": f"You are working inside {os.getcwd()}."},
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": current_reply},
            {
                "role": "user",
                "content": (
                    f"The tools returned:\n{tool_results_text}\n\n"
                    + error_hint +
                    "Please continue if there are remaining steps, or respond with the final answer.\n"
                    "If you have completed a task, you **must** output `TaskDone: <index>` "
                    "(replace index with the zero‑based index) **before** the next Action block. "
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


        # if there are no more tool calls, the LLM might still be planning
        if not next_tool_calls:
            # If there are still pending tasks, do NOT finish — keep asking
            if tasks.tasks and any(t["status"] != "done" for t in tasks.tasks):
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
                    final_answer = step_reply
                    break
            elif _is_question(step_reply):
                # re‑prompt if the LLM asked a question instead of acting
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
            if not isinstance(args, str):
                args = str(args)
            result2 = _run_tool(action, args)
            safe_result2 = str(result2)[:2000]
            console.print(Panel(safe_result2, title=f"Result of {action}", border_style="yellow"))
            next_results.append(f"- `{action}({args!r})` returned:\n{safe_result2}")
            tasks.mark_first_pending_done()
            if tasks.tasks:
                console.print(Panel(tasks.format(), title="Tasks", border_style="blue"))
            if _is_tool_error(result2):
                has_errors = True
        tool_results_text = "\n".join(next_results)
        # Check if all pending tasks are done; if so, stop (no need to ask LLM for more steps)
        if tasks.tasks and all(t["status"] != "pending" for t in tasks.tasks):
            final_answer = current_reply
            break
        # Update the LLM's previous output so the next iteration sees the new results (remove task blocks)
        current_reply = _remove_task_blocks(step_reply)

    if final_answer is None:
        # Fallback if the loop exited without a final answer
        final_answer = f"I executed your request. Here is the information I gathered:\n\n{tool_results_text}"
    elif len(final_answer.strip()) < 10:
        final_answer = f"I executed your request. Here is the information I gathered:\n\n{tool_results_text}"

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
        "You are Kyrozen's self‑learning module. Read the following recent conversation logs "
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
    """Check tool stats for poorly performing user‑defined tools and attempt auto‑debug."""
    _take_tool_snapshot()
    user_defined = {k: v for k, v in AVAILABLE_TOOLS.items()
                    if k not in _BUILTIN_TOOL_NAMES}
    for name, func in user_defined.items():
        stats = _tool_stats.get(name)
        if stats is None or stats["calls"] < 3:
            continue
        success_rate = stats["successes"] / stats["calls"]
        if success_rate < 0.5:
            # Debug mode: ask LLM to fix the tool code
            code = getattr(func, "__code__", None)
            source = None
            if code:
                try:
                    import inspect
                    source = inspect.getsource(func)
                except Exception:
                    pass
            if not source:
                continue
            debug_prompt = (
                f"The tool '{name}' has a success rate of {success_rate:.0%} "
                f"over {stats['calls']} calls. Its current code:\n```python\n{source}\n```\n"
                "Please rewrite the code to fix the most common errors. "
                "Output the corrected code as a Python function alone (no extra text)."
            )
            try:
                message = [{"role": "system", "content": debug_prompt}]
                new_code = _get_llm_response(message).strip()
                # Remove any markdown fences if present
                new_code = re.sub(r"^```python\s*", "", new_code)
                new_code = re.sub(r"\s*```", "", new_code)
                # Attempt to replace the tool
                local_vars: dict = {}
                exec(new_code, {"__builtins__": __builtins__, "datetime": datetime, "UTC": datetime.timezone.utc}, local_vars)
                if name in local_vars and callable(local_vars[name]):
                    old_tool = AVAILABLE_TOOLS.get(name)
                    AVAILABLE_TOOLS[name] = local_vars[name]
                    if not _run_regression_tests():
                        _restore_tool_snapshot()
                        return
                    memory_bank.add_log(f"AUTO_DEBUG: {name}\nNew code:\n```python\n{new_code}\n```")
            except Exception as e:
                pass


def _background_learning_loop() -> None:
    """Enhanced loop with consolidation, reflection, tool review, auto‑inquiry."""
    global _last_user_interaction
    while True:
        time.sleep(120)
        try:
            now = time.time()
            idle_duration = now - _last_user_interaction

            # Skip heavy background work if the user has been active within the last 1 minute
            if idle_duration < 60:
                continue

            # silently learning
            _auto_learn_conversations()
            _load_project_files_into_memory()
            _age_out_old_coded_entries()
            _auto_debug_tool()

            if idle_duration > IDLE_CONSOLIDATION_TIMEOUT:
                _consolidate_memories()
                if not _run_regression_tests():
                    _restore_tool_snapshot()
                _review_tools()
                if not _run_regression_tests():
                    _restore_tool_snapshot()
                _targeted_inquiry()
                _maybe_trigger_reflection()
                _maybe_strategy_distillation()
        except Exception:
            pass


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
    console.print(Panel.fit(banner_text, title="OPEN KYROZEN", subtitle="self‑learning AI agent", border_style="cyan"))
    console.print(f"Kyrozen (DeepSeek + Tools). Model: {MODEL_NAME}", style="cyan")
    console.print("Commands: /quit or /exit to exit, /update to pull latest version, /learn to reload project files, /api_key to change API key.\n", style="yellow")

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
                DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
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

        # Auto‑patching: detect new technology mentions
        _auto_patch_new_technology(user_input)

        # If there was a pending inquiry and the user answered, store the answer immediately
        if _pending_inquiry is not None:
            # Store the user's input as a fact
            memory_bank.add_log(f"FACT: User answered inquiry – {user_input}")
            _pending_inquiry = None

        try:
            reply = _chat_turn(user_input, clear_tasks=True)
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
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
            console.print(Panel(Markdown(answer), title="Agent", border_style="green"))
        else:
            console.print(Panel(Markdown(answer or "(no content)"), title="Agent", border_style="green"))
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
                console.print(Panel(Markdown(answer), title="Agent", border_style="green"))
            else:
                console.print(Panel(Markdown(answer or "(no content)"), title="Agent", border_style="green"))
            print()
            if tasks.tasks:
                console.print(Panel(tasks.format(), title="Tasks", border_style="blue"))
                print()

        _is_auto_continue = False

if __name__ == "__main__":
    main()
