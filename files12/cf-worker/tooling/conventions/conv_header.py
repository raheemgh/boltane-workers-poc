"""conv_header - checks the mandatory module docstring header (section 2).

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

from conv_config import MODULE_FIELDS, STAGE_PATTERN, TEST_FIELDS, TEST_LEVELS
from conv_model import SourceFile, Violation

_FIRST = re.compile(r"^([a-z][a-z0-9_]*) - (\S.*)$")
_FIELD = re.compile(r"^([A-Za-z]+):\s+(\S.*)$")
_STAGES = re.compile(rf"^{STAGE_PATTERN}(, {STAGE_PATTERN})*$")


def parse_header(doc: str | None) -> tuple[str, str, dict[str, str]] | None:
    """Split a module docstring into (name, purpose, fields); None if malformed."""
    if not doc:
        return None
    lines = doc.splitlines()
    first = _FIRST.match(lines[0])
    if first is None:
        return None
    fields = {}
    for line in lines[1:]:
        match = _FIELD.match(line)
        if match:
            fields[match.group(1)] = match.group(2).strip()
    return first.group(1), first.group(2), fields


def source_header(sf: SourceFile):
    """parse_header() of a loaded source file (None if it has no usable header)."""
    if sf.tree is None:
        return None
    return parse_header(ast.get_docstring(sf.tree, clean=True))


def _tests_problem(rel, layer, area, value, root):
    expected = f"tests/v2/{area}/test_{Path(rel).stem}.py"
    if layer == "tooling":
        if not value.startswith("tests/v2/"):
            return f"Tests '{value}' must be under tests/v2/"
    elif value != expected:
        return f"Tests must be '{expected}', got '{value}'"
    if not (root / value).is_file():
        return f"Tests file '{value}' does not exist"
    return None


def _value_problems(rel, layer, area, fields, root):
    problems = []
    stage = fields.get("Stage")
    if stage is not None and not _STAGES.match(stage):
        problems.append(f"Stage '{stage}' must look like V2-01 or V2-01, V2-02")
    if layer == "test":
        level = fields.get("Level")
        if level is not None and level not in TEST_LEVELS:
            problems.append(f"Level '{level}' must be one of {sorted(TEST_LEVELS)}")
        return problems
    if layer is None:
        return problems
    if fields.get("Layer") not in (None, layer):
        problems.append(f"Layer '{fields['Layer']}' but the path says '{layer}'")
    if "Tests" in fields:
        problem = _tests_problem(rel, layer, area, fields["Tests"], root)
        if problem:
            problems.append(problem)
    return problems


def check_header(sf: SourceFile, layer, area, root: Path) -> list[Violation]:
    if sf.tree is None or (sf.rel.endswith("__init__.py") and not sf.tree.body):
        return []
    parsed = source_header(sf)
    if parsed is None:
        return [Violation("header", sf.rel, 1, "docstring must start with '<name> - <purpose>'")]
    name, _purpose, fields = parsed
    problems = []
    stem = Path(sf.rel).stem
    if name != stem:
        problems.append(f"header name '{name}' must equal the file name '{stem}'")
    required = TEST_FIELDS if layer == "test" else MODULE_FIELDS
    missing = [item for item in required if item not in fields]
    if missing:
        problems.append("missing header field(s): " + ", ".join(missing))
    problems += _value_problems(sf.rel, layer, area, fields, root)
    return [Violation("header", sf.rel, 1, problem) for problem in problems]
