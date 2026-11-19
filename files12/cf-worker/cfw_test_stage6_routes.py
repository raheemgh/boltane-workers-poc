#!/usr/bin/env python3
"""
Stage 6 — OFFLINE test of the webhook wiring (no network, no credentials).

Lives at cf-worker/test_stage6_routes.py. Run from cf-worker/, in the same
venv as the Stage 5 test (needs fastapi + httpx installed):

    PYTHONPATH=tooling/local-test-support:src python test_stage6_routes.py

What it checks
  1 static    src/app/main.py includes the webhook and health routers, includes
              NO debug router, and still ends with `Default = asgi.entrypoint(app)`.
  2 parity    (needs the Render original at ../app/) webhook.py on Workers differs
              from Render ONLY by the get_settings() access pattern: verify_webhook,
              _verify_meta_signature, receive_message and _process_message are
              AST-identical after mapping `get_settings()` back to `settings`;
              include_router order in main.py equals Render's.
  3 routes    the REAL app object (app.main.app) answered through FastAPI's
              TestClient:
                GET  /webhook  right token -> 200 + challenge echoed; wrong token -> 403;
                               missing query params -> 422
                POST /webhook  no / wrong / malformed / tampered-body signature -> 403
                               and NOTHING scheduled; valid signature -> 200
                               {"status": "received"} and the pipeline function is
                               scheduled exactly once with the parsed payload
                GET  /health   200
                /debug/*       404 (the four temporary routers are gone)
  4 sabotage  the route checks must FAIL when the signature check is forced to
              always-pass or always-fail, and when the verify token is wrong.

What this CANNOT show: that the Cloudflare runtime really keeps a background
task alive after the response (that is test_stage6_deployed.py's job, against
a deployed Worker). Here `_process_message` is replaced by a recorder, so the
firewall/Supabase/OpenRouter/Meta pipeline is NOT exercised (Stage 5 does that).
"""

import ast
import contextlib
import hashlib
import hmac
import json
import os
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent            # cf-worker/
REPO_ROOT = HERE.parent                           # holds the Render original under app/
SRC = HERE / "src"

MAIN_PATH = SRC / "app" / "main.py"
WEBHOOK_PATH = SRC / "app" / "routers" / "webhook.py"
RENDER_MAIN = REPO_ROOT / "app" / "main.py"
RENDER_WEBHOOK = REPO_ROOT / "app" / "routers" / "webhook.py"

VERIFY_TOKEN = "stage6-verify-token"
APP_SECRET = "stage6-app-secret"

# Known values for the two settings the routes use; dummies for the rest.
os.environ["META_VERIFY_TOKEN"] = VERIFY_TOKEN
os.environ["META_APP_SECRET"] = APP_SECRET
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-supabase-key")
os.environ.setdefault("OPENROUTER_DEFAULT_KEY", "test-openrouter-key")
os.environ.pop("OPENROUTER_SOCKS5_PROXY", None)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


# ---------------------------------------------------------------------------
# 1 + 2: static checks
# ---------------------------------------------------------------------------

def _include_router_args(path: Path) -> list[str]:
    tree = ast.parse(path.read_text("utf-8"))
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "include_router" and node.args):
            found.append(ast.unparse(node.args[0]))
    return found


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text("utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def t_static_main() -> None:
    routers = _include_router_args(MAIN_PATH)
    imported = _imported_names(MAIN_PATH)
    source = MAIN_PATH.read_text("utf-8")
    tree = ast.parse(source)

    check("1 static: main.py includes the webhook router and the health router",
          "webhook.router" in routers and "health.router" in routers, str(routers))
    check("1 static: main.py includes NO debug router and imports no debug module",
          not any("debug" in r for r in routers) and not any("debug" in n for n in imported),
          f"routers={routers} imports={sorted(n for n in imported if 'debug' in n)}")
    ends_with_default = any(
        isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "Default" for t in node.targets)
        and ast.unparse(node.value) == "asgi.entrypoint(app)"
        for node in tree.body)
    check("1 static: main.py still defines `Default = asgi.entrypoint(app)`", ends_with_default)


class _SettingsToSingleton(ast.NodeTransformer):
    """Maps the Workers pattern `get_settings()` back to Render's `settings` singleton."""

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id == "get_settings" and not node.args:
            return ast.copy_location(ast.Name(id="settings", ctx=ast.Load()), node)
        return node


def _functions(path: Path, normalise: bool) -> dict:
    tree = ast.parse(path.read_text("utf-8"))
    if normalise:
        tree = _SettingsToSingleton().visit(tree)
        ast.fix_missing_locations(tree)
    return {n.name: ast.dump(n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _constants(path: Path) -> dict:
    tree = ast.parse(path.read_text("utf-8"))
    return {t.id: ast.dump(n.value) for n in tree.body if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name)}


def t_parity_with_render() -> None:
    if not (RENDER_WEBHOOK.exists() and RENDER_MAIN.exists()):
        print("SKIPPED: Render original not found at", REPO_ROOT / "app", "- parity checks not run")
        return
    render, workers = _functions(RENDER_WEBHOOK, normalise=False), _functions(WEBHOOK_PATH, normalise=True)
    for name in ("verify_webhook", "_verify_meta_signature", "receive_message", "_process_message",
                 "resolve_monthly_conversation_cap"):
        check(f"2 parity: webhook.{name} is identical to Render (get_settings() mapped back to settings)",
              name in render and name in workers and render[name] == workers[name])
    check("2 parity: every module-level constant of webhook.py is identical to Render",
          _constants(RENDER_WEBHOOK) == _constants(WEBHOOK_PATH))
    check("2 parity: main.py includes the routers in the same order as Render",
          _include_router_args(RENDER_MAIN) == _include_router_args(MAIN_PATH),
          f"render={_include_router_args(RENDER_MAIN)} workers={_include_router_args(MAIN_PATH)}")


# ---------------------------------------------------------------------------
# 3: routes through the REAL app object
# ---------------------------------------------------------------------------

def sign(raw: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def meta_payload(text: str) -> dict:
    """Same shape as a real Meta delivery (only the fields _process_message reads matter)."""
    return {"object": "whatsapp_business_account", "entry": [{"id": "0", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550000000", "phone_number_id": "PN-STAGE6"},
        "contacts": [{"profile": {"name": "Stage6"}, "wa_id": "15551230001"}],
        "messages": [{"from": "15551230001", "id": "wamid.STAGE6", "timestamp": "1760000000",
                      "type": "text", "text": {"body": text}}]}}]}]}


def build_app():
    import workers
    if not hasattr(workers, "asgi"):   # the local stub only provides `env`
        workers.asgi = types.SimpleNamespace(entrypoint=lambda app: app)
    import app.config as cfg
    cfg._settings = None
    import app.routers.webhook as wh
    from app.main import app as fastapi_app
    from fastapi.testclient import TestClient
    return TestClient(fastapi_app, raise_server_exceptions=False), fastapi_app, wh


@contextlib.contextmanager
def recorded_pipeline(wh):
    """Replace the background pipeline with a recorder; always restore."""
    calls: list = []

    async def fake_process_message(payload):
        calls.append(payload)

    original = wh._process_message
    wh._process_message = fake_process_message
    try:
        yield calls
    finally:
        wh._process_message = original


def get_checks(client) -> list[str]:
    """Failures of the GET /webhook handshake checks (empty list = all good)."""
    bad = []
    r = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN,
                                       "hub.challenge": "1158201444"})
    if not (r.status_code == 200 and r.text == "1158201444" and r.headers.get("content-type", "").startswith("text/plain")):
        bad.append(f"right token: {r.status_code} {r.text!r}")
    r = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1158201444"})
    if not (r.status_code == 403 and r.text == "verification token mismatch"):
        bad.append(f"wrong token: {r.status_code} {r.text!r}")
    r = client.get("/webhook")
    if r.status_code != 422:
        bad.append(f"missing params: {r.status_code}")
    return bad


def negative_signature_checks(client, wh) -> list[str]:
    """Every request below must be rejected with 403 and must schedule nothing."""
    bad = []
    raw = json.dumps(meta_payload("hello"), ensure_ascii=False).encode("utf-8")
    cases = {
        "no signature header": {},
        "signature made with the wrong secret": {"x-hub-signature-256": sign(raw, "not-the-secret")},
        "hex digest without the sha256= prefix": {"x-hub-signature-256": sign(raw)[len("sha256="):]},
        "empty signature value": {"x-hub-signature-256": ""},
        "sha1= prefix instead of sha256=": {"x-hub-signature-256": "sha1=" + sign(raw)[len("sha256="):]},
    }
    for label, extra in cases.items():
        with recorded_pipeline(wh) as calls:
            r = client.post("/webhook", content=raw, headers={"content-type": "application/json", **extra})
        if r.status_code != 403 or calls:
            bad.append(f"{label}: status={r.status_code} scheduled={len(calls)}")
    # valid signature, but the body was changed after signing
    tampered = raw.replace(b"hello", b"hellp")
    with recorded_pipeline(wh) as calls:
        r = client.post("/webhook", content=tampered,
                        headers={"content-type": "application/json", "x-hub-signature-256": sign(raw)})
    if r.status_code != 403 or calls:
        bad.append(f"tampered body: status={r.status_code} scheduled={len(calls)}")
    return bad


def positive_signature_checks(client, wh) -> list[str]:
    bad = []
    payload = meta_payload("السلام عليكم، شو عندكم اليوم؟")
    for label, raw in (("raw UTF-8 body", json.dumps(payload, ensure_ascii=False).encode("utf-8")),
                       ("ASCII-escaped body", json.dumps(payload, ensure_ascii=True).encode("utf-8"))):
        with recorded_pipeline(wh) as calls:
            r = client.post("/webhook", content=raw,
                            headers={"content-type": "application/json", "x-hub-signature-256": sign(raw)})
        if not (r.status_code == 200 and r.json() == {"status": "received"} and calls == [payload]):
            bad.append(f"{label}: status={r.status_code} body={r.text[:80]!r} scheduled={len(calls)}")
    return bad


def t_routes() -> None:
    try:
        client, app, wh = build_app()
    except Exception as exc:  # noqa: BLE001
        check("3 routes: the real app (app.main) imports", False, f"{type(exc).__name__}: {exc}")
        return
    check("3 routes: the real app (app.main) imports", True)

    paths = {(getattr(r, "path", None), tuple(sorted(getattr(r, "methods", None) or ()))) for r in app.routes}
    has = lambda path, method: any(p == path and method in m for p, m in paths)
    check("3 routes: /webhook answers GET and POST, /health answers GET",
          has("/webhook", "GET") and has("/webhook", "POST") and has("/health", "GET"), str(sorted(paths, key=str)))
    check("3 routes: no /debug/* route is registered",
          not any((p or "").startswith("/debug") for p, _ in paths))

    r = client.get("/health")
    check("3 routes: GET /health -> 200 {'status': 'ok'}", r.status_code == 200 and r.json() == {"status": "ok"}, r.text)

    bad = get_checks(client)
    check("3 routes: GET /webhook handshake (right token echoes the challenge, wrong -> 403, missing -> 422)",
          not bad, "; ".join(bad))

    bad = negative_signature_checks(client, wh)
    check("3 routes: POST /webhook rejects 6 kinds of bad signature with 403 and schedules NOTHING",
          not bad, "; ".join(bad))

    bad = positive_signature_checks(client, wh)
    check("3 routes: POST /webhook with a valid signature -> 200 {'status': 'received'} and the pipeline is "
          "scheduled once with the exact payload (raw UTF-8 and ASCII-escaped bodies)", not bad, "; ".join(bad))

    # Characterisation (same code as Render): validly signed but not JSON -> unhandled -> 500, nothing scheduled.
    raw = b"this is not json"
    with recorded_pipeline(wh) as calls:
        r = client.post("/webhook", content=raw,
                        headers={"content-type": "application/json", "x-hub-signature-256": sign(raw)})
    check("3 routes [characterisation]: validly signed non-JSON body -> 500, nothing scheduled "
          "(identical code on Render; Meta never sends this)", r.status_code == 500 and not calls,
          f"status={r.status_code} scheduled={len(calls)}")

    gone = {"/debug/settings": client.get("/debug/settings").status_code,
            "/debug/meta-send": client.get("/debug/meta-send", params={"case": "bad_token"}).status_code,
            "/debug/supabase": client.get("/debug/supabase", params={"op": "select"}).status_code,
            "/debug/ai": client.post("/debug/ai", json={"system": "s", "message": "m", "package": "basic"}).status_code}
    check("3 routes: all four temporary /debug/* routes answer 404", all(code == 404 for code in gone.values()), str(gone))

    t_sabotage(client, wh)


# ---------------------------------------------------------------------------
# 4: sabotage — the route checks must notice a weakened webhook
# ---------------------------------------------------------------------------

def t_sabotage(client, wh) -> None:
    original_verify, original_settings = wh._verify_meta_signature, wh.get_settings

    wh._verify_meta_signature = lambda raw_body, signature_header: True
    try:
        caught = negative_signature_checks(client, wh)
    finally:
        wh._verify_meta_signature = original_verify
    check("4 sabotage detected - signature check forced to always pass (negative checks fail)", bool(caught),
          "NOT DETECTED" if not caught else f"{len(caught)} failure(s)")

    wh._verify_meta_signature = lambda raw_body, signature_header: False
    try:
        caught = positive_signature_checks(client, wh)
    finally:
        wh._verify_meta_signature = original_verify
    check("4 sabotage detected - signature check forced to always fail (valid delivery check fails)", bool(caught),
          "NOT DETECTED" if not caught else f"{len(caught)} failure(s)")

    wh.get_settings = lambda: types.SimpleNamespace(META_VERIFY_TOKEN="a-different-token", META_APP_SECRET=APP_SECRET)
    try:
        caught = get_checks(client)
    finally:
        wh.get_settings = original_settings
    check("4 sabotage detected - verify token differs from the configured one (handshake check fails)", bool(caught),
          "NOT DETECTED" if not caught else f"{len(caught)} failure(s)")

    # And the unmodified app is green again afterwards (the patches were really undone).
    check("4 sabotage: after restoring, handshake and signature checks are green again",
          not get_checks(client) and not negative_signature_checks(client, wh) and not positive_signature_checks(client, wh))


def main() -> int:
    t_static_main()
    t_parity_with_render()
    t_routes()
    failed = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok or not detail else f"\n        -> {detail}"))
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
