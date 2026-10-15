"""
TEMPORARY Stage 4 diagnostic route — runs the REAL ai_client.call_ai inside
the Workers runtime so test_stage4_real.py can compare it with the Render
original and with the same code under plain CPython.

    POST /debug/ai   {"system": "...", "message": "...", "package": "basic"}
      -> {"reply", "model_used", "tokens_used", "elapsed_s", "models_tried",
          "outbound_user", "graceful", "log",
          "traces", "body_sha256"}

Inert unless STAGE4_DEBUG_ENABLED=1 is set in .dev.vars (answers 404
otherwise) — same pattern as the Stage 2/3 debug routes. It spends real
OpenRouter credit with the key in .dev.vars, uses the shared platform key
only (no store key, no store model), and exists purely for local
`pywrangler dev` runs. Remove this file and its two lines in main.py before
Stage 6 / before anything is deployed.

"outbound_user" is the text of the user message actually sent to OpenRouter
(after phone-number masking). Capturing it needs a temporary wrapper around
httpx.AsyncClient.stream; it is restored in a finally block, and this route
is meant to be called one request at a time.
"""

import hashlib
import json
import logging
import time

import httpx
from fastapi import APIRouter, HTTPException, Request

from app.services import ai_client

router = APIRouter()

VALID_PACKAGES = ("basic", "pro", "low-tier")


def _enabled() -> bool:
    from workers import env

    return str(getattr(env, "STAGE4_DEBUG_ENABLED", "") or "") == "1"


class _TappedCtx:
    """Wraps httpx's stream context manager so the SSE lines can be inspected WITHOUT changing them:
    records which provider served the call, the finish reason, usage, how much was content vs
    reasoning, and (diagnostics only) the HTTP version / content-encoding the runtime negotiated."""

    def __init__(self, inner, traces):
        self._inner, self._traces = inner, traces

    async def __aenter__(self):
        resp = await self._inner.__aenter__()
        trace = {"provider": None, "finish_reason": None, "usage": None, "content_chars": 0,
                 "reasoning_chars": 0, "data_lines": 0, "status": getattr(resp, "status_code", None)}
        for key, getter in (("http_version", lambda: resp.http_version),
                            ("content_type", lambda: resp.headers.get("content-type")),
                            ("content_encoding", lambda: resp.headers.get("content-encoding"))):
            try:
                trace[key] = getter()
            except Exception:  # noqa: BLE001 — diagnostics must never break the call
                trace[key] = None
        self._traces.append(trace)
        original = resp.aiter_lines

        async def tapped():
            async for line in original():
                stripped = line.strip()
                if stripped.startswith("data:") and not stripped.endswith("[DONE]"):
                    trace["data_lines"] += 1
                    try:
                        event = json.loads(stripped[5:].strip())
                    except ValueError:
                        event = None
                    if event:
                        trace["provider"] = event.get("provider") or trace["provider"]
                        for choice in event.get("choices") or []:
                            delta = choice.get("delta") or {}
                            trace["content_chars"] += len(delta.get("content") or "")
                            trace["reasoning_chars"] += len(delta.get("reasoning") or delta.get("reasoning_content") or "")
                            trace["finish_reason"] = choice.get("finish_reason") or trace["finish_reason"]
                        if event.get("usage"):
                            trace["usage"] = event["usage"]
                yield line

        resp.aiter_lines = tapped
        return resp

    async def __aexit__(self, *exc):
        return await self._inner.__aexit__(*exc)


def _body_sha256(body):
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


class _CollectWarnings(logging.Handler):
    """Keeps ai_client's WARNING+ log lines for this one call, so a failed call can say WHY."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(f"{record.levelname}: {record.getMessage()[:300]}")


async def run_call(system: str, message: str, package: str) -> dict:
    bodies: list[dict] = []
    traces: list[dict] = []
    collector = _CollectWarnings()
    logger = logging.getLogger("boltane.ai_client")
    logger.addHandler(collector)
    client_class = httpx.AsyncClient
    original_stream = client_class.stream

    def recording_stream(self, method, url, **kwargs):
        if kwargs.get("json") is not None:
            bodies.append(kwargs["json"])
        return _TappedCtx(original_stream(self, method, url, **kwargs), traces)

    client_class.stream = recording_stream
    started = time.perf_counter()
    try:
        reply, model_used, tokens_used = await ai_client.call_ai(
            system, message, package=package,
            store_ai_model=None, store_api_key=None, store_id="stage4-debug",
        )
    finally:
        client_class.stream = original_stream
        logger.removeHandler(collector)

    return {
        "reply": reply,
        "model_used": model_used,
        "tokens_used": tokens_used,
        "elapsed_s": round(time.perf_counter() - started, 2),
        "models_tried": [b["model"] for b in bodies],
        "outbound_user": bodies[-1]["messages"][-1]["content"] if bodies else None,
        "graceful": reply == ai_client.GRACEFUL_FAILURE_REPLY,
        "log": collector.lines,
        "traces": traces,
        "body_sha256": _body_sha256(bodies[-1]) if bodies else None,
    }


@router.post("/debug/ai")
async def debug_ai(request: Request) -> dict:
    if not _enabled():
        raise HTTPException(status_code=404)
    payload = await request.json()
    system, message, package = payload.get("system"), payload.get("message"), payload.get("package")
    if not isinstance(system, str) or not isinstance(message, str) or package not in VALID_PACKAGES:
        raise HTTPException(status_code=422, detail="need string 'system', string 'message', and package in " + str(VALID_PACKAGES))
    return await run_call(system, message, package)
