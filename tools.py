"""
Agent capabilities: file I/O, shell commands, web search.
"""

import os
import subprocess
import re
from typing import Any

from googlesearch import search


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
        print(f"[write_file] absolute path: {abs_path}")
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
    Returns a summary of the top 3 search results (title, description, URL).
    Use whenever the user asks for current events, prices, or facts you don't know.
    """
    try:
        query = (args or "").strip()
        if not query:
            return "Search Error: query is empty."

        # Try advanced search first (returns objects with .title, .description, .url)
        try:
            results = list(search(query, num_results=3, advanced=True))
            if not results:
                raise ValueError("No results")
            lines = []
            for result in results:
                title = getattr(result, "title", "") or ""
                description = getattr(result, "description", "") or ""
                url = getattr(result, "url", "") or ""
                lines.append(f"- Title: {title}\n  Description: {description}\n  URL: {url}")
            return "\n\n".join(lines)
        except (AttributeError, TypeError, ValueError):
            # Fallback: standard search returns URL strings only
            results = list(search(query, num_results=3))
            if not results:
                return "No results found."
            lines = [f"- URL: {u}" for u in results]
            return "\n\n".join(lines)

    except Exception as e:
        return f"Search Error: {str(e)}"


AVAILABLE_TOOLS: dict[str, Any] = {
    "write_file": write_file,
    "read_file": read_file,
    "run_cmd": run_cmd,
    "search_web": search_web,
}
