from __future__ import annotations

import os
import glob
import fnmatch
import hashlib
import json
import codecs
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

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
        if args.lstrip().startswith("{"):
            request = json.loads(args)
            if not isinstance(request, dict) or not isinstance(request.get("path"), str):
                return "Error: structured read requires a path"
            abs_path = self._resolve_workspace_path(request["path"])
            start = request.get("start_line", 1)
            limit = request.get("max_lines", 200)
            max_chars = request.get("max_chars", 20_000)
            if (any(not isinstance(item, int) or isinstance(item, bool) for item in (start, limit, max_chars))
                    or start < 1 or limit < 1 or max_chars < 1):
                return "Error: start_line, max_lines, and max_chars must be positive integers"
            limit, max_chars = min(limit, 200), min(max_chars, 20_000)
            digest = hashlib.sha256()
            decoder = codecs.getincrementaldecoder("utf-8")()
            excerpt_parts, excerpt_size = [], 0
            line_number, newline_count, has_content, ends_newline = 1, 0, False, False
            pending = ""

            def keep_piece(piece: str) -> None:
                nonlocal excerpt_size
                if start <= line_number < start + limit and excerpt_size <= max_chars:
                    excerpt_parts.append(piece[:max_chars + 1 - excerpt_size])
                    excerpt_size += len(piece)

            with abs_path.open("rb") as source:
                while chunk := source.read(64 * 1024):
                    has_content = True
                    digest.update(chunk)
                    decoded = decoder.decode(chunk)
                    parts = decoded.split("\n")
                    for piece in parts[:-1]:
                        keep_piece(pending + piece + "\n")
                        pending = ""
                        line_number += 1
                        newline_count += 1
                    pending = parts[-1] if start <= line_number < start + limit else ""
                    ends_newline = decoded.endswith("\n") if decoded else ends_newline
                final = decoder.decode(b"", final=True)
                if final:
                    has_content = True
                    pending += final
                if pending or (has_content and not ends_newline):
                    keep_piece(pending)
            total = newline_count + (1 if has_content and not ends_newline else 0)
            excerpt = "".join(excerpt_parts)[:max_chars]
            clipped = excerpt_size > max_chars
            relative = abs_path.relative_to(self._WORKSPACE_ROOT).as_posix()
            return json.dumps({
                "path": relative, "sha256": digest.hexdigest(),
                "start_line": start, "end_line": min(total, start + limit - 1),
                "total_lines": total, "truncated": clipped or start + limit - 1 < total,
                "content": excerpt,
            })
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


def edit_file(self, args: str) -> str:
    """Replace one exact snippet only when the file still matches its read hash."""
    temporary = None
    try:
        request = json.loads(args)
        if not isinstance(request, dict) or not all(isinstance(request.get(key), str)
                for key in ("path", "old_text", "new_text", "expected_sha256")):
            return "Error: edit_file requires path, old_text, new_text, and expected_sha256 strings"
        old, new = request["old_text"], request["new_text"]
        if not old:
            return "Error: old_text must not be empty"
        path = self._resolve_workspace_path(request["path"])
        original = path.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        if digest != request["expected_sha256"]:
            return "Error: file is stale; read it again before editing"
        content = original.decode("utf-8")
        if content.count(old) != 1:
            return "Error: old_text must match exactly once"
        updated = content.replace(old, new, 1).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(descriptor, "wb") as output:
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        if hashlib.sha256(path.read_bytes()).hexdigest() != request["expected_sha256"]:
            return "Error: file changed during edit; read it again before editing"
        os.replace(temporary, path)
        temporary = None
        return f"Updated {path.relative_to(self._WORKSPACE_ROOT).as_posix()}"
    except Exception as exc:
        return f"Error editing file: {exc}"
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def search_files(self, args: str) -> str:
    """Search literal text with bounded, relative file-and-line results."""
    try:
        request = json.loads(args)
        if not isinstance(request, dict) or not isinstance(request.get("query"), str) or not request["query"]:
            return "Error: search_files requires a non-empty literal query"
        scope = self._resolve_workspace_path(request.get("path", "."))
        pattern = request.get("glob", "*")
        if not isinstance(pattern, str) or Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            return "Error: glob must be a workspace-relative pattern"
        limit = request.get("limit", 100)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            return "Error: limit must be a positive integer"
        limit = min(limit, 100)
        query = request["query"]
        rg = shutil.which("rg")
        if rg:
            target = scope.relative_to(self._WORKSPACE_ROOT).as_posix() or "."
            command = [rg, "--fixed-strings", "--line-number", "--no-heading", "--color", "never",
                       "--max-columns", "400", "--max-columns-preview", "--glob", pattern,
                       "-m", str(limit), "-e", query, "--", target]
            process = subprocess.Popen(command, cwd=self._WORKSPACE_ROOT, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL)
            output: list[str] = []
            size = 0
            deadline = time.monotonic() + 5
            for raw in process.stdout:
                if time.monotonic() >= deadline or len(output) >= limit:
                    process.terminate()
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if len(line) > 400:
                    line = line[:400] + " …"
                remaining = 20_000 - size
                if remaining <= 0:
                    process.terminate()
                    break
                line = line[:remaining]
                output.append(line)
                size += len(line) + 1
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()
            return "\n".join(output) if output else "No matches."

        output, size = [], 0
        files = [scope] if scope.is_file() else scope.rglob("*")
        for path in files:
            try:
                path = self._resolve_workspace_path(str(path))
            except ValueError:
                continue
            if not path.is_file() or any(part in {".git", "venv", ".venv", "node_modules", "__pycache__"}
                                         for part in path.relative_to(scope if scope.is_dir() else scope.parent).parts):
                continue
            relative = path.relative_to(self._WORKSPACE_ROOT).as_posix()
            if not fnmatch.fnmatch(path.name, pattern) and not fnmatch.fnmatch(relative, pattern):
                continue
            try:
                with path.open(encoding="utf-8", errors="replace") as source:
                    for number, line in enumerate(source, 1):
                        if query not in line:
                            continue
                        remaining = 20_000 - size
                        if remaining <= 0 or len(output) >= limit:
                            return "\n".join(output)
                        result = f"{relative}:{number}:{line.rstrip()}"[:remaining]
                        output.append(result)
                        size += len(result) + 1
            except OSError:
                continue
        return "\n".join(output) if output else "No matches."
    except Exception as exc:
        return f"Error searching files: {exc}"
