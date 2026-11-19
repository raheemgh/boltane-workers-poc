"""conv_layout - repo layout rules: folders inside the map, SQL migrations paired with rollbacks.

Layer:   tooling
Stage:   V2-00B
Flags:   none
Raises:  never
Budget:  none
Tests:   tests/v2/conventions/test_conv_rules.py
"""

import re
from pathlib import Path

from conv_config import ALLOWED_SUBDIRS
from conv_model import SourceFile, Violation

_MIGRATION = re.compile(r"^\d{3}_[a-z0-9_]+\.sql$")


def _folder_violations(root: Path):
    out = []
    for parent, allowed in sorted(ALLOWED_SUBDIRS.items()):
        base = root / parent
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            hidden = child.name.startswith((".", "__"))
            if child.is_dir() and child.name not in allowed and not hidden:
                message = "folder is not in the repo map (section 1); add it through an ADR first"
                out.append(Violation("folder-map", f"{parent}/{child.name}", 1, message))
    return out


def _sql_violations(root: Path):
    migrations = root / "sql" / "migrations"
    if not migrations.is_dir():
        return []
    out = []
    for path in sorted(migrations.glob("*.sql")):
        rel = f"sql/migrations/{path.name}"
        if not _MIGRATION.match(path.name):
            out.append(Violation("naming", rel, 1, "migration must be named NNN_name.sql"))
        elif not (root / "sql" / "rollbacks" / path.name).is_file():
            out.append(Violation("sql-pair", rel, 1, "no rollback with the same name"))
    return out


def check_layout(root: Path, _sources: dict[str, SourceFile]) -> list[Violation]:
    return _folder_violations(root) + _sql_violations(root)
