#!/usr/bin/env python3
"""
Stage 3 verification for meta_client.py — Render copy vs Workers copy.

Lives at cf-worker/test_stage3_meta.py. Run from inside cf-worker/.
Four sub-commands, meant to be run in this order:

  check     No network. Proves the Workers copy of meta_client.py is
            byte-identical to the Render one, and that it touches
            neither `app.*` nor settings/os.environ (Task step 1).

  render    Runs the scenarios through the RENDER code's
            send_whatsapp() as a plain Python process (real network,
            real Meta). Writes stage3_render.json.

  workers   Calls GET /debug/meta-send?case=... on a `pywrangler dev`
            you already have running, so the SAME scenarios run
            through the Workers-tree copy under workerd. Writes
            stage3_workers.json.

  compare   Diffs the two JSON files (after stripping per-request noise
            like fbtrace_id) and prints a verdict per case.

Scenarios (see app/routers/debug_meta.py):
  bad_token            invalid token            -> Meta 401 / OAuth 190
  network_error        unresolvable host        -> transport failure
  bad_phone_number_id  real token, bogus id     -> Meta 4xx
  unlisted_recipient   real token, 555-0100     -> Meta 4xx
  happy                real token+id+recipient  -> a REAL message is sent
                       (only runs with --happy)

bad_token and network_error need no Meta credentials at all. The rest
read META_TEST_ACCESS_TOKEN / META_TEST_PHONE_NUMBER_ID /
META_TEST_RECIPIENT from cf-worker/.dev.vars (or the environment) and
are reported as SKIPPED when those are missing.

Output files contain phone numbers (not tokens) — don't commit them.
"""

import argparse
import ast
import asyncio
import hashlib
import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent  # cf-worker/
REPO_ROOT = HERE.parent

RENDER_META = REPO_ROOT / "app" / "services" / "meta_client.py"
WORKERS_META = HERE / "src" / "app" / "services" / "meta_client.py"
DEBUG_META = HERE / "src" / "app" / "routers" / "debug_meta.py"
DEV_VARS = HERE / ".dev.vars"
RENDER_OUT = HERE / "stage3_render.json"
WORKERS_OUT = HERE / "stage3_workers.json"

DEFAULT_CASES = ["bad_token", "network_error", "bad_phone_number_id", "unlisted_recipient"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_dev_vars() -> dict:
    values = {}
    if not DEV_VARS.exists():
        return values
    for raw in DEV_VARS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _load_creds() -> dict:
    file_vars = _parse_dev_vars()

    def get(name):
        return os.environ.get(name) or file_vars.get(name) or None

    return {
        "access_token": get("META_TEST_ACCESS_TOKEN"),
        "phone_number_id": get("META_TEST_PHONE_NUMBER_ID"),
        "recipient": get("META_TEST_RECIPIENT"),
    }


def _pick_cases(args) -> list:
    if args.cases:
        cases = [c.strip() for c in args.cases.split(",") if c.strip()]
    else:
        cases = list(DEFAULT_CASES)
        if args.happy:
            cases.append("happy")
    if "happy" in cases and not args.happy:
        sys.exit("refusing to run 'happy' without --happy: it sends a REAL WhatsApp message.")
    return cases


def _load_debug_module():
    spec = importlib.util.spec_from_file_location("stage3_debug_meta", DEBUG_META)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def cmd_check(_args) -> int:
    ok = True

    for path in (RENDER_META, WORKERS_META):
        if not path.exists():
            print(f"FAIL: missing {path}")
            ok = False
    if not ok:
        return 1

    render_hash, workers_hash = _sha256(RENDER_META), _sha256(WORKERS_META)
    print(f"render : {render_hash}  {RENDER_META.relative_to(REPO_ROOT)}")
    print(f"workers: {workers_hash}  {WORKERS_META.relative_to(REPO_ROOT)}")
    if render_hash == workers_hash:
        print("PASS: Workers copy is byte-identical to the Render original")
    else:
        print("FAIL: the two copies differ — Stage 3 expects a verbatim port")
        ok = False

    tree = ast.parse(WORKERS_META.read_text(encoding="utf-8"))
    imported = set()
    forbidden_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Name) and node.id in {"settings", "get_settings", "environ", "env"}:
            forbidden_names.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in {"settings", "get_settings", "environ"}:
            forbidden_names.add(node.attr)
    print(f"imports: {sorted(imported)}")
    if any(name == "app" or name.startswith("app.") for name in imported) or forbidden_names:
        print(f"FAIL: reads app/settings/env (imports={sorted(imported)}, names={sorted(forbidden_names)})")
        ok = False
    else:
        print("PASS: no app.* import and no settings/os.environ access — no Stage 1 settings change needed")

    return 0 if ok else 1


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

def cmd_render(args) -> int:
    cases = _pick_cases(args)
    creds = _load_creds()

    sys.path.insert(0, str(REPO_ROOT))
    import app.services.meta_client as meta_client  # the RENDER copy

    if Path(meta_client.__file__).resolve() != RENDER_META.resolve():
        sys.exit(f"imported the wrong meta_client: {meta_client.__file__} (expected {RENDER_META})")

    debug_module = _load_debug_module()

    async def run_all():
        return [
            await debug_module.run_case(case, meta_client.send_whatsapp, creds, runtime="render")
            for case in cases
        ]

    results = asyncio.run(run_all())
    RENDER_OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")
    for r in results:
        print(f"{r['case']:22s} {_outcome(r)}")
    print(f"wrote {RENDER_OUT.name}")
    return 0


# ---------------------------------------------------------------------------
# workers
# ---------------------------------------------------------------------------

def _get_json(url: str, timeout: float = 90.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        return exc.code, {"http_error": body}


def cmd_workers(args) -> int:
    cases = _pick_cases(args)
    base = args.base_url.rstrip("/")

    try:
        status, _ = _get_json(f"{base}/health", timeout=5)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"cannot reach {base}/health ({exc}). Is `pywrangler dev` running in cf-worker/?")
    print(f"GET /health -> {status}")

    results = []
    for case in cases:
        status, body = _get_json(f"{base}/debug/meta-send?case={case}")
        if status != 200:
            hint = " (is STAGE3_DEBUG_ENABLED=1 in .dev.vars, and did you restart pywrangler dev?)" if status == 404 else ""
            body = {"case": case, "runtime": "workers", "route_error": f"HTTP {status}: {body}{hint}"}
        results.append(body)
        print(f"{case:22s} {_outcome(body)}")

    WORKERS_OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"wrote {WORKERS_OUT.name}")
    return 0


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------

def _normalise(message: str) -> str:
    """Strip noise that is different on EVERY request even when the behaviour is identical."""
    message = re.sub(r'"fbtrace_id"\s*:\s*"[^"]*"', '"fbtrace_id":"<ID>"', message)
    message = re.sub(r"wamid\.[\w=\-]+", "wamid.<ID>", message)
    # The transport's own error text legitimately differs between
    # runtimes (socket error vs. fetch error). What must match is that
    # the module took its "request error" branch, so drop the tail.
    message = re.sub(r"(\(request error\).*? to=\S+): .*", r"\1: <transport error text>", message, flags=re.S)
    return message


def _signature(record: dict):
    if record.get("skipped"):
        return ("SKIPPED", record["skipped"])
    if record.get("route_error"):
        return ("ROUTE_ERROR", record["route_error"])
    return {
        "raised": record.get("raised"),
        "returned": record.get("returned"),
        "logs": [(log["level"], _normalise(log["message"])) for log in record.get("logs", [])],
    }


def _outcome(record: dict) -> str:
    if record.get("skipped"):
        return "SKIPPED (" + record["skipped"] + ")"
    if record.get("route_error"):
        return "ROUTE ERROR: " + record["route_error"]
    if record.get("raised"):
        return "RAISED " + record["raised"].split(":")[0]
    text = " | ".join(log["message"] for log in record.get("logs", []))
    ms = record.get("elapsed_ms")
    if "send succeeded" in text:
        return f"accepted by Meta ({ms} ms)"
    status = re.search(r"status=(\d+)", text)
    if status:
        code = re.search(r'"code"\s*:\s*(\d+)', text)
        return f"HTTP {status.group(1)}" + (f", Meta code {code.group(1)}" if code else "") + f" ({ms} ms)"
    if "request error" in text:
        return f"request-error branch ({ms} ms)"
    return f"no log line ({ms} ms)"


def cmd_compare(_args) -> int:
    for path in (RENDER_OUT, WORKERS_OUT):
        if not path.exists():
            sys.exit(f"missing {path.name} — run the `render` and `workers` sub-commands first")

    render = {r["case"]: r for r in json.loads(RENDER_OUT.read_text(encoding="utf-8"))}
    workers = {r["case"]: r for r in json.loads(WORKERS_OUT.read_text(encoding="utf-8"))}

    compared = 0
    problems = 0
    for case in [c for c in render if c in workers] + [c for c in workers if c not in render]:
        r, w = render.get(case), workers.get(case)
        print(f"\n=== {case}")
        if r is None or w is None:
            print("  only present on one side — not comparable")
            problems += 1
            continue
        print(f"  render : {_outcome(r)}")
        print(f"  workers: {_outcome(w)}")

        sr, sw = _signature(r), _signature(w)
        if isinstance(sr, tuple) or isinstance(sw, tuple):
            if sr != sw:
                print("  verdict: NOT COMPARABLE (skipped / route error on one side)")
                problems += 1
            else:
                print("  verdict: SKIPPED on both sides")
            continue

        compared += 1
        if sr == sw:
            print("  verdict: IDENTICAL (return value, exception behaviour, log levels and text)")
            continue

        problems += 1
        print("  verdict: DIFFERENT")
        print(f"    raised   render={sr['raised']!r}  workers={sw['raised']!r}")
        print(f"    returned render={sr['returned']!r}  workers={sw['returned']!r}")
        for i in range(max(len(sr["logs"]), len(sw["logs"]))):
            left = sr["logs"][i] if i < len(sr["logs"]) else None
            right = sw["logs"][i] if i < len(sw["logs"]) else None
            if left != right:
                print(f"    log[{i}] render : {left}")
                print(f"    log[{i}] workers: {right}")
        if sw["raised"] and not sr["raised"]:
            print("    !! send_whatsapp() RAISED on Workers but not on Render: its 'never raises for")
            print("       ordinary send failures' contract does not hold there (see the exception type above).")

    print("\n--- notes ---")
    print("* elapsed_ms is informational only. `pywrangler dev` runs workerd on THIS machine, so both")
    print("  timings come from the same network position — they say nothing about Cloudflare-edge vs")
    print("  Render-region latency to graph.facebook.com.")
    print("* 'accepted by Meta' means HTTP 200 from the Graph API; the phone actually receiving the")
    print("  message is the real proof for the happy path.")
    print(f"\n{compared} case(s) compared, {problems} problem(s)")
    return 0 if problems == 0 and compared > 0 else 1


# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="static checks, no network").set_defaults(func=cmd_check)

    for name, func in (("render", cmd_render), ("workers", cmd_workers)):
        p = sub.add_parser(name)
        p.add_argument("--cases", help="comma-separated subset (default: every case except 'happy')")
        p.add_argument("--happy", action="store_true", help="also run 'happy' — sends a REAL message to META_TEST_RECIPIENT")
        if name == "workers":
            p.add_argument("--base-url", default="http://127.0.0.1:8787")
        p.set_defaults(func=func)

    sub.add_parser("compare", help="diff stage3_render.json vs stage3_workers.json").set_defaults(func=cmd_compare)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
