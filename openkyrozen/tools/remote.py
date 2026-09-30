from __future__ import annotations

import os
import subprocess
import tempfile
import shutil

from openkyrozen.tools.models import CommandResult
from openkyrozen.security.command_policy import _BLOCKED_RE

def analyze_remote_repo(self, args: str) -> str:
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
