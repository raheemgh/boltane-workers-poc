#!/usr/bin/env python3
"""
install_stage6b.py - Stage 6 bundle-size fix: replace the `supabase` umbrella package with `postgrest` alone.

HOW (one step, from the cf-worker folder):
    cd /workspaces/boltane-workers-poc/files12/cf-worker
    python install_stage6b.py            # add --force only if I ask you to

It changes exactly three files and nothing else, ALL OR NOTHING (every check runs first; if any fails, nothing is written):
  1. src/app/services/supabase_client.py   replaced - ONLY if your current file is byte-identical to the Knowledge
                                           copy it was built from (otherwise it stops and tells you; --force overrides)
  2. pyproject.toml                        the dependency line "supabase>=2.9.0" becomes "postgrest>=2.31.0,<3"
  3. test_stage2_supabase_unit.py          one static check (it looked for the old import text) is updated
Running it twice is safe: a change that is already applied is reported, not repeated.
git keeps the previous versions: `git diff`.
"""
import base64, hashlib, py_compile, sys, zlib
from pathlib import Path

OLD_SHA = "368ecc54b602a7dd463bc4c87846027d3728086517b7be6379cc7089fc2ffd3a"
NEW_SHA = "5f1df8832f980dddc4aa0544dcd3251bb148ca57c2872426e70728af3519b177"
NEW_DATA = "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"

PYPROJECT_OLD = '"supabase>=2.9.0",'
PYPROJECT_NEW = '"postgrest>=2.31.0,<3",'

TEST_OLD = '    check("1 static: acreate_client is what gets imported", "from supabase import acreate_client" in source)'
TEST_NEW = (
    '    check("1 static: the PostgREST-only client is what gets built (the `supabase` umbrella package is not imported)",\n'
    '          "from postgrest import AsyncPostgrestClient" in source\n'
    '          and not re.search(r"^\\s*(?:from|import)\\s+supabase\\b", source, re.MULTILINE))'
)

force = "--force" in sys.argv
root = Path.cwd()
if not (root / "wrangler.jsonc").exists() or not (root / "src" / "app" / "services" / "supabase_client.py").exists():
    sys.exit("This does not look like the cf-worker folder.\nRun:  cd /workspaces/boltane-workers-poc/files12/cf-worker   and try again.")

sha = lambda data: hashlib.sha256(data).hexdigest()
new_bytes = zlib.decompress(base64.b64decode(NEW_DATA))
if sha(new_bytes) != NEW_SHA:
    sys.exit("embedded data is corrupted (hash mismatch) - download install_stage6b.py again")

# ---- phase 1: decide what to do with each file, writing nothing ------------------------------------------------
problems, actions = [], []   # actions: (path, new_bytes_or_text, label, message)
lines = []

client_path = root / "src" / "app" / "services" / "supabase_client.py"
current = client_path.read_bytes()
if sha(current) == NEW_SHA:
    lines.append(("ALREADY", "src/app/services/supabase_client.py", "(new version already in place)"))
elif sha(current) == OLD_SHA or force:
    actions.append((client_path, new_bytes, "REPLACED", "src/app/services/supabase_client.py"))
else:
    problems.append(f"src/app/services/supabase_client.py differs from the Knowledge copy (sha256={sha(current)[:12]}..., "
                    f"expected {OLD_SHA[:12]}...). Send me this file, or run again with --force if you accept losing the differences.")

pyproject = root / "pyproject.toml"
text = pyproject.read_text(encoding="utf-8")
if PYPROJECT_NEW in text and PYPROJECT_OLD not in text:
    lines.append(("ALREADY", "pyproject.toml", "(postgrest dependency already there)"))
elif text.count(PYPROJECT_OLD) == 1:
    actions.append((pyproject, text.replace(PYPROJECT_OLD, PYPROJECT_NEW).encode("utf-8"), "PATCHED", "pyproject.toml"))
else:
    problems.append(f"pyproject.toml: expected exactly one {PYPROJECT_OLD} line, found {text.count(PYPROJECT_OLD)}.")

test_path = root / "test_stage2_supabase_unit.py"
if not test_path.exists():
    lines.append(("SKIPPED", "test_stage2_supabase_unit.py", "(file not found here - nothing to patch)"))
else:
    text = test_path.read_text(encoding="utf-8")
    if TEST_NEW in text:
        lines.append(("ALREADY", "test_stage2_supabase_unit.py", "(check already updated)"))
    elif text.count(TEST_OLD) == 1:
        actions.append((test_path, text.replace(TEST_OLD, TEST_NEW).encode("utf-8"), "PATCHED", "test_stage2_supabase_unit.py"))
    else:
        problems.append("test_stage2_supabase_unit.py: the line to update was not found exactly once - send me this file.")

if problems:
    for problem in problems:
        print("REFUSED  ", problem)
    print("\nNOTHING WAS WRITTEN (all or nothing).")
    sys.exit(1)

# ---- phase 2: write, then compile-check --------------------------------------------------------------------------
failed = False
for path, data, label, name in actions:
    path.write_bytes(data)
    detail = ""
    if path.suffix == ".py":
        try:
            py_compile.compile(str(path), doraise=True, cfile="/tmp/_stage6b_check.pyc")
            detail = "compiles"
        except py_compile.PyCompileError as exc:
            detail, failed = f"DOES NOT COMPILE: {exc}", True
    lines.append((label, name, detail))
for tag, name, detail in lines:
    print(f"{tag:<9} {name}   {detail}".rstrip())

print()
if failed:
    print("A written file does not compile - restore it with `git checkout -- <file>` and send me the message above.")
    sys.exit(1)
print("Done. Next: the offline tests, then  rm -rf python_modules  and a fresh  uv run pywrangler deploy --dry-run")
