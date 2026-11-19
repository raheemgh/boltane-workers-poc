"""check_conventions - the V2 conventions checker: runs every rule and generates the docs.

Layer:   tooling
Stage:   V2-00B
Flags:   none
Raises:  never
Budget:  none
Tests:   tests/v2/conventions/test_check_conventions.py
"""

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import conv_docs
import conv_flags
import conv_layout
from conv_config import RULES, STAGE_PATTERN, V2_ROOT
from conv_header import check_header
from conv_layers import check_imports, check_v1_side
from conv_metrics import check_metrics
from conv_model import Allow, SourceFile, Violation, classify_path, is_governed, load_source
from conv_rules_code import check_code_rules
from conv_rules_content import check_content_rules

_COMMIT = re.compile(rf"^{STAGE_PATTERN}: \S")
_COMMIT_FREE = ("Merge ", "Revert ", "fixup! ", "squash! ")
_ALLOW_HELP = "use: # conv: allow <limit-rule> - <reason> (V2-xx)"


@dataclass
class Report:
    violations: list[Violation] = field(default_factory=list)
    allowed: list[tuple[Violation, Allow]] = field(default_factory=list)
    scanned: int = 0

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "error"]


def discover(root: Path) -> list[str]:
    """Repo-relative paths of every governed Python file."""
    found = set()
    for prefix in ("src/app/v2", "tests/v2", "tooling"):
        base = root / prefix
        if base.is_dir():
            found.update(p.relative_to(root).as_posix() for p in base.rglob("*.py"))
    return sorted(rel for rel in found if is_governed(rel) and "__pycache__" not in rel)


def discover_legacy(root: Path) -> list[str]:
    """V1 Python files (only their imports of V2 are checked)."""
    base = root / "src" / "app"
    rels = [p.relative_to(root).as_posix() for p in base.rglob("*.py")] if base.is_dir() else []
    if (root / "src" / "entry.py").is_file():
        rels.append("src/entry.py")
    return sorted(r for r in rels if not r.startswith(V2_ROOT) and "__pycache__" not in r)


def _siblings(root: Path, rel: str) -> frozenset[str]:
    return frozenset(p.stem for p in (root / Path(rel).parent).glob("*.py"))


def file_violations(root: Path, sf: SourceFile) -> list[Violation]:
    if sf.tree is None:
        return [Violation("syntax", sf.rel, 1, sf.syntax_error)]
    layer, area = classify_path(sf.rel)
    out = check_header(sf, layer, area, root)
    out += check_imports(sf, layer, area, _siblings(root, sf.rel))
    out += check_metrics(sf) + check_code_rules(sf, layer) + check_content_rules(sf, layer)
    if sf.rel.startswith(V2_ROOT) and layer is None and not sf.rel.endswith("__init__.py"):
        out.append(Violation("folder-map", sf.rel, 1, "module is outside the layer/domain folders"))
    return out


def _matching_allow(violation: Violation, sf: SourceFile | None) -> Allow | None:
    for allow in sf.allows if sf else []:
        if allow.rule == violation.rule and allow.line in (violation.line, violation.line - 1):
            return allow
    return None


def apply_allows(violations, sources):
    """Split violations into (kept, [(violation, allow)]) and add allow-comment problems."""
    kept, used, used_lines = [], [], set()
    for violation in violations:
        allow = _matching_allow(violation, sources.get(violation.path))
        if allow is None:
            kept.append(violation)
        else:
            used.append((violation, allow))
            used_lines.add((violation.path, allow.line))
    for rel, sf in sources.items():
        kept += [
            Violation("allow-unused", rel, a.line, f"allow '{a.rule}' matches nothing", "warn")
            for a in sf.allows
            if (rel, a.line) not in used_lines
        ]
        kept += [
            Violation("allow-syntax", rel, line, _ALLOW_HELP)
            for line, _text in sf.bad_allows
        ]
    return kept, used


def _order(violation: Violation) -> tuple[str, int, str]:
    return violation.path, violation.line, violation.rule


def _order_used(pair: tuple[Violation, Allow]) -> tuple[str, int, str]:
    return _order(pair[0])


def run_checks(root: Path) -> Report:
    rels = discover(root)
    sources = {rel: load_source(root, rel) for rel in rels}
    found = []
    for sf in sources.values():
        found += file_violations(root, sf)
    found += conv_flags.check_flags(sources) + conv_layout.check_layout(root, sources)
    for rel in discover_legacy(root):
        found += check_v1_side(load_source(root, rel))
    found += conv_docs.check_flat_names(root) + conv_docs.docs_violations(root, sources)
    kept, used = apply_allows(found, sources)
    return Report(sorted(kept, key=_order), sorted(used, key=_order_used), len(rels))


def render(report: Report, root: Path) -> list[str]:
    lines = [f"conventions check: root={root} governed-files={report.scanned}"]
    for v in report.violations:
        tag = "ERROR" if v.severity == "error" else "WARN "
        lines.append(f"{tag} {v.path}:{v.line} [{v.rule}] {v.message}")
    for v, allow in report.allowed:
        lines.append(f"ALLOW {v.path}:{allow.line} [{allow.rule}] {allow.reason} ({allow.stage})")
    errors = len(report.errors)
    lines.append(
        f"summary: errors={errors} warnings={len(report.violations) - errors} "
        f"allowed={len(report.allowed)}"
    )
    return lines


def check_commit_message(path: str) -> list[str]:
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            if _COMMIT.match(line) or line.startswith(_COMMIT_FREE):
                return []
            return [f"commit message must start with 'V2-xx: <what changed>', got {line[:60]!r}"]
    return ["empty commit message"]


def _emit(lines) -> None:
    for line in lines:
        print(line)


def cmd_check(root: Path, quiet: bool) -> int:
    report = run_checks(root)
    if report.errors or not quiet:
        _emit(render(report, root))
    return 1 if report.errors else 0


def cmd_write_docs(root: Path, quiet: bool) -> int:
    sources = {rel: load_source(root, rel) for rel in discover(root)}
    written = conv_docs.write_docs(root, sources)
    if not quiet:
        print("wrote " + ", ".join(written))
    return 0


def cmd_commit_msg(path: str) -> int:
    problems = check_commit_message(path)
    _emit(f"ERROR [commit-msg] {p}" for p in problems)
    return 1 if problems else 0


def cmd_verify(directory: Path) -> int:
    lines, code = conv_docs.verify_knowledge(directory)
    _emit(lines)
    return code


def cmd_export(root: Path, directory: Path, only) -> int:
    count = conv_docs.export_knowledge(root, directory, only)
    print(f"exported {count} file(s) to {directory}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="check_conventions", description="V2 conventions checker")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument(
        "--write-docs", action="store_true", help="regenerate docs/INDEX.md and KNOWLEDGE_MAP.md"
    )
    parser.add_argument("--rules", action="store_true", help="list rule names")
    parser.add_argument("--commit-msg", metavar="FILE", help="validate a commit message file")
    parser.add_argument("--verify-knowledge", metavar="DIR", type=Path)
    parser.add_argument("--export-knowledge", metavar="DIR", type=Path)
    parser.add_argument("--only", nargs="*", default=[], help="flat-name patterns for the export")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.rules:
        _emit(f"{name:14} {text}" for name, text in RULES.items())
        return 0
    if args.commit_msg:
        return cmd_commit_msg(args.commit_msg)
    if args.verify_knowledge:
        return cmd_verify(args.verify_knowledge)
    root = args.root.resolve()
    if args.write_docs:
        return cmd_write_docs(root, args.quiet)
    if args.export_knowledge:
        return cmd_export(root, args.export_knowledge, args.only)
    return cmd_check(root, args.quiet)


if __name__ == "__main__":
    sys.exit(main())
