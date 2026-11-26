"""installer_template - all-or-nothing installer logic reused by every install_v2_<nn>.py.

Layer:   tooling
Stage:   V2-00B
Flags:   none
Raises:  never
Budget:  none
Tests:   tests/v2/installers/test_installer_template.py
"""

import argparse
import base64
import hashlib
import io
import subprocess
import sys
import zipfile
from pathlib import Path

STAGE = ""  # @@STAGE@@
PAYLOAD = ""  # @@PAYLOAD@@
# Payload keys that may replace a file already on disk, with the sha256 prefix that file must have now.
EXPECTED_OLD: dict[str, str] = {}  # @@EXPECTED_OLD@@
ROOT_PREFIX = "@root/"
CHECKER = "tooling/conventions/check_conventions.py"
CFW_TOKEN = b"@CFW@"
TEXT_SUFFIXES = (".md", ".yml", ".yaml")


def decode_payload(blob: str) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(blob))) as archive:
        return {n: archive.read(n) for n in archive.namelist() if not n.endswith("/")}


def localize(files: dict[str, bytes], root: Path, git_root: Path | None) -> dict[str, bytes]:
    """Put the real cf-worker path (measured from the git root) where text files carry the token."""
    try:
        prefix = root.relative_to(git_root).as_posix() if git_root else "."
    except ValueError:
        prefix = "."
    return {
        key: data.replace(CFW_TOKEN, prefix.encode()) if key.endswith(TEXT_SUFFIXES) else data
        for key, data in files.items()
    }


def find_up(start: Path, marker: str) -> Path | None:
    for folder in [start, *start.parents]:
        if (folder / marker).exists():
            return folder
    return None


def target_path(key: str, root: Path, git_root: Path | None) -> Path | None:
    """Where a payload key goes; None if it cannot be placed safely."""
    if ".." in Path(key).parts:
        return None
    if key.startswith(ROOT_PREFIX):
        return None if git_root is None else git_root / key[len(ROOT_PREFIX):]
    return root / key


def sha12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def replaceable(key: str, path: Path) -> bool:
    """True if `key` may replace the file on disk: it is listed in EXPECTED_OLD and has exactly that content."""
    expected = EXPECTED_OLD.get(key)
    return bool(expected) and path.is_file() and sha12(path.read_bytes()).startswith(expected)


def plan(files, root, git_root):
    """Split the payload into (to_write, identical, conflicts, unplaced)."""
    to_write, identical, conflicts, unplaced = [], [], [], []
    for key, data in sorted(files.items()):
        path = target_path(key, root, git_root)
        if path is None:
            unplaced.append(key)
        elif not path.exists():
            to_write.append((key, path, data))
        elif path.is_file() and path.read_bytes() == data:
            identical.append(key)
        elif replaceable(key, path):
            to_write.append((key, path, data))
        else:
            conflicts.append(key)
    return to_write, identical, conflicts, unplaced


def write_file(path: Path, data: bytes, replace: bool) -> None:
    """Create `path` (it must not exist), or replace it through a temporary file."""
    if not replace:
        with path.open("xb") as handle:
            handle.write(data)
        return
    temp = path.with_name(path.name + ".installing")
    try:
        temp.write_bytes(data)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def undo(done) -> None:
    """Remove the files created and restore the files replaced, newest first."""
    for path, previous in reversed(done):
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(previous)


def write_all(to_write) -> str | None:
    """Create or replace every file, or none: on failure the created files are removed and the replaced restored."""
    done = []  # (path, the bytes it had before, or None if it was created)
    try:
        for key, path, data in to_write:
            path.parent.mkdir(parents=True, exist_ok=True)
            previous = path.read_bytes() if replaceable(key, path) else None
            write_file(path, data, previous is not None)
            done.append((path, previous))
    except OSError as exc:
        undo(done)
        return f"write failed ({exc}); {len(done)} file(s) rolled back"
    return None


def report_problems(conflicts, unplaced, root, out) -> None:
    for key in conflicts:
        hint = f" (the old version must have sha256 {EXPECTED_OLD[key]})" if key in EXPECTED_OLD else ""
        out(f"CONFLICT {key}: exists and differs{hint}; nothing was written")
    for key in unplaced:
        out(f"UNPLACED {key}: needs a git root (.git) above {root}; nothing was written")


def report_written(to_write, before, identical, out) -> None:
    for key, _path, data in to_write:
        if key in before:
            out(f"REPLACED {key} (sha256 {before[key]} -> {sha12(data)}; git checkout restores the old file)")
        else:
            out(f"WROTE {key} ({len(data)} bytes, sha256 {sha12(data)})")
    extra = f" replaced={len(before)}" if before else ""
    out(f"SUMMARY written={len(to_write)} identical={identical} conflicts=0{extra}")


def install(files, root, git_root, out=print) -> int:
    """Install the payload; returns 0 on success, 1 if nothing was written."""
    to_write, identical, conflicts, unplaced = plan(files, root, git_root)
    report_problems(conflicts, unplaced, root, out)
    if conflicts or unplaced:
        return 1
    before = {key: sha12(path.read_bytes()) for key, path, _data in to_write if path.is_file()}
    error = write_all(to_write)
    if error:
        out(error)
        return 1
    report_written(to_write, before, len(identical), out)
    return 0


def post_install(root: Path) -> int:
    """Regenerate the docs, then run the conventions checker; its exit code is the proof."""
    script = root / CHECKER
    if not script.is_file():
        return 0
    for extra in (["--write-docs", "--quiet"], []):
        result = subprocess.run(
            [sys.executable, str(script), "--root", str(root), *extra], check=False
        )
        if result.returncode:
            return result.returncode
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=f"install {STAGE}")
    parser.add_argument("--root", type=Path, help="the cf-worker folder (default: auto-detect)")
    parser.add_argument("--git-root", type=Path, help="the repo root (default: auto-detect)")
    parser.add_argument("--no-check", action="store_true", help="skip the conventions check")
    args = parser.parse_args(argv)
    if not PAYLOAD:
        print("empty payload: this is the template, not a generated installer")
        return 2
    here = Path(__file__).resolve()
    root = (args.root or find_up(here, "wrangler.jsonc") or Path.cwd()).resolve()
    git_root = args.git_root.resolve() if args.git_root else find_up(root, ".git")
    print(f"install {STAGE} -> {root}")
    code = install(localize(decode_payload(PAYLOAD), root, git_root), root, git_root)
    return post_install(root) if code == 0 and not args.no_check else code


if __name__ == "__main__":
    sys.exit(main())
