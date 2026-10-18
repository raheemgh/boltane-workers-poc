"""
TEMPORARY Stage 2 diagnostic — remove together with debug_settings.py
and debug_meta.py before Stage 6 / before anything here is deployed.

One file, three jobs:
  - run_op(): the scenarios. Each of the three operations the Stage 2
    task names — filtered select, upsert-on-conflict, update — gets its
    own scenario, plus an "errors" scenario for the error-type
    comparison. A scenario drives the supabase_client MODULE UNDER TEST
    and checks what really happened through an independent `db`
    object, so a wrong write cannot hide behind the module's own
    reads. Returns plain JSON-able observations.
  - EXPECTED: what those observations must be, derived from the Render
    version's documented behaviour. The unit tests check the async port
    against it using an in-memory database; the real-project runs check
    Render, the async port in CPython, and the port under `pywrangler
    dev` against it AND against each other.
  - HttpDB + the /debug/supabase route: the real-project plumbing.

run_op() imports nothing from `app.*` and nothing Workers-specific and
takes the module as an argument, so the very same scenarios run against
the Render module (plain Python), the async port (plain Python), and
the async port inside workerd.

SAFETY — this writes and deletes rows with the service key:
  - 404 unless STAGE2_DEBUG_ENABLED=1.
  - 403 unless STAGE2_CONFIRM_TEST_PROJECT equals SUPABASE_URL exactly.
    Point SUPABASE_URL at a TEST project, then type that same URL into
    the confirm variable. Never at production.
  - every row it creates carries a unique "stage2-<tag>-" marker and is
    deleted in a finally block (conversation_windows rows cascade).
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

router = APIRouter()

OPS = ("select", "upsert", "update", "errors")

STORE_COLUMNS = (
    "id", "phone_number_id", "store_name", "system_prompt", "ai_model", "api_key", "access_token",
    "package", "is_api_free", "is_trial", "monthly_conversation_count", "conversations_remaining",
    "last_used_model", "usage_reset_at", "created_at",
)


# ---------------------------------------------------------------------------
# Real-project verifier: plain PostgREST over httpx, independent of supabase-py
# ---------------------------------------------------------------------------

class HttpDB:
    def __init__(self, url: str, key: str) -> None:
        self._base = url.rstrip("/") + "/rest/v1"
        self._headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    async def _request(self, method: str, table: str, *, params=None, body=None):
        import httpx  # imported lazily: only the real-project path needs it

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method, f"{self._base}/{table}", params=params, headers=self._headers, json=body
            )
        if response.status_code >= 300:
            raise RuntimeError(f"verifier {method} {table} -> {response.status_code}: {response.text[:200]}")
        return response.json() if response.text else []

    async def insert_store(self, **fields) -> str:
        rows = await self._request("POST", "stores", body=fields)
        return rows[0]["id"]

    async def get_store(self, store_id: str):
        rows = await self._request("GET", "stores", params={"id": f"eq.{store_id}", "select": "*"})
        return rows[0] if rows else None

    async def insert_window(self, store_id: str, phone: str, token_count: int) -> None:
        await self._request(
            "POST", "conversation_windows",
            body={"store_id": store_id, "customer_phone": phone, "token_count": token_count,
                  "last_message_at": _naive(datetime.now(timezone.utc))},
        )

    def _window_params(self, store_id: str, phone: str) -> dict:
        return {"store_id": f"eq.{store_id}", "customer_phone": f"eq.{phone}", "select": "*"}

    async def get_window(self, store_id: str, phone: str):
        rows = await self._request("GET", "conversation_windows", params=self._window_params(store_id, phone))
        return rows[0] if rows else None

    async def count_windows(self, store_id: str, phone: str) -> int:
        return len(await self._request("GET", "conversation_windows", params=self._window_params(store_id, phone)))

    async def patch_window(self, store_id: str, phone: str, **fields) -> None:
        await self._request("PATCH", "conversation_windows", params=self._window_params(store_id, phone), body=fields)

    async def delete_store(self, store_id: str) -> None:
        await self._request("DELETE", "stores", params={"id": f"eq.{store_id}"})


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def _naive(moment: datetime) -> str:
    """ISO text the way a `timestamp` (no time zone) column hands it back."""
    return moment.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def _store_fields(tag: str, suffix: str, **overrides) -> dict:
    fields = {
        "phone_number_id": f"stage2-{tag}-{suffix}",
        "store_name": f"stage2-{tag}-{suffix}",
        "system_prompt": f"prompt {suffix}",
        "access_token": f"tok-{suffix}",
        "package": "pro",
        "is_api_free": True,
        "is_trial": False,
        "monthly_conversation_count": 11,
    }
    fields.update(overrides)
    return fields


def _pick(row: dict | None, store_id: str):
    if row is None:
        return None
    return {
        "id_matches": row["id"] == store_id,
        "store_name_suffix": row["store_name"][-1],
        "package": row["package"],
        "is_trial": row["is_trial"],
        "is_api_free": row["is_api_free"],
        "monthly_conversation_count": row["monthly_conversation_count"],
        "access_token": row["access_token"],
        "system_prompt": row["system_prompt"],
        "ai_model": row.get("ai_model"),
    }


async def _capture(awaitable) -> dict:
    try:
        await awaitable
    except Exception as exc:  # noqa: BLE001 — recording the type IS the observation
        return {"raised": type(exc).__name__, "code": getattr(exc, "code", None)}
    return {"raised": None, "code": None}


async def _op_select(module, db, tag, created) -> dict:
    a = await db.insert_store(**_store_fields(tag, "A"))
    created.append(a)
    b = await db.insert_store(
        **_store_fields(tag, "B", package="basic", is_trial=True, monthly_conversation_count=22, access_token="tok-B")
    )
    created.append(b)

    row_a = await module.get_store_by_phone_number_id(f"stage2-{tag}-A")
    row_b = await module.get_store_by_phone_number_id(f"stage2-{tag}-B")
    missing = await module.get_store_by_phone_number_id(f"stage2-{tag}-does-not-exist")
    return {
        "A": _pick(row_a, a),
        "B": _pick(row_b, b),
        "nonexistent": missing,
        "missing_columns": sorted(set(STORE_COLUMNS) - set(row_a or {})),
        "return_type": type(row_a).__name__,
    }


async def _op_upsert(module, db, tag, created) -> dict:
    store_id = await db.insert_store(**_store_fields(tag, "U"))
    created.append(store_id)
    phone = "15550000001"
    cap = module.CONVERSATION_TOKEN_CAP
    obs: dict = {}

    first = await module.is_new_conversation_and_touch(store_id, phone)
    window = await db.get_window(store_id, phone)
    obs["first_call"] = list(first)
    obs["after_first"] = {"rows": await db.count_windows(store_id, phone), "token_count": window["token_count"]}
    stamp_before = module._parse_timestamp(window["last_message_at"])

    await db.patch_window(store_id, phone, token_count=500)
    second = await module.is_new_conversation_and_touch(store_id, phone)
    window = await db.get_window(store_id, phone)
    obs["second_call_continuing"] = list(second)
    obs["after_second"] = {
        "rows": await db.count_windows(store_id, phone),
        "token_count": window["token_count"],  # the omitted column must survive the conflict-update
        "last_message_at_advanced": module._parse_timestamp(window["last_message_at"]) >= stamp_before,
    }

    await db.patch_window(store_id, phone, last_message_at=_naive(datetime.now(timezone.utc) - timedelta(hours=3)))
    third = await module.is_new_conversation_and_touch(store_id, phone)
    window = await db.get_window(store_id, phone)
    obs["third_call_window_expired"] = list(third)
    obs["after_third"] = {"rows": await db.count_windows(store_id, phone), "token_count": window["token_count"]}

    await db.patch_window(store_id, phone, token_count=cap)
    fourth = await module.is_new_conversation_and_touch(store_id, phone)
    window = await db.get_window(store_id, phone)
    obs["fourth_call_token_cap_reset"] = list(fourth)
    obs["after_fourth"] = {"rows": await db.count_windows(store_id, phone), "token_count": window["token_count"]}

    await db.patch_window(store_id, phone, token_count=cap - 1)
    fifth = await module.is_new_conversation_and_touch(store_id, phone)
    window = await db.get_window(store_id, phone)
    obs["fifth_call_just_under_cap"] = list(fifth)
    obs["after_fifth"] = {"rows": await db.count_windows(store_id, phone), "token_count": window["token_count"]}
    return obs


async def _op_update(module, db, tag, created) -> dict:
    store_id = await db.insert_store(**_store_fields(tag, "P", monthly_conversation_count=5))
    created.append(store_id)
    other_id = await db.insert_store(**_store_fields(tag, "Q", monthly_conversation_count=22))
    created.append(other_id)
    phone_1, phone_2 = "15550000011", "15550000012"
    await db.insert_window(store_id, phone_1, 300)
    await db.insert_window(store_id, phone_2, 77)
    obs: dict = {}

    returned = await module.add_conversation_tokens(store_id, phone_1, 120, 300)
    obs["add_tokens_returns_none"] = returned is None
    obs["token_after_add"] = (await db.get_window(store_id, phone_1))["token_count"]
    obs["other_customer_untouched"] = (await db.get_window(store_id, phone_2))["token_count"]

    async def usage_row():
        row = await db.get_store(store_id)
        return {
            "monthly": row["monthly_conversation_count"],
            "remaining": row["conversations_remaining"],
            "model": row["last_used_model"],
        }

    returned = await module.update_store_usage(
        store_id, 7, conversations_remaining=293, last_used_model="test/model"
    )
    obs["update_usage_returns_none"] = returned is None
    obs["full_update"] = await usage_row()
    await module.update_store_usage(store_id, 8)
    obs["count_only_leaves_others"] = await usage_row()
    await module.update_store_usage(store_id, 9, conversations_remaining=0)
    obs["zero_remaining_is_written"] = await usage_row()
    obs["other_store_untouched"] = (await db.get_store(other_id))["monthly_conversation_count"]
    obs["update_of_nonexistent_store"] = await _capture(module.update_store_usage(str(uuid.uuid4()), 1))
    return obs


async def _op_errors(module, db, tag, created) -> dict:
    return {
        "update_usage_invalid_uuid": await _capture(module.update_store_usage("not-a-uuid", 1)),
        "window_upsert_unknown_store": await _capture(
            module.is_new_conversation_and_touch(str(uuid.uuid4()), "15550000099")
        ),
        "add_tokens_unknown_store": await _capture(
            module.add_conversation_tokens(str(uuid.uuid4()), "15550000099", 10, 0)
        ),
    }


_SCENARIOS = {"select": _op_select, "upsert": _op_upsert, "update": _op_update, "errors": _op_errors}


async def run_op(op: str, module, db, tag: str, runtime: str) -> dict:
    """Runs one scenario. Never raises: a crash is recorded, then rows are cleaned up."""
    created: list[str] = []
    result = {"op": op, "runtime": runtime, "observations": None, "crashed": None}
    try:
        result["observations"] = await _SCENARIOS[op](module, db, tag, created)
    except Exception as exc:  # noqa: BLE001
        result["crashed"] = f"{type(exc).__module__}.{type(exc).__qualname__}: {str(exc)[:300]}"
    finally:
        for store_id in created:
            try:
                await db.delete_store(store_id)
            except Exception as exc:  # noqa: BLE001
                result.setdefault("cleanup_errors", []).append(str(exc)[:200])
    return result


_SELECT_A = {"id_matches": True, "store_name_suffix": "A", "package": "pro", "is_trial": False,
             "is_api_free": True, "monthly_conversation_count": 11, "access_token": "tok-A",
             "system_prompt": "prompt A", "ai_model": None}
_SELECT_B = {**_SELECT_A, "store_name_suffix": "B", "package": "basic", "is_trial": True,
             "monthly_conversation_count": 22, "access_token": "tok-B", "system_prompt": "prompt B"}

EXPECTED = {
    "select": {"A": _SELECT_A, "B": _SELECT_B, "nonexistent": None, "missing_columns": [], "return_type": "dict"},
    "upsert": {
        "first_call": [True, 0],
        "after_first": {"rows": 1, "token_count": 0},
        "second_call_continuing": [False, 500],
        "after_second": {"rows": 1, "token_count": 500, "last_message_at_advanced": True},
        "third_call_window_expired": [True, 0],
        "after_third": {"rows": 1, "token_count": 0},
        "fourth_call_token_cap_reset": [True, 0],
        "after_fourth": {"rows": 1, "token_count": 0},
        "fifth_call_just_under_cap": [False, 2999],
        "after_fifth": {"rows": 1, "token_count": 2999},
    },
    "update": {
        "add_tokens_returns_none": True,
        "token_after_add": 420,
        "other_customer_untouched": 77,
        "update_usage_returns_none": True,
        "full_update": {"monthly": 7, "remaining": 293, "model": "test/model"},
        "count_only_leaves_others": {"monthly": 8, "remaining": 293, "model": "test/model"},
        "zero_remaining_is_written": {"monthly": 9, "remaining": 0, "model": "test/model"},
        "other_store_untouched": 22,
        "update_of_nonexistent_store": {"raised": None, "code": None},
    },
    "errors": {
        "update_usage_invalid_uuid": {"raised": "APIError", "code": "22P02"},
        "window_upsert_unknown_store": {"raised": "APIError", "code": "23503"},
        "add_tokens_unknown_store": {"raised": None, "code": None},
    },
}
# "fifth_call_just_under_cap" assumes CONVERSATION_TOKEN_CAP == 3000 (token_limits.py, Stage 0).


# ---------------------------------------------------------------------------
# Workers-only route
# ---------------------------------------------------------------------------

def _env_value(env, name: str) -> str | None:
    try:
        value = getattr(env, name)
    except Exception:  # noqa: BLE001 — a missing binding just means "not set"
        return None
    if value is None:
        return None
    value = str(value)
    return value or None


@router.get("/debug/supabase")
async def debug_supabase(op: str) -> dict:
    # Lazy imports so this file can also be loaded by path from plain Python.
    from workers import env

    if _env_value(env, "STAGE2_DEBUG_ENABLED") != "1":
        raise HTTPException(status_code=404)
    if op not in OPS:
        raise HTTPException(status_code=400, detail=f"unknown op; expected one of {list(OPS)}")

    from app.config import get_settings

    settings = get_settings()
    if _env_value(env, "STAGE2_CONFIRM_TEST_PROJECT") != settings.SUPABASE_URL:
        raise HTTPException(
            status_code=403,
            detail="STAGE2_CONFIRM_TEST_PROJECT must equal SUPABASE_URL exactly "
                   "(this route writes and deletes rows; point both at a TEST project).",
        )

    import app.services.supabase_client as module

    return await run_op(op, module, HttpDB(settings.SUPABASE_URL, settings.SUPABASE_KEY),
                        uuid.uuid4().hex[:8], runtime="workers")
