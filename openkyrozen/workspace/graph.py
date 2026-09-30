"""Private, incremental Graphify index for one OpenKyrozen workspace."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


MAX_FILES = 50_000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_QUERY_CHARS = 12_000
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "vendor",
    "dist", "build", "target", "__pycache__", "chroma_memory", "graphify-out",
}
CODE_SUFFIXES = {
    ".bash", ".c", ".cc", ".clj", ".cljs", ".cpp", ".cs", ".css", ".cxx",
    ".dart", ".ex", ".exs", ".fish", ".fs", ".fsx", ".go", ".gradle", ".groovy",
    ".h", ".hcl", ".hh", ".hpp", ".html", ".java", ".js", ".json", ".jsx",
    ".kt", ".kts", ".less", ".lua", ".m", ".mm", ".pas", ".php", ".pl", ".pm",
    ".ps1", ".py", ".pyi", ".r", ".rb", ".rs", ".sass", ".scala", ".scss",
    ".sh", ".sql", ".svelte", ".swift", ".tf", ".tfvars", ".toml", ".ts", ".tsx",
    ".vb", ".vue", ".xml", ".yaml", ".yml", ".zig", ".zsh",
}
CODE_NAMES = {
    ".dockerignore", ".gitignore", ".graphifyignore", "CMakeLists.txt", "Dockerfile",
    "Gemfile", "Makefile", "Podfile", "Rakefile", "WORKSPACE", "build.gradle",
    "go.mod", "go.sum", "package.json", "pyproject.toml", "requirements.txt",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProjectGraph:
    """Keep Graphify output in private state while preserving project-relative paths."""

    def __init__(
        self,
        source_root: str | os.PathLike[str],
        state_root: str | os.PathLike[str],
        source_scope_id: str,
        *,
        python: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.source_root = Path(source_root).expanduser().resolve()
        self.cache_root = Path(state_root).expanduser().resolve() / "graphs" / source_scope_id
        self.mirror_root = self.cache_root / "source"
        self.graph_dir = self.mirror_root / "graphify-out"
        self.graph_path = self.graph_dir / "graph.json"
        self.sync_path = self.cache_root / "sync.json"
        self.state_path = self.cache_root / "state.json"
        self.python = python or sys.executable
        self.runner = runner
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self.cache_root.mkdir(parents=True, exist_ok=True)

    def _read_json(self, path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return default

    def _write_json(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def _eligible(relative: Path) -> bool:
        if any(part in SKIP_DIRS for part in relative.parts):
            return False
        name = relative.name
        if name.startswith((".env", "credentials", "secrets")):
            return False
        return relative.suffix.lower() in CODE_SUFFIXES or name in CODE_NAMES

    def _git_files(self) -> list[Path] | None:
        try:
            result = self.runner(
                ["git", "-C", str(self.source_root), "ls-files", "-co", "--exclude-standard", "-z"],
                capture_output=True, text=False, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return [Path(os.fsdecode(item)) for item in result.stdout.split(b"\0") if item]

    def _walk_files(self) -> list[Path]:
        files: list[Path] = []
        for root, dirs, names in os.walk(self.source_root, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS and not (Path(root) / name).is_symlink())
            base = Path(root)
            for name in sorted(names):
                path = base / name
                try:
                    relative = path.relative_to(self.source_root)
                except ValueError:
                    continue
                if self._eligible(relative):
                    files.append(relative)
                    if len(files) >= MAX_FILES:
                        return files
        return files

    def _source_files(self) -> list[Path]:
        candidates = self._git_files()
        if candidates is None:
            candidates = self._walk_files()
        result: list[Path] = []
        for relative in candidates:
            if relative.is_absolute() or ".." in relative.parts or not self._eligible(relative):
                continue
            source = self.source_root / relative
            try:
                if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            result.append(relative)
            if len(result) >= MAX_FILES:
                break
        return sorted(set(result), key=lambda item: item.as_posix())

    def sync(self, *, full: bool = False) -> dict[str, Any]:
        previous = {} if full else self._read_json(self.sync_path, {}).get("files", {})
        current: dict[str, dict[str, int]] = {}
        copied = 0
        self.mirror_root.mkdir(parents=True, exist_ok=True)
        for relative in self._source_files():
            source = self.source_root / relative
            try:
                stat = source.stat()
            except OSError:
                continue
            key = relative.as_posix()
            fingerprint = {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}
            current[key] = fingerprint
            target = self.mirror_root / relative
            if previous.get(key) == fingerprint and target.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=False)
            copied += 1
        deleted = 0
        for key in sorted(set(previous) - set(current)):
            target = self.mirror_root / Path(key)
            try:
                target.relative_to(self.mirror_root)
                target.unlink(missing_ok=True)
                deleted += 1
            except (OSError, ValueError):
                continue
        self._write_json(self.sync_path, {"source": str(self.source_root), "files": current, "updated_at": _utc_now()})
        return {"files": len(current), "copied": copied, "deleted": deleted, "changed": copied + deleted}

    def _set_state(self, status: str, **values: Any) -> dict[str, Any]:
        state = {**self._read_json(self.state_path, {}), "status": status, **values}
        self._write_json(self.state_path, state)
        return state

    def _run_graphify(self, args: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
        return self.runner(
            [self.python, "-m", "graphify", *args], cwd=str(self.mirror_root),
            capture_output=True, text=True, timeout=timeout, check=False,
        )

    def refresh(self, *, full: bool = False) -> dict[str, Any]:
        with self._lock:
            self._set_state("indexing", started_at=_utc_now(), error="")
            backup = self.cache_root / ".last-valid-graphify-out"
            try:
                synced = self.sync(full=full)
                if synced["files"] == 0:
                    return self._set_state("missing", message="No supported code files found.", **synced)
                if backup.exists():
                    shutil.rmtree(backup)
                if self.graph_dir.exists():
                    if full:
                        self.graph_dir.replace(backup)
                    elif synced["changed"]:
                        shutil.copytree(self.graph_dir, backup)
                if not self.graph_path.is_file():
                    command = ["extract", str(self.mirror_root), "--code-only", "--no-viz"]
                elif synced["changed"]:
                    command = ["update", str(self.mirror_root), "--force"]
                else:
                    return self._set_state("ready", message="Graph is current.", **synced, **self._graph_counts())
                result = self._run_graphify(command)
                if result.returncode != 0 or not self.graph_path.is_file():
                    if backup.exists():
                        shutil.rmtree(self.graph_dir, ignore_errors=True)
                        backup.replace(self.graph_dir)
                    detail = (result.stderr or result.stdout or "Graphify produced no graph.").strip()[-2000:]
                    status = "stale" if self.graph_path.is_file() else "error"
                    return self._set_state(status, error=detail, message="Graph refresh failed; using the last valid graph." if status == "stale" else "Graph build failed.", **synced)
                shutil.rmtree(backup, ignore_errors=True)
                return self._set_state(
                    "ready", updated_at=_utc_now(), error="", message="Graph refreshed.",
                    **synced, **self._graph_counts(),
                )
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                if backup.exists():
                    shutil.rmtree(self.graph_dir, ignore_errors=True)
                    backup.replace(self.graph_dir)
                status = "stale" if self.graph_path.is_file() else "error"
                return self._set_state(status, error=str(exc)[:2000], message="Graph refresh failed.")

    def refresh_async(self, *, full: bool = False, callback: Callable[[dict[str, Any]], None] | None = None) -> bool:
        if self._worker is not None and self._worker.is_alive():
            return False

        def work() -> None:
            state = self.refresh(full=full)
            if callback is not None:
                callback(state)

        self._worker = threading.Thread(target=work, name="kyrozen-project-graph", daemon=True)
        self._worker.start()
        return True

    def _graph_counts(self) -> dict[str, int]:
        graph = self._read_json(self.graph_path, {})
        nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
        edges = graph.get("links", graph.get("edges", [])) if isinstance(graph, dict) else []
        communities = {str(item.get("community")) for item in nodes if item.get("community") is not None}
        return {"nodes": len(nodes), "edges": len(edges), "communities": len(communities)}

    def snapshot(self, *, limit: int = 14) -> dict[str, Any]:
        return self.explore(limit=limit)

    def explore(self, *, query: str = "", node_id: str = "", community: int | None = None,
                limit: int = 30) -> dict[str, Any]:
        with self._lock:
            state = self._read_json(self.state_path, {})
            if not state:
                state = {"status": "ready" if self.graph_path.is_file() else "missing"}
            state.pop("graphify_output", None)
            graph = self._read_json(self.graph_path, {})
            nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
            edges = graph.get("links", graph.get("edges", [])) if isinstance(graph, dict) else []
        degree: dict[str, int] = {}
        for edge in edges:
            source, target = str(edge.get("source", "")), str(edge.get("target", ""))
            degree[source] = degree.get(source, 0) + 1
            degree[target] = degree.get(target, 0) + 1
        query_lower = str(query).strip().lower()
        node_id = str(node_id).strip()
        neighbors: set[str] = {node_id} if node_id else set()
        if node_id:
            for edge in edges:
                source, target = str(edge.get("source", "")), str(edge.get("target", ""))
                if source == node_id:
                    neighbors.add(target)
                elif target == node_id:
                    neighbors.add(source)
        filtered = [item for item in nodes if (
            (not query_lower or query_lower in str(item.get("label", "")).lower())
            and (community is None or int(item.get("community", 0) or 0) == community)
            and (not neighbors or str(item.get("id", "")) in neighbors)
        )]
        selected = sorted(filtered, key=lambda item: (-degree.get(str(item.get("id", "")), 0), str(item.get("label", ""))))[:max(1, min(limit, 30))]
        selected_ids = {str(item.get("id", "")) for item in selected}
        mini_nodes = [{
            "id": str(item.get("id", ""))[:200], "label": str(item.get("label", item.get("id", "")))[:120],
            "community": int(item.get("community", 0) or 0), "degree": degree.get(str(item.get("id", "")), 0),
            "source": self._remap(str(item.get("source_file", "")))[:300], "location": str(item.get("source_location", ""))[:80],
        } for item in selected]
        mini_edges = [{
            "source": str(edge.get("source", ""))[:200], "target": str(edge.get("target", ""))[:200],
            "relation": str(edge.get("relation", ""))[:80], "confidence": str(edge.get("confidence", ""))[:40],
        } for edge in edges if str(edge.get("source", "")) in selected_ids and str(edge.get("target", "")) in selected_ids][:40]
        counts = self._graph_counts()
        return {**state, **counts, "scope": self.cache_root.name, "mini": {"nodes": mini_nodes, "edges": mini_edges}}

    def _query_command(self, args: list[str]) -> str:
        with self._lock:
            if not self.graph_path.is_file():
                return "Error: project graph is not ready. Run /graph refresh."
            try:
                result = self._run_graphify(args, timeout=120)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return f"Error: Graphify query failed: {exc}"
        output = (result.stdout or result.stderr or "").strip()
        if result.returncode != 0:
            return f"Error: Graphify exited with {result.returncode}: {output[-2000:]}"
        return self._remap(output)[:MAX_QUERY_CHARS] or "No graph results."

    def _remap(self, value: str) -> str:
        return str(value).replace(str(self.mirror_root), str(self.source_root))

    def query(self, question: str, *, budget: int = 2500) -> str:
        question = str(question).strip()[:2000]
        if not question:
            return "Error: graph_query requires a question."
        return self._query_command(["query", question, "--budget", str(max(200, min(int(budget), 5000)))])

    def explain(self, node: str) -> str:
        node = str(node).strip()[:300]
        return self._query_command(["explain", node]) if node else "Error: graph_explain requires a node."

    def path(self, left: str, right: str) -> str:
        left, right = str(left).strip()[:300], str(right).strip()[:300]
        return self._query_command(["path", left, right]) if left and right else "Error: graph_path requires left|right."
