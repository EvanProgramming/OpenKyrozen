"""Static validation boundary for LLM-generated Python tools."""

from __future__ import annotations

import ast
import builtins


BLOCKED_NAMES = {
    "__import__", "eval", "exec", "compile", "open", "input", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "breakpoint", "object", "type",
}
BLOCKED_ATTRIBUTES = {"system", "popen", "run", "remove", "unlink", "rmdir", "connect", "request"}
SAFE_BUILTINS = {
    name: getattr(builtins, name) for name in
    ("abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "len", "list",
     "max", "min", "range", "reversed", "round", "set", "sorted", "str", "sum", "tuple", "zip")
}


def validate_tool_source(source: str, function_name: str) -> tuple[bool, str]:
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
        if isinstance(node, ast.Name) and node.id in BLOCKED_NAMES:
            return False, f"blocked builtin: {node.id}"
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__") or node.attr in BLOCKED_ATTRIBUTES:
                return False, f"blocked attribute: {node.attr}"
    return True, "static validation passed"
