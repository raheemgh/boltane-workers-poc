#!/usr/bin/env python3
"""
Stage 3 — OFFLINE conformance test for meta_client.send_whatsapp().

Lives at cf-worker/test_stage3_offline.py. Needs no network, no Meta
credentials and no `pywrangler` — run it from cf-worker/ with the same
Python environment where `httpx` is installed:

    python test_stage3_offline.py

What it is: a small simulator of Meta's WhatsApp Cloud API
`POST /{version}/{phone_number_id}/messages` endpoint, plugged in
underneath the REAL httpx client through httpx.MockTransport, so
meta_client's own code (URL building, headers, body, status handling,
logging, exception handling) runs unmodified against spec-shaped
requests and responses. It runs against BOTH copies of the file (the
Render original and the cf-worker copy) by loading each by path.

What the simulator enforces (Meta's documented contract):
  request : POST, host graph.facebook.com, path /v<N>.<N>/<phone_number_id>/messages,
            Authorization: Bearer <token>, JSON body with
            messaging_product="whatsapp", "to", type="text",
            text.body a non-empty string of at most 4096 characters.
  success : HTTP 200 {"messaging_product","contacts":[{"input","wa_id"}],
            "messages":[{"id":"wamid...."}]}
  failure : {"error": {"message": "(#code) title", "type", "code",
            "error_data": {"messaging_product","details"}, "fbtrace_id"}}

What it does NOT prove — read this before trusting a green run:
  * It cannot show how the real Meta behaves; it encodes Meta's
    DOCUMENTED behaviour. The exact HTTP status paired with each
    error code is not asserted anywhere: Meta's own docs tell
    integrators to key on the error `code`, not the HTTP status, and
    meta_client treats every non-200 identically, so the tests sweep a
    range of statuses instead of pinning a mapping.
  * It runs on CPython. It says nothing about Workers' fetch-based
    transport — that is what `test_stage3_meta.py` + `pywrangler dev`
    are for.
  * The `characterization` tests LOCK IN current behaviour that may
    deserve a decision (no retry on 429/5xx, no pre-validation of the
    4096-char limit, 2xx-but-not-200 treated as failure). They pass
    on purpose; they exist so a future change is a visible decision.
"""

import asyncio
import importlib.util
import json
import logging
import random
import re
import string
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent

TARGETS = {
    "render-original": REPO_ROOT / "app" / "services" / "meta_client.py",
    "cf-worker-copy": HERE / "src" / "app" / "services" / "meta_client.py",
}

VALID_TOKEN = "EAAB-valid-test-token-0123456789"
VALID_PHONE_NUMBER_ID = "106540352242922"
ALLOWED_RECIPIENT = "15551230000"
WINDOW_CLOSED_RECIPIENT = "15551230001"      # allowed number, but >24h since it last wrote
UNLISTED_RECIPIENT = "15555550100"           # not on the test allow-list
MAX_TEXT_LENGTH = 4096


# ---------------------------------------------------------------------------
# The Meta simulator
# ---------------------------------------------------------------------------

def _fbtrace() -> str:
    return "A" + "".join(random.choices(string.ascii_letters + string.digits, k=18))


def meta_error(status: int, code: int, title: str, *, type_="OAuthException", details=None) -> httpx.Response:
    body = {
        "error": {
            "message": f"(#{code}) {title}",
            "type": type_,
            "code": code,
            "fbtrace_id": _fbtrace(),
        }
    }
    if details:
        body["error"]["error_data"] = {"messaging_product": "whatsapp", "details": details}
    return httpx.Response(status, json=body)


class FakeMeta:
    """Records every request it receives and answers per Meta's documented contract."""

    PATH = re.compile(r"^/v\d+\.\d+/(?P<pnid>[^/]+)/messages$")

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.override = None  # optional callable(request) -> Response | raises

    def __call__(self, request: httpx.Request) -> httpx.Response:
        try:
            body = json.loads(request.content.decode("utf-8")) if request.content else None
        except ValueError:
            body = "<<not json>>"
        self.requests.append(
            {
                "method": request.method,
                "host": request.url.host,
                "path": request.url.path,
                "headers": dict(request.headers),
                "body": body,
            }
        )
        if self.override is not None:
            return self.override(request)
        return self.handle(request, body)

    def handle(self, request: httpx.Request, body) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth[len("Bearer "):] != VALID_TOKEN:
            return meta_error(401, 190, "Access token has expired or is invalid",
                              details="Invalid OAuth access token - Cannot parse access token")

        match = self.PATH.match(request.url.path)
        if request.url.host != "graph.facebook.com" or not match or match.group("pnid") != VALID_PHONE_NUMBER_ID:
            return meta_error(400, 100, "Unsupported post request. Object does not exist, cannot be loaded "
                              "due to missing permissions, or does not support this operation",
                              type_="GraphMethodException")
        if request.method != "POST":
            return meta_error(400, 100, "Unsupported request method", type_="GraphMethodException")

        if not isinstance(body, dict) or body.get("messaging_product") != "whatsapp":
            return meta_error(400, 100, "Invalid parameter", type_="OAuthException",
                              details="Parameter messaging_product is required and must be 'whatsapp'")
        to = body.get("to")
        if not isinstance(to, str) or not to:
            return meta_error(400, 100, "Invalid parameter", details="Parameter 'to' is required")
        if body.get("type") != "text":
            return meta_error(400, 100, "Invalid parameter", details="Only type='text' is simulated here")
        text = (body.get("text") or {}).get("body")
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT_LENGTH:
            return meta_error(400, 100, "Invalid parameter",
                              details="text.body must be a non-empty string of at most 4096 characters")

        if to == UNLISTED_RECIPIENT:
            return meta_error(400, 131030, "Recipient phone number not in allowed list",
                              details="Recipient phone number not in allowed list: Add recipient phone "
                                      "number to recipient list and try again.")
        if to == WINDOW_CLOSED_RECIPIENT:
            return meta_error(400, 131047, "Re-engagement message",
                              details="Message failed to send because more than 24 hours have passed "
                                      "since the customer last replied to this number.")

        return httpx.Response(
            200,
            json={
                "messaging_product": "whatsapp",
                "contacts": [{"input": to, "wa_id": to}],
                "messages": [{"id": "wamid.HBgL" + "".join(random.choices(string.ascii_letters + string.digits, k=40))}],
            },
        )


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(f"stage3_offline_{name.replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))


async def call(module, fake: FakeMeta, *, token=VALID_TOKEN, pnid=VALID_PHONE_NUMBER_ID,
               to=ALLOWED_RECIPIENT, message="مرحبا! كيف أساعدك؟"):
    """
    Runs send_whatsapp() with `fake` underneath the real httpx client.
    Returns (returned, raised, log_records, client_init_kwargs).
    """
    init_kwargs: list[dict] = []
    transport = httpx.MockTransport(fake)

    class _Client(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            init_kwargs.append(dict(kwargs))
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    g = module.send_whatsapp.__globals__
    real = g["httpx"]
    g["httpx"] = SimpleNamespace(AsyncClient=_Client, HTTPError=httpx.HTTPError)

    logger = logging.getLogger("boltane.meta_client")
    cap, prev = _Capture(), logger.level
    logger.addHandler(cap)
    logger.setLevel(logging.INFO)
    returned = raised = None
    try:
        returned = await module.send_whatsapp(pnid, token, to, message)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    finally:
        g["httpx"] = real
        logger.removeHandler(cap)
        logger.setLevel(prev)
    return returned, raised, cap.records, init_kwargs


RESULTS: list[tuple[str, str, bool, str]] = []


def check(target: str, name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((target, name, bool(condition), detail))


# ---------------------------------------------------------------------------
# Tests (each takes a loaded module; results recorded via check())
# ---------------------------------------------------------------------------

async def t_request_conformance(target, module):
    fake = FakeMeta()
    returned, raised, logs, init = await call(module, fake)
    req = fake.requests[0] if fake.requests else {}
    body = req.get("body") or {}
    check(target, "request: exactly one POST to graph.facebook.com/v<N>.<N>/{id}/messages",
          len(fake.requests) == 1 and req.get("method") == "POST" and req.get("host") == "graph.facebook.com"
          and bool(FakeMeta.PATH.match(req.get("path", ""))), str(req.get("path")))
    check(target, "request: Bearer auth + JSON content-type",
          req.get("headers", {}).get("authorization") == f"Bearer {VALID_TOKEN}"
          and req.get("headers", {}).get("content-type", "").startswith("application/json"))
    check(target, "request: body has messaging_product/recipient_type/to/type/text.body/preview_url=false",
          body.get("messaging_product") == "whatsapp" and body.get("recipient_type") == "individual"
          and body.get("to") == ALLOWED_RECIPIENT and body.get("type") == "text"
          and body.get("text", {}).get("body") == "مرحبا! كيف أساعدك؟"
          and body.get("text", {}).get("preview_url") is False, json.dumps(body, ensure_ascii=False))
    check(target, "request: httpx client built with timeout=20.0", init and init[0].get("timeout") == 20.0, str(init))
    check(target, "happy path: returns None, raises nothing", returned is None and raised is None)
    check(target, "happy path: one INFO 'Meta send succeeded' with phone_number_id and to, no warnings",
          [lvl for lvl, _ in logs] == ["INFO"] and "succeeded" in logs[0][1]
          and VALID_PHONE_NUMBER_ID in logs[0][1] and ALLOWED_RECIPIENT in logs[0][1], str(logs))


async def t_body_round_trip(target, module):
    footer = "\n\n🤖 Powered by Boltane — https://boltane.ai"
    text = "أهلاً وسهلاً 👋 ساعات الدوام من ٩ صباحاً حتى ٥ مساءً. +963 999 123 456" + footer
    fake = FakeMeta()
    returned, raised, logs, _ = await call(module, fake, message=text)
    sent = (fake.requests[0]["body"] or {}).get("text", {}).get("body") if fake.requests else None
    check(target, "Arabic + emoji + branding footer survive the request body byte-for-byte", sent == text)
    check(target, "…and Meta accepts it (INFO succeeded)", returned is None and raised is None and logs and logs[0][0] == "INFO")

    exact = "x" * MAX_TEXT_LENGTH
    fake = FakeMeta()
    _, raised, logs, _ = await call(module, fake, message=exact)
    check(target, "a text of exactly 4096 chars is accepted", raised is None and logs[0][0] == "INFO", str(logs)[:120])


async def t_error_envelopes(target, module):
    statuses = [400, 401, 403, 404, 429, 500, 502, 503]
    envelopes = [
        (190, "Access token has expired or is invalid"),
        (100, "Invalid parameter"),
        (131030, "Recipient phone number not in allowed list"),
        (131047, "Re-engagement message"),
        (130429, "Rate limit hit"),
        (131056, "(Business Account, Consumer Account) pair rate limit hit"),
        (131000, "Something went wrong"),
        (1, "An unknown error occurred"),
        (2, "Service temporarily unavailable"),
        (4, "Application request limit reached"),
    ]
    bad = []
    for status in statuses:
        for code, title in envelopes:
            fake = FakeMeta()
            fake.override = lambda r, s=status, c=code, t=title: meta_error(s, c, t, details=t)
            returned, raised, logs, _ = await call(module, fake)
            ok = (
                returned is None and raised is None and len(fake.requests) == 1
                and len(logs) == 1 and logs[0][0] == "WARNING"
                and f"status={status}" in logs[0][1] and re.search(rf'"code"\s*:\s*{code}\b', logs[0][1])
                and VALID_PHONE_NUMBER_ID in logs[0][1] and ALLOWED_RECIPIENT in logs[0][1]
            )
            if not ok:
                bad.append((status, code, logs, raised))
    check(target, f"{len(statuses) * len(envelopes)} status x error-code combinations: never raises, exactly 1 request "
                  "(no retry), exactly 1 WARNING carrying status, code, phone_number_id and to",
          not bad, str(bad[:2]))

    # The two flows the task names explicitly, through the real simulator rules:
    for label, kwargs, want_code in (
        ("bad token", {"token": "wrong-token"}, 190),
        ("phone number not in allowed list", {"to": UNLISTED_RECIPIENT}, 131030),
        ("24h window closed", {"to": WINDOW_CLOSED_RECIPIENT}, 131047),
        ("unknown phone_number_id", {"pnid": "000000000000000"}, 100),
    ):
        fake = FakeMeta()
        returned, raised, logs, _ = await call(module, fake, **kwargs)
        check(target, f"simulator-decided error '{label}': WARNING with Meta code {want_code}, returns None, no raise",
              returned is None and raised is None and len(logs) == 1 and logs[0][0] == "WARNING"
              and "status=" in logs[0][1] and str(want_code) in logs[0][1], str(logs))


async def t_odd_bodies(target, module):
    cases = {
        "HTML gateway page (502)": httpx.Response(502, text="<html><body><h1>502 Bad Gateway</h1></body></html>"),
        "empty body (500)": httpx.Response(500, content=b""),
        "non-UTF8 bytes (503)": httpx.Response(503, content=b"\xff\xfe\x00broken"),
        "huge error body (400)": httpx.Response(400, json={"error": {"message": "x" * 5000, "code": 100}}),
    }
    for label, response in cases.items():
        fake = FakeMeta()
        fake.override = lambda r, resp=response: resp
        returned, raised, logs, _ = await call(module, fake)
        ok = returned is None and raised is None and len(logs) == 1 and logs[0][0] == "WARNING"
        if label.startswith("huge"):
            ok = ok and len(logs[0][1]) < 600
        check(target, f"odd response body — {label}: WARNING, no raise" + (" and log is truncated" if label.startswith("huge") else ""),
              ok, str(logs)[:160])


async def t_transport_exceptions(target, module):
    names = ["ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
             "ReadError", "WriteError", "RemoteProtocolError", "LocalProtocolError", "TooManyRedirects", "DecodingError"]
    bad = []
    for name in names:
        exc_class = getattr(httpx, name, None)
        if exc_class is None:
            continue
        fake = FakeMeta()

        def boom(request, cls=exc_class):
            raise cls("simulated transport failure", request=request)

        fake.override = boom
        returned, raised, logs, _ = await call(module, fake)
        if not (returned is None and raised is None and len(logs) == 1 and logs[0][0] == "WARNING"
                and "request error" in logs[0][1]):
            bad.append((name, raised, logs))
    check(target, "every httpx transport/timeout exception is swallowed into ONE 'request error' WARNING, no retry",
          not bad, str(bad[:2]))

    fake = FakeMeta()

    def not_httpx(request):
        raise RuntimeError("not an httpx error")

    fake.override = not_httpx
    returned, raised, logs, _ = await call(module, fake)
    check(target, "[characterization] a NON-httpx exception propagates (webhook.py's own try/except is what contains it)",
          isinstance(raised, RuntimeError) and not logs, repr(raised))


async def t_secrets_not_logged(target, module):
    leaked = []
    for kwargs in ({}, {"token": "wrong-token-SECRETVALUE"}, {"to": UNLISTED_RECIPIENT}):
        token = kwargs.get("token", VALID_TOKEN)
        fake = FakeMeta()
        _, _, logs, _ = await call(module, fake, **kwargs)
        if any(token in message for _, message in logs):
            leaked.append(kwargs)

    fake = FakeMeta()

    def boom(request):
        raise httpx.ConnectError("simulated", request=request)

    fake.override = boom
    _, _, logs, _ = await call(module, fake)
    if any(VALID_TOKEN in message for _, message in logs):
        leaked.append("request-error path")
    check(target, "the access token never appears in any log line (success, 4xx, transport error)", not leaked, str(leaked))


async def t_characterization(target, module):
    # 1. 2xx that isn't exactly 200 is treated as a FAILURE (Meta documents 200 for success).
    fake = FakeMeta()
    fake.override = lambda r: httpx.Response(201, json={"messages": [{"id": "wamid.X"}]})
    _, raised, logs, _ = await call(module, fake)
    check(target, "[characterization] HTTP 201 is logged as a failed send (only exactly 200 counts as success)",
          raised is None and logs and logs[0][0] == "WARNING" and "status=201" in logs[0][1], str(logs))

    # 2. No retry on 429 / 5xx — exactly one request, message is simply lost.
    for status, code in ((429, 130429), (503, 2)):
        fake = FakeMeta()
        fake.override = lambda r, s=status, c=code: meta_error(s, c, "throttled/unavailable")
        await call(module, fake)
        check(target, f"[characterization] HTTP {status} (Meta code {code}) is NOT retried — exactly 1 request",
              len(fake.requests) == 1)

    # 3. No client-side 4096 check: an oversized text is sent as-is and Meta rejects it.
    fake = FakeMeta()
    _, raised, logs, _ = await call(module, fake, message="y" * (MAX_TEXT_LENGTH + 1))
    check(target, "[characterization] a 4097-char text is NOT pre-validated: it is sent, Meta rejects it, WARNING logged",
          len(fake.requests) == 1 and raised is None and logs and logs[0][0] == "WARNING", str(logs)[:120])

    # 4. A fresh client per call, no state leaking between sends.
    fake = FakeMeta()
    _, _, _, init1 = await call(module, fake)
    _, _, _, init2 = await call(module, fake, to=UNLISTED_RECIPIENT)
    check(target, "two consecutive sends are independent (success then failure, one request each)",
          len(fake.requests) == 2 and len(init1) == 1 and len(init2) == 1)


TESTS = [t_request_conformance, t_body_round_trip, t_error_envelopes, t_odd_bodies,
         t_transport_exceptions, t_secrets_not_logged, t_characterization]


async def main() -> int:
    print(f"httpx {httpx.__version__}")
    for target, path in TARGETS.items():
        if not path.exists():
            print(f"SKIP {target}: {path} not found")
            continue
        module = load_module(target, path)
        for test in TESTS:
            await test(target, module)

    failed = [r for r in RESULTS if not r[2]]
    current = None
    for target, name, ok, detail in RESULTS:
        if target != current:
            print(f"\n=== {target}  ({TARGETS[target].relative_to(REPO_ROOT)})")
            current = target
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok or not detail else f"\n        -> {detail}"))
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed or not RESULTS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
