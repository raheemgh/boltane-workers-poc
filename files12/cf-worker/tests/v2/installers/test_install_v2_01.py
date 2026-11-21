"""test_install_v2_01 - tests for the V2-01 installer: plan, refusal, all-or-nothing, payload integrity.

Level:   L1
Stage:   V2-01
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import install_v2_01 as installer

FILES = {"a/one.txt": b"one", "a/b/two.txt": b"two", "three.txt": b""}


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "src" / "app").mkdir(parents=True)

    def run_quiet(self, files, dry_run=False):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = installer.run(files, self.root, dry_run)
        return code, out.getvalue()

    def test_install_v2_01__creates_files_then_reports_unchanged(self):
        code, text = self.run_quiet(FILES)
        self.assertEqual(code, 0)
        self.assertIn("created=3", text)
        for rel, data in FILES.items():
            self.assertEqual((self.root / rel).read_bytes(), data)
        code, text = self.run_quiet(FILES)
        self.assertEqual(code, 0)
        self.assertIn("created=0", text)
        self.assertIn("unchanged=3", text)

    def test_install_v2_01__dry_run_writes_nothing(self):
        code, text = self.run_quiet(FILES, dry_run=True)
        self.assertEqual(code, 0)
        self.assertIn("would_create=3", text)
        self.assertFalse((self.root / "a").exists())

    def test_install_v2_01__refuses_and_writes_nothing_when_a_file_differs(self):
        (self.root / "a").mkdir()
        (self.root / "a" / "one.txt").write_bytes(b"someone edited me")
        code, text = self.run_quiet(FILES)
        self.assertEqual(code, 1)
        self.assertIn("REFUSE a/one.txt", text)
        self.assertEqual((self.root / "a" / "one.txt").read_bytes(), b"someone edited me")
        self.assertFalse((self.root / "three.txt").exists())
        self.assertFalse((self.root / "a" / "b").exists())

    def test_install_v2_01__a_failed_write_undoes_everything(self):
        (self.root / "blocker").write_bytes(b"i am a file, not a folder")
        files = {"first.txt": b"1", "blocker/second.txt": b"2"}
        with self.assertRaises(OSError):
            with contextlib.redirect_stdout(io.StringIO()):
                installer.run(files, self.root, False)
        self.assertFalse((self.root / "first.txt").exists())
        self.assertEqual([p.name for p in self.root.rglob("*.v2tmp")], [])

    def test_install_v2_01__refuses_a_root_that_is_not_cf_worker(self):
        with tempfile.TemporaryDirectory() as other:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(installer.run(FILES, Path(other), False), 1)
            self.assertEqual(list(Path(other).iterdir()), [])

    def test_install_v2_01__payload_is_intact_and_complete(self):
        files = installer.load_payload()
        for rel in ("src/app/v2/core/flags.py", "src/app/v2/core/errors.py", "sql/migrations/001_core_schema.sql",
                    "sql/rollbacks/001_core_schema.sql", "contracts/store_config.schema.json", "docs/stages/V2-01.md",
                    "tests/v2/installers/test_install_v2_01.py", "src/app/v2/__init__.py"):
            self.assertIn(rel, files)
        self.assertEqual(files["src/app/v2/__init__.py"], b"")
        self.assertFalse([rel for rel in files if rel.startswith("tooling/")])


if __name__ == "__main__":
    unittest.main()
