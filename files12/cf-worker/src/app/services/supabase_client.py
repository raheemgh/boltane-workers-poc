"""
Supabase access for the stores and conversation_windows tables —
Cloudflare Workers build (Stage 2).

The Render original wrapped supabase-py's synchronous client in a
thread-offloading helper for every call. Cloudflare Python Workers
cannot start threads at all (confirmed by this migration's audit/PoC),
so every call here goes through supabase-py's native async client
instead: `await acreate_client(url, key)` once, then plain
`await ...execute()` on each query. The four public functions keep
their exact signatures, return shapes and docstring contracts, so
webhook.py (Stage 6) and the existing fakes need no changes to call
them.

History this file builds on: the Render original built its (sync) client
eagerly at import time, from a Settings() singleton read out of os.environ.
Workers has no os.environ to read at import time, so Stage 1 made it lazy —
`_get_client()` builds on first use via get_settings() — and cached it on the
argument that SUPABASE_URL/KEY are static for a deployed Worker version. This
stage keeps that lazy, cached shape and the `_client` / `_get_client` names,
but "the settings are static" is no longer enough on its own, because the
async client is more than settings (see below).

Client lifetime — the decision Stage 2 had to make
--------------------------------------------------
The client is built LAZILY on first use and cached at module scope for
the life of the isolate (CACHE_CLIENT_ACROSS_CALLS = True), never at
import time. Reasoning:

  * Never at import: a Python Worker's imports run once at deploy time
    and the runtime snapshots the result. Building the client there
    would bake it into the snapshot. Stage 1's get_settings() is lazy
    for the same reason, and this file reuses it for the URL/key.
  * Why caching is believed safe: Cloudflare's "Cannot perform I/O on
    behalf of a different request" error is about I/O objects — streams,
    request/response bodies — created in one request and touched from
    another; its documented fix is to keep only plain data in global
    scope. A supabase client is configuration plus HTTP session
    objects; with a service key there is no auth session or refresh
    timer, and every query is an independent HTTPS call (httpx ->
    fetch). Nothing request-scoped is retained between calls.
  * Why it is not assumed: neither Cloudflare's nor supabase-py's docs
    say anything about reusing this particular client (httpx patched
    onto fetch, inside Pyodide) across requests. That is why
    test_stage2_supabase_real.py has a reuse probe that hits the
    cached client from separate AND concurrent HTTP requests under
    `pywrangler dev`.
  * Correctness never depends on the cache surviving: isolates are
    evicted and recreated whenever the platform likes, and the next
    call simply rebuilds the client.
  * Fallback if the probe ever fails: set CACHE_CLIENT_ACROSS_CALLS to
    False. Every call then builds a fresh client and stores nothing
    globally. Costlier (several client constructions per customer
    message), but it shares nothing across requests by construction.
    Both modes are covered by the unit tests.

Stage 6 dependency change (bundle size): this file no longer imports the
`supabase` umbrella package. It builds the PostgREST client directly (see
`acreate_client` below). Behaviour, signatures and queries are unchanged.
It was first made against a 3 MB compressed bundle limit that Cloudflare's docs
no longer have (the Free limit is now 64 MiB uncompressed); it is kept because
it removes about 8.5 MB of packages never used here and lowers startup work.

`_client` is also the injection point the existing tests use
(`sc._client = fake`); it is honoured in both modes.
"""

import asyncio
import warnings
from datetime import datetime, timedelta, timezone

from postgrest import AsyncPostgrestClient
from postgrest.constants import DEFAULT_POSTGREST_CLIENT_TIMEOUT

from app.config import get_settings
from app.services.token_limits import CONVERSATION_TOKEN_CAP

# Boltane's own definition of a "conversation": messages from the same
# customer to the same store within a rolling window count as one
# conversation. Deliberately 2 hours, NOT Meta's 24h WhatsApp billing-
# conversation window — that's a different concept entirely (Meta's is
# about what THEY bill businesses for; this is about what separates
# one customer visit from the next for OUR usage metering). 2h was
# chosen specifically because 24h was too coarse to treat genuinely
# separate visits — e.g. a morning question and an unrelated evening
# one from the same customer — as distinct conversations.
CONVERSATION_WINDOW_HOURS = 2

# See "Client lifetime" in the module docstring before changing this.
CACHE_CLIENT_ACROSS_CALLS = True

_client: AsyncPostgrestClient | None = None
_init_lock: asyncio.Lock | None = None


async def acreate_client(url: str, key: str) -> AsyncPostgrestClient:
    """
    Builds the PostgREST client for the project at `url` with `key`.

    Stage 6 replacement for the `acreate_client` that the `supabase` umbrella
    package provides. That package also pulls in auth, storage, realtime and
    functions clients this service never calls; one of them (auth) drags in
    `cryptography` (about 8 MB uncompressed with its helpers), which alone
    was roughly 40% of the vendored bundle.

    The call below is the one the umbrella package makes internally, rebuilt
    by hand: base URL `<url>/rest/v1`, the `apikey` and `Authorization:
    Bearer` headers it adds, schema "public", and the same default timeout
    constant. The query-builder API (`.table().select().eq()...execute()`)
    and the error class (`postgrest.APIError`) are the same objects as before.
    The deprecation notice the library prints for an explicit `timeout=` is
    silenced because passing it is exactly what keeps the old behaviour.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return AsyncPostgrestClient(
            f"{url.rstrip('/')}/rest/v1",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=DEFAULT_POSTGREST_CLIENT_TIMEOUT,
        )


async def _new_client() -> AsyncPostgrestClient:
    settings = get_settings()
    return await acreate_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)


async def _get_client() -> AsyncPostgrestClient:
    """
    Returns the client to use for one query, per the lifetime policy in
    the module docstring. A failure to build it (e.g. get_settings()
    raising its clear "missing env var(s)" RuntimeError) propagates to
    the caller unchanged and leaves nothing half-initialised behind.
    """
    global _client, _init_lock

    if _client is not None:
        return _client

    if not CACHE_CLIENT_ACROSS_CALLS:
        return await _new_client()

    if _init_lock is None:  # no await between check and set -> race-free
        _init_lock = asyncio.Lock()
    async with _init_lock:
        # Re-check: a concurrent first call may have finished while we waited.
        if _client is None:
            _client = await _new_client()
    return _client


async def get_store_by_phone_number_id(phone_number_id: str) -> dict | None:
    """
    Look up a store row by its WhatsApp phone_number_id.
    Returns None if no matching row exists — the caller is expected
    to log that and stop cleanly, not treat it as an error.
    """
    client = await _get_client()
    response = await (
        client.table("stores")
        .select("*")
        .eq("phone_number_id", phone_number_id)
        .limit(1)
        .execute()
    )
    rows = response.data
    return rows[0] if rows else None


def _parse_timestamp(value: str) -> datetime:
    """
    Supabase/Postgrest returns timestamps as ISO 8601 strings. Handles
    both a trailing "Z" (which datetime.fromisoformat only accepts
    from Python 3.11+) and a bare, timezone-naive timestamp (the
    `timestamp` column type used here has no timezone info attached —
    treated as UTC, matching now(utc) below).
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


async def is_new_conversation_and_touch(store_id: str, customer_phone: str) -> tuple[bool, int]:
    """
    Looks up conversation_windows for (store_id, customer_phone) and
    reports whether this message starts a new conversation, as
    (is_new, token_count_before_this_message).

    is_new is True when EITHER of two independent things has happened
    — either is sufficient on its own to force a fresh conversation:
      1. Time: there's no existing row, or its last_message_at is more
         than CONVERSATION_WINDOW_HOURS old (the "natural" window
         expiry).
      2. Tokens: the existing row's token_count has already reached
         CONVERSATION_TOKEN_CAP — the "token firewall" forcing a reset
         even though the time window hasn't expired, so one chatty
         customer can't run up unbounded cost within what the counter
         would otherwise treat as a single conversation. See
         token_limits.py.

    token_count_before_this_message is 0 if is_new (a fresh
    conversation always starts its token count at zero — see the
    upsert below) or the existing row's token_count if continuing.
    The caller needs this to later call add_conversation_tokens() with
    the right base to add this turn's usage onto, once that usage is
    known (only after the AI call completes).

    Either way, upserts last_message_at = now() — so a customer who
    keeps messaging has their window keep sliding forward and never
    expires mid-conversation, and a brand-new customer gets a row
    created for next time. When is_new is True, this upsert also
    resets token_count to 0 in the same write, so the new conversation
    starts with a full budget.

    Only a new conversation should count against the monthly cap or
    increment monthly_conversation_count — the caller (webhook.py's
    _process_message) is responsible for acting on the return value;
    this function only tracks the window and its token budget, it
    doesn't touch the monthly usage counters itself.
    """
    client = await _get_client()

    response = await (
        client.table("conversation_windows")
        .select("last_message_at, token_count")
        .eq("store_id", store_id)
        .eq("customer_phone", customer_phone)
        .limit(1)
        .execute()
    )
    rows = response.data

    is_new = True
    existing_token_count = 0
    if rows:
        row = rows[0]
        existing_token_count = row.get("token_count") or 0
        last_message_at = _parse_timestamp(row["last_message_at"])
        window_expired = (datetime.now(timezone.utc) - last_message_at) > timedelta(
            hours=CONVERSATION_WINDOW_HOURS
        )
        token_cap_reached = existing_token_count >= CONVERSATION_TOKEN_CAP
        is_new = window_expired or token_cap_reached

    payload = {
        "store_id": store_id,
        "customer_phone": customer_phone,
        "last_message_at": datetime.now(timezone.utc).isoformat(),
    }
    if is_new:
        # Starting fresh (whether from a time-window expiry or a
        # token-cap-forced reset) — zero out token_count in the
        # same write so the new conversation gets its own full
        # CONVERSATION_TOKEN_CAP budget. Deliberately omitted from
        # the payload when NOT new: Postgrest's upsert only writes
        # the columns present in the payload on conflict, so
        # leaving token_count out here preserves the existing
        # running total untouched.
        payload["token_count"] = 0
    await (
        client.table("conversation_windows")
        .upsert(payload, on_conflict="store_id,customer_phone")
        .execute()
    )

    return is_new, (0 if is_new else existing_token_count)


async def add_conversation_tokens(
    store_id: str, customer_phone: str, tokens_used: int, token_count_before: int
) -> None:
    """
    Persists a conversation's running token total after a completed AI
    turn: token_count_before (as returned by is_new_conversation_and_touch
    for this same message) plus tokens_used (the actual prompt+completion
    tokens OpenRouter reported for this turn — see ai_client.call_ai).

    Called only after the AI reply comes back, never before — the
    exact token cost of a turn doesn't exist until the model has
    actually finished generating it. token_count_before is passed in
    (rather than re-read here) because it was already fetched earlier
    in the same message's processing; there's no reason to pay for a
    second read of a row this same request already has in hand.

    This is a plain update, not an upsert — by the time this is
    called, is_new_conversation_and_touch has always already created
    the row for (store_id, customer_phone) earlier in the same
    request, so it's guaranteed to exist.
    """
    new_total = token_count_before + tokens_used

    client = await _get_client()
    await (
        client.table("conversation_windows")
        .update({"token_count": new_total})
        .eq("store_id", store_id)
        .eq("customer_phone", customer_phone)
        .execute()
    )


async def update_store_usage(
    store_id: str,
    new_count: int,
    *,
    conversations_remaining: int | None = None,
    last_used_model: str | None = None,
) -> None:
    """
    Persist an updated monthly_conversation_count for a store, plus
    two observability-only fields: conversations_remaining (the
    store's package cap minus new_count — None for BYOK stores, since
    no cap applies to them) and last_used_model (whichever OpenRouter
    model actually produced the last reply, from ai_client's fallback
    chain). Neither field drives any behavior — they exist purely so
    the numbers are visible directly in the Supabase table instead of
    requiring a calculation.
    """
    payload = {"monthly_conversation_count": new_count}
    if conversations_remaining is not None:
        payload["conversations_remaining"] = conversations_remaining
    if last_used_model is not None:
        payload["last_used_model"] = last_used_model

    client = await _get_client()
    await client.table("stores").update(payload).eq("id", store_id).execute()
