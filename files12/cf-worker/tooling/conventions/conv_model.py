"""conv_model - data types, path classification and source loading for the checker.

Layer:   tooling
Stage:   V2-00B
Flags:   none
Raises:  never
Budget:  none
Tests:   tests/v2/conventions/test_conv_rules.py
"""

import ast
import io
import re
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

from conv_config import DOMAIN_DIRS, GOVERNED_PREFIXES, LAYER_DIRS, LIMITS, STAGE_PATTERN, V2_ROOT

FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)
_ALLOW = re.compile(rf"#\s*conv:\s*allow\s+(\S+)\s+-\s+(.+?)\s+\(({STAGE_PATTERN})\)\s*$")


@dataclass(frozen=True)
class Violation:
    rule: str
    path: str
    line: int
    message: str
    severity: str = "error"


@dataclass(frozen=True)
class Allow:
    rule: str
    reason: str
    stage: str
    line: int


@dataclass
class SourceFile:
    rel: str
    text: str
    lines: list[str]
    tree: ast.Module | None
    syntax_error: str = ""
    comments: list[tuple[int, str]] = field(default_factory=list)
    allows: list[Allow] = field(default_factory=list)
    bad_allows: list[tuple[int, str]] = field(default_factory=list)


def _comments(text: str) -> list[tuple[int, str]]:
    found = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                found.append((tok.start[0], tok.string))
    except (tokenize.TokenError, SyntaxError):
        pass
    return found


def _split_allows(comments):
    allows, bad = [], []
    for line, text in comments:
        if not re.match(r"#\s*conv:", text):
            continue
        match = _ALLOW.match(text)
        if match and match.group(1) in LIMITS:
            allows.append(Allow(match.group(1), match.group(2), match.group(3), line))
        else:
            bad.append((line, text))
    return allows, bad


def load_source(root: Path, rel: str) -> SourceFile:
    text = (root / rel).read_text(encoding="utf-8", errors="replace")
    try:
        tree, error = ast.parse(text, filename=rel), ""
    except SyntaxError as exc:
        tree, error = None, f"syntax error: {exc.msg} (line {exc.lineno})"
    comments = _comments(text)
    allows, bad = _split_allows(comments)
    return SourceFile(rel, text, text.splitlines(), tree, error, comments, allows, bad)


def classify_path(rel: str) -> tuple[str | None, str | None]:
    """(layer, area) implied by a repo path; layer is 'test' for tests/v2 files."""
    parts = rel.split("/")
    if rel.startswith(V2_ROOT) and len(parts) >= 5:
        folder = parts[3]
        if folder in LAYER_DIRS:
            return folder, folder
        if folder in DOMAIN_DIRS:
            return "domain", folder
        return None, folder
    if parts[0] == "tooling" and len(parts) >= 3:
        return "tooling", parts[1]
    if rel.startswith("tests/v2/") and len(parts) >= 4:
        return "test", parts[2]
    return None, None


def is_governed(rel: str) -> bool:
    return rel.endswith(".py") and rel.startswith(GOVERNED_PREFIXES)
