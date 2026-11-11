#!/usr/bin/env python3
"""
Stage 7 preflight — READ-ONLY. Collects the facts the Stage 7 plan still needs.

Put it in cf-worker/ (drag and drop), activate the venv, then:

    cd /workspaces/boltane-workers-poc/files12/cf-worker
    source ~/venv-tests/bin/activate
    python stage7_preflight.py --url https://boltane-core-engine.boltane.workers.dev

What it does (and ONLY this):
  1  tool/package check          python version, whether fastapi / httpx / postgrest / pydantic import
  2  git                         branch, last 3 commits, short status, stray src/app/cfw_main.py
  3  test files                  where test_package_system / test_usage_protection / test_webhook_timing
                                 live in the repo (and whether git tracks them)
  4  baseline                    runs each ORIGINAL test function of test_package_system.py and
                                 test_usage_protection.py ONE BY ONE against the Workers tree
                                 (copies them to a temp folder first; nothing in the repo is changed)
  5  .dev.vars / environment     which required settings exist (names only, never values)
  6  test store row              read-only REST select of stores where phone_number_id = test store
                                 (access_token is reported as set/missing, never printed)
  7  Worker health               3 GET /health calls with timings (only if a URL is known)

It writes nothing to Supabase, sends nothing to Meta/OpenRouter, changes no repo file.
The full output is also saved to stage7_preflight_report.txt (no secrets inside; do not commit it).
"""

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CF = Path(__file__).resolve().parent
REPORT = CF / "stage7_preflight_report.txt"
DEV_VARS = CF / ".dev.vars"
SKIP_DIRS = {".git", "node_modules", "python_modules", ".venv", "venv", ".wrangler", "__pycache__",
             "knowledge_upload", "cf-probe", ".uv-cache"}
TEST_NAMES = ("test_package_system.py", "test_usage_protection.py", "test_webhook_timing.py")
NEEDED = ("META_VERIFY_TOKEN", "META_APP_SECRET", "SUPABASE_URL", "SUPABASE_KEY", "OPENROUTER_DEFAULT_KEY",
          "META_TEST_PHONE_NUMBER_ID", "META_TEST_RECIPIENT", "WORKER_URL")
DEFAULT_TEST_PNID = "stage6-test-pn"
USER_AGENT = "boltane-stage7-preflight/1.0"

_lines: list[str] = []


def out(text: str = "") -> None:
    print(text)
    _lines.append(text)


def section(title: str) -> None:
    out()
    out("=" * 70)
    out(title)
    out("=" * 70)


def run(cmd: list, cwd: Path, timeout: float = 30.0, env: dict | None = None):
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env)
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:.0f}s"


# ---------------------------------------------------------------------------
# settings (names only are ever printed)
# ---------------------------------------------------------------------------

def read_dev_vars() -> dict:
    values: dict = {}
    if DEV_VARS.exists():
        for raw in DEV_VARS.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def effective_settings(file_vars: dict) -> dict:
    merged = dict(file_vars)
    for key in NEEDED:
        if os.environ.get(key):
            merged[key] = os.environ[key]
    return merged


# ---------------------------------------------------------------------------
# 1 packages
# ---------------------------------------------------------------------------

def check_environment() -> None:
    section("1) Python and packages")
    out(f"python: {sys.version.split()[0]}   executable: {sys.executable}")
    out(f"venv active: {'yes' if sys.prefix != getattr(sys, 'base_prefix', sys.prefix) else 'NO (activate ~/venv-tests first)'}")
    for module in ("fastapi", "httpx", "postgrest", "pydantic"):
        found = importlib.util.find_spec(module) is not None
        out(f"  {module:<10} {'importable' if found else 'MISSING in this python'}")
    code, so, se = run(["uv", "--version"], CF, 10)
    out(f"  uv         {so.strip() if code == 0 else 'not found (' + se.strip()[:60] + ')'}")


# ---------------------------------------------------------------------------
# 2 git
# ---------------------------------------------------------------------------

def check_git() -> Path:
    section("2) Git")
    code, so, _ = run(["git", "rev-parse", "--show-toplevel"], CF, 10)
    if code != 0:
        out("not inside a git repository (or git missing)")
        return CF.parent
    root = Path(so.strip())
    out(f"repo root: {root}")
    out(f"cf-worker: {CF}")
    for label, cmd in (("branch", ["git", "branch", "--show-current"]),
                       ("last 3 commits", ["git", "log", "--oneline", "-3"]),
                       ("unpushed vs upstream", ["git", "log", "--oneline", "@{u}..HEAD"])):
        code, so, se = run(cmd, root, 15)
        text = so.strip() if code == 0 else f"(n/a: {se.strip()[:80]})"
        out(f"{label}:")
        out("  " + (text.replace("\n", "\n  ") if text else "(none)"))
    code, so, _ = run(["git", "status", "--short"], root, 20)
    status = so.strip().splitlines()
    out(f"git status --short: {len(status)} line(s) (first 20)")
    for line in status[:20]:
        out("  " + line)
    stray = CF / "src" / "app" / "cfw_main.py"
    out(f"stray download copy src/app/cfw_main.py: {'STILL EXISTS' if stray.exists() else 'not present'}")
    return root


# ---------------------------------------------------------------------------
# 3 where are the test files
# ---------------------------------------------------------------------------

def find_tests(root: Path) -> dict:
    section("3) The three original test files")
    code, so, _ = run(["git", "ls-files"], root, 30)
    tracked = set(so.splitlines()) if code == 0 else set()
    found: dict = {name: [] for name in TEST_NAMES}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in TEST_NAMES:
            if name in filenames:
                path = Path(dirpath) / name
                rel = path.relative_to(root).as_posix()
                digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
                found[name].append((path, rel, rel in tracked, digest))
    for name in TEST_NAMES:
        if not found[name]:
            out(f"{name}: NOT FOUND anywhere under {root} (skipped: {', '.join(sorted(SKIP_DIRS))})")
        for _path, rel, is_tracked, digest in found[name]:
            out(f"{name}: {rel}   [{'tracked by git' if is_tracked else 'NOT tracked'}]  sha256:{digest}")
    out()
    out("files the parity / timing steps reuse:")
    wanted = [
        CF.parent / "app" / "routers" / "webhook.py",          # the Render original, unzipped
        CF.parent / "app" / "services" / "ai_client.py",
        CF / "src" / "app" / "worker_ctx.py",
        CF / "test_stage4_offline.py", CF / "test_stage4_real.py", CF / "test_stage5_firewall.py",
        CF / "test_stage6_routes.py", CF / "test_stage6_deployed.py", CF / "test_stage6_meta_sim.py",
        CF / "tooling" / "local-test-support" / "workers.py",
        CF / "tooling" / "local-test-support" / "memory_supabase.py",
    ]
    for path in wanted:
        out(f"  {'present ' if path.exists() else 'MISSING '} {path.relative_to(CF.parent).as_posix()}")
    return found


# ---------------------------------------------------------------------------
# 4 baseline: each original test function, one by one, on the Workers tree
# ---------------------------------------------------------------------------

RUNNER = '''
import asyncio, importlib, inspect, sys
name = sys.argv[1]
try:
    mod = importlib.import_module(name)
except BaseException as exc:
    print(f"RESULT IMPORT-FAIL {name}: {type(exc).__name__}: {str(exc).splitlines()[0][:160] if str(exc) else ''}")
    sys.exit(0)
tests = [(n, f) for n, f in vars(mod).items()
         if n.startswith("test_") and inspect.iscoroutinefunction(f) and f.__module__ == mod.__name__]
tests.sort(key=lambda pair: pair[1].__code__.co_firstlineno)
for n, f in tests:
    try:
        asyncio.run(f())
        print(f"RESULT PASS {n}")
    except BaseException as exc:
        first = str(exc).splitlines()[0][:160] if str(exc) else ""
        print(f"RESULT FAIL {n}: {type(exc).__name__}: {first}")
'''


def baseline(found: dict) -> None:
    section("4) Baseline: the ORIGINAL tests, unmodified, one function at a time, on the Workers tree")
    support = CF / "tooling" / "local-test-support"
    src = CF / "src"
    if not (support / "workers.py").exists() or not (src / "app").is_dir():
        out(f"cannot run: need {support / 'workers.py'} and {src / 'app'} (not both present)")
        return

    by_dir: dict = {}
    for name in ("test_package_system.py", "test_usage_protection.py"):
        for path, rel, _tracked, _digest in found.get(name, []):
            by_dir.setdefault(path.parent, {})[name] = (path, rel)
    complete = {d: files for d, files in by_dir.items() if len(files) == 2}
    if not complete:
        out("no folder contains BOTH test_package_system.py and test_usage_protection.py — baseline not run")
        return

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(support), str(src)])
    env.pop("OPENROUTER_SOCKS5_PROXY", None)
    for directory, files in complete.items():
        label = Path(*Path(files["test_package_system.py"][1]).parts[:-1]).as_posix() or "."
        out(f"--- originals found in: {label}   (copied to a temp folder, run with cwd=cf-worker)")
        with tempfile.TemporaryDirectory(prefix="stage7_baseline_") as tmp:
            tmp_path = Path(tmp)
            for name, (path, _rel) in files.items():
                shutil.copy2(path, tmp_path / name)
            (tmp_path / "_runner.py").write_text(RUNNER, encoding="utf-8")
            for module in ("test_package_system", "test_usage_protection"):
                code, so, se = run([sys.executable, str(tmp_path / "_runner.py"), module], CF, 150, env)
                results = [line for line in so.splitlines() if line.startswith("RESULT ")]
                out(f"  {module}.py:")
                if not results:
                    out(f"    no result lines (exit {code}). stderr tail: {se.strip()[-300:] or '(empty)'}")
                for line in results:
                    out("    " + line[len("RESULT "):])
                passed = sum(1 for line in results if line.startswith("RESULT PASS"))
                out(f"    -> {passed}/{len(results)} passed")


# ---------------------------------------------------------------------------
# 5 settings by name
# ---------------------------------------------------------------------------

def check_settings(file_vars: dict) -> dict:
    section("5) .dev.vars and environment (names only, no values)")
    out(f".dev.vars: {'exists' if DEV_VARS.exists() else 'DOES NOT EXIST'} at {DEV_VARS}")
    out("keys in .dev.vars: " + (", ".join(sorted(file_vars)) if file_vars else "(none)"))
    for key in NEEDED:
        in_file, in_env = bool(file_vars.get(key)), bool(os.environ.get(key))
        where = "both" if in_file and in_env else ".dev.vars" if in_file else "environment only" if in_env else "MISSING"
        out(f"  {key:<28} {where}")
    return effective_settings(file_vars)


# ---------------------------------------------------------------------------
# 6 test store row (read-only)
# ---------------------------------------------------------------------------

def http_get(url: str, headers: dict | None = None, timeout: float = 20.0):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace"), time.perf_counter() - started
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), time.perf_counter() - started
    except Exception as exc:  # noqa: BLE001 — a connection failure is a result
        return 0, f"{type(exc).__name__}: {str(exc)[:120]}", time.perf_counter() - started


def check_store(settings: dict) -> None:
    section("6) Test store row in Supabase (read-only select)")
    url, key = settings.get("SUPABASE_URL"), settings.get("SUPABASE_KEY")
    if not url or not key:
        out("SUPABASE_URL / SUPABASE_KEY not available — skipped")
        return
    pnid = settings.get("META_TEST_PHONE_NUMBER_ID") or DEFAULT_TEST_PNID
    params = {"phone_number_id": f"eq.{pnid}", "limit": "1",
              "select": "phone_number_id,ai_model,package,is_trial,is_api_free,monthly_conversation_count,"
                        "conversations_remaining,access_token"}
    status, body, _ = http_get(f"{url.rstrip('/')}/rest/v1/stores?{urllib.parse.urlencode(params)}",
                               {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"})
    if status != 200:
        out(f"Supabase REST answered HTTP {status}: {body[:160]}")
        out("(HTTP 0 / 5xx right after a long idle period can mean the free project is paused — check the dashboard)")
        return
    rows = json.loads(body)
    if not rows:
        out(f"NO row with phone_number_id={pnid} (deleted or never created) — `deliver` cannot run until it is recreated")
        return
    row = rows[0]
    token = row.pop("access_token", None)
    for field, value in row.items():
        out(f"  {field}: {value!r}")
    out(f"  access_token: {'set' if token else 'MISSING'}")


# ---------------------------------------------------------------------------
# 7 worker health
# ---------------------------------------------------------------------------

def check_worker(settings: dict, url_arg: str | None) -> None:
    section("7) Deployed Worker /health (3 calls, 1 s apart)")
    base = (url_arg or settings.get("WORKER_URL") or "").rstrip("/")
    if not base:
        out("no URL given (--url or WORKER_URL) — skipped")
        return
    out(f"url: {base}")
    for i in range(1, 4):
        status, body, seconds = http_get(base + "/health", timeout=15.0)
        out(f"  #{i}: HTTP {status}  {seconds:.2f}s  {body[:60]!r}")
        if i < 3:
            time.sleep(1.0)
    out("(HTTP 0 right away = URL without the full domain; see the handoff notes)")


# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 7 read-only preflight")
    parser.add_argument("--url", help="deployed Worker base URL, e.g. https://boltane-core-engine.boltane.workers.dev")
    args = parser.parse_args()

    out("Stage 7 preflight (read-only) — " + time.strftime("%Y-%m-%d %H:%M:%S"))
    file_vars = read_dev_vars()
    check_environment()
    root = check_git()
    found = find_tests(root)
    baseline(found)
    settings = check_settings(file_vars)
    check_store(settings)
    check_worker(settings, args.url)

    REPORT.write_text("\n".join(_lines) + "\n", encoding="utf-8")
    out()
    out(f"saved: {REPORT}   (send me this file or a screenshot of the output)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
