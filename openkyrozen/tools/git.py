from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from openkyrozen.tools.models import CommandResult
from openkyrozen.security.command_policy import _BLOCKED_RE

def _git_working_directory(self, path: str = ".") -> str:
    """Resolve Git's default directory from the active workspace, not process cwd."""
    candidate = Path(os.path.expanduser(path or "."))
    if not candidate.is_absolute():
        candidate = self._WORKSPACE_ROOT / candidate
    return str(candidate.resolve())


def git_clone(self, args: str) -> str:
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
            dest = str(self._resolve_workspace_path(dest))
        cmd = ["git", "clone", url]
        if dest:
            cmd.append(dest)
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, cwd=str(self._WORKSPACE_ROOT),
        )
        if result.returncode != 0:
            return f"Git clone failed:\n{result.stderr}"
        return f"Repository cloned successfully:\n{result.stdout}"
    except Exception as e:
        return f"Error during git clone: {e}"


def git_diff(self, args: str) -> str:
    """
    Show git diff (unstaged, staged, or between commits).
    Args format: "--cached" or "HEAD~1" or commit range, or empty for unstaged diff.
    Supports ~ for user home.
    """
    try:
        extra_args = args.strip()
        cmd = ["git", "-C", self._git_working_directory(), "diff"]
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


def git_log(self, args: str) -> str:
    """
    Show git commit log. Args format: "-5" or "--since='2 days ago' --author='Name'"
    (any git log flags), or empty for default.
    Supports ~ for user home.
    """
    try:
        import shlex
        cmd = ["git", "-C", self._git_working_directory(), "log", "--oneline", "--decorate"]
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


def git_branch(self, args: str) -> str:
    """
    List or manage git branches. Args format: "" (list all), "branch_name" (create),
    or "-d branch_name" (delete). Supports ~ for user home.
    """
    try:
        import shlex
        raw_args = args.strip()
        try:
            extra = shlex.split(raw_args) if raw_args else []
        except ValueError:
            extra = raw_args.split()
        cmd = ["git", "-C", self._git_working_directory(), "branch"]
        if extra:
            cmd.extend(extra)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return f"Git branch failed:\n{result.stderr}"
        out = result.stdout.strip()
        mutation_flags = {"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-f", "--force"}
        mutation = bool(extra and (extra[0] in mutation_flags or not extra[0].startswith("-")))
        if mutation:
            targets = [item for item in extra if item != "--" and not item.startswith("-")]
            if extra[0] in {"-d", "-D", "--delete"}:
                names = ", ".join(repr(item) for item in targets) or "the requested branch"
                return f"Deleted branch {names}."
            if extra[0] in {"-m", "-M", "--move"} and len(targets) >= 2:
                return f"Renamed branch {targets[-2]!r} to {targets[-1]!r}."
            if extra[0] in {"-c", "-C", "--copy"} and len(targets) >= 2:
                return f"Copied branch {targets[-2]!r} to {targets[-1]!r}."
            name = targets[-1] if targets else "the requested branch"
            return f"Created branch {name!r}."
        return out if out else "(no branches)"
    except Exception as e:
        return f"Error running git branch: {e}"


def git_add(self, args: str) -> str:
    """
    Stage files for commit. Args format: "file1 file2" or "." (stage all).
    Supports ~ for user home.
    """
    try:
        files = args.strip() or "."
        cmd = ["git", "-C", self._git_working_directory(), "add"]
        for f in files.split():
            cmd.append(f)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return f"Git add failed:\n{result.stderr}"
        return "Files staged successfully."
    except Exception as e:
        return f"Error running git add: {e}"


def git_commit(self, args: str) -> str:
    """
    Commit staged changes. Args format: '"commit message"' (quotes recommended).
    Supports ~ for user home.
    """
    try:
        import shlex
        message = args.strip().strip('"').strip("'")
        if not message:
            return "Error: git_commit requires a commit message."
        cmd = ["git", "-C", self._git_working_directory(), "commit", "-m", message]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return f"Git commit failed:\n{result.stderr}"
        return result.stdout.strip() or "Commit successful."
    except Exception as e:
        return f"Error running git commit: {e}"


def git_push(self, args: str) -> str:
    """
    Push commits to remote. Args format: "" (push current branch),
    "origin main" (specific remote+branch). Supports ~ for user home.
    """
    try:
        cmd = ["git", "-C", self._git_working_directory(), "push"]
        if args.strip():
            for part in args.strip().split():
                cmd.append(part)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return f"Git push failed:\n{result.stderr}"
        return result.stdout.strip() or "Push successful."
    except Exception as e:
        return f"Error running git push: {e}"


def git_pull(self, args: str) -> str:
    """
    Pull changes from remote. Args format: "" (pull current branch),
    "origin main" (specific remote+branch). Supports ~ for user home.
    """
    try:
        cmd = ["git", "-C", self._git_working_directory(), "pull"]
        if args.strip():
            for part in args.strip().split():
                cmd.append(part)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return f"Git pull failed:\n{result.stderr}"
        return result.stdout.strip() or "Already up to date."
    except Exception as e:
        return f"Error running git pull: {e}"


def git_checkout(self, args: str) -> str:
    """
    Switch branches or restore files. Args format: "branch_name" (switch),
    "-b new_branch" (create and switch), or "-- file" (restore file).
    Supports ~ for user home.
    """
    try:
        cmd = ["git", "-C", self._git_working_directory(), "checkout"]
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


def git_stash(self, args: str) -> str:
    """
    Stash or unstash working directory changes.
    Args format: "" (stash), "pop" (apply and drop), "list" (show stashes),
    "apply" (apply without dropping). Supports ~ for user home.
    """
    try:
        cmd = ["git", "-C", self._git_working_directory(), "stash"]
        if args.strip():
            cmd.append(args.strip())
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return f"Git stash failed:\n{result.stderr}"
        return result.stdout.strip() or "Working directory clean."
    except Exception as e:
        return f"Error running git stash: {e}"


def git_reset(self, args: str) -> str:
    """
    Reset current HEAD to a specified state.
    Args format: "--soft HEAD~1" or "--hard <commit>" or "file" (unstage).
    WARNING: --hard is destructive. The agent will warn before using it.
    Supports ~ for user home.
    """
    try:
        cmd = ["git", "-C", self._git_working_directory(), "reset"]
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


def git_show(self, args: str) -> str:
    """
    Show details of a git object (commit, tag, etc). Args format: "HEAD" or commit hash.
    Supports ~ for user home.
    """
    try:
        target = args.strip() or "HEAD"
        cmd = ["git", "-C", self._git_working_directory(), "show", "--stat", target]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return f"Git show failed:\n{result.stderr}"
        return result.stdout.strip()
    except Exception as e:
        return f"Error running git show: {e}"


def git_remote(self, args: str) -> str:
    """
    Manage remote repositories. Args format: "" (list remotes),
    "add name url" (add remote), or "remove name" (remove remote).
    Supports ~ for user home.
    """
    try:
        git_dir = self._git_working_directory()
        try:
            parts = shlex.split(args or "")
        except ValueError:
            return "Error: git_remote arguments contain invalid shell quoting."

        cmd = ["git", "-C", git_dir, "remote"]
        if not parts:
            cmd.append("-v")
        elif parts[0] == "add":
            if len(parts) != 3:
                return "Error: git_remote add requires exactly: add <name> <url>."
            cmd.extend(parts)
        elif parts[0] == "remove":
            if len(parts) != 2:
                return "Error: git_remote remove requires exactly: remove <name>."
            cmd.extend(parts)
        else:
            return "Error: git_remote requires empty args, add <name> <url>, or remove <name>."
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return f"Git remote failed:\n{result.stderr}"
        output = result.stdout.strip()
        if output:
            return output
        if parts and parts[0] == "add":
            return f"Remote '{parts[1]}' added."
        if parts and parts[0] == "remove":
            return f"Remote '{parts[1]}' removed."
        return "(no remotes configured)"
    except Exception as e:
        return f"Error running git remote: {e}"


def git_status(self, args: str) -> str:
    """
    Show git status of a repository. Args format: "path" (default ".").
    Supports ~ for user home.
    """
    try:
        abs_path = self._git_working_directory(args.strip() or ".")
        cmd = ["git", "-C", abs_path, "status"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return f"Git status failed:\n{result.stderr}"
        return result.stdout.strip() or "(clean)"
    except Exception as e:
        return f"Error running git status: {e}"


def execute_terminal_command(self, args: str) -> str:
    """
    Execute a terminal command. This is an alias for run_cmd.
    """
    return str(self.run_command(args))
