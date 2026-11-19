"""conv_rules_code - AST rules: import-time effects, print, except handlers, free-text errors.

Layer:   tooling
Stage:   V2-00B
Flags:   none
Raises:  never
Budget:  none
Tests:   tests/v2/conventions/test_conv_rules.py
"""

import ast

from conv_config import (
    BUILTIN_ERRORS,
    COUNTER_CALLS,
    IMPORT_TIME_ALLOWED,
    IMPORT_TIME_CALLS,
    IMPORT_TIME_PREFIXES,
    LOG_METHODS,
    V2_ROOT,
)
from conv_model import FUNC_TYPES, SourceFile, Violation


def alias_map(tree):
    """Local name -> qualified name for every import in the file."""
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                aliases[alias.asname or top] = alias.name if alias.asname else top
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def dotted(node, aliases):
    """Qualified dotted name of a Name/Attribute chain, or None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([aliases.get(node.id, node.id), *reversed(parts)])


def _is_main_guard(node):
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
    )


def import_time_nodes(tree):
    """Yield every AST node that runs when the module is imported."""
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.If) and _is_main_guard(node):
            continue
        if isinstance(node, (*FUNC_TYPES, ast.Lambda)):
            stack.extend(getattr(node, "decorator_list", []))
            args = node.args
            stack.extend([*args.defaults, *[d for d in args.kw_defaults if d is not None]])
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _forbidden_at_import(name):
    if name in IMPORT_TIME_ALLOWED:
        return False
    return name in IMPORT_TIME_CALLS or name.startswith(IMPORT_TIME_PREFIXES)


def check_import_time(sf: SourceFile) -> list[Violation]:
    aliases = alias_map(sf.tree)
    out = []
    for node in import_time_nodes(sf.tree):
        if isinstance(node, ast.Call):
            name = dotted(node.func, aliases)
        elif isinstance(node, ast.Attribute):
            name = dotted(node, aliases)
        else:
            continue
        if name and _forbidden_at_import(name):
            message = f"'{name}' runs at import time"
            out.append(Violation("import-time", sf.rel, node.lineno, message))
    return out


def check_print(sf: SourceFile) -> list[Violation]:
    return [
        Violation("print", sf.rel, node.lineno, "use the structured logger, not print()")
        for node in ast.walk(sf.tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]


def _has_call(handler, names):
    for node in ast.walk(handler):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in names:
                return True
    return False


def _is_broad(handler):
    kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(k, ast.Name) and k.id in ("Exception", "BaseException") for k in kinds)


def _handled(handler):
    if any(isinstance(node, ast.Raise) for node in ast.walk(handler)):
        return True
    return _has_call(handler, LOG_METHODS) and _has_call(handler, COUNTER_CALLS)


def check_excepts(sf: SourceFile) -> list[Violation]:
    out = []
    for node in ast.walk(sf.tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:
            out.append(Violation("bare-except", sf.rel, node.lineno, "bare 'except:' is forbidden"))
        elif _is_broad(node) and not _handled(node):
            message = "'except Exception' must re-raise, or log and count"
            out.append(Violation("broad-except", sf.rel, node.lineno, message))
    return out


def _is_free_text(arg):
    if isinstance(arg, ast.Constant):
        return isinstance(arg.value, str)
    if isinstance(arg, ast.JoinedStr):
        return True
    if isinstance(arg, ast.BinOp):
        return _is_free_text(arg.left) or _is_free_text(arg.right)
    return (
        isinstance(arg, ast.Call)
        and isinstance(arg.func, ast.Attribute)
        and arg.func.attr == "format"
    )


def check_free_errors(sf: SourceFile) -> list[Violation]:
    if sf.rel == f"{V2_ROOT}core/errors.py":
        return []
    out = []
    for node in ast.walk(sf.tree):
        exc = node.exc if isinstance(node, ast.Raise) else None
        if (
            isinstance(exc, ast.Call)
            and isinstance(exc.func, ast.Name)
            and exc.func.id in BUILTIN_ERRORS
            and exc.args
            and _is_free_text(exc.args[0])
        ):
            message = f"free-text {exc.func.id}; raise a core/errors.py error with a code"
            out.append(Violation("free-error", sf.rel, node.lineno, message))
    return out


def check_code_rules(sf: SourceFile, layer) -> list[Violation]:
    if sf.tree is None:
        return []
    out = check_import_time(sf) + check_excepts(sf)
    if layer != "tooling":
        out += check_print(sf)
    if sf.rel.startswith(V2_ROOT):
        out += check_free_errors(sf)
    return out
