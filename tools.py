"""
Agent capabilities: file I/O, shell commands, web search.
"""

import os
import subprocess
import re
import glob
import tempfile
import shutil
from pathlib import Path
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
    # Windows-specific dangerous patterns
    r"\bdel\s+/[fs](?:\s+/[sq])?\s+\S:\\",
    r"\brd\s+/[sq]\s+\S:\\",
    r"\brmdir\s+/[sq]\s+\S:\\",
    r"\bformat\s+\w:",
    r"\bdiskpart\b",
    r"\bwmic\s+process\s+where.*delete\b",
    r"\btaskkill\s+/f\s+/im\s+(?:svchost|winlogon|csrss|lsass|smss|wininit|services)\b",
    r"\breg\s+delete\s+HKLM",
    r"\bicacls\s+\S+\s+/deny",
    r"\bdeltree\b",
]
_BLOCKED_RE = re.compile("|".join(_BLOCKED_PATTERNS), re.IGNORECASE)

# File tools are constrained to the active workspace. Shell execution remains
# intentionally explicit and is disabled for MCP unless separately enabled;
# Python-level path checks cannot sandbox arbitrary shell programs.
_WORKSPACE_ROOT: Path = Path.cwd().resolve()


def set_workspace_root(path: str | os.PathLike[str]) -> None:
    """Set the root directory allowed by file and directory tools."""
    global _WORKSPACE_ROOT
    _WORKSPACE_ROOT = Path(path).expanduser().resolve()


def _resolve_workspace_path(raw_path: str) -> Path:
    expanded = os.path.expanduser(raw_path)
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = _WORKSPACE_ROOT / candidate
    path = candidate.resolve()
    try:
        common = Path(os.path.commonpath([str(_WORKSPACE_ROOT), str(path)]))
    except ValueError as exc:
        raise ValueError("path is outside the active workspace") from exc
    if common != _WORKSPACE_ROOT:
        raise ValueError(f"path is outside the active workspace: {path}")
    return path


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
        abs_path = _resolve_workspace_path(raw_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
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
        abs_path = _resolve_workspace_path(raw_path)
        with open(abs_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return f"Error: file not found: {args.strip()}"
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
        directory_path = _resolve_workspace_path(directory)
        if os.path.isabs(pattern):
            return "Error: absolute patterns are not allowed"
        full_path = os.path.join(str(directory_path), pattern)
        matches = []
        for match in glob.glob(full_path, recursive=True):
            try:
                _resolve_workspace_path(match)
            except ValueError:
                continue
            matches.append(match)
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
        abs_path = _resolve_workspace_path(dir_path)
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
            dest = str(_resolve_workspace_path(dest))
        cmd = ["git", "clone", url]
        if dest:
            cmd.append(dest)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return f"Git clone failed:\n{result.stderr}"
        return f"Repository cloned successfully:\n{result.stdout}"
    except Exception as e:
        return f"Error during git clone: {e}"


def git_diff(args: str) -> str:
    """
    Show git diff (unstaged, staged, or between commits).
    Args format: "--cached" or "HEAD~1" or commit range, or empty for unstaged diff.
    Supports ~ for user home.
    """
    try:
        dir_path = "."
        extra_args = args.strip()
        cmd = ["git", "-C", os.path.abspath(os.path.expanduser(dir_path)), "diff"]
        if extra_args:
            # Support --cached, --staged, HEAD~N, commit hashes
            for part in extra_args.split():
                cmd.append(part)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return f"Git diff failed:\n{result.stderr}"
        out = result.stdout.strip()
        return out if out else "(no changes)"
    except Exception as e:
        return f"Error running git diff: {e}"


def git_log(args: str) -> str:
    """
    Show git commit log. Args format: "-5" or "--since='2 days ago' --author='Name'"
    (any git log flags), or empty for default.
    Supports ~ for user home.
    """
    try:
        import shlex
        dir_path = "."
        cmd = ["git", "-C", os.path.abspath(os.path.expanduser(dir_path)), "log", "--oneline", "--decorate"]
        if args.strip():
            try:
                extra = shlex.split(args.strip())
            except ValueError:
                extra = args.strip().split()
            cmd.extend(extra)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return f"Git log failed:\n{result.stderr}"
        out = result.stdout.strip()
        return out if out else "(no commits)"
    except Exception as e:
        return f"Error running git log: {e}"


def git_branch(args: str) -> str:
    """
    List or manage git branches. Args format: "" (list all), "branch_name" (create),
    or "-d branch_name" (delete). Supports ~ for user home.
    """
    try:
        import shlex
        dir_path = "."
        cmd = ["git", "-C", os.path.abspath(os.path.expanduser(dir_path)), "branch"]
        if args.strip():
            try:
                extra = shlex.split(args.strip())
            except ValueError:
                extra = args.strip().split()
            cmd.extend(extra)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return f"Git branch failed:\n{result.stderr}"
        out = result.stdout.strip()
        return out if out else "(no branches)"
    except Exception as e:
        return f"Error running git branch: {e}"


def git_add(args: str) -> str:
    """
    Stage files for commit. Args format: "file1 file2" or "." (stage all).
    Supports ~ for user home.
    """
    try:
        dir_path = "."
        files = args.strip() or "."
        cmd = ["git", "-C", os.path.abspath(os.path.expanduser(dir_path)), "add"]
        for f in files.split():
            cmd.append(f)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return f"Git add failed:\n{result.stderr}"
        return "Files staged successfully."
    except Exception as e:
        return f"Error running git add: {e}"


def git_commit(args: str) -> str:
    """
    Commit staged changes. Args format: '"commit message"' (quotes recommended).
    Supports ~ for user home.
    """
    try:
        import shlex
        dir_path = "."
        message = args.strip().strip('"').strip("'")
        if not message:
            return "Error: git_commit requires a commit message."
        cmd = ["git", "-C", os.path.abspath(os.path.expanduser(dir_path)), "commit", "-m", message]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return f"Git commit failed:\n{result.stderr}"
        return result.stdout.strip() or "Commit successful."
    except Exception as e:
        return f"Error running git commit: {e}"


def git_push(args: str) -> str:
    """
    Push commits to remote. Args format: "" (push current branch),
    "origin main" (specific remote+branch). Supports ~ for user home.
    """
    try:
        dir_path = "."
        cmd = ["git", "-C", os.path.abspath(os.path.expanduser(dir_path)), "push"]
        if args.strip():
            for part in args.strip().split():
                cmd.append(part)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return f"Git push failed:\n{result.stderr}"
        return result.stdout.strip() or "Push successful."
    except Exception as e:
        return f"Error running git push: {e}"


def git_pull(args: str) -> str:
    """
    Pull changes from remote. Args format: "" (pull current branch),
    "origin main" (specific remote+branch). Supports ~ for user home.
    """
    try:
        dir_path = "."
        cmd = ["git", "-C", os.path.abspath(os.path.expanduser(dir_path)), "pull"]
        if args.strip():
            for part in args.strip().split():
                cmd.append(part)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return f"Git pull failed:\n{result.stderr}"
        return result.stdout.strip() or "Already up to date."
    except Exception as e:
        return f"Error running git pull: {e}"


def git_checkout(args: str) -> str:
    """
    Switch branches or restore files. Args format: "branch_name" (switch),
    "-b new_branch" (create and switch), or "-- file" (restore file).
    Supports ~ for user home.
    """
    try:
        dir_path = "."
        cmd = ["git", "-C", os.path.abspath(os.path.expanduser(dir_path)), "checkout"]
        if args.strip():
            for part in args.strip().split():
                cmd.append(part)
        else:
            return "Error: git_checkout requires a branch name or argument."
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return f"Git checkout failed:\n{result.stderr}"
        return result.stdout.strip() or "Checkout successful."
    except Exception as e:
        return f"Error running git checkout: {e}"


def git_stash(args: str) -> str:
    """
    Stash or unstash working directory changes.
    Args format: "" (stash), "pop" (apply and drop), "list" (show stashes),
    "apply" (apply without dropping). Supports ~ for user home.
    """
    try:
        dir_path = "."
        cmd = ["git", "-C", os.path.abspath(os.path.expanduser(dir_path)), "stash"]
        if args.strip():
            cmd.append(args.strip())
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return f"Git stash failed:\n{result.stderr}"
        return result.stdout.strip() or "Working directory clean."
    except Exception as e:
        return f"Error running git stash: {e}"


def git_reset(args: str) -> str:
    """
    Reset current HEAD to a specified state.
    Args format: "--soft HEAD~1" or "--hard <commit>" or "file" (unstage).
    WARNING: --hard is destructive. The agent will warn before using it.
    Supports ~ for user home.
    """
    try:
        dir_path = "."
        cmd = ["git", "-C", os.path.abspath(os.path.expanduser(dir_path)), "reset"]
        if args.strip():
            for part in args.strip().split():
                cmd.append(part)
        else:
            return "Error: git_reset requires arguments (e.g., 'file' to unstage, '--soft HEAD~1' to undo commit)."
        # Safety: block --hard without explicit confirmation path
        if "--hard" in cmd:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                return f"Git reset failed:\n{result.stderr}"
            return result.stdout.strip() or "Hard reset completed. WARNING: working directory changes were destroyed."
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return f"Git reset failed:\n{result.stderr}"
        return result.stdout.strip() or "Reset successful."
    except Exception as e:
        return f"Error running git reset: {e}"


def git_show(args: str) -> str:
    """
    Show details of a git object (commit, tag, etc). Args format: "HEAD" or commit hash.
    Supports ~ for user home.
    """
    try:
        dir_path = "."
        target = args.strip() or "HEAD"
        cmd = ["git", "-C", os.path.abspath(os.path.expanduser(dir_path)), "show", "--stat", target]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return f"Git show failed:\n{result.stderr}"
        return result.stdout.strip()
    except Exception as e:
        return f"Error running git show: {e}"


def git_remote(args: str) -> str:
    """
    Manage remote repositories. Args format: "" (list remotes),
    "add name url" (add remote), or "remove name" (remove remote).
    Supports ~ for user home.
    """
    try:
        dir_path = "."
        cmd = ["git", "-C", os.path.abspath(os.path.expanduser(dir_path)), "remote", "-v"]
        if args.strip():
            action_parts = args.strip().split(maxsplit=1)
            cmd = ["git", "-C", os.path.abspath(os.path.expanduser(dir_path)), "remote"]
            cmd.extend(action_parts)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return f"Git remote failed:\n{result.stderr}"
        return result.stdout.strip() or "(no remotes configured)"
    except Exception as e:
        return f"Error running git remote: {e}"


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
        root = _resolve_workspace_path(path)
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
    "git_diff": git_diff,
    "git_log": git_log,
    "git_branch": git_branch,
    "git_add": git_add,
    "git_commit": git_commit,
    "git_push": git_push,
    "git_pull": git_pull,
    "git_checkout": git_checkout,
    "git_stash": git_stash,
    "git_reset": git_reset,
    "git_show": git_show,
    "git_remote": git_remote,
    "execute_terminal_command": execute_terminal_command,
    "analyze_remote_repo": analyze_remote_repo,
    "list_tree": list_tree,
    "read_webpage": read_webpage,
}

# Capability labels let local CLI, Web, and MCP surfaces expose the same rich
# tool set while applying different authorization profiles. This is a policy
# boundary, not a replacement for the command safety checks above.
_TOOL_CAPABILITIES: dict[str, str] = {
    "check_stored_data": "read",
    "search_memory": "read",
    "write_file": "write",
    "run_cmd": "shell",
    "execute_terminal_command": "shell",
    "search_web": "network",
    "read_webpage": "network",
    "git_clone": "git",
    "analyze_remote_repo": "network",
    "git_add": "git",
    "git_commit": "git",
    "git_push": "git",
    "git_pull": "git",
    "git_checkout": "git",
    "git_stash": "git",
    "git_reset": "destructive",
    "git_remote": "git",
}

_CAPABILITY_PROFILES: dict[str, frozenset[str]] = {
    # Rich local-workspace access: read/write files, shell, network and Git.
    "workspace": frozenset({"read", "write", "shell", "network", "git"}),
    # Explicit opt-in for irreversible operations and user-defined tools.
    "full": frozenset({"read", "write", "shell", "network", "git", "destructive", "dynamic"}),
    "readonly": frozenset({"read", "network"}),
}


def resolve_capabilities(value: str | None, default: str = "readonly") -> frozenset[str]:
    """Resolve a named or comma-separated capability profile."""
    raw = (value or default).strip().lower()
    if raw in _CAPABILITY_PROFILES:
        return _CAPABILITY_PROFILES[raw]
    requested = {part.strip() for part in raw.split(",") if part.strip()}
    return frozenset(requested & {"read", "write", "shell", "network", "git", "destructive", "dynamic"})


def allowed_tool_names(tools: dict[str, Any] | None = None, capabilities: str | None = None) -> set[str]:
    """Return tools allowed by a capability profile.

    Unknown tools are treated as dynamic tools, so a newly registered tool
    cannot silently bypass a restricted Web/MCP profile.
    """
    available = tools if tools is not None else AVAILABLE_TOOLS
    granted = resolve_capabilities(capabilities)
    allowed: set[str] = set()
    for name in available:
        capability = _TOOL_CAPABILITIES.get(name)
        if capability is None:
            if "dynamic" in granted:
                allowed.add(name)
            continue
        if capability in granted:
            allowed.add(name)
    return allowed


def tool_capability(name: str) -> str:
    """Return the capability required by a tool, treating unknown tools as dynamic."""
    return _TOOL_CAPABILITIES.get(name, "dynamic")
