"""
Webhook routes for Meta's WhatsApp Cloud API.

Meta requires POST /webhook to return 200 OK within roughly 3 seconds
of delivery, or it treats the message as a failed delivery. The real
pipeline (Supabase lookup, AI reply, Meta send) takes longer than
that, so none of it can happen before the HTTP response is sent. It's
scheduled as a FastAPI background task instead, which only starts
running after the response has already gone out over the wire.

Supabase, OpenRouter (call_ai), and Meta send (send_whatsapp) are all
real now. Every POST /webhook is also required to carry a valid
X-Hub-Signature-256 header — computed by Meta over the raw request
body using the App Secret — before anything in it is trusted. This
closes a real gap: without it, anyone who found the webhook URL could
POST fake messages and have the bot act on them.

package (one of "low-tier", "basic", "pro") and is_api_free are two
independent flags. package alone gates Boltane branding (the footer
and the "mention only if asked" instruction) — branding shows only
for package == "low-tier"; is_api_free alone gates the shared
platform-key monthly cap — the cap applies when is_api_free is true,
since that's the case actually drawing on the shared quota, and it
exempts BYOK stores regardless of package. Neither flag should be
inferred from the other. package == "basic" further splits into two
different monthly caps depending on is_trial — see
resolve_monthly_conversation_cap() below.

Usage is metered in conversations, not raw messages: a "conversation"
is all messages from one customer_phone to one store within a rolling
2h window (Boltane's own definition — see CONVERSATION_WINDOW_HOURS in
supabase_client.py; this is NOT Meta's 24h WhatsApp billing-conversation
concept). Only a new conversation counts against the cap and increments
monthly_conversation_count; a message continuing an existing window
skips the cap check entirely and goes straight to the AI call.

USAGE-PROTECTION LAYERS ("the token firewall"), enforced in the order
they appear in _process_message, each independent of the others:
  1. Per-message input cap (MAX_INPUT_TOKENS_PER_MESSAGE, token_limits.py)
     — an oversized customer message is rejected before it ever reaches
     the model or touches the conversation window/counters at all.
  2. Per-conversation token cap (CONVERSATION_TOKEN_CAP, token_limits.py)
     — enforced inside is_new_conversation_and_touch(): once a
     conversation's cumulative real token usage reaches this, the next
     message is treated as a new conversation (fresh token budget AND
     it counts against the monthly cap), even if the 2h window hasn't
     expired.
  3. Monthly conversation cap (resolve_monthly_conversation_cap() below)
     — unchanged in spirit from before, now trial-aware for "basic".
  4. Per-message output cap (MAX_OUTPUT_TOKENS_PER_MESSAGE) — enforced
     inside ai_client.call_ai() as `max_tokens` on every OpenRouter
     call; nothing to do here except pass package/store info through.
Together these bound the worst case a single customer (or a single
conversation) can cost, on top of the existing monthly ceiling.
"""

import asyncio
import hashlib
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import get_settings
from app.services.ai_client import call_ai
from app.services.meta_client import send_whatsapp
from app.services.supabase_client import (
    add_conversation_tokens,
    get_store_by_phone_number_id,
    is_new_conversation_and_touch,
    update_store_usage,
)
from app.services.token_limits import MAX_INPUT_TOKENS_PER_MESSAGE, estimate_tokens
from app.worker_ctx import get_worker_ctx

try:  # V2-02B: the V2 selector. If it cannot load, V1 runs exactly as it did before.
    from app.v2.pipeline.entry import route_delivery as _v2_route_delivery
except Exception:  # V2 must never be able to stop V1 from starting
    _v2_route_delivery = None

logger = logging.getLogger("boltane.webhook")

router = APIRouter()

# How many NEW conversations (not raw messages — see module docstring)
# a store on the shared PLATFORM key gets per month, before the bot
# stops calling the AI and sends a fixed limit-reached notice instead.
# This protects the shared OpenRouter quota — it does NOT apply to
# BYOK stores (is_api_free=false), since they're using their own key
# and never draw on that shared quota.
#
# low-tier and pro have one cap each regardless of anything else.
# basic is split further by is_trial (see resolve_monthly_conversation_cap
# below) — a referral-code free trial gets the smaller cap, a
# converted, actually-paying subscriber gets the larger one. There's
# no billing automation, so is_trial is a manually-set stores column;
# see stores_table.sql for who sets it and when.
PACKAGE_MONTHLY_CONVERSATION_CAP = {"low-tier": 40, "pro": 1000}
BASIC_TRIAL_MONTHLY_CONVERSATION_CAP = 100
BASIC_PAID_MONTHLY_CONVERSATION_CAP = 300


def resolve_monthly_conversation_cap(package: str, is_trial: bool) -> int:
    """
    One place, not scattered if/else — a future cap change for any
    tier is a one-line edit here.

    package='basic' means two different things sharing one value:
    a referral-code-granted free trial (is_trial=True, the smaller
    cap) and a converted, actually-paying Basic subscriber
    (is_trial=False, the larger, unchanged-from-before cap). Every
    other package ignores is_trial entirely.

    An unrecognized package falls back to the paid Basic cap (300) —
    the same safe default this function replaces used before is_trial
    existed, and the DB's own check constraint means this should never
    actually be hit in practice.
    """
    if package == "basic":
        return BASIC_TRIAL_MONTHLY_CONVERSATION_CAP if is_trial else BASIC_PAID_MONTHLY_CONVERSATION_CAP
    return PACKAGE_MONTHLY_CONVERSATION_CAP.get(package, BASIC_PAID_MONTHLY_CONVERSATION_CAP)


MONTHLY_LIMIT_REACHED_MESSAGE = (
    "This store has reached its monthly conversation limit. "
    "Please contact the store directly, or check back next month."
)

# Sent instead of calling the AI at all when a customer's single
# message estimates over MAX_INPUT_TOKENS_PER_MESSAGE. Rejecting
# (rather than silently truncating) was the deliberate choice here:
# truncation risks cutting off the actual question and having the
# model confidently answer something the customer didn't ask, which is
# worse than asking them to shorten it. This never touches the
# conversation window, the token counter, or the monthly cap — it's a
# hard technical limit on a single message, orthogonal to all of them.
MESSAGE_TOO_LONG_REPLY = (
    "That message is a bit long for me to process in one go — could "
    "you break it into a shorter question?"
)

# Appended to system_prompt for low-tier stores (see module docstring
# — branding is gated on package == "low-tier" only), so the AI knows
# the rule: mention Boltane only if directly asked, never unprompted,
# never at the expense of answering the customer's actual question.
BOLTANE_MENTION_INSTRUCTION = (
    "\n\nIf — and only if — the customer directly asks who built this "
    "bot, what platform it runs on, or something clearly equivalent, "
    "you may mention it was built with Boltane. Never bring this up "
    "unprompted, and never let it interrupt or delay answering the "
    "customer's actual question."
)

# Plain https:// URL so WhatsApp auto-links it as tappable text.
# Placeholder domain — swap in the real production domain.
BOLTANE_URL = "https://boltane.ai"
BOLTANE_BRANDING_FOOTER = f"\n\n🤖 Powered by Boltane — {BOLTANE_URL}"



@router.get("/webhook")
async def verify_webhook(
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
) -> PlainTextResponse:
    """
    Meta's one-time verification handshake, sent when the webhook URL
    is registered in the Meta App Dashboard. Must echo back
    hub.challenge as plain text if hub.verify_token matches.
    """
    if hub_verify_token == get_settings().META_VERIFY_TOKEN:
        return PlainTextResponse(content=hub_challenge, status_code=200)

    return PlainTextResponse(content="verification token mismatch", status_code=403)


def _verify_meta_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """
    Verifies Meta's X-Hub-Signature-256 header: an HMAC-SHA256 of the
    raw request body, keyed with the App Secret, sent as
    "sha256=<hex digest>". Constant-time comparison to avoid a timing
    side-channel.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected_signature = "sha256=" + hmac.new(
        get_settings().META_APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected_signature, signature_header)


@router.post("/webhook", response_model=None)
async def receive_message(
    request: Request, background_tasks: BackgroundTasks
) -> dict | PlainTextResponse | JSONResponse:
    """
    Receives a WhatsApp message payload from Meta.

    The signature is checked against the raw body FIRST, before the
    payload is parsed or anything is scheduled — an invalid or
    missing signature is rejected with 403 and nothing further
    happens. Once verified, returns 200 immediately; the full
    pipeline runs afterward as a background task so it can never
    delay Meta's required acknowledgement.
    """
    raw_body = await request.body()
    signature_header = request.headers.get("x-hub-signature-256")

    if not _verify_meta_signature(raw_body, signature_header):
        logger.warning("rejected POST /webhook: missing or invalid X-Hub-Signature-256")
        return PlainTextResponse(content="invalid signature", status_code=403)

    payload = await request.json()

    # V2-02B selector. With every V2 flag off (the default) it answers None at once and V1 carries on below,
    # unchanged. When V2 takes the delivery it has already stored it, and V1 only passes its answer on.
    # It runs before the payload is logged below, so a delivery that V2 takes never reaches that log line.
    if _v2_route_delivery is not None:
        try:
            verdict = await _v2_route_delivery(payload, request.scope.get("env"), get_worker_ctx(request.scope))
        except Exception:
            logger.exception("V2 selector failed; continuing with V1")
            verdict = None
        if verdict is not None:
            logger.info("%s", verdict.note)
            return JSONResponse(content=verdict.body, status_code=verdict.status)

    logger.info("received webhook payload: %s", payload)

    # Stage 6 — the one deliberate difference from Render's receive_message.
    # On Workers the pipeline is handed to ctx.waitUntil(): the 200 goes out at once and the work keeps running
    # for up to ~30 s after it (measured on a deployed probe: 28 s finished, 35 s was cancelled). FastAPI's
    # BackgroundTasks would instead hold the response until the whole pipeline ended (5.7 s / 40.9 s / 70.0 s
    # measured for tasks of 5 / 40 / 70 s). Outside a Worker (offline tests) there is no ctx and the original
    # BackgroundTasks path is used unchanged.
    ctx = get_worker_ctx(request.scope)
    if ctx is not None:
        task = asyncio.ensure_future(_process_message(payload))
        try:
            ctx.waitUntil(task)
        except Exception:
            # The task is already running; it just is not protected from cancellation once the response ends.
            logger.exception("ctx.waitUntil failed — the pipeline may be cancelled when the response ends")
    else:
        background_tasks.add_task(_process_message, payload)

    return {"status": "received"}


async def _process_message(payload: dict) -> None:
    """
    The real background pipeline: parse the payload, look up the
    store, enforce the per-message input cap, determine the
    conversation window (including the token-cap-forced reset),
    enforce the monthly cap, get an AI reply, record usage (both the
    monthly counter and the conversation's running token total), and
    send it back. Every external step is individually guarded so one
    malformed payload or one bad store row can never crash the process
    or take down a message for every other store.
    """
    logger.info("background task started")

    # --- Step 1: extract what we need from the payload ---
    try:
        entry = payload["entry"][0]["changes"][0]["value"]
        phone_number_id = entry["metadata"]["phone_number_id"]
        message = entry["messages"][0]
        customer_number = message["from"]
        # Non-text messages (image, button, etc.) won't have "text" —
        # default to "" rather than raising.
        customer_text = message.get("text", {}).get("body", "")
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning("could not parse webhook payload, dropping: %s", exc)
        return

    # --- Step 2: look up the store ---
    try:
        store = await get_store_by_phone_number_id(phone_number_id)
    except Exception:
        logger.exception("Supabase lookup failed for phone_number_id=%s", phone_number_id)
        return

    if store is None:
        logger.info("no store found for phone_number_id=%s — dropping", phone_number_id)
        return

    # --- Step 3: token firewall, part 1 — reject an oversized single message ---
    # Estimated (not exact — nothing has been sent to a model yet), but
    # deliberately conservative so it never underestimates and lets an
    # oversized message through. This is checked before anything else
    # below: it never touches the conversation window, the token
    # counter, or the monthly cap, and it never calls the AI.
    estimated_input_tokens = estimate_tokens(customer_text)
    if estimated_input_tokens > MAX_INPUT_TOKENS_PER_MESSAGE:
        logger.info(
            "message from %s to store %s is too long (~%d estimated tokens > %d) — "
            "rejecting without calling the AI",
            customer_number, store.get("id"), estimated_input_tokens, MAX_INPUT_TOKENS_PER_MESSAGE,
        )
        try:
            await send_whatsapp(phone_number_id, store.get("access_token"), customer_number, MESSAGE_TOO_LONG_REPLY)
        except Exception:
            logger.exception("send_whatsapp failed while sending message-too-long reply")
        return

    # package: which pricing tier the store is on — "low-tier" gets
    # Boltane branding and the smaller free-model cap; "basic"/"pro"
    # get neither (see PACKAGE_DEFAULT_MODELS in ai_client.py).
    # Defaults "basic" to match the DB column's default if it's ever
    # missing from a row.
    package = store.get("package", "basic")

    # is_trial only affects package == "basic" — see
    # resolve_monthly_conversation_cap(). Defaults True (the stricter,
    # smaller cap) matching the DB column's own default, so a row
    # missing this never silently gets the larger paid allowance.
    is_trial = store.get("is_trial", True)

    # is_api_free: true = store is running on Boltane's shared free
    # platform API key (the default for a new signup with no BYOK key
    # supplied); false = store brought its own OpenRouter key (BYOK).
    # Independent of package — this only affects (a) trying the
    # store's own model first [already handled by ai_client.py
    # whenever store.ai_model is set] and (b) the shared platform-key
    # monthly cap below. It has no effect on branding.
    is_api_free = store.get("is_api_free", True)

    conversation_count = store.get("monthly_conversation_count", 0)
    cap = resolve_monthly_conversation_cap(package, is_trial)

    # --- Step 4: token firewall, part 2 — resolve the conversation window ---
    # A message that continues an existing conversation (same
    # customer, same store, within the 2h window AND under the
    # per-conversation token cap) never counts against the monthly cap
    # and never increments the usage counter — only a brand-new
    # conversation does, whether that's from a natural time gap or
    # from the token cap forcing a reset (see
    # is_new_conversation_and_touch's docstring). On an unexpected
    # failure here, fail safe by treating it as a new conversation
    # with a zero token count, so a transient Supabase hiccup can't
    # silently grant unlimited free conversations or an inflated token
    # budget.
    try:
        is_new_conversation, conversation_token_count = await is_new_conversation_and_touch(
            store["id"], customer_number
        )
    except Exception:
        logger.exception(
            "conversation-window lookup failed for store %s — treating as new conversation",
            store.get("id"),
        )
        is_new_conversation = True
        conversation_token_count = 0

    # --- Step 5: enforce the monthly conversation cap ---
    if is_api_free and is_new_conversation and conversation_count >= cap:
        logger.info(
            "store %s is over its %s-package cap (%d new conversations/month, is_trial=%s) — "
            "sending limit message",
            store.get("id"), package, cap, is_trial,
        )
        try:
            await send_whatsapp(
                phone_number_id, store.get("access_token"), customer_number, MONTHLY_LIMIT_REACHED_MESSAGE
            )
        except Exception:
            logger.exception("send_whatsapp failed while sending limit-reached message")
        return

    # --- Step 6: get an AI reply, record usage, send it back ---
    system_prompt = store.get("system_prompt", "")
    if package == "low-tier":
        system_prompt += BOLTANE_MENTION_INSTRUCTION

    try:
        reply, model_used, tokens_used = await call_ai(
            system_prompt,
            customer_text,
            package=package,
            store_ai_model=store.get("ai_model"),
            store_api_key=store.get("api_key"),
            store_id=store.get("id"),
        )
    except Exception:
        # call_ai() already handles ordinary OpenRouter failures (429,
        # unavailable models, etc.) internally and returns a graceful
        # reply for those — this catches only genuinely unexpected bugs.
        logger.exception("call_ai failed unexpectedly for store %s", store.get("id"))
        return

    # Only a new conversation increments the counter — a message
    # continuing an existing window leaves it exactly where it was.
    new_count = conversation_count + 1 if is_new_conversation else conversation_count
    # conversations_remaining only means something for stores on the
    # shared platform cap — BYOK stores (is_api_free=False) have no
    # cap, so it's left as None (displays as unlimited/not-applicable)
    # for them.
    remaining = max(cap - new_count, 0) if is_api_free else None

    try:
        await update_store_usage(
            store["id"], new_count, conversations_remaining=remaining, last_used_model=model_used
        )
    except Exception:
        # Not fatal to the reply itself — the customer still gets an
        # answer even if this write fails; it'll just undercount.
        logger.exception("failed to update usage fields for store %s", store.get("id"))

    # Token firewall bookkeeping: add this turn's real (not estimated)
    # usage onto the conversation's running total, so the NEXT message
    # in this window sees an accurate count against CONVERSATION_TOKEN_CAP.
    # A failure here doesn't block the reply, but it does mean this
    # conversation's token accounting undercounts going forward — worth
    # knowing about, not worth losing the customer's reply over.
    try:
        await add_conversation_tokens(store["id"], customer_number, tokens_used, conversation_token_count)
    except Exception:
        logger.exception("failed to update conversation token count for store %s", store.get("id"))

    if package == "low-tier":
        reply += BOLTANE_BRANDING_FOOTER

    try:
        await send_whatsapp(phone_number_id, store.get("access_token"), customer_number, reply)
    except Exception:
        logger.exception("send_whatsapp failed while sending AI reply")

    logger.info("background task finished for phone_number_id=%s", phone_number_id)
