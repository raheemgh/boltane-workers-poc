"""conv_testkit - builds throw-away repo trees so the checker's rules can be tested.

Layer:   tooling
Stage:   V2-00B
Flags:   none
Raises:  never
Budget:  none
Tests:   tests/v2/conventions/test_conv_rules.py
"""

import tempfile
from pathlib import Path

import check_conventions
import conv_docs
from conv_config import LAYER_DIRS
from conv_model import load_source


def module_text(name, layer, tests, body="", prefix="", **over):
    """A V2 module source with a valid header; pass Field=None to drop a header field."""
    fields = {"Layer": layer, "Stage": "V2-00B", "Flags": "none", "Raises": "never",
              "Budget": "none", "Tests": tests}
    fields.update(over)
    lines = [f'"""{name} - sample module', ""]
    lines += [f"{key}: {value}" for key, value in fields.items() if value is not None]
    return prefix + "\n".join([*lines, '"""', "", body, ""])


def suite_text(name, body="", level="L1"):
    return f'"""{name} - sample tests\n\nLevel:   {level}\nStage:   V2-00B\n"""\n\n{body}\n'


def module_pair(area, name, body="", prefix="", **over):
    """{path: text} for src/app/v2/<area>/<name>.py and its test stub."""
    layer = area if area in LAYER_DIRS else "domain"
    tests = f"tests/v2/{area}/test_{name}.py"
    return {
        f"src/app/v2/{area}/{name}.py": module_text(name, layer, tests, body, prefix, **over),
        tests: suite_text(f"test_{name}"),
    }


BASE = module_pair("core", "sample", "VALUE = 1")


def write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def run_tree(files, regenerate=True):
    """Build BASE + files (None deletes a BASE entry), regenerate docs, run every rule."""
    merged = {k: v for k, v in {**BASE, **files}.items() if v is not None}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_tree(root, merged)
        if regenerate:
            sources = {rel: load_source(root, rel) for rel in check_conventions.discover(root)}
            conv_docs.write_docs(root, sources)
        return check_conventions.run_checks(root)
