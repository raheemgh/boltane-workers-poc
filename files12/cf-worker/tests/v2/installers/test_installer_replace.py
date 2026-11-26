"""test_installer_replace - an installer may replace a listed file whose content is exactly the expected old one.

Level:   L1
Stage:   V2-02B
"""

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build_installer as builder
import installer_template as template

OLD, NEW = b"old v1 file\n", b"new v1 file\n"


def sha12(data):
    return hashlib.sha256(data).hexdigest()[:12]


class ReplaceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / "a.py").write_bytes(OLD)
        self.lines = []

    def tearDown(self):
        self.dir.cleanup()

    def install(self, files, expected):
        with mock.patch.object(template, "EXPECTED_OLD", expected):
            return template.install(files, self.root, None, out=self.lines.append)

    def test_replace__a_listed_file_with_the_expected_old_content_is_replaced(self):
        code = self.install({"a.py": NEW, "b.py": b"brand new\n"}, {"a.py": sha12(OLD)})
        self.assertEqual(code, 0)
        self.assertEqual((self.root / "a.py").read_bytes(), NEW)
        self.assertEqual((self.root / "b.py").read_bytes(), b"brand new\n")
        self.assertIn(f"REPLACED a.py (sha256 {sha12(OLD)} -> {sha12(NEW)}; git checkout restores the old file)", self.lines)
        self.assertTrue(self.lines[-1].endswith("replaced=1"), self.lines[-1])
        self.assertEqual(list(self.root.glob("*.installing")), [])

    def test_replace__a_listed_file_with_any_other_content_is_a_conflict_and_nothing_is_written(self):
        (self.root / "a.py").write_bytes(b"someone edited this\n")
        code = self.install({"a.py": NEW, "b.py": b"brand new\n"}, {"a.py": sha12(OLD)})
        self.assertEqual(code, 1)
        self.assertEqual((self.root / "a.py").read_bytes(), b"someone edited this\n")
        self.assertFalse((self.root / "b.py").exists())
        self.assertTrue(any(line.startswith("CONFLICT a.py") and sha12(OLD) in line for line in self.lines))

    def test_replace__a_file_that_is_not_listed_is_still_a_conflict(self):
        self.assertEqual(self.install({"a.py": NEW}, {}), 1)
        self.assertEqual((self.root / "a.py").read_bytes(), OLD)
        self.assertEqual(self.lines, ["CONFLICT a.py: exists and differs; nothing was written"])

    def test_replace__running_again_finds_the_new_content_identical(self):
        self.install({"a.py": NEW}, {"a.py": sha12(OLD)})
        self.lines.clear()
        self.assertEqual(self.install({"a.py": NEW}, {"a.py": sha12(OLD)}), 0)
        self.assertEqual(self.lines, ["SUMMARY written=0 identical=1 conflicts=0"])

    def test_replace__a_failure_part_way_restores_the_replaced_file_and_removes_the_created_one(self):
        (self.root / "blocker").write_bytes(b"a file where a folder is needed\n")
        files = {"a.py": NEW, "b.py": b"created\n", "blocker/c.py": b"cannot be written\n"}
        code = self.install(files, {"a.py": sha12(OLD)})
        self.assertEqual(code, 1)
        self.assertEqual((self.root / "a.py").read_bytes(), OLD)
        self.assertFalse((self.root / "b.py").exists())
        self.assertTrue(any("rolled back" in line for line in self.lines))

    def test_replace__without_a_list_write_all_still_refuses_to_touch_an_existing_file(self):
        with mock.patch.object(template, "EXPECTED_OLD", {}):
            error = template.write_all([("a.py", self.root / "a.py", NEW)])
        self.assertIn("write failed", error)
        self.assertEqual((self.root / "a.py").read_bytes(), OLD)


class BuilderTest(unittest.TestCase):
    def build(self, tree, replace=()):
        src = Path(self.dir.name) / "src"
        for rel, data in tree.items():
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_bytes(data)
        out = Path(self.dir.name) / "install_v2_99.py"
        args = ["--stage", "V2-99", "--src", str(src), "--out", str(out)]
        code = builder.main(args + (["--replace-old", *replace] if replace else []))
        return code, out

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def test_builder__the_installer_it_writes_replaces_the_old_file_and_runs_end_to_end(self):
        code, out = self.build({"a.py": NEW, "n/new.py": b"x\n"}, [f"a.py={sha12(OLD)}"])
        self.assertEqual(code, 0)
        self.assertIn(f"{{'a.py': '{sha12(OLD)}'}}", out.read_text(encoding="utf-8"))
        target = Path(self.dir.name) / "repo"
        target.mkdir()
        (target / "a.py").write_bytes(OLD)
        run = subprocess.run([sys.executable, str(out), "--root", str(target), "--git-root", str(target), "--no-check"],
                             capture_output=True, text=True, check=False)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("REPLACED a.py", run.stdout)
        self.assertEqual((target / "a.py").read_bytes(), NEW)
        again = subprocess.run([sys.executable, str(out), "--root", str(target), "--git-root", str(target), "--no-check"],
                               capture_output=True, text=True, check=False)
        self.assertEqual(again.returncode, 0)
        self.assertIn("identical=2", again.stdout)

    def test_builder__without_replace_old_the_installer_is_as_before(self):
        code, out = self.build({"a.py": NEW})
        self.assertEqual(code, 0)
        self.assertIn("EXPECTED_OLD: dict[str, str] = {}", out.read_text(encoding="utf-8"))

    def test_builder__a_bad_replace_old_item_is_refused(self):
        for item in ("a.py=zzzzzz", "a.py=abc", "missing.py=abcdef", "a.py", "a.py=ABCDEF"):
            code, out = self.build({"a.py": NEW}, [item])
            self.assertEqual(code, 2, item)
            self.assertFalse(out.exists(), item)


if __name__ == "__main__":
    unittest.main()
