"""
Unit tests for the usage-protection layers added on top of the
existing monthly conversation cap: the token firewall (per-message
input/output caps + per-conversation cumulative cap) and is_trial-based
cap branching for package='basic'.

Same approach as test_package_system.py: calls the real app functions
directly with external I/O (Supabase, OpenRouter, Meta) replaced by
small in-memory fakes — no network, no real credentials, runs in well
under a second.

Run it yourself with:

    pip install -r requirements.txt
    python test_usage_protection.py

STAGE 7 — Workers-tree adaptation (this copy lives in cf-worker/, next to test_package_system.py)
------------------------------------------------------------------------------------------------
    PYTHONPATH=tooling/local-test-support:src python test_usage_protection.py

No test body and no assertion was changed. What changed: (1) the run command above; (2) the comment about
how settings are read (see below); (3) it imports the fakes from the Workers copy of test_package_system.py,
whose Supabase fake is now async (see that file's header) — that is what makes
test_supabase_layer_forces_reset_when_token_cap_reached work against the async Workers supabase_client.
test_every_openrouter_call_sets_max_output_tokens needs the `workers` stub on PYTHONPATH because
ai_client.call_ai() reads get_settings().

Covers:
    1. a customer message estimated over MAX_INPUT_TOKENS_PER_MESSAGE
       is rejected before ever reaching the AI — no call_ai, no
       window/counter/token-count changes
    2. every OpenRouter call sets max_tokens=MAX_OUTPUT_TOKENS_PER_MESSAGE,
       with no code path that omits it
    3. once a conversation's cumulative token_count has reached
       CONVERSATION_TOKEN_CAP, the NEXT message forces a reset: treated
       as a new conversation (fresh token budget, counts against the
       monthly cap) even though the 2h window hasn't expired
    4. is_trial-based cap branching for package='basic': is_trial=True
       caps at 100/month, is_trial=False caps at 300/month; low-tier
       and pro are unaffected by is_trial
"""

import asyncio
import os

# The Render Settings() read these at import time and had no defaults.
# On the Workers tree get_settings() (app/config.py) reads them lazily
# through `workers.env`, which the local stub backs with os.environ — so
# these lines feed it the same way. Dummy values only — nothing here
# makes a real network call.
os.environ.setdefault("META_VERIFY_TOKEN", "test-token")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-supabase-key")
os.environ.setdefault("OPENROUTER_DEFAULT_KEY", "test-openrouter-key")

# Reuse the fakes and helpers already built for the package-system
# tests rather than duplicating them — same fake Supabase/webhook
# plumbing, just exercised differently here.
from test_package_system import (  # noqa: E402
    _base_store,
    _build_payload,
    patched_webhook,
)


# ---------------------------------------------------------------------------
# 1 & 2: per-message input/output caps
# ---------------------------------------------------------------------------

async def test_oversized_input_message_is_rejected_before_calling_ai():
    from app.services.token_limits import MAX_INPUT_TOKENS_PER_MESSAGE

    # ~4 chars/token estimator (see token_limits.estimate_tokens) — this
    # comfortably clears MAX_INPUT_TOKENS_PER_MESSAGE regardless of the
    # exact constant's value.
    oversized_text = "a" * ((MAX_INPUT_TOKENS_PER_MESSAGE + 200) * 4)

    store_row = _base_store(id="store-oversized-input")
    with patched_webhook(store_row, is_new_conversation=True) as (wh, fake_call_ai, fake_send, fake_update, fake_add_tokens):
        await wh._process_message(_build_payload(text=oversized_text))

    assert not fake_call_ai.calls, "an oversized message must never reach call_ai"
    assert not fake_update.calls, "an oversized message must never touch the monthly usage counter"
    assert not fake_add_tokens.calls, "an oversized message must never touch the conversation token count"
    assert fake_send.calls, "the customer must still get a reply explaining the message was too long"
    assert fake_send.calls[0]["message"] == wh.MESSAGE_TOO_LONG_REPLY

    print("PASS: an oversized customer message is rejected before ever reaching the AI, with no side effects")


async def test_normal_sized_message_is_not_rejected():
    store_row = _base_store(id="store-normal-input")
    with patched_webhook(store_row, is_new_conversation=True) as (wh, fake_call_ai, fake_send, fake_update, _):
        await wh._process_message(_build_payload(text="What time do you open on Fridays?"))

    assert fake_call_ai.calls, "an ordinary short message must still reach the AI"
    assert fake_send.calls[0]["message"] != wh.MESSAGE_TOO_LONG_REPLY

    print("PASS: a normal-length message is not affected by the input-length firewall")


async def test_every_openrouter_call_sets_max_output_tokens():
    """
    ai_client.call_ai must set max_tokens=MAX_OUTPUT_TOKENS_PER_MESSAGE
    on every request body it builds — including retries across the
    fallback chain — with no code path that omits it. Patches
    httpx.AsyncClient.stream to simulate a streamed SSE response and
    capture every outgoing request body, instead of hitting the network.
    """
    import httpx

    from app.services.ai_client import call_ai
    from app.services.token_limits import MAX_OUTPUT_TOKENS_PER_MESSAGE

    captured_bodies = []

    class _FakeStreamResponse:
        status_code = 200

        async def aiter_lines(self):
            # Two content deltas + a final usage-only chunk, same shape
            # OpenRouter sends with stream_options.include_usage=True —
            # exercises the accumulation logic in
            # ai_client._stream_completion, not just a canned full reply.
            import json as _json

            yield "data: " + _json.dumps(
                {"choices": [{"delta": {"content": "Sure, "}}]}
            )
            yield "data: " + _json.dumps(
                {"choices": [{"delta": {"content": "here's the answer."}}]}
            )
            yield "data: " + _json.dumps(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 40,
                        "completion_tokens": 20,
                        "total_tokens": 60,
                    },
                }
            )
            yield "data: [DONE]"

        async def aread(self):
            return b""

        text = ""

    class _FakeStreamContextManager:
        def __init__(self, json_body):
            captured_bodies.append(json_body)

        async def __aenter__(self):
            return _FakeStreamResponse()

        async def __aexit__(self, *_a):
            return False

    class _FakeAsyncClient:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        def stream(self, method, url, headers=None, json=None):
            return _FakeStreamContextManager(json)

    original_async_client = httpx.AsyncClient
    httpx.AsyncClient = _FakeAsyncClient
    try:
        for package in ("basic", "pro", "low-tier"):
            captured_bodies.clear()
            await call_ai("You are a helpful assistant.", "hello", package=package)
            assert captured_bodies, f"expected at least one OpenRouter call for package={package}"
            for body in captured_bodies:
                assert body.get("max_tokens") == MAX_OUTPUT_TOKENS_PER_MESSAGE, (
                    f"every OpenRouter call must cap max_tokens at {MAX_OUTPUT_TOKENS_PER_MESSAGE}, "
                    f"got {body.get('max_tokens')!r} for package={package}"
                )
    finally:
        httpx.AsyncClient = original_async_client

    print("PASS: every OpenRouter call sets max_tokens to MAX_OUTPUT_TOKENS_PER_MESSAGE, no exceptions")


# ---------------------------------------------------------------------------
# 3: token-cap-forced conversation reset
# ---------------------------------------------------------------------------

async def test_supabase_layer_forces_reset_when_token_cap_reached():
    """
    Directly exercises is_new_conversation_and_touch + add_conversation_tokens
    against a fake Supabase table: simulates a conversation accumulating
    tokens turn by turn until it crosses CONVERSATION_TOKEN_CAP, then
    confirms the NEXT lookup reports a new conversation with a
    zeroed-out token count — even though no time has passed at all.
    """
    import app.services.supabase_client as sc
    from app.services.token_limits import CONVERSATION_TOKEN_CAP
    from test_package_system import _FakeSupabaseClient

    fake_client = _FakeSupabaseClient()
    original_client = sc._client
    sc._client = fake_client
    try:
        store_id, phone = "store-token-cap", "+15557778888"

        # First turn: brand-new conversation.
        is_new, token_count_before = await sc.is_new_conversation_and_touch(store_id, phone)
        assert is_new is True and token_count_before == 0

        # Simulate a turn that used most of one message's worth of tokens,
        # then repeat within the SAME window (no time passes) until the
        # running total reaches the cap.
        running_total = 0
        per_turn_tokens = 500
        while running_total < CONVERSATION_TOKEN_CAP:
            await sc.add_conversation_tokens(store_id, phone, per_turn_tokens, running_total)
            running_total += per_turn_tokens
            # Confirm the window itself still reports "continuing" as
            # long as we're under the cap — isolates the token-cap
            # trigger from ordinary window continuation.
            if running_total < CONVERSATION_TOKEN_CAP:
                is_new, token_count_before = await sc.is_new_conversation_and_touch(store_id, phone)
                assert is_new is False, (
                    f"at {running_total} tokens (under the {CONVERSATION_TOKEN_CAP} cap), "
                    "the conversation must still be treated as continuing"
                )

        assert running_total >= CONVERSATION_TOKEN_CAP

        # The next message after crossing the cap must be forced into a
        # brand-new conversation with a fresh (zeroed) token budget.
        is_new, token_count_before = await sc.is_new_conversation_and_touch(store_id, phone)
        assert is_new is True, "a conversation at/over CONVERSATION_TOKEN_CAP must force a reset on the next message"
        assert token_count_before == 0, "a token-cap-forced reset must start the new conversation's token count at zero"
    finally:
        sc._client = original_client

    print("PASS: the token firewall forces a conversation reset once CONVERSATION_TOKEN_CAP is reached")


async def test_webhook_counts_token_cap_reset_against_monthly_cap():
    """
    When is_new_conversation_and_touch reports is_new=True (whatever
    the reason — time gap or token-cap reset, webhook.py can't tell
    which and doesn't need to), _process_message must treat it exactly
    like any other new conversation: it counts against the monthly cap
    and increments monthly_conversation_count.
    """
    store_row = _base_store(id="store-token-reset-webhook", package="basic", is_trial=False, monthly_conversation_count=50)

    # is_new=True with a nonzero conversation_token_count passed in would
    # be a caller bug (a true reset always starts token_count at 0) —
    # this test uses 0, matching what is_new_conversation_and_touch
    # actually guarantees in that case.
    with patched_webhook(store_row, is_new_conversation=True, conversation_token_count=0) as (
        wh, fake_call_ai, fake_send, fake_update, fake_add_tokens,
    ):
        await wh._process_message(_build_payload())

    assert fake_call_ai.calls, "a token-cap-forced new conversation must still get an AI reply"
    assert fake_update.calls[0]["new_count"] == 51, "a token-cap-forced reset must increment monthly_conversation_count"
    assert fake_add_tokens.calls[0]["token_count_before"] == 0, "the new conversation's token count must start from zero"

    print("PASS: a token-cap-forced conversation reset counts against the monthly cap, same as a natural window expiry")


# ---------------------------------------------------------------------------
# 4: is_trial-based cap branching for package='basic'
# ---------------------------------------------------------------------------

async def test_resolve_monthly_conversation_cap_is_trial_branching():
    from app.routers.webhook import (
        BASIC_PAID_MONTHLY_CONVERSATION_CAP,
        BASIC_TRIAL_MONTHLY_CONVERSATION_CAP,
        PACKAGE_MONTHLY_CONVERSATION_CAP,
        resolve_monthly_conversation_cap,
    )

    assert BASIC_TRIAL_MONTHLY_CONVERSATION_CAP == 100
    assert BASIC_PAID_MONTHLY_CONVERSATION_CAP == 300

    assert resolve_monthly_conversation_cap("basic", is_trial=True) == 100
    assert resolve_monthly_conversation_cap("basic", is_trial=False) == 300

    # low-tier and pro are unaffected by is_trial either way.
    assert resolve_monthly_conversation_cap("low-tier", is_trial=True) == PACKAGE_MONTHLY_CONVERSATION_CAP["low-tier"]
    assert resolve_monthly_conversation_cap("low-tier", is_trial=False) == PACKAGE_MONTHLY_CONVERSATION_CAP["low-tier"]
    assert resolve_monthly_conversation_cap("pro", is_trial=True) == PACKAGE_MONTHLY_CONVERSATION_CAP["pro"]
    assert resolve_monthly_conversation_cap("pro", is_trial=False) == PACKAGE_MONTHLY_CONVERSATION_CAP["pro"]

    print("PASS: resolve_monthly_conversation_cap branches correctly on is_trial for basic, ignores it otherwise")


async def test_webhook_enforces_trial_basic_cap_at_100():
    """A trial 'basic' store (is_trial=True) must be capped at 100, not 300."""
    store_row = _base_store(
        id="store-basic-trial", package="basic", is_trial=True, monthly_conversation_count=100,
    )
    with patched_webhook(store_row, is_new_conversation=True) as (wh, fake_call_ai, fake_send, fake_update, _):
        await wh._process_message(_build_payload())

    assert not fake_call_ai.calls, "a trial basic store at exactly its 100/month cap must be blocked"
    assert fake_send.calls[0]["message"] == wh.MONTHLY_LIMIT_REACHED_MESSAGE

    print("PASS: a trial 'basic' store (is_trial=True) is capped at 100 conversations/month")


async def test_webhook_enforces_paid_basic_cap_at_300():
    """A converted, paying 'basic' store (is_trial=False) gets the full 300, and isn't blocked at 100."""
    store_row = _base_store(
        id="store-basic-paid", package="basic", is_trial=False, monthly_conversation_count=100,
    )
    with patched_webhook(store_row, is_new_conversation=True) as (wh, fake_call_ai, fake_send, fake_update, _):
        await wh._process_message(_build_payload())

    assert fake_call_ai.calls, "a paying basic store at 100/month must NOT be blocked — its cap is 300"
    assert fake_send.calls[0]["message"] != wh.MONTHLY_LIMIT_REACHED_MESSAGE

    # And it IS blocked once it actually reaches 300.
    store_row_at_cap = _base_store(
        id="store-basic-paid-at-cap", package="basic", is_trial=False, monthly_conversation_count=300,
    )
    with patched_webhook(store_row_at_cap, is_new_conversation=True) as (wh, fake_call_ai, fake_send, fake_update, _):
        await wh._process_message(_build_payload())

    assert not fake_call_ai.calls, "a paying basic store at exactly its 300/month cap must be blocked"
    assert fake_send.calls[0]["message"] == wh.MONTHLY_LIMIT_REACHED_MESSAGE

    print("PASS: a converted, paying 'basic' store (is_trial=False) is capped at 300 conversations/month, not 100")


async def main() -> None:
    await test_oversized_input_message_is_rejected_before_calling_ai()
    await test_normal_sized_message_is_not_rejected()
    await test_every_openrouter_call_sets_max_output_tokens()
    await test_supabase_layer_forces_reset_when_token_cap_reached()
    await test_webhook_counts_token_cap_reset_against_monthly_cap()
    await test_resolve_monthly_conversation_cap_is_trial_branching()
    await test_webhook_enforces_trial_basic_cap_at_100()
    await test_webhook_enforces_paid_basic_cap_at_300()
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
