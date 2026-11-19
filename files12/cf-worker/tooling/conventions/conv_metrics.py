"""conv_metrics - size and complexity limits: file, function, nesting, params, branches.

Layer:   tooling
Stage:   V2-00B
Flags:   none
Raises:  never
Budget:  none
Tests:   tests/v2/conventions/test_conv_rules.py
"""

import ast

from conv_config import LIMITS
from conv_model import FUNC_TYPES, SourceFile, Violation

_BLOCKS = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.Match,
)
_DECISIONS = (
    ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.match_case,
)
_OPAQUE = (*FUNC_TYPES, ast.Lambda, ast.ClassDef)


def _sub_blocks(stmt):
    """(statement list, is_elif) pairs nested directly inside a block statement."""
    if isinstance(stmt, ast.Match):
        return [(case.body, False) for case in stmt.cases]
    blocks = [(stmt.body, False)]
    orelse = getattr(stmt, "orelse", [])
    if orelse:
        is_elif = isinstance(stmt, ast.If) and len(orelse) == 1 and isinstance(orelse[0], ast.If)
        blocks.append((orelse, is_elif))
    blocks.extend((handler.body, False) for handler in getattr(stmt, "handlers", []))
    if getattr(stmt, "finalbody", []):
        blocks.append((stmt.finalbody, False))
    return blocks


def nesting(stmts, depth=0):
    """Deepest level of nested block statements; an 'elif' does not add a level."""
    deepest = depth
    for stmt in stmts:
        if not isinstance(stmt, _BLOCKS):
            continue
        inner = depth + 1
        deepest = max(deepest, inner)
        for body, is_elif in _sub_blocks(stmt):
            deepest = max(deepest, nesting(body, depth if is_elif else inner))
    return deepest


def _walk_no_funcs(node):
    stack = list(ast.iter_child_nodes(node))
    while stack:
        current = stack.pop()
        yield current
        if not isinstance(current, _OPAQUE):
            stack.extend(ast.iter_child_nodes(current))


def branches(func):
    """Decision points in a function body (nested functions are measured on their own)."""
    total = 0
    for node in _walk_no_funcs(func):
        if isinstance(node, _DECISIONS):
            total += 1
        elif isinstance(node, ast.BoolOp):
            total += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            total += 1 + len(node.ifs)
    return total


def param_count(func):
    args = func.args
    named = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    named += [extra for extra in (args.vararg, args.kwarg) if extra is not None]
    return len([arg for arg in named if arg.arg not in ("self", "cls")])


def _function_violations(rel, node):
    measures = {
        "func-lines": node.end_lineno - node.lineno + 1,
        "nesting": nesting(node.body),
        "params": param_count(node),
        "branches": branches(node),
    }
    return [
        Violation(rule, rel, node.lineno, f"function '{node.name}': {value} (max {LIMITS[rule]})")
        for rule, value in measures.items()
        if value > LIMITS[rule]
    ]


def check_metrics(sf: SourceFile) -> list[Violation]:
    out = []
    if len(sf.lines) > LIMITS["file-lines"]:
        message = f"{len(sf.lines)} lines (max {LIMITS['file-lines']})"
        out.append(Violation("file-lines", sf.rel, 1, message))
    if sf.tree is None:
        return out
    for node in ast.walk(sf.tree):
        if isinstance(node, FUNC_TYPES):
            out.extend(_function_violations(sf.rel, node))
    return out
