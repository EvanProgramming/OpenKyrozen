from __future__ import annotations

import re
import time
from typing import Any

def _load_project_files_into_memory(self, *, force: bool = False) -> dict[str, Any]:
    """Compatibility entry point: refresh Graphify instead of storing full files."""
    now = time.time()
    project_root = self._get_workspace_root()
    if not force and project_root == self._last_project_scan_root and now - self._last_project_scan_time < 900:
        return self._project_graph.snapshot() if self._project_graph is not None else {"status": "missing"}
    self._last_project_scan_time = now
    self._last_project_scan_root = project_root
    if self._project_graph is None:
        return {"status": "missing", "message": "Project graph is not configured."}
    if force:
        return self._project_graph.refresh(full=True)
    previous = self._project_graph.snapshot()
    def completed(state: dict[str, Any]) -> None:
        if state.get("status") == "ready" and state.get("nodes", 0):
            self._record_learning_event("learning.product_created", {
                "feature": "load_project_files_into_memory",
                "product_id": state.get("updated_at") or "project-graph",
            })

    started = self._project_graph.refresh_async(callback=completed)
    return {**previous, "status": "indexing" if started else previous.get("status", "missing")}


def _autonomous_inspection(self) -> None:
    """Proactive codebase health check: outdated packages, code smells, security issues."""
    now = time.time()
    if now - self._last_inspection_time < self._inspection_interval:
        return
    if now - self._last_user_interaction < 300:  # user active, don't disturb
        return
    self._last_inspection_time = now

    findings: list[str] = []

    # 1. Check for outdated pip packages
    try:
        outdated = self._outdated_packages()
        if outdated:
            findings.append("Outdated packages:\n  " + "\n  ".join(outdated))
    except Exception:
        pass

    # 2. Check for common code smells (bare except, print debugging, TODO markers)
    project_root = self._get_workspace_root()
    bare_excepts = 0
    print_debugs = 0
    todo_markers = 0
    skip_dirs = {".venv", "venv", "chroma_memory", "__pycache__", ".git"}

    for py_file in project_root.rglob("*.py"):
        if any(part in py_file.parts for part in skip_dirs):
            continue
        try:
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            rel = str(py_file.relative_to(project_root))

            # Count bare excepts
            bare = len(re.findall(r"except\s*:", content))
            if bare > 0:
                bare_excepts += bare

            # Count print debugging
            prints = len(re.findall(r"^\s*print\(", content, re.MULTILINE))
            if prints > 2:  # more than 2 prints in a file
                print_debugs += prints

            # Count TODOs
            todos = len(re.findall(r"#\s*TODO", content))
            if todos > 0:
                todo_markers += todos
        except Exception as exc:
            try:
                self.memory_bank.store.append_event(
                    "learning.inspection_file_failed", {"error": str(exc)[:1000], "path": str(py_file)},
                    user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
                    session_id=self.memory_bank.session_id,
                )
            except Exception:
                # The learning loop must not die if its diagnostic sink is unavailable.
                pass

    if bare_excepts > 0:
        findings.append(f"Code smell: {bare_excepts} bare 'except:' clauses (should specify exception type)")
    if print_debugs > 0:
        findings.append(f"Code smell: {print_debugs} print() statements may be leftover debugging")
    if todo_markers > 0:
        findings.append(f"Code hygiene: {todo_markers} TODO markers found in codebase")

    # 3. Check .gitignore for common missing entries
    gitignore_path = project_root / ".gitignore"
    if gitignore_path.exists():
        try:
            gi_content = gitignore_path.read_text(encoding="utf-8")
            missing = []
            for entry in ["*.log", "*.swp", ".env", "dist/", "build/"]:
                if entry not in gi_content:
                    missing.append(entry)
            if missing:
                findings.append(f"Gitignore missing entries: {', '.join(missing)}")
        except Exception as exc:
            try:
                self.memory_bank.store.append_event(
                    "learning.inspection_gitignore_failed", {"error": str(exc)[:1000]},
                    user_id=self.memory_bank.user_id, workspace_id=self.memory_bank.workspace_id,
                    session_id=self.memory_bank.session_id,
                )
            except Exception:
                pass

    if not findings:
        return

    # Store findings in memory for the agent to act on next time user asks
    report = "INSPECTION: Autonomous codebase health check\n" + "\n".join(f"- {f}" for f in findings)
    self._store_learning_product("autonomous_inspection", report)

    # If there are security-critical findings, also log as a FACT for immediate visibility
    if bare_excepts > 5:
        self._store_learning_product("autonomous_inspection", f"FACT: Codebase has {bare_excepts} bare except clauses — potential bug masking")
