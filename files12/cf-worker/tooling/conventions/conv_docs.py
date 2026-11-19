"""conv_docs - generates docs/INDEX.md and docs/KNOWLEDGE_MAP.md, checks them, exports Knowledge.

Layer:   tooling
Stage:   V2-00B
Flags:   none
Raises:  never
Budget:  none
Tests:   tests/v2/conventions/test_conv_docs.py
"""

import fnmatch
import hashlib
import re
import shutil
from pathlib import Path

from conv_config import V1_FLAT_NAMES
from conv_header import source_header
from conv_model import SourceFile, Violation, classify_path

INDEX_PATH = "docs/INDEX.md"
MAP_PATH = "docs/KNOWLEDGE_MAP.md"
NOTE = (
    "مولَّد آلياً بواسطة `python3 tooling/conventions/check_conventions.py --write-docs`؛ "
    "لا يُحرَّر يدوياً."
)
_SCAN_DIRS = ("src/app/v2", "tests/v2", "tooling", "sql", "contracts", "docs")
_SQL_PREFIX = {"migrations": "", "rollbacks": "rollback__", "tests": "test__"}


def _flat_tooling(parts, name):
    legacy = parts[1] == "local-test-support" and not name.startswith("v2_")
    if len(parts) == 3 and name.endswith(".py") and not legacy:
        return f"v2_tooling__{name}"
    return None


def _flat_sql(parts, name):
    if len(parts) == 3 and parts[1] in _SQL_PREFIX and name.endswith(".sql"):
        return f"v2_sql__{_SQL_PREFIX[parts[1]]}{name}"
    return None


def _flat_other(parts, name):
    top = parts[0]
    if top == "tooling":
        return _flat_tooling(parts, name)
    if top == "sql":
        return _flat_sql(parts, name)
    if top == "contracts" and len(parts) == 2:
        return f"v2_contracts__{name}"
    if name.endswith(".md") and (top == "docs" or parts == ["AGENTS.md"]):
        return name
    return None


def _skipped(parts, name):
    """Files that never get a flat name: caches, markers, and docs/archive (history)."""
    if name in ("__init__.py", ".gitkeep") or "__pycache__" in parts:
        return True
    return parts[:2] == ["docs", "archive"]


def flat_name(rel: str) -> str | None:
    """Flat Knowledge name for a repo path (section 8), or None if the file is not mapped."""
    if rel in V1_FLAT_NAMES:
        return V1_FLAT_NAMES[rel]
    parts = rel.split("/")
    name = parts[-1]
    if _skipped(parts, name):
        return None
    if parts[:3] == ["src", "app", "v2"] and len(parts) == 5 and name.endswith(".py"):
        return f"v2_{parts[3]}__{name}"
    is_test = name.startswith("test_") and name.endswith(".py")
    if parts[:2] == ["tests", "v2"] and len(parts) == 4 and is_test:
        return f"test_v2_{parts[2]}__{name[5:]}"
    return _flat_other(parts, name)


def candidate_paths(root: Path) -> list[str]:
    rels = set(V1_FLAT_NAMES) | {"AGENTS.md"}
    for directory in _SCAN_DIRS:
        base = root / directory
        if base.is_dir():
            rels.update(p.relative_to(root).as_posix() for p in base.rglob("*") if p.is_file())
    return sorted(rel for rel in rels if (root / rel).is_file())


def collect_entries(root: Path, include_map_file: bool = False) -> list[tuple[str, str, str]]:
    """Sorted (flat name, repo path, sha256[:12]) of every mapped file."""
    out = []
    for rel in candidate_paths(root):
        flat = flat_name(rel)
        if flat is None or (flat == "KNOWLEDGE_MAP.md" and not include_map_file):
            continue
        digest = hashlib.sha256((root / rel).read_bytes()).hexdigest()[:12]
        out.append((flat, rel, digest))
    return sorted(out)


def check_flat_names(root: Path) -> list[Violation]:
    seen: dict[str, list[str]] = {}
    for flat, rel, _sha in collect_entries(root, include_map_file=True):
        seen.setdefault(flat, []).append(rel)
    return [
        Violation("flat-name", rels[1], 1, f"flat name '{flat}' is also used by {rels[0]}")
        for flat, rels in sorted(seen.items())
        if len(rels) > 1
    ]


def _cell(value) -> str:
    return str(value).replace("|", "/")


def index_rows(sources: dict[str, SourceFile]) -> list[tuple]:
    rows = []
    for rel, sf in sorted(sources.items()):
        layer = classify_path(rel)[0]
        parsed = source_header(sf)
        if parsed is None or layer in (None, "test") or rel.endswith("__init__.py"):
            continue
        _name, purpose, fields = parsed
        columns = (fields.get(key, "?") for key in ("Layer", "Stage", "Flags", "Tests"))
        rows.append((rel, purpose, *columns))
    return rows


def render_index(sources: dict[str, SourceFile]) -> str:
    head = [
        "# فهرس الوحدات", "", NOTE, "",
        "| المسار | الغرض | الطبقة | المرحلة | الأعلام | ملف الاختبار |",
        "|---|---|---|---|---|---|",
    ]
    body = ["| " + " | ".join(_cell(c) for c in row) + " |" for row in index_rows(sources)]
    return "\n".join([*head, *body]) + "\n"


def render_map(entries) -> str:
    head = [
        "# خريطة Knowledge", "", NOTE, "",
        "| الاسم المسطّح | المسار (تحت cf-worker/) | sha256 |", "|---|---|---|",
    ]
    return "\n".join([*head, *(f"| {f} | {r} | {s} |" for f, r, s in entries)]) + "\n"


def write_docs(root: Path, sources: dict[str, SourceFile]) -> list[str]:
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / INDEX_PATH).write_text(render_index(sources), encoding="utf-8")
    (root / MAP_PATH).write_text(render_map(collect_entries(root)), encoding="utf-8")
    return [INDEX_PATH, MAP_PATH]


def docs_violations(root: Path, sources: dict[str, SourceFile]) -> list[Violation]:
    expected = {INDEX_PATH: render_index(sources), MAP_PATH: render_map(collect_entries(root))}
    out = []
    for rel, text in expected.items():
        path = root / rel
        if not path.is_file():
            state = "missing"
        elif path.read_text(encoding="utf-8") != text:
            state = "out of date"
        else:
            continue
        message = f"{state}; run check_conventions.py --write-docs"
        out.append(Violation("stale-docs", rel, 1, message))
    return out


def parse_map(text: str) -> list[tuple[str, str, str]]:
    rows = []
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 3 and re.fullmatch(r"[0-9a-f]{12}", cells[2]):
            rows.append((cells[0], cells[1], cells[2]))
    return rows


def verify_knowledge(directory: Path) -> tuple[list[str], int]:
    """Compare flat files in `directory` with the sha256 column of its KNOWLEDGE_MAP.md."""
    map_file = directory / "KNOWLEDGE_MAP.md"
    if not map_file.is_file():
        return [f"KNOWLEDGE_MAP.md not found in {directory}"], 1
    rows = parse_map(map_file.read_text(encoding="utf-8"))
    lines, counts = [], {"match": 0, "different": 0, "missing": 0}
    for flat, rel, sha in rows:
        path = directory / flat
        if not path.is_file():
            counts["missing"] += 1
        elif hashlib.sha256(path.read_bytes()).hexdigest()[:12] == sha:
            counts["match"] += 1
        else:
            counts["different"] += 1
            lines.append(f"DIFFERENT {flat} (repo path {rel}): the Knowledge copy is stale")
    known = {flat for flat, _rel, _sha in rows} | {"KNOWLEDGE_MAP.md"}
    extra = sorted(p.name for p in directory.iterdir() if p.is_file() and p.name not in known)
    lines += [f"EXTRA {name}: not in the map" for name in extra]
    lines.append(
        f"summary: match={counts['match']} different={counts['different']} "
        f"not-uploaded={counts['missing']} extra={len(extra)}"
    )
    return lines, 1 if counts["different"] else 0


def export_knowledge(root: Path, directory: Path, only=()) -> int:
    """Copy mapped files into `directory` under their flat names (map file always included)."""
    directory.mkdir(parents=True, exist_ok=True)
    count = 0
    for flat, rel, _sha in collect_entries(root, include_map_file=True):
        if flat == "KNOWLEDGE_MAP.md" or not only or any(fnmatch.fnmatch(flat, p) for p in only):
            shutil.copyfile(root / rel, directory / flat)
            count += 1
    return count
