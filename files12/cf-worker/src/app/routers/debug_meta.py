"""
TEMPORARY Stage 3 diagnostic — remove before Stage 6 / before anything
here is ever deployed (same status as debug_settings.py from Stage 1).

Exists only to exercise app.services.meta_client.send_whatsapp() for
real under `pywrangler dev`, and to hand back a JSON record of exactly
what happened: what the call returned, whether ANY exception escaped
it, and every log line meta_client wrote. test_stage3_meta.py runs the
same scenarios through the Render copy of send_whatsapp() and diffs
the two records.

Two things live in this file on purpose:
  - run_case(): pure scenario logic. It imports nothing from `app.*`
    and nothing Workers-specific, and takes send_whatsapp as an
    argument — so the exact same code can be loaded by path and run
    against the Render code from a plain Python process.
  - the route at the bottom: Workers-only glue (reads the test
    credentials from `.dev.vars` via `workers.env`).

Guard rails, since this is a route that can trigger a real WhatsApp
send:
  - 404 unless STAGE3_DEBUG_ENABLED=1 is set in .dev.vars. A
    deployed Worker would never have that variable, so a forgotten
    router is inert.
  - the recipient is NEVER taken from the request — only from
    META_TEST_RECIPIENT. The only request input is `case`, and it
    must be one of the names in CASES.
  - the only case that sends a real message is "happy" (test number,
    test recipient). Every other case uses a token that is invalid on
    purpose, or a fictional 555-01xx recipient Meta cannot deliver to.
  - the response never contains the access token.
"""

import logging
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException

router = APIRouter()

LOGGER_NAME = "boltane.meta_client"

# Cheap / no-real-send cases first, the one real send last.
CASES = ("bad_token", "network_error", "bad_phone_number_id", "unlisted_recipient", "happy")

# Credentials each case needs. bad_token and network_error need none
# at all — they can run with zero Meta access.
REQUIRED = {
    "bad_token": (),
    "network_error": (),
    "bad_phone_number_id": ("access_token",),
    "unlisted_recipient": ("access_token", "phone_number_id"),
    "happy": ("access_token", "phone_number_id", "recipient"),
}

INVALID_TOKEN = "stage3-deliberately-invalid-token"
NONEXISTENT_PHONE_NUMBER_ID = "999999999999999"
# 555-0100..0199 is the reserved fictional range for NANP numbers.
FICTIONAL_RECIPIENT = "15555550100"
# ".invalid" is a reserved TLD that is guaranteed never to resolve.
UNREACHABLE_URL = "https://stage3-unreachable.invalid/messages"


class _CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append({"level": record.levelname, "message": record.getMessage()})


def _unreachable_httpx(real_httpx):
    """
    A stand-in for the `httpx` name INSIDE meta_client's namespace only
    (not a global patch): same AsyncClient and the same HTTPError, but
    every .post() is redirected to a host that can never resolve. The
    request still goes through the real transport (fetch on Workers,
    real sockets on Render) — the point is to see what that transport
    raises on a connection failure, and whether meta_client's
    `except httpx.HTTPError` actually catches it there.
    """

    class _UnreachableClient(real_httpx.AsyncClient):
        async def post(self, url, *args, **kwargs):
            return await super().post(UNREACHABLE_URL, *args, **kwargs)

    return SimpleNamespace(AsyncClient=_UnreachableClient, HTTPError=real_httpx.HTTPError)


def _call_args(case: str, creds: dict) -> tuple[str, str, str, str]:
    """(phone_number_id, access_token, to, token_kind) for a case."""
    real_token = creds.get("access_token")
    real_id = creds.get("phone_number_id")
    if case == "happy":
        return real_id, real_token, creds["recipient"], "real"
    if case == "bad_phone_number_id":
        return NONEXISTENT_PHONE_NUMBER_ID, real_token, FICTIONAL_RECIPIENT, "real"
    if case == "unlisted_recipient":
        return real_id, real_token, FICTIONAL_RECIPIENT, "real"
    # bad_token / network_error: invalid token, so nothing can be sent
    # even if the network_error redirect somehow failed to apply.
    return real_id or NONEXISTENT_PHONE_NUMBER_ID, INVALID_TOKEN, FICTIONAL_RECIPIENT, "invalid-on-purpose"


async def run_case(case: str, send_whatsapp, creds: dict, runtime: str) -> dict:
    """
    Runs one scenario and returns a JSON-serialisable record. Never
    raises — an exception escaping send_whatsapp() is captured into
    the record, because "did it escape?" is itself the thing being
    measured (send_whatsapp's docstring promises it never raises for
    ordinary send failures).
    """
    missing = [name for name in REQUIRED[case] if not creds.get(name)]
    if missing:
        return {"case": case, "runtime": runtime, "skipped": "missing credentials: " + ", ".join(missing)}

    phone_number_id, token, to, token_kind = _call_args(case, creds)
    text = f"Boltane Stage 3 test [{runtime}] {datetime.now(timezone.utc):%H:%M:%S}Z"

    logger = logging.getLogger(LOGGER_NAME)
    handler = _CaptureHandler()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)  # the module's "succeeded" line is INFO

    module_globals = send_whatsapp.__globals__
    real_httpx = module_globals["httpx"]
    if case == "network_error":
        module_globals["httpx"] = _unreachable_httpx(real_httpx)

    raised = None
    returned = None
    started = time.perf_counter()
    try:
        returned = await send_whatsapp(phone_number_id, token, to, text)
    except Exception as exc:  # noqa: BLE001 — capturing is the whole point
        raised = f"{type(exc).__module__}.{type(exc).__qualname__}: {str(exc)[:200]}"
    finally:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        module_globals["httpx"] = real_httpx
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    return {
        "case": case,
        "runtime": runtime,
        "token_kind": token_kind,
        "phone_number_id": phone_number_id,
        "to": to,
        "raised": raised,
        "returned": None if returned is None else repr(returned),
        "elapsed_ms": elapsed_ms,
        "logs": handler.records,
    }


def _env_value(env, name: str) -> str | None:
    try:
        value = getattr(env, name)
    except Exception:  # noqa: BLE001 — a missing binding just means "not set"
        return None
    if value is None:
        return None
    value = str(value)
    return value or None


@router.get("/debug/meta-send")
async def debug_meta_send(case: str) -> dict:
    # Imported here, not at module top, so this file can also be loaded
    # by path from a plain Python process (see module docstring).
    from workers import env

    if _env_value(env, "STAGE3_DEBUG_ENABLED") != "1":
        raise HTTPException(status_code=404)
    if case not in CASES:
        raise HTTPException(status_code=400, detail=f"unknown case; expected one of {list(CASES)}")

    from app.services.meta_client import send_whatsapp

    creds = {
        "access_token": _env_value(env, "META_TEST_ACCESS_TOKEN"),
        "phone_number_id": _env_value(env, "META_TEST_PHONE_NUMBER_ID"),
        "recipient": _env_value(env, "META_TEST_RECIPIENT"),
    }
    return await run_case(case, send_whatsapp, creds, runtime="workers")
