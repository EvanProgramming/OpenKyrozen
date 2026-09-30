"""Static validation boundary for LLM-generated Python tools."""

from __future__ import annotations

import ast
import builtins
import re


BLOCKED_NAMES = {
    "__import__", "eval", "exec", "compile", "open", "input", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "breakpoint", "object", "type",
    "issue_capability_token", "grant_capability", "grant_permission", "authorize",
    "set_permissions", "allow_dynamic_tools",
}
BLOCKED_ATTRIBUTES = {
    "system", "popen", "run", "remove", "unlink", "rmdir", "connect", "request",
    "issue_capability_token", "grant_capability", "grant_permission", "authorize",
    "set_permissions",
}
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----"),
    re.compile(r"(?i)(?:api[_-]?key|secret|password|access[_-]?token)\s*=\s*['\"][^'\"]{6,}['\"]"),
)
SAFE_BUILTINS = {
    name: getattr(builtins, name) for name in
    ("abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "len", "list",
     "max", "min", "range", "reversed", "round", "set", "sorted", "str", "sum", "tuple", "zip")
}


def validate_tool_source(source: str, function_name: str) -> tuple[bool, str]:
    for pattern in _SECRET_PATTERNS:
        if pattern.search(source):
            return False, "secret-like material is not allowed in generated tools"
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        return False, f"syntax error: {exc}"
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not functions or not any(node.name == function_name for node in functions):
        return False, "source must define the requested tool function"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)):
            return False, "imports and global state are not allowed in generated tools"
        if isinstance(node, ast.Name):
            if node.id in BLOCKED_NAMES:
                return False, f"blocked builtin or permission API: {node.id}"
            if node.id.startswith("__"):
                return False, f"dunder name is not allowed: {node.id}"
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__") or node.attr in BLOCKED_ATTRIBUTES:
                return False, f"blocked attribute: {node.attr}"
    return True, "static validation passed"
