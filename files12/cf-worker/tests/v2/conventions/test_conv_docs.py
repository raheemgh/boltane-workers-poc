"""test_conv_docs - flat names, generated docs, Knowledge export and verify.

Level:   L1
Stage:   V2-00B
"""

import hashlib
import tempfile
import unittest
from pathlib import Path

import check_conventions
import conv_docs
from conv_model import load_source
from conv_testkit import BASE, write_tree

FLAT_CASES = {
    "src/app/v2/gate/session.py": "v2_gate__session.py",
    "tests/v2/gate/test_session.py": "test_v2_gate__session.py",
    "sql/migrations/001_x.sql": "v2_sql__001_x.sql",
    "sql/rollbacks/001_x.sql": "v2_sql__rollback__001_x.sql",
    "sql/tests/001_x.sql": "v2_sql__test__001_x.sql",
    "tooling/conventions/check_conventions.py": "v2_tooling__check_conventions.py",
    "tooling/local-test-support/memory_supabase.py": None,
    "tooling/local-test-support/v2_memory_db.py": "v2_tooling__v2_memory_db.py",
    "contracts/store_config.json": "v2_contracts__store_config.json",
    "docs/stages/V2-01.md": "V2-01.md",
    "AGENTS.md": "AGENTS.md",
    "src/app/routers/webhook.py": "cfw_routers_webhook.py",
    "src/app/worker_ctx.py": "worker_ctx.py",
    "src/app/v2/gate/__init__.py": None,
    "docs/adr/.gitkeep": None,
    "docs/archive/knowledge-2026-11/v2-conventions.md": None,
    "tests/v2/gate/helper.py": None,
}


def make_repo(root: Path, files=None):
    write_tree(root, {**BASE, **(files or {})})
    sources = {rel: load_source(root, rel) for rel in check_conventions.discover(root)}
    conv_docs.write_docs(root, sources)
    return sources


class DocsTests(unittest.TestCase):
    def test_flat_name__follows_the_section_8_table(self):
        for rel, expected in FLAT_CASES.items():
            with self.subTest(rel):
                self.assertEqual(expected, conv_docs.flat_name(rel))

    def test_docs__generation_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = make_repo(root)
            first = [(root / p).read_bytes() for p in (conv_docs.INDEX_PATH, conv_docs.MAP_PATH)]
            conv_docs.write_docs(root, sources)
            second = [(root / p).read_bytes() for p in (conv_docs.INDEX_PATH, conv_docs.MAP_PATH)]
            self.assertEqual(first, second)

    def test_docs__index_lists_modules_not_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(Path(tmp))
            text = (Path(tmp) / conv_docs.INDEX_PATH).read_text(encoding="utf-8")
            rows = [line for line in text.splitlines() if line.startswith("| src/")]
            self.assertEqual(1, len(rows))
            self.assertIn("src/app/v2/core/sample.py", rows[0])
            self.assertFalse(any(line.startswith("| tests/") for line in text.splitlines()))

    def test_docs__map_hashes_v1_files_under_their_cfw_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(Path(tmp), {"src/app/main.py": "x = 1\n"})
            rows = conv_docs.parse_map((Path(tmp) / conv_docs.MAP_PATH).read_text(encoding="utf-8"))
            digest = hashlib.sha256(b"x = 1\n").hexdigest()[:12]
            self.assertIn(("cfw_main.py", "src/app/main.py", digest), rows)

    def test_docs__stale_after_a_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = make_repo(root)
            (root / "src/app/v2/core/sample.py").write_text("changed\n", encoding="utf-8")
            rules = {v.rule for v in conv_docs.docs_violations(root, sources)}
            self.assertEqual({"stale-docs"}, rules)

    def test_docs__parse_map_ignores_headings_and_separators(self):
        text = "# t\n\n| a | b | c |\n|---|---|---|\n| x.py | p/x.py | 0123456789ab |\n"
        self.assertEqual([("x.py", "p/x.py", "0123456789ab")], conv_docs.parse_map(text))


class KnowledgeTests(unittest.TestCase):
    def test_knowledge__export_then_verify_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "repo", Path(tmp) / "flat"
            make_repo(root)
            count = conv_docs.export_knowledge(root, out)
            lines, code = conv_docs.verify_knowledge(out)
            self.assertGreater(count, 2)
            self.assertEqual(0, code)
            self.assertIn("different=0", lines[-1])

    def test_knowledge__verify_flags_a_stale_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "repo", Path(tmp) / "flat"
            make_repo(root)
            conv_docs.export_knowledge(root, out)
            (out / "v2_core__sample.py").write_text("old copy\n", encoding="utf-8")
            lines, code = conv_docs.verify_knowledge(out)
            self.assertEqual(1, code)
            self.assertTrue(any(line.startswith("DIFFERENT v2_core__sample.py") for line in lines))

    def test_knowledge__export_only_keeps_matching_names_and_the_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "repo", Path(tmp) / "flat"
            make_repo(root)
            conv_docs.export_knowledge(root, out, only=["v2_core__*"])
            self.assertEqual(
                ["KNOWLEDGE_MAP.md", "v2_core__sample.py"], sorted(p.name for p in out.iterdir())
            )

    def test_knowledge__verify_needs_the_map_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            _lines, code = conv_docs.verify_knowledge(Path(tmp))
            self.assertEqual(1, code)


if __name__ == "__main__":
    unittest.main()
