"""Tiny safe expression evaluator for bucket rules.

Supports: numbers, strings, names (looked up in a dict), and/or/not,
comparisons (incl. chained `0.3 <= energy < 0.6`), + - * / %, unary -,
`in` / `not in` with list/tuple literals, and calls to min/max/abs.
Anything else (attribute access, subscripts, lambdas, other calls) is rejected.
NaN / missing values make comparisons False instead of raising.
"""
from __future__ import annotations

import ast
import math
import operator as op
from typing import Any

_BIN = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv, ast.Mod: op.mod}
_CMP = {ast.Lt: op.lt, ast.LtE: op.le, ast.Gt: op.gt, ast.GtE: op.ge, ast.Eq: op.eq, ast.NotEq: op.ne}
_FUNCS = {"min": min, "max": max, "abs": abs}


class RuleError(ValueError):
    pass


def _is_nan(v: Any) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def compile_rule(expr: str) -> ast.Expression:
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as e:
        raise RuleError(f"Syntax error: {e.msg}") from None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
                             ast.BinOp, ast.Compare, ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple,
                             ast.In, ast.NotIn, *_BIN.keys(), *_CMP.keys())):
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
            continue
        raise RuleError(f"Not allowed in rules: {type(node).__name__}")
    return tree


def names_in(expr: str) -> set[str]:
    tree = compile_rule(expr)
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id not in _FUNCS}


def _eval(node: ast.AST, env: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, env)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in ("True", "False"):
            return node.id == "True"
        if node.id not in env:
            raise RuleError(f"Unknown name: {node.id}")
        return env[node.id]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e, env) for e in node.elts]
    if isinstance(node, ast.BoolOp):
        vals = (_eval(v, env) for v in node.values)
        return all(bool(v) for v in vals) if isinstance(node.op, ast.And) else any(bool(v) for v in vals)
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, env)
        if isinstance(node.op, ast.Not):
            return not bool(v)
        if _is_nan(v):
            return float("nan")
        return -v if isinstance(node.op, ast.USub) else +v
    if isinstance(node, ast.BinOp):
        a, b = _eval(node.left, env), _eval(node.right, env)
        if _is_nan(a) or _is_nan(b):
            return float("nan")
        try:
            return _BIN[type(node.op)](a, b)
        except ZeroDivisionError:
            return float("nan")
    if isinstance(node, ast.Compare):
        left = _eval(node.left, env)
        for cmp_op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, env)
            if isinstance(cmp_op, (ast.In, ast.NotIn)):
                ok = (left in right) if isinstance(cmp_op, ast.In) else (left not in right)
            else:
                if _is_nan(left) or _is_nan(right):
                    return False
                ok = _CMP[type(cmp_op)](left, right)
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        args = [_eval(a, env) for a in node.args]
        if any(_is_nan(a) for a in args):
            return float("nan")
        return _FUNCS[node.func.id](*args)
    raise RuleError(f"Unsupported: {type(node).__name__}")


def evaluate(expr: str | ast.Expression, env: dict[str, Any]) -> Any:
    tree = compile_rule(expr) if isinstance(expr, str) else expr
    return _eval(tree, env)
