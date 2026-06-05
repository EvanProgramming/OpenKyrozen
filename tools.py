"""
Agent capabilities: file I/O, shell commands, web search.
"""

import os
import subprocess
import re
import glob
import tempfile
import shutil
from typing import Any

from duckduckgo_search import DDGS


# ---- Safety: block dangerous shell commands ----
_BLOCKED_PATTERNS = [
    r"\brm\s+(-rf?|-\s*rf?)\s",
    r"\brm\s+.*-r",
    r"^\s*rm\s+-rf",
    r"\brm\s+-rf\b",
    r"\bmkfs\.\w+",
    r">\s*/dev/sd",
    r':\(\)\s*\{\s*:\s*\|\s*:\s*&',
    r"wget\s+.*\|\s*sh\s*$",
    r"curl\s+.*\|\s*sh\s*$",
]
_BLOCKED_RE = re.compile("|".join(_BLOCKED_PATTERNS), re.IGNORECASE)


def _is_dangerous(cmd: str) -> bool:
    """Return True if the command looks dangerous and should be blocked."""
    return bool(_BLOCKED_RE.search(cmd))


def write_file(args: str) -> str:
    """
    Write content to a file. Args format: "path|content".
    Supports ~ for user home (e.g. ~/Desktop/file.txt).
    """
    try:
        parts = args.split("|", 1)
        if len(parts) < 2:
            return "Error: write_file requires args in format path|content"
        raw_path, content = parts[0].strip(), parts[1]
        path = os.path.expanduser(raw_path)
        abs_path = os.path.abspath(path)
        os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Wrote {len(content)} characters to {abs_path}"
    except Exception as e:
        return f"Error writing file: {e}"


def read_file(args: str) -> str:
    """
    Read content from a file. Args format: "path".
    Supports ~ for user home.
    """
    try:
        raw_path = args.strip()
        if not raw_path:
            return "Error: read_file requires a path"
        path = os.path.expanduser(raw_path)
        abs_path = os.path.abspath(path)
        with open(abs_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return f"Error: file not found: {os.path.abspath(os.path.expanduser(args.strip()))}"
    except Exception as e:
        return f"Error reading file: {e}"


def run_cmd(args: str) -> str:
    """
    Execute a shell command. Args: the full command string.
    Blocks dangerous operations (e.g. rm -rf).
    """
    cmd = args.strip()
    if not cmd:
        return "Error: run_cmd requires a command"
    if _is_dangerous(cmd):
        return "Error: command blocked for safety (e.g. rm -rf or similar)."
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = result.stdout or ""
        err = result.stderr or ""
        if result.returncode != 0:
            return f"Exit code {result.returncode}\nstdout:\n{out}\nstderr:\n{err}".strip()
        return out.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 60s"
    except Exception as e:
        return f"Error running command: {e}"


def search_web(args: str) -> str:
    """
    Search the internet for real-time information.
    Args format: "query" (e.g., "latest bitcoin price", "who won the super bowl").
    Returns Title + Snippet for top 3 results. Use for current events, prices, or unknown facts.
    """
    query = (args or "").strip()
    if not query:
        return "Search Error: query is empty."

    try:
        ddgs = DDGS()
        results = list(ddgs.text(query, max_results=3))
    except Exception as e:
        # Log to console; do not crash. Return a clear message for the agent.
        print(f"[search_web] DDGS error (rate limit/network): {e}", flush=True)
        return f"Search temporarily unavailable: {e}. Please try again or rephrase the query."

    if not results:
        return "No search results found. Please try a different query."

    lines = []
    for r in results:
        title = r.get("title", "")
        snippet = r.get("body", "")
        lines.append(f"- Title: {title}\n  Snippet: {snippet}")
    return "\n\n".join(lines)


def find_files(args: str) -> str:
    """
    Find files matching a pattern. Args format: "pattern" or "pattern|directory".
    Supports ~ for user home. Returns newline-separated list of relative paths.
    """
    try:
        parts = args.split("|", 1)
        pattern = parts[0].strip()
        directory = "."
        if len(parts) > 1:
            directory = parts[1].strip()
        pattern = os.path.expanduser(pattern)
        directory = os.path.expanduser(directory)
        # Ensure pattern is absolute if directory is not "."?
        if not os.path.isabs(directory):
            directory = os.path.abspath(directory)
        full_path = os.path.join(directory, pattern)
        matches = glob.glob(full_path, recursive=True)
        if not matches:
            return "No files found."
        return "\n".join(matches)
    except Exception as e:
        return f"Error finding files: {e}"


def list_dir(args: str) -> str:
    """
    List contents of a directory. Args format: "path" (default ".").
    Supports ~ for user home.
    """
    try:
        dir_path = args.strip() or "."
        dir_path = os.path.expanduser(dir_path)
        abs_path = os.path.abspath(dir_path)
        entries = os.listdir(abs_path)
        return "\n".join(sorted(entries))
    except Exception as e:
        return f"Error listing directory: {e}"


def git_clone(args: str) -> str:
    """
    Clone a git repository. Args format: "url" or "url|destination".
    Supports ~ for user home.
    """
    try:
        parts = args.split("|", 1)
        url = parts[0].strip()
        dest = ""
        if len(parts) > 1:
            dest = parts[1].strip()
            dest = os.path.expanduser(dest)
        cmd = ["git", "clone", url]
        if dest:
            cmd.append(dest)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return f"Git clone failed:\n{result.stderr}"
        return f"Repository cloned successfully:\n{result.stdout}"
    except Exception as e:
        return f"Error during git clone: {e}"


def git_status(args: str) -> str:
    """
    Show git status of a repository. Args format: "path" (default ".").
    Supports ~ for user home.
    """
    try:
        dir_path = args.strip() or "."
        dir_path = os.path.expanduser(dir_path)
        abs_path = os.path.abspath(dir_path)
        cmd = ["git", "-C", abs_path, "status"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return f"Git status failed:\n{result.stderr}"
        return result.stdout.strip() or "(clean)"
    except Exception as e:
        return f"Error running git status: {e}"


def execute_terminal_command(args: str) -> str:
    """
    Execute a terminal command. This is an alias for run_cmd.
    """
    return run_cmd(args)


def analyze_remote_repo(args: str) -> str:
    """
    Clone a remote git repository, read its files, and return a summary.
    Args format: "git_url" (e.g., "https://github.com/user/repo.git")
    """
    url = args.strip()
    if not url:
        return "Error: no URL provided."
    try:
        import tempfile
        import shutil
        tmp_dir = tempfile.mkdtemp(prefix="kyrozen_repo_")
        clone_cmd = ["git", "clone", url, tmp_dir]
        proc = subprocess.run(
            clone_cmd, capture_output=True, text=True, timeout=120
        )
        if proc.returncode != 0:
            shutil.rmtree(tmp_dir)
            return f"Clone failed:\n{proc.stderr}"
        summaries = []
        for root, dirs, files in os.walk(tmp_dir):
            dirs[:] = [d for d in dirs if d != ".git"]
            for fname in files:
                fpath = os.path.join(root, fname)
                rel_path = os.path.relpath(fpath, tmp_dir)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read(5000)
                except Exception:
                    content = "(binary or unreadable)"
                preview = content[:400]
                summaries.append(f"{rel_path}:\n```\n{preview}\n```")
                if len(summaries) >= 30:
                    summaries.append("...(more files omitted)")
                    break
        if not summaries:
            summaries.append("(empty repo or all files binary)")
        shutil.rmtree(tmp_dir)
        return "\n\n".join(summaries)
    except Exception as e:
        return f"Error analyzing repository: {e}"


AVAILABLE_TOOLS: dict[str, Any] = {
    "write_file": write_file,
    "read_file": read_file,
    "run_cmd": run_cmd,
    "search_web": search_web,
    "find_files": find_files,
    "list_dir": list_dir,
    "git_clone": git_clone,
    "git_status": git_status,
    "execute_terminal_command": execute_terminal_command,
    "analyze_remote_repo": analyze_remote_repo,
}
