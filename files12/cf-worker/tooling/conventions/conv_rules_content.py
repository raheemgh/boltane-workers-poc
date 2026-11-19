"""conv_rules_content - content rules: TODO ids, secrets, select *, logged text, naming.

Layer:   tooling
Stage:   V2-00B
Flags:   none
Raises:  never
Budget:  none
Tests:   tests/v2/conventions/test_conv_rules.py
"""

import ast
import re
from pathlib import Path

from conv_config import LOG_METHODS, SENSITIVE_NAMES, V2_ROOT
from conv_model import FUNC_TYPES, SourceFile, Violation

_TODO = re.compile(r"\bTODO\b(?!\(V2-\d\d[A-Z]?\))")
_SECRETS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"),
)
_RAW_SELECT = re.compile(r"select\s+\*\s+from\s+(?:public\.)?stores\b", re.IGNORECASE)
_MODULE_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")
_TEST_NAME = re.compile(r"^test_[a-z0-9_]+?__[a-z0-9_]+$")


def check_todo(sf: SourceFile) -> list[Violation]:
    return [
        Violation("todo", sf.rel, line, "TODO needs a stage id: TODO(V2-xx)")
        for line, text in sf.comments
        if _TODO.search(text)
    ]


def check_secrets(sf: SourceFile) -> list[Violation]:
    out = []
    for number, line in enumerate(sf.lines, start=1):
        if any(pattern.search(line) for pattern in _SECRETS):
            out.append(Violation("secret", sf.rel, number, "looks like a hard-coded secret"))
    return out


def _const_str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _chain_reads_table(node, table):
    """True if the call chain below `node` contains .table(<table>) or .from_(<table>)."""
    while isinstance(node, (ast.Call, ast.Attribute)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("table", "from_")
            and node.args
            and _const_str(node.args[0]) == table
        ):
            return True
        node = node.func if isinstance(node, ast.Call) else node.value
    return False


def _is_stores_select_all(node):
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    if node.func.attr != "select":
        return False
    wants_all = not node.args or _const_str(node.args[0]) == "*"
    return wants_all and _chain_reads_table(node.func.value, "stores")


def check_select_star(sf: SourceFile) -> list[Violation]:
    out = []
    for node in ast.walk(sf.tree):
        text = _const_str(node)
        if text is not None and _RAW_SELECT.search(text):
            out.append(Violation("select-star", sf.rel, node.lineno, "raw 'select *' on stores"))
        elif _is_stores_select_all(node):
            message = "select('*') on stores pulls secrets; name the columns"
            out.append(Violation("select-star", sf.rel, node.lineno, message))
    return out


def _names_in(args):
    found = set()
    for arg in args:
        for node in ast.walk(arg):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
    return found


def check_log_text(sf: SourceFile) -> list[Violation]:
    out = []
    for node in ast.walk(sf.tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr in LOG_METHODS and _names_in(node.args) & SENSITIVE_NAMES:
            message = "customer text or payload may reach the log"
            out.append(Violation("log-text", sf.rel, node.lineno, message, "warn"))
    return out


def check_naming(sf: SourceFile, layer) -> list[Violation]:
    out = []
    stem = Path(sf.rel).stem
    if not _MODULE_NAME.match(stem):
        out.append(Violation("naming", sf.rel, 1, f"module name '{stem}' must be snake_case"))
    if layer != "test" or not stem.startswith("test_") or sf.tree is None:
        return out
    for node in ast.walk(sf.tree):
        if isinstance(node, FUNC_TYPES) and node.name.startswith("test"):
            if not _TEST_NAME.match(node.name):
                message = f"test '{node.name}' must be named test_<unit>__<behavior>"
                out.append(Violation("naming", sf.rel, node.lineno, message))
    return out


def check_content_rules(sf: SourceFile, layer) -> list[Violation]:
    out = check_todo(sf) + check_secrets(sf) + check_naming(sf, layer)
    if sf.tree is not None and sf.rel.startswith(V2_ROOT):
        out += check_select_star(sf) + check_log_text(sf)
    return out
