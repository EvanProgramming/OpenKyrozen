from __future__ import annotations

import os
import glob

from openkyrozen.tools.models import CommandResult
from openkyrozen.security.command_policy import _BLOCKED_RE

def find_files(self, args: str) -> str:
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
        directory_path = self._resolve_workspace_path(directory)
        if os.path.isabs(pattern):
            return "Error: absolute patterns are not allowed"
        full_path = os.path.join(str(directory_path), pattern)
        matches = []
        for match in glob.glob(full_path, recursive=True):
            try:
                self._resolve_workspace_path(match)
            except ValueError:
                continue
            matches.append(match)
        if not matches:
            return "No files found."
        return "\n".join(matches)
    except Exception as e:
        return f"Error finding files: {e}"


def list_dir(self, args: str) -> str:
    """
    List contents of a directory. Args format: "path" (default ".").
    Supports ~ for user home.
    """
    try:
        dir_path = args.strip() or "."
        abs_path = self._resolve_workspace_path(dir_path)
        entries = os.listdir(abs_path)
        return "\n".join(sorted(entries))
    except Exception as e:
        return f"Error listing directory: {e}"


def list_tree(self, args: str) -> str:
    """
    Recursively list the directory tree of the given path. Args format: "path" (default ".").
    Returns a tree‑like textual representation of the directory structure.
    """
    import os
    import pathlib
    try:
        path = args.strip() or "."
        root = self._resolve_workspace_path(path)
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


def write_file(self, args: str) -> str:
    """
    Write content to a file. Args format: "path|content".
    The path must be relative to the active workspace (e.g. "notes.txt").
    """
    try:
        parts = args.split("|", 1)
        if len(parts) < 2:
            return "Error: write_file requires args in format path|content"
        raw_path, content = parts[0].strip(), parts[1]
        abs_path = self._resolve_workspace_path(raw_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Wrote {len(content)} characters to {abs_path}"
    except Exception as e:
        return f"Error writing file: {e}"


def read_file(self, args: str) -> str:
    """
    Read content from a file. Args format: "path".
    The path must be relative to the active workspace (e.g. "notes.txt").
    """
    try:
        raw_path = args.strip()
        if not raw_path:
            return "Error: read_file requires a path"
        abs_path = self._resolve_workspace_path(raw_path)
        with open(abs_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return f"Error: file not found: {args.strip()}"
    except Exception as e:
        return f"Error reading file: {e}"
