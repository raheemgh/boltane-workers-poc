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
        else:
            conflicts.append(key)
    return to_write, identical, conflicts, unplaced


def write_all(to_write) -> str | None:
    """Create every file or none: on failure the files created so far are removed."""
    created = []
    try:
        for _key, path, data in to_write:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as handle:
                handle.write(data)
            created.append(path)
    except OSError as exc:
        for path in created:
            path.unlink(missing_ok=True)
        return f"write failed ({exc}); {len(created)} file(s) rolled back"
    return None


def install(files, root, git_root, out=print) -> int:
    """Install the payload; returns 0 on success, 1 if nothing was written."""
    to_write, identical, conflicts, unplaced = plan(files, root, git_root)
    for key in conflicts:
        out(f"CONFLICT {key}: exists and differs; nothing was written")
    for key in unplaced:
        out(f"UNPLACED {key}: needs a git root (.git) above {root}; nothing was written")
    if conflicts or unplaced:
        return 1
    error = write_all(to_write)
    if error:
        out(error)
        return 1
    for key, _path, data in to_write:
        out(f"WROTE {key} ({len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()[:12]})")
    out(f"SUMMARY written={len(to_write)} identical={len(identical)} conflicts=0")
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
