"""
Cloudflare Python Workers -- 3-point proof of concept.

Tests, in isolation, the three items flagged as "unconfirmed" in the
Core Engine -> Workers feasibility audit. This is a throwaway scratch
project -- it does not touch the real Core Engine repo, and none of
this code is meant to be copied into it directly.

    1. Does FastAPI's BackgroundTasks actually keep running after the
       response is returned (i.e. is it bridged to ctx.waitUntil()
       under workers.asgi), or does the Workers runtime tear the work
       down early?
    2. Does a real streamed (SSE) chat-completion call to OpenRouter,
       via httpx.AsyncClient().stream() + aiter_lines(), come back
       intact and un-truncated -- the same pattern ai_client.py uses?
    3. Does supabase-py's native ASYNC client (acreate_client) work for
       a simple read -- the replacement for the sync-client-wrapped-in-
       asyncio.to_thread pattern that is confirmed broken on Workers?

Run locally with:
    uv run pywrangler dev

Then exercise the endpoints below (see README.md for exact commands)
and watch BOTH the JSON response AND the `pywrangler dev` terminal log
output -- test 1's real signal is a log line appearing ~2s AFTER the
response already returned, not anything in the response itself.

Secrets go in a local .dev.vars file (see .dev.vars.example), which
must NEVER be committed:
    OPENROUTER_API_KEY=...
    SUPABASE_URL=...
    SUPABASE_KEY=...
    SUPABASE_TEST_TABLE=stores        # any small, real table is fine
"""

import json
import logging
import time

import httpx
from fastapi import BackgroundTasks, FastAPI, Request

from workers import asgi

logger = logging.getLogger("poc")
app = FastAPI()

# In-memory only -- fine for this throwaway local-dev test, NOT a
# pattern to carry into the real Core Engine. Cloudflare explicitly
# warns against relying on global/module state in production, since
# separate requests aren't guaranteed to hit the same Worker instance.
# For this local test it just gives you a second way (besides the
# terminal log) to see which background calls actually completed.
_background_markers: list[dict] = []


# ---------------------------------------------------------------------------
# Test 1 -- BackgroundTasks + ctx.waitUntil()
# ---------------------------------------------------------------------------

def _do_background_work(call_id: int) -> None:
    time.sleep(2)  # stand-in for the real pipeline's Supabase+AI+Meta latency
    marker = {"call_id": call_id, "finished_at": time.time()}
    _background_markers.append(marker)
    logger.info("BACKGROUND TASK COMPLETED: %s", marker)


@app.get("/test/background")
async def test_background(background_tasks: BackgroundTasks, call_id: int = 0):
    background_tasks.add_task(_do_background_work, call_id)
    return {"status": "response sent", "call_id": call_id, "sent_at": time.time()}


@app.get("/test/background-check")
async def test_background_check():
    """Call this a few seconds AFTER a batch of /test/background calls
    to see which markers actually landed."""
    return {"markers_received": _background_markers, "count": len(_background_markers)}


# ---------------------------------------------------------------------------
# Test 2 -- OpenRouter SSE streaming via httpx (same pattern as ai_client.py)
# ---------------------------------------------------------------------------

@app.get("/test/stream")
async def test_stream(
    request: Request,
    prompt: str = "Write a 300-word story about a lighthouse keeper.",
):
    env = request.scope["env"]
    api_key = env.OPENROUTER_API_KEY

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": "deepseek/deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 500,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    full_reply = ""
    usage = None
    chunk_count = 0
    error_detail = None
    http_status = None

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST",
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=body,
            ) as response:
                http_status = response.status_code
                if response.status_code != 200:
                    await response.aread()
                    return {
                        "ok": False,
                        "status": response.status_code,
                        "body": response.text[:500],
                    }

                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        continue
                    chunk_count += 1
                    try:
                        event = json.loads(payload)
                    except ValueError:
                        continue
                    choices = event.get("choices") or []
                    if choices:
                        delta = (choices[0].get("delta") or {}).get("content")
                        if delta:
                            full_reply += delta
                    if event.get("usage"):
                        usage = event["usage"]
    except Exception as exc:  # noqa: BLE001 -- want the raw error text for the PoC
        error_detail = f"{type(exc).__name__}: {exc}"

    return {
        "ok": error_detail is None and bool(full_reply),
        "error": error_detail,
        "http_status": http_status,
        "chunk_count": chunk_count,
        "reply_length_chars": len(full_reply),
        "reply_preview": full_reply[:200],
        "reply_full": full_reply,
        "usage": usage,
    }


# ---------------------------------------------------------------------------
# Test 3 -- supabase-py's ASYNC client (acreate_client), not sync-in-a-thread
# ---------------------------------------------------------------------------

@app.get("/test/supabase")
async def test_supabase(request: Request):
    env = request.scope["env"]
    error_detail = None
    row_count = None

    try:
        from supabase import acreate_client

        table = getattr(env, "SUPABASE_TEST_TABLE", None) or "stores"
        client = await acreate_client(env.SUPABASE_URL, env.SUPABASE_KEY)
        response = await client.table(table).select("*", count="exact").limit(1).execute()
        row_count = response.count
    except Exception as exc:  # noqa: BLE001
        error_detail = f"{type(exc).__name__}: {exc}"

    return {"ok": error_detail is None, "error": error_detail, "row_count": row_count}


@app.get("/health")
async def health():
    return {"status": "ok"}


Default = asgi.entrypoint(app)
