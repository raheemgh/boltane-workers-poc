"""test_installer_template - all-or-nothing writes, conflict refusal, git-root files, payload.

Level:   L1
Stage:   V2-00B
"""

import base64
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import installer_template as inst


def run_install(files, root, git_root=None):
    log = []
    code = inst.install(files, root, git_root, out=log.append)
    return code, log


class InstallTests(unittest.TestCase):
    def test_install__writes_every_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, log = run_install({"a/x.py": b"1", "b.txt": b"2"}, root)
            self.assertEqual(0, code)
            self.assertEqual(b"1", (root / "a/x.py").read_bytes())
            self.assertIn("written=2", log[-1])

    def test_install__second_run_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {"a.txt": b"1", "b.txt": b"2"}
            run_install(files, root)
            code, log = run_install(files, root)
            self.assertEqual(0, code)
            self.assertIn("written=0 identical=2", log[-1])

    def test_install__refuses_everything_when_one_file_differs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_bytes(b"mine")
            code, log = run_install({"a.txt": b"theirs", "new.txt": b"n"}, root)
            self.assertEqual(1, code)
            self.assertFalse((root / "new.txt").exists())
            self.assertEqual(b"mine", (root / "a.txt").read_bytes())
            self.assertTrue(any(line.startswith("CONFLICT a.txt") for line in log))

    def test_install__rolls_back_when_a_write_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dir").write_bytes(b"i am a file, not a folder")
            code, log = run_install({"a.txt": b"1", "dir/x.txt": b"2"}, root)
            self.assertEqual(1, code)
            self.assertFalse((root / "a.txt").exists())
            self.assertIn("rolled back", log[-1])

    def test_install__root_prefix_goes_to_the_git_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cf-worker"
            root.mkdir()
            code, _log = run_install({"@root/.github/ci.yml": b"ci"}, root, Path(tmp))
            self.assertEqual(0, code)
            self.assertEqual(b"ci", (Path(tmp) / ".github/ci.yml").read_bytes())

    def test_install__refuses_root_prefix_without_a_git_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, log = run_install({"@root/x.yml": b"1", "a.txt": b"2"}, Path(tmp))
            self.assertEqual(1, code)
            self.assertFalse((Path(tmp) / "a.txt").exists())
            self.assertTrue(any(line.startswith("UNPLACED @root/x.yml") for line in log))

    def test_install__refuses_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cf-worker"
            root.mkdir()
            code, _log = run_install({"../evil.txt": b"x"}, root)
            self.assertEqual(1, code)
            self.assertFalse((Path(tmp) / "evil.txt").exists())

    def test_payload__decodes_what_build_installer_packs(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("a/b.txt", b"hello")
            archive.writestr("empty/", b"")
        blob = base64.b64encode(buffer.getvalue()).decode("ascii")
        self.assertEqual({"a/b.txt": b"hello"}, inst.decode_payload(blob))

    def test_localize__replaces_the_path_token_in_text_files_only(self):
        files = {"a.md": b"cd @CFW@", "b.yml": b"x: @CFW@", "c.py": b"T = '@CFW@'"}
        base = Path("/repo")
        out = inst.localize(files, base / "files12" / "cf-worker", base)
        self.assertEqual(b"cd files12/cf-worker", out["a.md"])
        self.assertEqual(b"x: files12/cf-worker", out["b.yml"])
        self.assertEqual(b"T = '@CFW@'", out["c.py"])

    def test_localize__root_equal_to_git_root_gives_a_dot(self):
        out = inst.localize({"a.md": b"@CFW@/x"}, Path("/repo"), Path("/repo"))
        self.assertEqual(b"./x", out["a.md"])
        self.assertEqual(b"./x", inst.localize({"a.md": b"@CFW@/x"}, Path("/r"), None)["a.md"])

    def test_find_up__walks_towards_the_filesystem_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            deep = Path(tmp) / "a" / "b"
            deep.mkdir(parents=True)
            (Path(tmp) / "wrangler.jsonc").write_text("{}", encoding="utf-8")
            self.assertEqual(Path(tmp), inst.find_up(deep, "wrangler.jsonc"))
            self.assertIsNone(inst.find_up(deep, "no-such-marker-file"))


if __name__ == "__main__":
    unittest.main()
