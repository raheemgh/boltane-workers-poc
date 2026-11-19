"""test_check_conventions - the checker as a command: exit codes, modes, commit messages.

Level:   L1
Stage:   V2-00B
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from conv_testkit import BASE, module_pair, write_tree

SCRIPT = Path(__file__).resolve().parents[3] / "tooling" / "conventions" / "check_conventions.py"


def cli(*args):
    command = [sys.executable, str(SCRIPT), *args]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def repo(tmp, files=None, docs=True):
    root = Path(tmp)
    write_tree(root, {**BASE, **(files or {})})
    if docs:
        cli("--root", str(root), "--write-docs", "--quiet")
    return str(root)


def message_file(tmp, text):
    path = Path(tmp) / "MSG"
    path.write_text(text, encoding="utf-8")
    return str(path)


class CliTests(unittest.TestCase):
    def test_cli__clean_tree_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = cli("--root", repo(tmp))
            self.assertEqual(0, result.returncode, result.stdout)
            self.assertIn("errors=0", result.stdout)

    def test_cli__violation_exits_one_and_names_the_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = repo(tmp, module_pair("core", "sample", "def f():\n    print(1)\n"))
            result = cli("--root", root)
            self.assertEqual(1, result.returncode)
            self.assertIn("[print]", result.stdout)

    def test_cli__quiet_is_silent_when_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = cli("--root", repo(tmp), "--quiet")
            self.assertEqual((0, ""), (result.returncode, result.stdout))

    def test_cli__missing_docs_fail_until_generated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = repo(tmp, docs=False)
            self.assertEqual(1, cli("--root", root).returncode)
            cli("--root", root, "--write-docs", "--quiet")
            self.assertEqual(0, cli("--root", root).returncode)

    def test_cli__rules_lists_every_rule_name(self):
        result = cli("--rules")
        self.assertEqual(0, result.returncode)
        for name in ("header", "layer-import", "file-lines", "import-time", "stale-docs"):
            self.assertIn(name, result.stdout)

    def test_cli__commit_message_rule(self):
        cases = [
            ("V2-00B: conventions kit\n", 0),
            ("V2-12: add memory\n\nbody\n", 0),
            ("Merge branch 'x'\n", 0),
            ("# comment\nV2-01: after comment\n", 0),
            ("fix stuff\n", 1),
            ("v2-01: lowercase\n", 1),
            ("", 1),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for text, code in cases:
                with self.subTest(text):
                    result = cli("--commit-msg", message_file(tmp, text))
                    self.assertEqual(code, result.returncode)

    def test_cli__export_then_verify_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, out = repo(tmp + "/repo"), str(Path(tmp) / "flat")
            self.assertEqual(0, cli("--root", root, "--export-knowledge", out).returncode)
            verified = cli("--verify-knowledge", out)
            self.assertEqual(0, verified.returncode)
            self.assertIn("different=0", verified.stdout)


if __name__ == "__main__":
    unittest.main()
