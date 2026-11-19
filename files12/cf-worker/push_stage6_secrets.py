#!/usr/bin/env python3
"""
push_stage6_secrets.py - sends the Worker's five secrets from cf-worker/.dev.vars straight to Cloudflare.
Values are NEVER printed, never typed and never pasted anywhere: they go from the file to `wrangler secret put` through stdin.

    cd /workspaces/boltane-workers-poc/files12/cf-worker
    export CLOUDFLARE_API_TOKEN='...'        # same terminal session as before
    python push_stage6_secrets.py            # shows the plan, then asks y/N before sending anything

What it does, in order:
  1. checks it runs in cf-worker, that CLOUDFLARE_API_TOKEN is set, and that npx exists;
  2. reads .dev.vars and builds the plan for META_VERIFY_TOKEN, META_APP_SECRET, SUPABASE_URL, SUPABASE_KEY,
     OPENROUTER_DEFAULT_KEY (names, where each comes from, and only non-secret facts: the Supabase host, the key length);
  3. Phase A (no Meta access): if META_VERIFY_TOKEN or META_APP_SECRET is missing, it generates a random value —
     the Worker and test_stage6_deployed.py then agree on it, and any text works for Phase A. It also adds
     META_TEST_PHONE_NUMBER_ID=stage6-test-pn and META_TEST_RECIPIENT=15551230001 to .dev.vars if they are missing;
     nothing already in .dev.vars is ever changed;
  4. refuses (writes and sends nothing) if a required value is missing or still looks like a placeholder;
  5. asks "[y/N]", then appends only the generated lines to .dev.vars and runs `npx wrangler secret put NAME` per secret.
Use --dry-run to see the plan and stop. Use --yes to skip the question.
"""
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

REQUIRED = ["META_VERIFY_TOKEN", "META_APP_SECRET", "SUPABASE_URL", "SUPABASE_KEY", "OPENROUTER_DEFAULT_KEY"]
GENERATABLE = ("META_VERIFY_TOKEN", "META_APP_SECRET")
TEST_DEFAULTS = {"META_TEST_PHONE_NUMBER_ID": "stage6-test-pn", "META_TEST_RECIPIENT": "15551230001"}
PLACEHOLDER_PREFIXES = ("test-", "replace-", "dummy", "changeme", "your-", "placeholder")

root = Path.cwd()
dev_vars = root / ".dev.vars"
dry_run, assume_yes = "--dry-run" in sys.argv, "--yes" in sys.argv

if not (root / "wrangler.jsonc").exists() or not dev_vars.exists():
    sys.exit("Run this from the cf-worker folder (wrangler.jsonc and .dev.vars must be there):\n"
             "  cd /workspaces/boltane-workers-poc/files12/cf-worker")
if not os.environ.get("CLOUDFLARE_API_TOKEN"):
    sys.exit("CLOUDFLARE_API_TOKEN is not set in this terminal. Run  export CLOUDFLARE_API_TOKEN='...'  first.")
if not shutil.which("npx"):
    sys.exit("npx was not found.")

match = re.search(r'"name"\s*:\s*"([^"]+)"', (root / "wrangler.jsonc").read_text(encoding="utf-8"))
worker = match.group(1) if match else None
if not worker:
    sys.exit("could not read the Worker name from wrangler.jsonc")


def read_vars(path: Path) -> dict:
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


current = read_vars(dev_vars)
plan, generated, problems = {}, {}, []
for name in REQUIRED:
    value = current.get(name, "")
    if value:
        plan[name] = (value, "from .dev.vars")
    elif name in GENERATABLE:
        value = secrets.token_hex(20)
        plan[name] = (value, "GENERATED (random, will also be appended to .dev.vars)")
        generated[name] = value
    else:
        problems.append(f"{name} is missing from .dev.vars")

for name in ("SUPABASE_KEY", "OPENROUTER_DEFAULT_KEY"):
    value = current.get(name, "")
    if value and (value.lower().startswith(PLACEHOLDER_PREFIXES) or len(value) < 20):
        problems.append(f"{name} still looks like a placeholder (too short or starts with a dummy prefix)")
url = current.get("SUPABASE_URL", "")
host = urlparse(url).hostname or ""
if url and not (url.startswith("https://") and host.endswith(".supabase.co")):
    problems.append("SUPABASE_URL does not look like https://<project>.supabase.co")

print(f"Worker:  {worker}")
print(f"Supabase project host: {host or '(missing)'}")
print()
for name in REQUIRED:
    if name in plan:
        value, origin = plan[name]
        extra = f"  (length {len(value)})" if name in ("SUPABASE_KEY", "OPENROUTER_DEFAULT_KEY") else ""
        print(f"  {name:<24} {origin}{extra}")
    else:
        print(f"  {name:<24} MISSING")
added = {k: v for k, v in TEST_DEFAULTS.items() if k not in current}
for name, value in TEST_DEFAULTS.items():
    shown = current.get(name) or value
    print(f"  {name:<24} {'already in .dev.vars' if name in current else 'will be added'}: {shown}   (not a secret; used by test_stage6_deployed.py)")
print()
if problems:
    for problem in problems:
        print("PROBLEM:", problem)
    sys.exit("\nNothing was written and nothing was sent.")
if dry_run:
    sys.exit("--dry-run: nothing was written and nothing was sent.")
if not assume_yes and input(f"Send these {len(REQUIRED)} secrets to '{worker}'? [y/N] ").strip().lower() != "y":
    sys.exit("Cancelled. Nothing was written and nothing was sent.")

if generated or added:
    lines = ["", "# added by push_stage6_secrets.py (Phase A: no real Meta access, any value works)"]
    lines += [f"{k}={v}" for k, v in {**generated, **added}.items()]
    with dev_vars.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print(f"appended to .dev.vars: {', '.join({**generated, **added})}")

failed = False
for name in REQUIRED:
    value = plan[name][0]
    # --yes: if npx ever asks "Ok to proceed?", it must not read the secret from stdin as its answer
    result = subprocess.run(["npx", "--yes", "wrangler", "secret", "put", name, "--name", worker],
                            input=value, text=True, capture_output=True, cwd=root, timeout=180)
    output = (result.stdout + result.stderr).replace(value, "<hidden>")
    if result.returncode == 0:
        print(f"OK    {name}")
    else:
        failed = True
        tail = [line for line in output.splitlines() if line.strip()][-3:]
        print(f"FAIL  {name}: " + " | ".join(tail))
print()
print("Some secrets failed - nothing else was changed." if failed else
      "Done. Next:  python test_stage6_deployed.py check --url https://" + worker + ".<your-subdomain>.workers.dev")
sys.exit(1 if failed else 0)
