"""conv_flags - feature-flag rules: registry shape, registered use, header Flags (5.7).

Layer:   tooling
Stage:   V2-00B
Flags:   none
Raises:  never
Budget:  none
Tests:   tests/v2/conventions/test_conv_rules.py
"""

import ast
import re

from conv_config import FLAG_CALLS, STAGE_PATTERN, V2_ROOT
from conv_header import source_header
from conv_model import SourceFile, Violation

# Contract for V2-01: core/flags.py registers each flag as Flag(name, default, description, stage).
REGISTRY = f"{V2_ROOT}core/flags.py"
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_STAGE = re.compile(rf"^{STAGE_PATTERN}$")
_FIELDS = ("name", "default", "description", "stage")


def _call_name(call):
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def _literal(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return None


def _entry_problems(values):
    problems = []
    if not (isinstance(values.get("name"), str) and _NAME.match(values["name"])):
        problems.append("flag name must be lower_snake")
    if values.get("default") is not False:
        problems.append("default must be the literal False")
    if not (isinstance(values.get("description"), str) and values["description"].strip()):
        problems.append("description is required")
    if not (isinstance(values.get("stage"), str) and _STAGE.match(values["stage"])):
        problems.append("owner stage is required (V2-xx)")
    return problems


def registry_entries(tree):
    """{flag name: [problems]} for every Flag(name, default, description, stage) call."""
    entries = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _call_name(node) == "Flag"):
            continue
        values = dict(zip(_FIELDS, map(_literal, node.args), strict=False))
        values.update({kw.arg: _literal(kw.value) for kw in node.keywords if kw.arg})
        entries[str(values.get("name"))] = _entry_problems(values)
    return entries


def used_flags(tree):
    """(line, flag name) for each string literal passed to a flag-check call."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) in FLAG_CALLS:
            args = [*node.args, *(kw.value for kw in node.keywords)]
            found += [
                (node.lineno, arg.value)
                for arg in args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
    return found


def declared_flags(sf: SourceFile) -> list[str]:
    parsed = source_header(sf)
    raw = parsed[2].get("Flags", "none") if parsed else "none"
    if raw.strip().lower() == "none":
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _registry_violations(registry_file):
    if registry_file is None or registry_file.tree is None:
        return {}, []
    registry = registry_entries(registry_file.tree)
    out = []
    for name, problems in sorted(registry.items()):
        out += [Violation("flag", REGISTRY, 1, f"flag '{name}': {text}") for text in problems]
    return registry, out


def _usage_violations(sf, registry):
    out = [
        Violation("flag", sf.rel, 1, f"header Flags names unregistered flag '{name}'")
        for name in declared_flags(sf)
        if name not in registry
    ]
    if sf.rel.startswith(V2_ROOT):
        out += [
            Violation("flag", sf.rel, line, f"unregistered flag '{name}'")
            for line, name in used_flags(sf.tree)
            if name not in registry
        ]
    return out


def check_flags(sources: dict[str, SourceFile]) -> list[Violation]:
    registry, out = _registry_violations(sources.get(REGISTRY))
    for rel, sf in sorted(sources.items()):
        if sf.tree is None or rel == REGISTRY or rel.startswith("tests/"):
            continue
        out += _usage_violations(sf, registry)
    return out
