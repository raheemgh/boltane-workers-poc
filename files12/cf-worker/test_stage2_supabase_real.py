#!/usr/bin/env python3
"""
Stage 2 — REAL-project test for supabase_client: Render sync version vs
the async port vs the async port inside workerd.

Lives at cf-worker/test_stage2_supabase_real.py. Run from cf-worker/.
Sub-commands (run each side in its own process — the two `app` packages
cannot share one interpreter):

  render    The Render module (sync client wrapped in thread offloading),
            plain Python, against the TEST project.        -> stage2_render.json
  async     The Workers-tree async port, plain Python (the local `workers`
            stub feeds get_settings()), same project.      -> stage2_async.json
  workers   The same port INSIDE workerd: calls /debug/supabase on a
            `pywrangler dev` you already have running, then (with
            --reuse-probe) hammers the cached client from separate and
            concurrent HTTP requests.                      -> stage2_workers.json
  compare   Diffs every side that has a result file against the others
            AND against EXPECTED (debug_supabase.py).

What each side runs is debug_supabase.run_op(): one scenario per
operation the Stage 2 task names (filtered select, upsert-on-conflict,
update) plus an "errors" scenario that records the exception TYPE and
Postgres code of three failing calls. Rows are verified through plain
PostgREST (httpx), not through supabase-py, and cleaned up afterwards.

SETUP — a TEST Supabase project, schema from stores_table.sql, and in
cf-worker/.dev.vars (never production!):

    SUPABASE_URL=https://<test-project>.supabase.co
    SUPABASE_KEY=<test project service key>
    STAGE2_DEBUG_ENABLED=1
    STAGE2_CONFIRM_TEST_PROJECT=https://<test-project>.supabase.co   # same as SUPABASE_URL
    (the other 4 required Settings vars as usual — dummies are fine here)

This CLI refuses to run unless the confirm value equals SUPABASE_URL.

Why 'async' AND 'workers': the first isolates the logic/query behaviour of
the port against real PostgREST; the second adds the Pyodide + fetch-based
transport, which is where a threading-free client could still behave
differently. A difference between them is a Workers-runtime difference.
"""

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent  # cf-worker/
REPO_ROOT = HERE.parent
SRC = HERE / "src"
SUPPORT = HERE / "tooling" / "local-test-support"
DEBUG_PATH = SRC / "app" / "routers" / "debug_supabase.py"
DEV_VARS = HERE / ".dev.vars"
OUT = {side: HERE / f"stage2_{side}.json" for side in ("render", "async", "workers")}

DUMMY_SETTINGS = {
    "META_VERIFY_TOKEN": "stage2-dummy",
    "META_APP_SECRET": "stage2-dummy",
    "OPENROUTER_DEFAULT_KEY": "stage2-dummy",
}


# ---------------------------------------------------------------------------

def _dev_vars() -> dict:
    values = {}
    if DEV_VARS.exists():
        for raw in DEV_VARS.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _project_credentials() -> tuple[str, str]:
    values = {**_dev_vars(), **{k: v for k, v in os.environ.items() if k.startswith(("SUPABASE_", "STAGE2_"))}}
    url, key = values.get("SUPABASE_URL"), values.get("SUPABASE_KEY")
    if not url or not key:
        sys.exit("SUPABASE_URL / SUPABASE_KEY missing (cf-worker/.dev.vars) — point them at a TEST project.")
    if values.get("STAGE2_CONFIRM_TEST_PROJECT") != url:
        sys.exit("refusing to run: STAGE2_CONFIRM_TEST_PROJECT must equal SUPABASE_URL exactly.\n"
                 f"  SUPABASE_URL                = {url}\n"
                 f"  STAGE2_CONFIRM_TEST_PROJECT = {values.get('STAGE2_CONFIRM_TEST_PROJECT')}\n"
                 "This test writes and deletes rows with the service key; it must only ever touch a test project.")
    return url, key


def _load_debug():
    spec = importlib.util.spec_from_file_location("stage2_debug_supabase", DEBUG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_side(side: str, module, debug, url: str, key: str) -> int:
    db = debug.HttpDB(url, key)

    async def run_all():
        results = {}
        for op in debug.OPS:
            started = time.perf_counter()
            result = await debug.run_op(op, module, db, uuid.uuid4().hex[:8], runtime=side)
            result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
            results[op] = result
            print(f"{op:8s} {'CRASHED: ' + result['crashed'] if result['crashed'] else 'ran'}  ({result['elapsed_ms']} ms)")
        return results

    results = asyncio.run(run_all())
    OUT[side].write_text(json.dumps({"side": side, "ops": results}, indent=2), encoding="utf-8")
    print(f"wrote {OUT[side].name}")
    return 0


def _set_env(url: str, key: str) -> None:
    os.environ.update(DUMMY_SETTINGS)
    os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"] = url, key


def cmd_render(_args) -> int:
    url, key = _project_credentials()
    _set_env(url, key)
    sys.path.insert(0, str(REPO_ROOT))
    import app.services.supabase_client as module  # the RENDER copy

    if Path(module.__file__).resolve() != (REPO_ROOT / "app" / "services" / "supabase_client.py").resolve():
        sys.exit(f"imported the wrong supabase_client: {module.__file__}")
    return _run_side("render", module, _load_debug(), url, key)


def cmd_async(_args) -> int:
    url, key = _project_credentials()
    _set_env(url, key)
    sys.path[:0] = [str(SUPPORT), str(SRC)]  # local `workers` stub first, then the Workers tree
    import app.services.supabase_client as module  # the WORKERS-tree copy

    if Path(module.__file__).resolve() != (SRC / "app" / "services" / "supabase_client.py").resolve():
        sys.exit(f"imported the wrong supabase_client: {module.__file__}")
    return _run_side("async", module, _load_debug(), url, key)


# ---------------------------------------------------------------------------

def _get(url: str, timeout: float = 120.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, {"http_error": exc.read().decode("utf-8", "replace")[:400]}


def cmd_workers(args) -> int:
    debug = _load_debug()
    base = args.base_url.rstrip("/")
    try:
        status, _ = _get(f"{base}/health", timeout=5)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"cannot reach {base}/health ({exc}). Is `pywrangler dev` running in cf-worker/?")
    print(f"GET /health -> {status}")

    ops = {}
    for op in debug.OPS:
        started = time.perf_counter()
        status, body = _get(f"{base}/debug/supabase?op={op}")
        if status != 200:
            hint = {404: " (STAGE2_DEBUG_ENABLED=1 in .dev.vars? restart pywrangler dev after editing it)",
                    403: " (STAGE2_CONFIRM_TEST_PROJECT must equal SUPABASE_URL)"}.get(status, "")
            body = {"op": op, "runtime": "workers", "observations": None, "crashed": f"route HTTP {status}: {body}{hint}"}
        body["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        ops[op] = body
        print(f"{op:8s} {'CRASHED: ' + str(body['crashed']) if body.get('crashed') else 'ran'}  ({body['elapsed_ms']} ms)")

    payload = {"side": "workers", "ops": ops}

    if args.reuse_probe:
        # The cached client is built on the first request that needs it. These calls
        # reuse it from DIFFERENT HTTP requests — one after another, then concurrently.
        sequential = [_get(f"{base}/debug/supabase?op=select")[1] for _ in range(3)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            concurrent = list(pool.map(lambda _i: _get(f"{base}/debug/supabase?op=select"), range(6)))
        expected = debug.EXPECTED["select"]
        payload["reuse_probe"] = {
            "sequential_all_match_expected": all(r.get("observations") == expected for r in sequential),
            "concurrent_statuses": [status for status, _ in concurrent],
            "concurrent_all_match_expected": all(body.get("observations") == expected for _, body in concurrent),
            "first_failure": next((body for status, body in concurrent if status != 200 or body.get("crashed")), None),
        }
        print("reuse probe:", json.dumps({k: v for k, v in payload["reuse_probe"].items() if k != "first_failure"}))

    OUT["workers"].write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {OUT['workers'].name}")
    return 0


# ---------------------------------------------------------------------------

def _diff(left, right, path=""):
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            yield from _diff(left.get(key, "<absent>"), right.get(key, "<absent>"), f"{path}.{key}" if path else key)
    elif left != right:
        yield path, left, right


def cmd_compare(_args) -> int:
    debug = _load_debug()
    sides = {side: json.loads(OUT[side].read_text(encoding="utf-8")) for side in OUT if OUT[side].exists()}
    if len(sides) < 1:
        sys.exit("no stage2_*.json files — run render / async / workers first")
    print("sides present:", ", ".join(sides))

    problems = 0
    for op in debug.OPS:
        print(f"\n=== {op}")
        observations = {}
        for side, data in sides.items():
            result = (data.get("ops") or {}).get(op)
            if result is None:
                print(f"  {side:8s}: no result")
                problems += 1
                continue
            if result.get("crashed"):
                print(f"  {side:8s}: CRASHED  {result['crashed']}")
                problems += 1
                continue
            observations[side] = result["observations"]
            wrong = list(_diff(debug.EXPECTED[op], result["observations"]))
            print(f"  {side:8s}: {'matches EXPECTED' if not wrong else 'DIFFERS from EXPECTED'}  ({result.get('elapsed_ms')} ms)")
            for path, want, got in wrong:
                print(f"      {path}: expected {want!r}, got {got!r}")
            problems += bool(wrong)

        names = list(observations)
        for other in names[1:]:
            differences = list(_diff(observations[names[0]], observations[other]))
            if differences:
                problems += 1
                print(f"  {names[0]} vs {other}: DIFFERENT")
                for path, left, right in differences:
                    print(f"      {path}: {names[0]}={left!r}  {other}={right!r}")
            else:
                print(f"  {names[0]} vs {other}: IDENTICAL")

    probe = (sides.get("workers") or {}).get("reuse_probe")
    if probe:
        ok = probe["sequential_all_match_expected"] and probe["concurrent_all_match_expected"] \
            and all(code == 200 for code in probe["concurrent_statuses"])
        print(f"\n=== reuse probe (cached client across separate + concurrent requests): {'OK' if ok else 'FAILED'}")
        if not ok:
            problems += 1
            print(f"  {json.dumps(probe, indent=2)[:900]}")
            print("  -> if the failure is 'Cannot perform I/O on behalf of a different request' or similar,")
            print("     set CACHE_CLIENT_ACROSS_CALLS = False in supabase_client.py and re-run.")

    print("\n--- notes ---")
    print("* elapsed_ms is informational: each side runs the same scenario on the same machine/project.")
    print("* Errors: the 'errors' scenario records the exception CLASS NAME and Postgres code, which is what")
    print("  Stage 2 step 4 asks to document. Identical rows above mean no divergence was observed.")
    print(f"\n{problems} problem(s)")
    return 0 if problems == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("render").set_defaults(func=cmd_render)
    sub.add_parser("async").set_defaults(func=cmd_async)
    workers = sub.add_parser("workers")
    workers.add_argument("--base-url", default="http://127.0.0.1:8787")
    workers.add_argument("--reuse-probe", action="store_true")
    workers.set_defaults(func=cmd_workers)
    sub.add_parser("compare").set_defaults(func=cmd_compare)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
