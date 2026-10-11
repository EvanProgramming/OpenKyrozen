from __future__ import annotations
from openkyrozen.tools.models import ToolError, ToolResult, command_tool_result, legacy_text_api, tool_failure, tool_json_result, tool_success

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
import threading
import time
from pathlib import Path

from openkyrozen.security.command_policy import _BLOCKED_RE

_SEARCH_EXCLUDED_DIRECTORIES = (".git", "venv", ".venv", "node_modules", "__pycache__")

@legacy_text_api
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
            return tool_failure('invalid_arguments', "Error: absolute patterns are not allowed", retriable=False)
        full_path = os.path.join(str(directory_path), pattern)
        matches = []
        for match in glob.glob(full_path, recursive=True):
            try:
                self._resolve_workspace_path(match)
            except ValueError:
                continue
            matches.append(match)
        if not matches:
            return tool_success(str("No files found."))
        return tool_success(str("\n".join(matches)))
    except Exception as e:
        return tool_failure('execution_error', f"Error finding files: {e}", retriable=False)


@legacy_text_api
def list_dir(self, args: str) -> str:
    """
    List contents of a directory. Args format: "path" (default ".").
    Supports ~ for user home.
    """
    try:
        dir_path = args.strip() or "."
        abs_path = self._resolve_workspace_path(dir_path)
        entries = os.listdir(abs_path)
        return tool_success(str("\n".join(sorted(entries))))
    except FileNotFoundError as e:
        return tool_failure('not_found', f"Error listing directory: {e}", retriable=False)
    except Exception as e:
        return tool_failure('execution_error', f"Error listing directory: {e}", retriable=False)


@legacy_text_api
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
            return tool_failure('not_found', f"Error: '{path}' is not a directory", retriable=False)
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
        return tool_success(str("\n".join(lines)))
    except Exception as e:
        return tool_failure('execution_error', f"Error listing tree: {e}", retriable=False)


@legacy_text_api
def write_file(self, args: str) -> str:
    """
    Write content to a file. Args format: "path|content".
    The path must be relative to the active workspace (e.g. "notes.txt").
    """
    try:
        parts = args.split("|", 1)
        if len(parts) < 2:
            return tool_failure('invalid_arguments', "Error: write_file requires args in format path|content", retriable=False)
        raw_path, content = parts[0].strip(), parts[1]
        abs_path = self._resolve_workspace_path(raw_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        return tool_success(str(f"Wrote {len(content)} characters to {abs_path}"))
    except Exception as e:
        return tool_failure('execution_error', f"Error writing file: {e}", retriable=False)


@legacy_text_api
def read_file(self, args: str) -> str:
    """
    Read content from a file. Args format: "path".
    The path must be relative to the active workspace (e.g. "notes.txt").
    """
    try:
        if args.lstrip().startswith("{"):
            request = json.loads(args)
            if not isinstance(request, dict) or not isinstance(request.get("path"), str):
                return tool_failure('invalid_arguments', "Error: structured read requires a path", retriable=False)
            abs_path = self._resolve_workspace_path(request["path"])
            start = request.get("start_line", 1)
            limit = request.get("max_lines", 200)
            max_chars = request.get("max_chars", 20_000)
            if (any(not isinstance(item, int) or isinstance(item, bool) for item in (start, limit, max_chars))
                    or start < 1 or limit < 1 or max_chars < 1):
                return tool_failure('invalid_arguments', "Error: start_line, max_lines, and max_chars must be positive integers", retriable=False)
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
                    if start <= line_number < start + limit:
                        pending = (pending + parts[-1])[:max(0, max_chars + 1 - excerpt_size)]
                    else:
                        pending = ""
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
            data = {
                "path": relative, "sha256": digest.hexdigest(),
                "start_line": start, "end_line": min(total, start + limit - 1),
                "total_lines": total, "truncated": clipped or start + limit - 1 < total,
                "content": excerpt,
            }
            return tool_json_result(data, text=json.dumps(data, ensure_ascii=False))
        raw_path = args.strip()
        if not raw_path:
            return tool_failure('invalid_arguments', "Error: read_file requires a path", retriable=False)
        abs_path = self._resolve_workspace_path(raw_path)
        with open(abs_path, "r", encoding="utf-8") as f:
            return tool_success(str(f.read()))
    except FileNotFoundError:
        return tool_failure('not_found', f"Error: file not found: {args.strip()}", retriable=False)
    except Exception as e:
        return tool_failure('execution_error', f"Error reading file: {e}", retriable=False)


@legacy_text_api
def edit_file(self, args: str) -> str:
    """Replace one exact snippet only when the file still matches its read hash."""
    temporary = None
    try:
        request = json.loads(args)
        if not isinstance(request, dict) or not all(isinstance(request.get(key), str)
                for key in ("path", "old_text", "new_text", "expected_sha256")):
            return tool_failure('invalid_arguments', "Error: edit_file requires path, old_text, new_text, and expected_sha256 strings", retriable=False)
        old, new = request["old_text"], request["new_text"]
        if not old:
            return tool_failure('invalid_arguments', "Error: old_text must not be empty", retriable=False)
        path = self._resolve_workspace_path(request["path"])
        original = path.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        if digest != request["expected_sha256"]:
            return tool_failure('stale_file', "Error: file is stale; read it again before editing", retriable=False)
        content = original.decode("utf-8")
        if content.count(old) != 1:
            return tool_failure('stale_file', "Error: old_text must match exactly once", retriable=False)
        updated = content.replace(old, new, 1).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(descriptor, "wb") as output:
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        if hashlib.sha256(path.read_bytes()).hexdigest() != request["expected_sha256"]:
            return tool_failure('stale_file', "Error: file changed during edit; read it again before editing", retriable=False)
        os.replace(temporary, path)
        temporary = None
        return tool_success(str(f"Updated {path.relative_to(self._WORKSPACE_ROOT).as_posix()}"))
    except Exception as exc:
        return tool_failure('execution_error', f"Error editing file: {exc}", retriable=False)
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


@legacy_text_api
def search_files(self, args: str) -> str:
    """Search literal text with bounded, relative file-and-line results."""
    try:
        request = json.loads(args)
        if not isinstance(request, dict) or not isinstance(request.get("query"), str) or not request["query"]:
            return tool_failure('invalid_arguments', "Error: search_files requires a non-empty literal query", retriable=False)
        scope = self._resolve_workspace_path(request.get("path", "."))
        pattern = request.get("glob", "*")
        if not isinstance(pattern, str) or Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            return tool_failure('invalid_arguments', "Error: glob must be a workspace-relative pattern", retriable=False)
        limit = request.get("limit", 100)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            return tool_failure('invalid_arguments', "Error: limit must be a positive integer", retriable=False)
        limit = min(limit, 100)
        query = request["query"]
        rg = shutil.which("rg")
        if rg:
            target = scope.relative_to(self._WORKSPACE_ROOT).as_posix() or "."
            command = [rg, "--fixed-strings", "--line-number", "--no-heading", "--with-filename", "--color", "never",
                       "--max-columns", "400", "--max-columns-preview", "--hidden", "--glob", pattern,
            ]
            if scope.is_dir():
                prefix = "" if target == "." else f"{target.rstrip('/')}/"
                for directory in _SEARCH_EXCLUDED_DIRECTORIES:
                    command.extend(("--glob", f"!{prefix}**/{directory}",
                                    "--glob", f"!{prefix}**/{directory}/**"))
            command.extend(("-m", str(limit), "-e", query, "--", target))
            process = subprocess.Popen(command, cwd=self._WORKSPACE_ROOT, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL)
            output: list[str] = []
            size = 0
            timed_out = threading.Event()

            def kill_search() -> None:
                if process.poll() is not None:
                    return
                try:
                    process.kill()
                except OSError:
                    return
                timed_out.set()

            timer = threading.Timer(5, kill_search)
            timer.daemon = True
            timer.start()
            try:
                for raw in process.stdout:
                    if len(output) >= limit:
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
            finally:
                timer.cancel()
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
            process.stdout.close()
            if output:
                return tool_success(str("\n".join(output)))
            return (tool_failure('timeout', "Search timed out.", retriable=False) if timed_out.is_set() else tool_success(str("No matches.")))

        output, size = [], 0
        deadline = time.monotonic() + 5
        files = [scope] if scope.is_file() else scope.rglob("*")
        for path in files:
            if time.monotonic() >= deadline:
                return (tool_success(str("\n".join(output))) if output else tool_failure('timeout', "Search timed out.", retriable=False))
            try:
                path = self._resolve_workspace_path(str(path))
            except ValueError:
                continue
            if not path.is_file() or any(part in _SEARCH_EXCLUDED_DIRECTORIES
                                         for part in path.relative_to(scope if scope.is_dir() else scope.parent).parts):
                continue
            relative = path.relative_to(self._WORKSPACE_ROOT).as_posix()
            if not fnmatch.fnmatch(path.name, pattern) and not fnmatch.fnmatch(relative, pattern):
                continue
            try:
                needle = query.encode("utf-8")
                overlap, prefix, found, number = b"", b"", False, 1

                def record_line() -> bool:
                    nonlocal size
                    if not found:
                        return False
                    remaining = 20_000 - size
                    if remaining <= 0 or len(output) >= limit:
                        return True
                    text = prefix.decode("utf-8", "replace").rstrip()
                    result = f"{relative}:{number}:{text}"[:remaining]
                    output.append(result)
                    size += len(result) + 1
                    return len(output) >= limit or size >= 20_000

                with path.open("rb") as source:
                    while chunk := source.read(64 * 1024):
                        if time.monotonic() >= deadline:
                            return (tool_success(str("\n".join(output))) if output else tool_failure('timeout', "Search timed out.", retriable=False))
                        parts = chunk.split(b"\n")
                        for piece in parts[:-1]:
                            probe = overlap + piece
                            found = found or needle in probe
                            prefix = (prefix + piece)[:401]
                            if record_line():
                                return tool_success(str("\n".join(output)))
                            number += 1
                            overlap, prefix, found = b"", b"", False
                        piece = parts[-1]
                        probe = overlap + piece
                        found = found or needle in probe
                        prefix = (prefix + piece)[:401]
                        overlap = probe[-(len(needle) - 1):] if len(needle) > 1 else b""
                    if prefix or found:
                        if record_line():
                            return tool_success(str("\n".join(output)))
            except OSError:
                continue
        return (tool_success(str("\n".join(output))) if output else tool_success(str("No matches.")))
    except Exception as exc:
        return tool_failure('execution_error', f"Error searching files: {exc}", retriable=False)
