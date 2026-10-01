"""Bounded arithmetic checks, with no code, filesystem or process execution."""
from __future__ import annotations

import ast
from decimal import Decimal, localcontext
import operator


_FUNCTIONS = {"sum": sum, "round": round, "abs": abs, "min": min, "max": max,
              "len": len, "Decimal": Decimal, "float": float, "int": int}
_OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
              ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}
_COMPARISONS = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
                ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


def _bounded(value):
    if isinstance(value, int) and value.bit_length() > 1024:
        raise ValueError("Integer exceeds calculation budget")
    if isinstance(value, Decimal) and value.is_finite() and (len(value.as_tuple().digits) > 256 or abs(value.adjusted()) > 256):
        raise ValueError("Decimal exceeds calculation budget")
    if isinstance(value, (str, list, tuple)) and len(value) > 128:
        raise ValueError("Value exceeds calculation budget")
    if not isinstance(value, (int, float, Decimal, str, list, tuple)):
        raise ValueError("Unsupported value")
    return value


def _value(node):
    if isinstance(node, ast.Constant):
        return _bounded(node.value)
    if isinstance(node, (ast.List, ast.Tuple)):
        values = [_value(item) for item in node.elts]
        return _bounded(tuple(values) if isinstance(node, ast.Tuple) else values)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS and not node.keywords:
        return _bounded(_FUNCTIONS[node.func.id](*[_value(arg) for arg in node.args]))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _value(node.operand)
        if not isinstance(value, (int, float, Decimal)):
            raise ValueError("Unary arithmetic requires numbers")
        return _bounded(value if isinstance(node.op, ast.UAdd) else -value)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        left, right = _value(node.left), _value(node.right)
        if not all(isinstance(item, (int, float, Decimal)) for item in (left, right)):
            raise ValueError("Binary arithmetic requires numbers")
        return _bounded(_OPERATORS[type(node.op)](left, right))
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _COMPARISONS:
        return _COMPARISONS[type(node.ops[0])](_value(node.left), _value(node.comparators[0]))
    raise ValueError("Only literals, arithmetic, comparisons and listed pure functions are allowed")


def calculate(self, args: str) -> str:
    """Check a bounded arithmetic expression, e.g. sum([Decimal('1.00'), Decimal('2.00')]); no workspace code runs."""
    try:
        if not isinstance(args, str) or len(args) > 4000:
            raise ValueError("Expression exceeds calculation budget")
        tree = ast.parse(args, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 256:
            raise ValueError("Expression exceeds calculation budget")
        with localcontext() as context:
            context.prec, context.Emax, context.Emin = 28, 256, -256
            value = _value(tree.body)
        return f"{type(value).__name__}: {value!r} (arithmetic check only; Decimal precision 28)"
    except Exception as exc:
        return f"Error: arithmetic check {type(exc).__name__}: {exc}"
