"""conv_layers - checks that imports respect the layer table (section 3).

Layer:   tooling
Stage:   V2-00B
Flags:   none
Raises:  never
Budget:  none
Tests:   tests/v2/conventions/test_conv_rules.py
"""

import ast
import sys

from conv_config import ALLOWED_IMPORTS, DOMAIN_DIRS, LAYER_DIRS
from conv_model import FUNC_TYPES, SourceFile, Violation

_STDLIB = sys.stdlib_module_names


def iter_imports(node, in_func=False):
    """Yield (import node, inside_function) for every import under `node`."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.Import, ast.ImportFrom)):
            yield child, in_func
        else:
            yield from iter_imports(child, in_func or isinstance(child, FUNC_TYPES))


def package_parts(rel: str) -> list[str]:
    parts = rel.split("/")
    return parts[1:-1] if parts[0] == "src" else parts[:-1]


def import_targets(node, pkg):
    """Absolute dotted module names named by one import statement."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        anchor = pkg[: len(pkg) - (node.level - 1)]
        base = ".".join([*anchor, *([node.module] if node.module else [])])
    if base in ("app", "app.v2"):
        return [f"{base}.{alias.name}" for alias in node.names]
    return [base] if base else []


def classify(target, siblings):
    """(kind, detail): kind is v2, v1, std, local (sibling tooling module) or ext."""
    top = target.split(".")[0]
    if target == "app.v2" or target.startswith("app.v2."):
        parts = target.split(".")
        return "v2", (parts[2] if len(parts) > 2 else "")
    if top == "app":
        return "v1", target
    if top in _STDLIB:
        return "std", ""
    if top in siblings:
        return "local", ""
    return "ext", top


def _judge_v2(rel, layer, area, detail):
    if detail == "":
        return None
    if detail in LAYER_DIRS:
        target_layer = detail
    elif detail in DOMAIN_DIRS:
        target_layer = "domain"
    else:
        return f"unknown V2 folder '{detail}'"
    if layer == "pipeline" and rel.endswith("pipeline/build.py") and target_layer == "adapters":
        return None
    if layer == "domain" and target_layer == "domain":
        return None if detail == area else f"domain '{area}' must not import domain '{detail}'"
    if target_layer in ALLOWED_IMPORTS[layer]:
        return None
    return f"{layer} must not import {target_layer} ('{detail}')"


def judge(rel, layer, area, target, in_func, siblings):
    """Reason why `layer` may not import `target`, or None if it may."""
    kind, detail = classify(target, siblings)
    if kind == "std" or (kind == "local" and layer == "tooling"):
        return None
    if layer == "tooling":
        return f"tooling is stdlib-only, got '{target}'"
    if kind == "ext":
        if layer == "adapters" and in_func:
            return None
        return f"external import '{target}' is allowed only inside functions in adapters"
    if kind == "v1":
        if rel.endswith("adapters/v1_compat.py"):
            return None
        return f"V1 import '{target}' is allowed only in adapters/v1_compat.py"
    if kind == "local":
        return f"unknown module '{target}'"
    return _judge_v2(rel, layer, area, detail)


def check_imports(sf: SourceFile, layer, area, siblings) -> list[Violation]:
    if sf.tree is None or not (layer in ALLOWED_IMPORTS or layer == "tooling"):
        return []
    pkg = package_parts(sf.rel)
    out = []
    for node, in_func in iter_imports(sf.tree):
        for target in import_targets(node, pkg):
            message = judge(sf.rel, layer, area, target, in_func, siblings)
            if message:
                out.append(Violation("layer-import", sf.rel, node.lineno, message))
    return out


def check_v1_side(sf: SourceFile) -> list[Violation]:
    """V1 code may import only app.v2.pipeline (section 3, last row)."""
    if sf.tree is None:
        return []
    pkg = package_parts(sf.rel)
    out = []
    for node, _in_func in iter_imports(sf.tree):
        for target in import_targets(node, pkg):
            kind, detail = classify(target, frozenset())
            if kind == "v2" and detail not in ("", "pipeline"):
                message = f"V1 may import only app.v2.pipeline, got '{target}'"
                out.append(Violation("layer-import", sf.rel, node.lineno, message))
    return out
