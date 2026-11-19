"""test_build_installer - a built installer installs, refuses conflicts, and passes the checker.

Level:   L1
Stage:   V2-00B
"""

import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import build_installer

CHECKER = Path(__file__).resolve().parents[3] / "tooling" / "conventions" / "check_conventions.py"
STUB = '"""test_build_installer - stub\n\nLevel:   L1\nStage:   V2-00B\n"""\n'


def write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def build(src: Path, out: Path, stage="V2-99") -> None:
    code = build_installer.main(["--stage", stage, "--src", str(src), "--out", str(out)])
    assert code == 0


def run(installer: Path, *args):
    command = [sys.executable, str(installer), *args]
    return subprocess.run(command, capture_output=True, text=True, check=False)


class BuildTests(unittest.TestCase):
    def test_build__installs_into_root_and_git_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write(base / "src", {"docs/a.md": "cd @CFW@\n", "@root/.github/ci.yml": "ci\n"})
            build(base / "src", base / "install_v2_99.py")
            (base / "files12" / "cf-worker").mkdir(parents=True)
            result = run(base / "install_v2_99.py", "--root", str(base / "files12" / "cf-worker"),
                         "--git-root", str(base), "--no-check")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            written = base / "files12/cf-worker/docs/a.md"
            self.assertEqual("cd files12/cf-worker\n", written.read_text(encoding="utf-8"))
            self.assertTrue((base / ".github/ci.yml").is_file())

    def test_build__output_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write(base / "src", {"a.txt": "1\n", "b/c.txt": "2\n"})
            (base / "one").mkdir()
            build(base / "src", base / "one" / "install_v2_99.py")
            build(base / "src", base / "install_v2_99.py")
            first = (base / "one/install_v2_99.py").read_bytes()
            self.assertEqual(first, (base / "install_v2_99.py").read_bytes())

    def test_build__rerun_is_a_no_op_and_changed_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write(base / "src", {"a.txt": "1\n"})
            build(base / "src", base / "install_v2_99.py")
            args = ("--root", str(base / "target"), "--no-check")
            self.assertEqual(0, run(base / "install_v2_99.py", *args).returncode)
            again = run(base / "install_v2_99.py", *args)
            self.assertEqual(0, again.returncode)
            self.assertIn("identical=1", again.stdout)
            (base / "target/a.txt").write_text("edited\n", encoding="utf-8")
            refused = run(base / "install_v2_99.py", *args)
            self.assertEqual(1, refused.returncode)
            self.assertIn("CONFLICT a.txt", refused.stdout)

    def test_build__generated_installer_passes_the_conventions_checker(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "src").mkdir()
            (base / "src/blob.bin").write_bytes(random.Random(1).randbytes(30000))
            target = base / "repo"
            write(target, {"tests/v2/installers/test_build_installer.py": STUB})
            (target / "tooling/installers").mkdir(parents=True)
            build(base / "src", target / "tooling/installers/install_v2_99.py")
            subprocess.run([sys.executable, str(CHECKER), "--root", str(target), "--write-docs",
                            "--quiet"], check=True)
            result = subprocess.run([sys.executable, str(CHECKER), "--root", str(target)],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(0, result.returncode, result.stdout)
            self.assertIn("ALLOW tooling/installers/install_v2_99.py:1 [file-lines]", result.stdout)

    def test_build__refuses_an_empty_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.mkdir()
            code = build_installer.main(
                ["--stage", "V2-99", "--src", str(empty), "--out", str(Path(tmp) / "i.py")]
            )
            self.assertEqual(2, code)


if __name__ == "__main__":
    unittest.main()
