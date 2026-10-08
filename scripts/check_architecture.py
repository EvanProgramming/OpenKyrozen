#!/usr/bin/env python3
"""Check package dependencies without importing application services."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY_MODULES = frozenset({
    "main", "server", "providers", "tools", "memory", "task_engine", "event_store",
    "learning_engine", "learning_worker", "interaction", "fast_mode", "system_one_policy",
    "workspace_context", "project_graph", "history", "agent_config", "context_compaction",
    "skill_registry", "instruction_loader", "subagents", "capability_tokens", "dynamic_tools",
    "plugin_runtime", "browser_manager", "github_cli", "tui_backend", "tui_launcher",
})
ADAPTERS = frozenset({
    "openkyrozen.memory.vector", "openkyrozen.persistence.database", "openkyrozen.persistence.store",
    "openkyrozen.tools.adapters", "openkyrozen.providers.factory", "openkyrozen.providers.openai",
    "openkyrozen.providers.anthropic", "openkyrozen.providers.google", "openkyrozen.providers.azure",
    "openkyrozen.providers.bedrock", "openkyrozen.providers.ollama", "openkyrozen.providers.perplexity",
    "openkyrozen.routing.transport", "openkyrozen.routing.kev",
    "openkyrozen.workspace.history", "openkyrozen.workspace.history_service",
    "openkyrozen.learning.worker", "openkyrozen.updates.learning_setup",
})
EXTERNAL_ADAPTERS = {"subprocess", "sqlite3", "chromadb", "rich", "fastapi", "uvicorn", "openai", "anthropic", "boto3"}
CORE = {"agent", "memory", "learning", "tasks"}
ENTRY_ADAPTERS = {"openkyrozen.learning.worker", "openkyrozen.learning.benchmark", "openkyrozen.memory.vector"}


def _imports_at_import_time(tree):
    """Traverse initialization statements, excluding deferred/type-only bodies."""
    if isinstance(tree, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return
    if isinstance(tree, (ast.Import, ast.ImportFrom)):
        yield tree
        return
    if isinstance(tree, ast.If):
        guard = tree.test
        if ((isinstance(guard, ast.Name) and guard.id == "TYPE_CHECKING")
                or (isinstance(guard, ast.Attribute) and guard.attr == "TYPE_CHECKING"
                    and isinstance(guard.value, ast.Name) and guard.value.id == "typing")):
            for statement in tree.orelse:
                yield from _imports_at_import_time(statement)
            return
    for child in ast.iter_child_nodes(tree):
        yield from _imports_at_import_time(child)


def _imported_module(module, path, node):
    """Resolve an ImportFrom namespace without importing application code."""
    if not node.level:
        return node.module or ""
    base = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    parts = base.split(".")
    base = ".".join(parts[:len(parts) - node.level + 1])
    return base + ("." + node.module if node.module else "")


def check(root: Path = ROOT) -> list[str]:
    paths = {".".join(p.relative_to(root).with_suffix("").parts).removesuffix(".__init__"): p
             for p in (root / "openkyrozen").rglob("*.py")}
    trees = {module: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
             for module, path in paths.items()}
    exports = {(module, alias.asname or alias.name): (_imported_module(module, paths[module], node), alias.name)
               for module, tree in trees.items() for node in tree.body
               if isinstance(node, ast.ImportFrom) for alias in node.names if alias.name != "*"}

    wildcard_exports = {}
    for module, tree in trees.items():
        names = {name for owner, name in exports if owner == module and not name.startswith("_")}
        for node in tree.body:
            if (isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)):
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError, SyntaxError):
                    continue
                if isinstance(value, (list, tuple)) and all(isinstance(name, str) for name in value):
                    names = set(value)
        wildcard_exports[module] = names

    def reexport_origins(module, name, seen=frozenset()):
        """Follow named import re-exports, terminating on circular bindings."""
        key = (module, name)
        if key in seen:
            return
        if name == "*":
            for exported in wildcard_exports.get(module, ()):
                yield from reexport_origins(module, exported, seen | {key})
        elif key in exports:
            source, imported = exports[key]
            yield source
            if source + "." + imported in paths:
                yield source + "." + imported
            yield from reexport_origins(source, imported, seen | {key})

    errors, graph = [], {}
    for module, path in paths.items():
        tree = trees[module]
        graph[module] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".")[0] in LEGACY_MODULES:
                    errors.append(f"{path.relative_to(root)}:{node.lineno}: legacy import {name}")
        import_time_nodes = set(_imports_at_import_time(tree))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            else:
                target = _imported_module(module, path, node)
                targets = [target]
                targets.extend(target + "." + alias.name for alias in node.names
                               if target + "." + alias.name in paths)
                policy_targets = [origin for alias in node.names
                                  for origin in reexport_origins(target, alias.name)]
            if isinstance(node, ast.Import):
                policy_targets = []
            for target in targets:
                if node in import_time_nodes and target in paths and target != module:
                    graph[module].add(target)
            for target in targets + policy_targets:
                if (module.partition(".")[2].split(".")[0] in CORE and module not in ENTRY_ADAPTERS
                        and (target == "openkyrozen.interfaces" or target.startswith("openkyrozen.interfaces.")
                             or target in ADAPTERS or target.split(".")[0] in EXTERNAL_ADAPTERS)):
                    errors.append(f"{path.relative_to(root)}:{node.lineno}: core imports adapter {target}")
    visiting, complete = [], set()

    def visit(module):
        if module in complete:
            return
        if module in visiting:
            errors.append("Import cycle: " + " -> ".join(visiting[visiting.index(module):] + [module]))
            return
        visiting.append(module)
        for dependency in sorted(graph[module]):
            visit(dependency)
        visiting.pop()
        complete.add(module)

    for module in sorted(graph):
        visit(module)
    return sorted(set(errors))


if __name__ == "__main__":
    failures = check()
    if failures:
        raise SystemExit("\n".join(failures))
    print("Package architecture checks passed.")
