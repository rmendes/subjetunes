# subjetunes/tagging/expr.py
"""Small safe evaluator for tag rules such as ``comp_acid >= 0.7 and energy > 0.5``.

Self-contained on purpose (whitelisted ``ast`` nodes, no ``eval``), so the tagging
package doesn't depend on the internals of ``subjetunes.rules``. Semantics match
the bucket rules: a missing or NaN value makes every comparison it's in False.
"""

from __future__ import annotations

import ast
import math
import operator
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping

_CMP = {
    ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
    ast.GtE: operator.ge, ast.Eq: operator.eq, ast.NotEq: operator.ne,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,   # "dubstep" in clues
}
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_FUNCS = {"min": min, "max": max, "abs": abs}


class RuleError(ValueError):
    """The rule text is invalid (syntax or a disallowed construct)."""


class _Missing:
    """Stands in for an absent variable: every comparison with it is False."""

    def __repr__(self) -> str:
        return "MISSING"


MISSING = _Missing()


def _is_nan(v: Any) -> bool:
    return v is MISSING or (isinstance(v, float) and math.isnan(v))


@dataclass(frozen=True)
class Rule:
    text: str
    tree: ast.Expression
    names: frozenset[str]

    def __call__(self, values: Mapping[str, Any]) -> bool:
        return bool(_eval(self.tree.body, values))


@lru_cache(maxsize=512)
def compile_rule(text: str) -> Rule:
    text = (text or "").strip()
    if not text:
        raise RuleError("empty rule")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as e:
        raise RuleError(f"syntax error in {text!r}: {e.msg}") from None
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Call):
            if not (isinstance(node.func, ast.Name) and node.func.id in _FUNCS) or node.keywords:
                raise RuleError(f"only min(), max(), abs() calls are allowed in {text!r}")
        elif not isinstance(node, (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
                                   ast.USub, ast.UAdd, ast.Compare, ast.BinOp, ast.Constant, ast.Load,
                                   *_CMP, *_BIN)):
            raise RuleError(f"{type(node).__name__} is not allowed in {text!r}")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, str, bool)):  # noqa
            raise RuleError(f"constant {node.value!r} is not allowed in {text!r}")
    names -= set(_FUNCS)
    return Rule(text, tree, frozenset(names))


def _eval(node: ast.AST, v: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(bool(_eval(x, v)) for x in node.values)
        return any(bool(_eval(x, v)) for x in node.values)
    if isinstance(node, ast.UnaryOp):
        x = _eval(node.operand, v)
        if isinstance(node.op, ast.Not):
            return not bool(x)
        if _is_nan(x):
            return MISSING
        return -x if isinstance(node.op, ast.USub) else +x
    if isinstance(node, ast.Compare):
        left = _eval(node.left, v)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, v)
            if isinstance(op, (ast.In, ast.NotIn)) and right is MISSING:
                right = frozenset()          # no clues at all: "x in clues" is False, "not in" True
            if _is_nan(left) or _is_nan(right):
                return False
            try:
                if not _CMP[type(op)](left, right):
                    return False
            except TypeError:
                return False  # e.g. comparing a string label with a number
            left = right
        return True
    if isinstance(node, ast.BinOp):
        a, b = _eval(node.left, v), _eval(node.right, v)
        if _is_nan(a) or _is_nan(b):
            return MISSING
        try:
            return _BIN[type(node.op)](a, b)
        except (TypeError, ZeroDivisionError):
            return MISSING
    if isinstance(node, ast.Call):
        args = [_eval(a, v) for a in node.args]
        if any(_is_nan(a) for a in args):
            return MISSING
        return _FUNCS[node.func.id](*args)  # type: ignore[attr-defined]
    if isinstance(node, ast.Name):
        x = v.get(node.id, MISSING)
        return MISSING if x is None else x
    if isinstance(node, ast.Constant):
        return node.value
    raise RuleError(f"unsupported node {type(node).__name__}")  # pragma: no cover


def is_stable(rule: Rule, values: Mapping[str, Any], margin: float) -> bool:
    """True if the rule's outcome doesn't flip when any single numeric input moves by ±margin.

    This is how a tag says "I'm sure": ``energy >= 0.75`` at energy 0.76 is a coin flip,
    at 0.9 it isn't. Cheap: 2 evaluations per referenced variable.
    """
    if margin <= 0:
        return True
    base = rule(values)
    for name in rule.names:
        x = values.get(name)
        if not isinstance(x, (int, float)) or isinstance(x, bool) or math.isnan(float(x)):
            continue
        for d in (-margin, margin):
            if rule({**values, name: float(x) + d}) != base:
                return False
    return True
