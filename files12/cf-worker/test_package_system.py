"""
Unit tests for the package system that replaced is_free
(package + resolve_monthly_conversation_cap + PACKAGE_DEFAULT_MODELS +
conversation-window usage metering).

See test_usage_protection.py for the token-firewall-specific coverage
(per-message input/output caps, the token-cap-forced conversation
reset, and is_trial cap branching) added alongside this file.

Unlike test_webhook_timing.py, this doesn't spin up a real server —
it calls the actual app functions directly (webhook._process_message,
supabase_client.is_new_conversation_and_touch,
ai_client._resolve_models) with the external I/O (Supabase, OpenRouter,
Meta) replaced by small in-memory fakes, so it runs in well under a
second with no network access and no real credentials.

Run it yourself with:

    pip install -r requirements.txt
    python test_package_system.py

STAGE 7 — Workers-tree adaptation (this copy lives in cf-worker/, next to the other stage tests)
------------------------------------------------------------------------------------------------
Run it from cf-worker/ against src/app (the Workers tree), in the same venv as the Stage 5 test:

    PYTHONPATH=tooling/local-test-support:src python test_package_system.py

The `workers` module that app/config.py's get_settings() imports is the local stub in
tooling/local-test-support/workers.py; it reads os.environ, so the os.environ.setdefault(...) lines
below still feed it exactly as they fed Settings() on Render.

The ONLY functional change from the Render version is one word: `_FakeQueryBuilder.execute` is now
`async def`. The Workers supabase_client (Stage 2) awaits `.execute()` — as postgrest's
AsyncPostgrestClient does — while the Render one calls it synchronously inside asyncio.to_thread.
With the old synchronous fake, test_conversation_window_layer failed with a TypeError at that `await`
(and so did test_supabase_layer_forces_reset_when_token_cap_reached in test_usage_protection.py, which
reuses this fake). No assertion, expected value or scenario was changed. This copy is therefore for the
Workers tree only; the Render tree keeps its own unmodified file.

Covers:
    1. low-tier gets Boltane branding + resolves to the free model chain
    2. basic/pro get no branding + resolve to DeepSeek V4 Flash
    3. BYOK stores (is_api_free=False) are never capped, regardless of package
    4. a same-customer message within the 2h window does not increment
       monthly_conversation_count; a message after the window expires
       does — tested both at the raw conversation_windows layer and
       through the webhook pipeline
"""

import asyncio
import contextlib
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeQueryBuilder:
    """
    Minimal stand-in for the supabase-py / postgrest chain used by
    is_new_conversation_and_touch() and add_conversation_tokens():
    .select/.eq/.limit/.execute() and .update/.eq/.execute() for reads
    and plain updates, .upsert/.execute() for the window-touch write.
    The builder methods are synchronous and .execute() is a coroutine, the
    same shape as postgrest's async client.
    `rows` is a plain dict shared across every builder created by the
    same fake client, keyed by (store_id, customer_phone) -> {"last_message_at":
    ..., "token_count": ...}, so reads see prior writes.
    """

    def __init__(self, rows: dict):
        self._rows = rows
        self._filters: dict = {}
        self._upsert_payload: dict | None = None
        self._update_payload: dict | None = None

    def select(self, *_a, **_k):
        return self

    def eq(self, column, value):
        self._filters[column] = value
        return self

    def limit(self, _n):
        return self

    def upsert(self, payload, on_conflict=None):
        self._upsert_payload = payload
        return self

    def update(self, payload):
        self._update_payload = payload
        return self

    async def execute(self):   # Stage 7: async, because the Workers supabase_client awaits it
        if self._upsert_payload is not None:
            key = (self._upsert_payload["store_id"], self._upsert_payload["customer_phone"])
            existing = self._rows.get(key, {"last_message_at": None, "token_count": 0})
            existing["last_message_at"] = self._upsert_payload["last_message_at"]
            if "token_count" in self._upsert_payload:
                existing["token_count"] = self._upsert_payload["token_count"]
            self._rows[key] = existing
            return SimpleNamespace(data=[self._upsert_payload])

        if self._update_payload is not None:
            key = (self._filters.get("store_id"), self._filters.get("customer_phone"))
            existing = self._rows.get(key, {"last_message_at": None, "token_count": 0})
            existing.update(self._update_payload)
            self._rows[key] = existing
            return SimpleNamespace(data=[self._update_payload])

        key = (self._filters.get("store_id"), self._filters.get("customer_phone"))
        row = self._rows.get(key)
        data = [dict(row)] if row and row.get("last_message_at") else []
        return SimpleNamespace(data=data)


class _FakeSupabaseClient:
    def __init__(self):
        self.rows: dict = {}

    def table(self, name):
        assert name == "conversation_windows", f"unexpected table: {name}"
        return _FakeQueryBuilder(self.rows)


def _build_payload(customer_number: str = "15551234567", text: str = "hi, what are your hours?") -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "PHONE123"},
                            "messages": [{"from": customer_number, "text": {"body": text}}],
                        }
                    }
                ]
            }
        ]
    }


def _fake_get_store(store_row: dict):
    async def _inner(phone_number_id):
        return dict(store_row)
    return _inner


def _fake_is_new_conversation(is_new: bool, token_count_before: int = 0):
    async def _inner(store_id, customer_phone):
        return is_new, token_count_before
    return _inner


def _fake_call_ai(reply_text: str, model_used: str = "some/model", tokens_used: int = 50):
    calls = []

    async def _inner(system_prompt, customer_message, *, package, store_ai_model=None, store_api_key=None, store_id=None):
        calls.append({"system_prompt": system_prompt, "package": package, "store_ai_model": store_ai_model})
        return reply_text, model_used, tokens_used

    _inner.calls = calls
    return _inner


def _fake_send_whatsapp():
    calls = []

    async def _inner(phone_number_id, access_token, to, message):
        calls.append({"to": to, "message": message})

    _inner.calls = calls
    return _inner


def _fake_update_store_usage():
    calls = []

    async def _inner(store_id, new_count, *, conversations_remaining=None, last_used_model=None):
        calls.append(
            {"new_count": new_count, "conversations_remaining": conversations_remaining, "last_used_model": last_used_model}
        )

    _inner.calls = calls
    return _inner


def _fake_add_conversation_tokens():
    calls = []

    async def _inner(store_id, customer_phone, tokens_used, token_count_before):
        calls.append({"tokens_used": tokens_used, "token_count_before": token_count_before})

    _inner.calls = calls
    return _inner


@contextlib.contextmanager
def patched_webhook(
    store_row: dict,
    is_new_conversation: bool,
    reply_text: str = "Thanks for reaching out!",
    conversation_token_count: int = 0,
):
    """
    Swaps every external dependency _process_message uses (Supabase
    lookups, the AI call, and the Meta send) for fakes, yields the
    webhook module plus the fakes whose calls tests assert on, then
    restores the originals — regardless of whether the test body
    raises.
    """
    from app.routers import webhook

    fake_call_ai = _fake_call_ai(reply_text)
    fake_send = _fake_send_whatsapp()
    fake_update = _fake_update_store_usage()
    fake_add_tokens = _fake_add_conversation_tokens()

    originals = {
        "get_store_by_phone_number_id": webhook.get_store_by_phone_number_id,
        "is_new_conversation_and_touch": webhook.is_new_conversation_and_touch,
        "call_ai": webhook.call_ai,
        "send_whatsapp": webhook.send_whatsapp,
        "update_store_usage": webhook.update_store_usage,
        "add_conversation_tokens": webhook.add_conversation_tokens,
    }

    webhook.get_store_by_phone_number_id = _fake_get_store(store_row)
    webhook.is_new_conversation_and_touch = _fake_is_new_conversation(is_new_conversation, conversation_token_count)
    webhook.call_ai = fake_call_ai
    webhook.send_whatsapp = fake_send
    webhook.update_store_usage = fake_update
    webhook.add_conversation_tokens = fake_add_tokens

    try:
        yield webhook, fake_call_ai, fake_send, fake_update, fake_add_tokens
    finally:
        for name, fn in originals.items():
            setattr(webhook, name, fn)


def _base_store(**overrides) -> dict:
    store = {
        "id": "store-1",
        "package": "basic",
        "is_api_free": True,
        "is_trial": False,  # so the default 300 cap matches pre-is_trial test expectations
        "monthly_conversation_count": 0,
        "ai_model": None,
        "api_key": None,
        "access_token": "test-access-token",
        "system_prompt": "You are a helpful shop assistant.",
    }
    store.update(overrides)
    return store


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_low_tier_branding_and_free_models():
    from app.services.ai_client import FALLBACK_MODELS, _resolve_models

    assert _resolve_models(None, "low-tier") == FALLBACK_MODELS

    store_row = _base_store(id="store-low", package="low-tier", monthly_conversation_count=5)
    with patched_webhook(store_row, is_new_conversation=True) as (wh, fake_call_ai, fake_send, fake_update, _):
        await wh._process_message(_build_payload())

    assert fake_call_ai.calls[0]["package"] == "low-tier"
    assert wh.BOLTANE_MENTION_INSTRUCTION in fake_call_ai.calls[0]["system_prompt"]
    assert fake_send.calls[0]["message"].endswith(wh.BOLTANE_BRANDING_FOOTER)
    assert fake_update.calls[0]["new_count"] == 6

    print("PASS: low-tier gets Boltane branding and resolves to the free model chain")


async def test_basic_and_pro_no_branding_deepseek():
    from app.services.ai_client import DEEPSEEK_MODEL, _resolve_models

    for package in ("basic", "pro"):
        assert _resolve_models(None, package) == [DEEPSEEK_MODEL]

        store_row = _base_store(id=f"store-{package}", package=package)
        with patched_webhook(store_row, is_new_conversation=True) as (wh, fake_call_ai, fake_send, fake_update, _):
            await wh._process_message(_build_payload())

        assert fake_call_ai.calls[0]["package"] == package
        assert wh.BOLTANE_MENTION_INSTRUCTION not in fake_call_ai.calls[0]["system_prompt"]
        assert wh.BOLTANE_BRANDING_FOOTER not in fake_send.calls[0]["message"]

    print("PASS: basic/pro get no Boltane branding and resolve to DeepSeek V4 Flash")


async def test_byok_never_capped_regardless_of_package():
    for package in ("low-tier", "basic", "pro"):
        store_row = _base_store(
            id=f"byok-{package}",
            package=package,
            is_api_free=False,
            monthly_conversation_count=999_999,  # absurdly over every package's cap
            api_key="sk-store-own-openrouter-key",
        )
        with patched_webhook(store_row, is_new_conversation=True, reply_text="Sure, here's the info.") as (
            wh, fake_call_ai, fake_send, fake_update, _,
        ):
            await wh._process_message(_build_payload())

        assert len(fake_call_ai.calls) == 1, f"BYOK store on package={package} should never be blocked by the cap"
        assert fake_send.calls[0]["message"] != wh.MONTHLY_LIMIT_REACHED_MESSAGE

    print("PASS: BYOK stores are never capped, regardless of package")


async def test_conversation_window_layer():
    """Directly exercises is_new_conversation_and_touch against a fake Supabase table."""
    import app.services.supabase_client as sc

    fake_client = _FakeSupabaseClient()
    original_client = sc._client
    sc._client = fake_client
    try:
        store_id, phone = "store-window", "+15559998888"

        is_new, token_count = await sc.is_new_conversation_and_touch(store_id, phone)
        assert is_new is True and token_count == 0, "a customer's first-ever message must start a new conversation"

        is_new, token_count = await sc.is_new_conversation_and_touch(store_id, phone)
        assert is_new is False, "a second message right after the first, within the 2h window, must NOT be a new conversation"

        # Back-date the stored window past the (now 2h) threshold to simulate a gap.
        stale_time = datetime.now(timezone.utc) - timedelta(hours=3)
        fake_client.rows[(store_id, phone)]["last_message_at"] = stale_time.isoformat()

        is_new, token_count = await sc.is_new_conversation_and_touch(store_id, phone)
        assert is_new is True and token_count == 0, "a message after the 2h window expires must start a new conversation"
    finally:
        sc._client = original_client

    print("PASS: conversation_windows correctly distinguishes new vs. continuing vs. expired windows")


async def test_webhook_only_counts_new_conversations():
    store_row = _base_store(id="store-window-webhook", package="basic", monthly_conversation_count=10)

    # Continuing an existing window: still gets an AI reply, but the
    # counter must not move and the cap must never be checked.
    with patched_webhook(store_row, is_new_conversation=False) as (wh, fake_call_ai, fake_send, fake_update, _):
        await wh._process_message(_build_payload())
    assert fake_call_ai.calls, "a continuing conversation should still get an AI reply"
    assert fake_update.calls[0]["new_count"] == 10, "a continuing conversation must not increment the counter"

    # A brand-new conversation from the same store: counts and increments.
    with patched_webhook(store_row, is_new_conversation=True) as (wh, fake_call_ai, fake_send, fake_update, _):
        await wh._process_message(_build_payload())
    assert fake_update.calls[0]["new_count"] == 11, "a new conversation must increment the counter by exactly one"

    print("PASS: webhook only counts/increments monthly_conversation_count on new conversations")


async def main() -> None:
    await test_low_tier_branding_and_free_models()
    await test_basic_and_pro_no_branding_deepseek()
    await test_byok_never_capped_regardless_of_package()
    await test_conversation_window_layer()
    await test_webhook_only_counts_new_conversations()
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
