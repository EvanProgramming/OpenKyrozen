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

import requests
import warnings
warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", message="This package.*has been renamed")


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
    Returns Title + Snippet for top 5 results. Use for current events, prices, or unknown facts.
    """
    import re
    import html
    import json
    from urllib.parse import quote
    import requests

    query = (args or "").strip()
    if not query:
        return "Search Error: query is empty."

    def _fmt(results):
        lines = []
        for r in results:
            title = r.get("title", "")
            snippet = r.get("body", "")
            url = r.get("url", "")
            line = f"- Title: {title}"
            if snippet:
                line += f"\n  Snippet: {snippet}"
            if url:
                line += f"\n  URL: {url}"
            lines.append(line)
        return "\n".join(lines)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/133.0.6943.126 Safari/537.36"
        ),
    }

    # ---- attempt 1: Google via googlesearch-python (no API key) ----
    try:
        from googlesearch import search as google_search
        urls = list(google_search(query, num_results=5, lang="en"))
    except Exception:
        pass
    else:
        if urls:
            results = []
            for url in urls:
                try:
                    resp = requests.get(url, headers=headers, timeout=5)
                    resp.raise_for_status()
                except requests.RequestException:
                    results.append({"title": url, "body": "", "url": url})
                    continue
                title = ""
                m = re.search(r'<title[^>]*>(.*?)</title>', resp.text, re.DOTALL|re.IGNORECASE)
                if m:
                    title = html.unescape(re.sub(r"<[^>]*>", "", m.group(1))).strip()
                snippet = ""
                m2 = re.search(
                    r'<meta\s+name\s*=\s*["\']description["\'][^>]*content\s*=\s*["\']([^"\']*)["\']',
                    resp.text, re.IGNORECASE
                )
                if m2:
                    snippet = html.unescape(m2.group(1)).strip()
                results.append({"title": title, "body": snippet, "url": url})
            if results:
                return _fmt(results)

    # ---- attempt 2: DuckDuckGo Lite HTML (GET) ----
    lite_url = f"https://lite.duckduckgo.com/lite/?q={quote(query)}"
    try:
        resp = requests.get(lite_url, headers=headers, timeout=10)
        resp.raise_for_status()
        titles = re.findall(r'<a[^>]*rel="nofollow"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
        snippets = re.findall(r'<td class="result-snippet"[^>]*>(.*?)</td>', resp.text, re.DOTALL)
        if titles:
            results = []
            for i, title_html in enumerate(titles[:5]):
                title = re.sub(r"<[^>]*>", "", title_html).strip()
                title = html.unescape(title)
                snippet = ""
                if i < len(snippets):
                    snip_html = snippets[i]
                    snippet = re.sub(r"<[^>]*>", "", snip_html).strip()
                    snippet = html.unescape(snippet)
                results.append({"title": title, "body": snippet, "url": ""})
            if results:
                return _fmt(results)
    except Exception:
        pass

    # ---- attempt 3: DuckDuckGo HTML (POST) ----
    html_url = "https://html.duckduckgo.com/html/"
    try:
        resp = requests.post(html_url, data={"q": query}, headers=headers, timeout=10)
        resp.raise_for_status()
        titles = re.findall(r'<a[^>]*class="result__a"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
        snippets = re.findall(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
        if titles:
            results = []
            for i, title_html in enumerate(titles[:5]):
                title = re.sub(r"<[^>]*>", "", title_html).strip()
                title = html.unescape(title)
                snippet = ""
                if i < len(snippets):
                    snip_html = snippets[i]
                    snippet = re.sub(r"<[^>]*>", "", snip_html).strip()
                    snippet = html.unescape(snippet)
                results.append({"title": title, "body": snippet, "url": ""})
            if results:
                return _fmt(results)
    except Exception:
        pass

    # ---- attempt 4: DuckDuckGo Instant Answer API (JSON) ----
    try:
        api_url = f"https://api.duckduckgo.com/?q={quote(query)}&format=json&no_html=1&skip_disambig=1"
        resp = requests.get(api_url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = json.loads(resp.text)
        related = data.get("RelatedTopics", [])
        if related:
            results = []
            for topic in related[:5]:
                title = topic.get("Text", "") or ""
                url = topic.get("FirstURL", "")
                # Clean up title (remove description suffix)
                if " - " in title:
                    title = title.split(" - ")[0]
                results.append({"title": title[:200], "body": "", "url": url})
            if results:
                return _fmt(results)
    except Exception:
        pass

    # ---- attempt 5: direct Wikipedia search for fact-based queries ----
    try:
        wiki_url = f"https://en.wikipedia.org/w/api.php?action=opensearch&search={quote(query)}&limit=5&format=json"
        resp = requests.get(wiki_url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = json.loads(resp.text)
        if len(data) >= 4 and data[1]:
            results = []
            for i, title in enumerate(data[1][:5]):
                desc = data[2][i] if i < len(data[2]) else ""
                url = data[3][i] if i < len(data[3]) else ""
                results.append({"title": title, "body": desc[:300], "url": url})
            if results:
                return _fmt(results)
    except Exception:
        pass

    # All backends failed — return a useful error message
    return (
        "Search temporarily unavailable: all backends (Google, DuckDuckGo, Wikipedia) "
        f"failed for query '{query[:80]}'. This may be due to rate-limiting or "
        "network issues. Suggestions: try a shorter query, wait a few seconds and "
        "retry, or use read_webpage on a known URL instead."
    )


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


def read_webpage(args: str) -> str:
    """
    Fetch the content of a web page and return its plain‑text body.
    Args format: "URL" (e.g., "https://example.com/page")
    """
    import html
    import re
    import requests
    url = args.strip()
    if not url.startswith("http"):
        return "Error: read_webpage requires a valid URL."
    try:
        headers = {"User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/133.0.6943.126 Safari/537.36"
        )}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        text = re.sub(r"<[^>]*>", "", resp.text)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 5000:
            text = text[:5000] + "\n[...truncated...]"
        return text
    except requests.RequestException as e:
        return f"Error fetching webpage: {e}"


def list_tree(args: str) -> str:
    """
    Recursively list the directory tree of the given path. Args format: "path" (default ".").
    Returns a tree‑like textual representation of the directory structure.
    """
    import os
    import pathlib
    try:
        path = args.strip() or "."
        path = os.path.expanduser(path)
        root = pathlib.Path(path).resolve()
        if not root.is_dir():
            return f"Error: '{path}' is not a directory"
        lines = [f"{root.name}/"]
        for dirpath, dirnames, filenames in os.walk(root):
            # skip hidden dirs
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            depth = len(pathlib.Path(dirpath).relative_to(root).parts)
            prefix = "│   " * (depth - 1) + "├── " if depth > 0 else ""
            for d in sorted(dirnames):
                lines.append(f"{prefix}{d}/")
            for f in sorted(filenames):
                lines.append(f"{prefix}{f}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing tree: {e}"


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
    "list_tree": list_tree,
    "read_webpage": read_webpage,
}
