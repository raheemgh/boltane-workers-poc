"""
AI reply generation via OpenRouter's chat completions endpoint
(OpenAI-compatible: POST /api/v1/chat/completions, Bearer auth,
{"model": ..., "messages": [...]} body, reply in
choices[0].message.content).

API key resolution: a store's own api_key (BYOK) if set, otherwise
the shared platform default key (OPENROUTER_DEFAULT_KEY env var).

Model selection: the store's ai_model if set, tried first; then each
model in the store's package's default chain (PACKAGE_DEFAULT_MODELS)
in order — basic/pro fall back onto DeepSeek V4 Flash (paid, but
cheap and capable), low-tier falls back onto FALLBACK_MODELS (currently
the same DeepSeek model — see the LOW-TIER MODEL CHAIN note below). A 429 (rate limited) or model-unavailable
response (502/503 — no provider available) moves to the next model.
If every model fails, a fixed graceful reply is returned instead of
leaving the customer with silence.

402 (insufficient credit) is handled separately from the above: it's
a key-level problem, not a model-level one, so every model would fail
identically with that same key — the fallback list is never cycled on
a 402. A BYOK store's own key running out never falls back onto the
shared platform key; it's logged as that store needing to top up and
stops there.

Token firewall: every call sets max_tokens=MAX_OUTPUT_TOKENS_PER_MESSAGE
(see token_limits.py) — there is no code path that omits it. call_ai()
returns the exact prompt+completion token count OpenRouter reports in
the response's `usage` field, so the caller (webhook.py) can track a
conversation's real cumulative usage against CONVERSATION_TOKEN_CAP —
this is measured, not estimated, since it's for text that has actually
been sent to and billed by the model.

--- Local-hosting additions (proxy / streaming / retry / masking) ---
Same three reasons as ava-backend's lib/openrouter.ts (see that file's
header for the fuller explanation) — this runs on a local device
behind an unstable connection and a mandatory VPN instead of on a
cloud host:

- OPENROUTER_SOCKS5_PROXY (optional, see config.py) — only the
  OpenRouter client below is routed through it.
- stream=True on every call, purely so the proxy sees a steady trickle
  of bytes instead of one long silence while the model generates (an
  idle SOCKS5 connection on a flaky link can get treated as dead and
  dropped). Chunks are accumulated into the same reply_text/tokens_used
  shape the rest of this file already expects — webhook.py needed no
  changes.
- _post_with_network_retry() below retries the SAME model 2 extra
  times (2s, then 4s backoff) on a raw network error (timeout,
  connection reset) specifically — deliberately separate from the
  existing model-fallback loop above it. The two solve different
  problems: model fallback means "this model/provider is genuinely
  unavailable, try a different one"; network retry means "my own
  connection just hiccuped, retry the SAME model before giving up on
  it" — collapsing them would mean a local WiFi blip could bump a
  paying basic/pro customer down to a free-tier model for no reason.
- Every customer_message is masked (phone numbers only — see
  app/services/pii_masking.py) before it's sent, and restored in the
  reply text before call_ai() returns it. system_prompt is never
  masked — it's Boltane's own instructions, not customer data.
--- Cloudflare Workers build (Stage 4) ---
Ported from the Render version with exactly these differences:
  * settings are read through get_settings() (Stage 1 pattern) instead of
    the module-level `settings` singleton;
  * httpx.AsyncClient is NEVER given proxy= (SOCKS5 is not usable over the
    Workers transport); a non-empty OPENROUTER_SOCKS5_PROXY logs one
    warning per isolate and is otherwise ignored;
  * FALLBACK_MODELS (the low-tier chain) is [DEEPSEEK_MODEL] instead of
    three placeholder ":free" IDs — a deliberate difference from Render,
    where low-tier could never have worked (see LOW-TIER MODEL CHAIN).
The streaming, retry, fallback, masking and usage logic is untouched
(test_stage4_offline.py proves the unchanged functions are AST-identical
to the Render original).

--- Documented, not changed (behaviour is identical to Render) ---
  * finish_reason is not captured: a reply cut off by max_tokens
    (finish_reason "length") is returned as-is, mid-sentence.
  * An error event that arrives INSIDE an HTTP 200 stream (OpenRouter
    reports mid-stream failures that way) is not detected: any text
    received before it is returned as a successful reply; if no text
    arrived, the stream counts as empty and the next model is tried.
  * Only delta.content is accumulated. A reasoning-style model can spend
    some or all of max_tokens (300) on reasoning tokens that never reach
    delta.content; with nothing in content that counts as an empty
    stream and the next model in the chain is tried.
  * DeepSeek V4 Flash's thinking tokens count INSIDE max_tokens (measured;
    same on Render): with default settings a share of replies come back
    empty — which on a one-model chain ends as GRACEFUL_FAILURE_REPLY —
    or cut off. Turning thinking off ("reasoning": {"enabled": false})
    fixed it in every test but is a post-migration option, not part of
    this port. Which provider serves a call varies; for identical requests
    CPython and Workers got the same provider and the same kind of result.
  * provider.zdr / data_collection are not set on requests (privacy
    controls are a future feature, not part of this migration).

- A fresh httpx.AsyncClient is still constructed per call (not cached
  at module level) — deliberately, to match the existing test suite's
  monkey-patching of httpx.AsyncClient itself
  (test_usage_protection.py), which expects a brand new instance every
  call.
"""

import asyncio
import json
import logging

import httpx

from app.config import get_settings
from app.services.pii_masking import mask_phone_numbers
from app.services.token_limits import MAX_OUTPUT_TOKENS_PER_MESSAGE

logger = logging.getLogger("boltane.ai_client")

OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"

REQUEST_TIMEOUT_SECONDS = 60.0
NETWORK_RETRY_DELAYS_SECONDS = [2.0, 4.0]

# Verified as a real, current OpenRouter model slug (checked against
# openrouter.ai/models) at the time this was written — but OpenRouter
# slugs do get superseded by dated revisions (e.g. a "-0731" suffix)
# over time. Worth re-checking periodically, and cross-checking against
# any DeepSeek model string already configured elsewhere in the project
# before deploying.
DEEPSEEK_MODEL = "deepseek/deepseek-v4-flash"

# ---------------------------------------------------------------------------
# LOW-TIER MODEL CHAIN — Stage 4 decision (2026-10-13).
# Render's chain was three PLACEHOLDER ":free" IDs, so low-tier could never
# have reached a real model there. Real free-model candidates were then
# tested with live calls at the production cap (300 tokens):
#   * thinkingmachines/inkling:free and inkling-small:free -> HTTP 403
#     ("only available on agentic harnesses");
#   * nemotron-3-super, nemotron-3.5-lightning, ling-3.1-flash -> EMPTY reply
#     (thinking used the whole cap);
#   * only nemotron-3-ultra and dots-3-note answered by default, in 19-54 s.
# Until the reasoning-off option exists (after the migration) low-tier
# therefore uses the same model as basic/pro. This is an intentional
# difference from Render; measurements and the alternatives are in
# stage4-findings-and-backlog.md. The name FALLBACK_MODELS is kept so
# existing imports and tests keep working.
# ---------------------------------------------------------------------------
FALLBACK_MODELS = [DEEPSEEK_MODEL]

# One mapping, not scattered if/else — a future pricing/model change
# is a one-line edit here, not a hunt through webhook.py/ai_client.py.
# basic and pro are both paid tiers and share the same default model
# (DeepSeek V4 Flash); low-tier uses FALLBACK_MODELS, currently the same model.
PACKAGE_DEFAULT_MODELS: dict[str, list[str]] = {
    "basic": [DEEPSEEK_MODEL],
    "pro": [DEEPSEEK_MODEL],
    "low-tier": FALLBACK_MODELS,
}

GRACEFUL_FAILURE_REPLY = (
    "We're experiencing high demand right now, please try again in a few minutes."
)

# 402 = insufficient credit on the key used. This is handled as its
# own case below, separate from RETRYABLE_STATUS_CODES — it's a
# key-level failure, not a model-level one, so trying the next model
# with the same key is pointless.
INSUFFICIENT_CREDIT_STATUS_CODE = 402

# Status codes that mean "this model/provider couldn't serve the request
# right now" — worth trying the next model instead of giving up.
# 429 = rate limited (the shared free-tier pool is 20 req/min, 50-1000
# req/day — see module docstring). 502/503 = provider down / no provider
# available for that model.
RETRYABLE_STATUS_CODES = {429, 502, 503}


_socks5_warning_logged = False


def _warn_once_if_socks5_proxy_configured() -> None:
    """
    Workers build: OPENROUTER_SOCKS5_PROXY is accepted by Settings (the
    Stage 1 settings test covers it) but is NEVER passed to httpx — the
    Workers fetch-based transport has no SOCKS5 support, and passing it
    would either fail or silently be ignored depending on the runtime.
    If someone sets it anyway, say so once per isolate instead of
    failing or pretending it took effect.
    """
    global _socks5_warning_logged
    if _socks5_warning_logged:
        return
    if get_settings().OPENROUTER_SOCKS5_PROXY:
        _socks5_warning_logged = True
        logger.warning(
            "OPENROUTER_SOCKS5_PROXY is set but is IGNORED on Cloudflare Workers "
            "(no SOCKS5 support in the Workers transport); OpenRouter calls go "
            "out directly."
        )


def _resolve_api_key(store_api_key: str | None) -> str:
    """BYOK if the store supplied its own key, else the shared platform key."""
    return store_api_key or get_settings().OPENROUTER_DEFAULT_KEY


def _resolve_models(store_ai_model: str | None, package: str) -> list[str]:
    """
    The store's configured model first (if any — this is the BYOK
    case, so the store's own chosen model is tried before falling
    back onto anything), then the store's package's default chain.
    Without a store override, the package's default chain is used
    as-is.
    """
    fallback = PACKAGE_DEFAULT_MODELS[package]
    if store_ai_model:
        return [store_ai_model, *fallback]
    return list(fallback)


class _NetworkError(Exception):
    """Raised for a raw connection-level failure (timeout, reset, DNS,
    proxy down) — deliberately distinct from an HTTP error response,
    which the caller loop handles via status codes instead."""


async def _stream_completion(
    client: httpx.AsyncClient, headers: dict[str, str], body: dict
) -> tuple[str | None, dict | None, int]:
    """
    Sends one streamed request and accumulates it. Returns
    (reply_text, info, status_code):
      - On success: (full_reply_text, {"usage": {...} or None}, 200).
      - On a non-200 response: (None, {"status": ..., "body": ...}, status).
    Raises _NetworkError for a connection-level failure — the caller
    decides whether that's worth retrying.

    Uses client.stream() (not client.post()) specifically to keep the
    connection actively receiving bytes the whole time the model is
    generating — see module docstring for why that matters on a SOCKS5
    proxy over an unstable link.
    """
    body = {**body, "stream": True, "stream_options": {"include_usage": True}}

    try:
        async with client.stream(
            "POST", OPENROUTER_CHAT_COMPLETIONS_URL, headers=headers, json=body
        ) as response:
            if response.status_code != 200:
                await response.aread()
                return (
                    None,
                    {"status": response.status_code, "body": response.text[:300]},
                    response.status_code,
                )

            full_reply = ""
            usage: dict | None = None
            async for line in response.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                except ValueError:
                    # Partial/malformed SSE line — a chunk-boundary
                    # artifact, not worth failing the whole turn on.
                    continue

                choices = event.get("choices") or []
                if choices:
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        full_reply += delta
                if event.get("usage"):
                    usage = event["usage"]

            if not full_reply:
                return None, {"status": None, "body": "empty stream"}, 0

            return full_reply, {"usage": usage} if usage else {}, 200

    except httpx.HTTPError as exc:
        raise _NetworkError(str(exc)) from exc


async def _post_with_network_retry(
    client: httpx.AsyncClient, headers: dict[str, str], body: dict, *, model: str
) -> tuple[str | None, dict | None, int]:
    """Retries the SAME model on a connection-level failure only (see
    module docstring for why this is kept separate from model
    fallback) — 2 extra attempts, 2s then 4s backoff."""
    last_exc: Exception | None = None
    for attempt in range(len(NETWORK_RETRY_DELAYS_SECONDS) + 1):
        try:
            return await _stream_completion(client, headers, body)
        except _NetworkError as exc:
            last_exc = exc
            if attempt == len(NETWORK_RETRY_DELAYS_SECONDS):
                break
            logger.warning(
                "Network error calling OpenRouter model=%s (attempt %d/%d): %s — "
                "retrying same model in %.0fs",
                model, attempt + 1, len(NETWORK_RETRY_DELAYS_SECONDS) + 1, exc,
                NETWORK_RETRY_DELAYS_SECONDS[attempt],
            )
            await asyncio.sleep(NETWORK_RETRY_DELAYS_SECONDS[attempt])
    assert last_exc is not None
    raise last_exc


async def call_ai(
    system_prompt: str,
    customer_message: str,
    *,
    package: str,
    store_ai_model: str | None = None,
    store_api_key: str | None = None,
    store_id: str | None = None,
) -> tuple[str, str | None, int]:
    """
    Calls OpenRouter for a chat completion, trying each candidate model
    in order and falling through to the next one on a 429 or
    model-unavailable response. Returns (reply_text, model_used,
    tokens_used):
      - model_used is the model ID that actually produced the reply,
        or None if every model failed and GRACEFUL_FAILURE_REPLY was
        returned instead.
      - tokens_used is the exact prompt_tokens + completion_tokens
        OpenRouter reported for the successful call (from the
        response's `usage` field), or 0 if every model failed — a
        failed call was never billed, so it never consumed any of the
        conversation's token budget.
    This function never raises for ordinary API failures, only for
    genuinely unexpected bugs.

    package (the store's pricing tier — "basic"/"pro"/"low-tier")
    selects the default model chain via PACKAGE_DEFAULT_MODELS; it has
    no effect on API key resolution, which is BYOK-vs-shared-key only.

    store_id is optional and used only for logging — it has no effect
    on behavior — so a 402 or failure event can be traced back to the
    specific store that hit it.
    """
    api_key = _resolve_api_key(store_api_key)
    models = _resolve_models(store_ai_model, package)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    masked = mask_phone_numbers(customer_message)

    _warn_once_if_socks5_proxy_configured()

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        for attempt, model in enumerate(models, start=1):
            body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": masked.masked_text},
                ],
                # Token firewall, output half — capped on every single
                # call, no exceptions, regardless of package or model.
                "max_tokens": MAX_OUTPUT_TOKENS_PER_MESSAGE,
            }

            try:
                reply_text, info, status_code = await _post_with_network_retry(
                    client, headers, body, model=model
                )
            except _NetworkError as exc:
                logger.warning(
                    "OpenRouter request error on model=%s (attempt %d/%d) after "
                    "network retries: %s — trying next model",
                    model, attempt, len(models), exc,
                )
                continue

            if status_code == INSUFFICIENT_CREDIT_STATUS_CODE:
                # Key-level failure, not model-level: this same key will
                # fail on every remaining model too, so cycling through
                # the fallback list is pointless — stop immediately.
                if store_api_key:
                    logger.warning(
                        "OUT OF CREDIT (402): store_id=%s is using its own "
                        "OpenRouter key (BYOK) and it's out of credit on "
                        "model=%s. NOT falling back to the shared platform "
                        "key — this store needs to top up their own balance.",
                        store_id, model,
                    )
                else:
                    logger.error(
                        "OUT OF CREDIT (402): the shared PLATFORM OpenRouter "
                        "key is out of credit on model=%s. This affects "
                        "every free-tier store, not just one — needs an "
                        "immediate top-up.",
                        model,
                    )
                return GRACEFUL_FAILURE_REPLY, None, 0

            if status_code in RETRYABLE_STATUS_CODES:
                logger.warning(
                    "OpenRouter model=%s unavailable (status=%d, attempt %d/%d) — "
                    "shared free-tier quota may be involved, trying next model",
                    model, status_code, attempt, len(models),
                )
                continue

            if status_code != 200:
                logger.warning(
                    "OpenRouter model=%s returned unexpected status=%d "
                    "(attempt %d/%d): %s — trying next model",
                    model, status_code, attempt, len(models),
                    (info or {}).get("body"),
                )
                continue

            if not reply_text:
                logger.warning(
                    "OpenRouter model=%s returned an empty/unparseable stream "
                    "(attempt %d/%d) — trying next model",
                    model, attempt, len(models),
                )
                continue

            # usage is the exact, provider-reported prompt+completion
            # token count for this call — not an estimate. Prefer
            # total_tokens when present; fall back to summing the two
            # halves if a provider ever omits it. Missing `usage`
            # entirely is not expected from OpenRouter (stream_options.
            # include_usage is set above), but tokens_used defaults to 0
            # rather than raising if it ever happens — this firewall
            # must never crash the reply path.
            usage = (info or {}).get("usage") or {}
            tokens_used = usage.get("total_tokens")
            if tokens_used is None:
                tokens_used = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)

            return masked.restore(reply_text), model, tokens_used

    logger.error(
        "all %d OpenRouter models failed (%s) — returning graceful failure reply",
        len(models), ", ".join(models),
    )
    return GRACEFUL_FAILURE_REPLY, None, 0
