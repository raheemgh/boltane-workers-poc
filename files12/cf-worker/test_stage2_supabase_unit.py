#!/usr/bin/env python3
"""
Stage 2 — OFFLINE unit tests for the async supabase_client port.

Lives at cf-worker/test_stage2_supabase_unit.py. No network, no Supabase
project. Same way the Stage 1 tests run (so `from workers import env`
resolves to the local stub):

    PYTHONPATH=tooling/local-test-support:src python test_stage2_supabase_unit.py

The database underneath is tooling/local-test-support/memory_supabase.py:
an in-memory emulator of the PostgREST behaviour the module relies on,
exposed with supabase-py's AsyncClient call shape. It lets this file
check everything that is OURS to get right:

  1  static    no thread-offloading anywhere in the file (the Stage 2
               "done" criterion), no import-time client, and every public
               function keeps the Render original's exact signature
  2  lifetime  lazy creation, exactly-once under concurrent first calls,
               the CACHE_CLIENT_ACROSS_CALLS=False fallback, test injection
  3  Stage 1   a missing SUPABASE_* var surfaces get_settings()'s clear
               error and does not leave a broken client cached
  4  queries   the EXACT queries built (columns, filters, limit, upsert
               payload and on_conflict, update payloads, the "omitted
               token_count" rule, "0 is a value" for conversations_remaining)
  5  scenarios the same four scenarios the real-project test runs, checked
               against EXPECTED (debug_supabase.py)
  6  parity    ports of the existing window-layer / token-cap tests
  7  Stage 6   call patterns webhook.py uses still bind, all awaitable
  8  Stage 3   a store row fetched here feeds send_whatsapp() correctly

What this CANNOT show: how a real Supabase project or supabase-py's async
client behaves. test_stage2_supabase_real.py does that.
"""

import ast
import asyncio
import importlib.util
import inspect
import os
import re
import sys
from pathlib import Path

# Settings are read lazily via workers.env (stub -> os.environ) — same
# dummy values the existing tests use.
os.environ.setdefault("META_VERIFY_TOKEN", "test-token")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-supabase-key")
os.environ.setdefault("OPENROUTER_DEFAULT_KEY", "test-openrouter-key")

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(HERE))  # for test_stage3_offline (the Meta simulator)

import app.config as cfg  # noqa: E402
import app.services.supabase_client as sc  # noqa: E402
from memory_supabase import APIError, MemoryDB  # noqa: E402

SC_PATH = HERE / "src" / "app" / "services" / "supabase_client.py"
RENDER_SC_PATH = REPO_ROOT / "app" / "services" / "supabase_client.py"
DEBUG_PATH = HERE / "src" / "app" / "routers" / "debug_supabase.py"

FORBIDDEN = ("to_thread", "threading", "ThreadPoolExecutor", "run_in_executor")
PUBLIC = ("get_store_by_phone_number_id", "is_new_conversation_and_touch",
          "add_conversation_tokens", "update_store_usage")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(condition), detail))


def load_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CountingFactory:
    """Stands in for supabase's acreate_client; counts constructions."""

    def __init__(self, db: MemoryDB) -> None:
        self.db, self.calls = db, []

    async def __call__(self, url, key):
        await asyncio.sleep(0)  # a real one yields; makes the concurrent-first-call race real
        self.calls.append((url, key))
        return self.db.client()


def reset(factory: CountingFactory | None = None, cache: bool = True) -> None:
    sc._client = None
    sc._init_lock = None
    sc.CACHE_CLIENT_ACROSS_CALLS = cache
    if factory is not None:
        sc.acreate_client = factory


# ---------------------------------------------------------------------------

def t_static() -> None:
    source = SC_PATH.read_text(encoding="utf-8")
    found = [token for token in FORBIDDEN if token in source]
    sync_factory = re.search(r"(?<![A-Za-z_])create_client\(", source)  # `acreate_client(` must not match
    check("1 static: no thread-offloading anywhere in the file (and no sync create_client)",
          not found and not sync_factory, str(found or sync_factory))
    check("1 static: the PostgREST-only client is what gets built (the `supabase` umbrella package is not imported)",
          "from postgrest import AsyncPostgrestClient" in source
          and not re.search(r"^\s*(?:from|import)\s+supabase\b", source, re.MULTILINE))

    if not RENDER_SC_PATH.exists():
        check("1 static: signatures identical to the Render original", False, f"{RENDER_SC_PATH} not found")
        return

    def signatures(path: Path) -> dict:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        return {
            node.name: (type(node).__name__, ast.unparse(node.args), ast.unparse(node.returns) if node.returns else None)
            for node in tree.body
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name in PUBLIC + ("_parse_timestamp",)
        }

    ours, theirs = signatures(SC_PATH), signatures(RENDER_SC_PATH)
    check("1 static: the 4 public functions + _parse_timestamp exist in both files",
          set(ours) == set(theirs) == set(PUBLIC + ("_parse_timestamp",)), f"{sorted(ours)} vs {sorted(theirs)}")
    check("1 static: every public signature (args, defaults, return annotation, async-ness) is identical to Render's",
          ours == theirs, "\n".join(f"{k}: {ours.get(k)} != {theirs.get(k)}" for k in theirs if ours.get(k) != theirs.get(k)))


async def t_lifetime() -> None:
    check("2 lifetime: nothing is built at import time", sc._client is None)
    reset()

    db = MemoryDB()
    factory = CountingFactory(db)
    reset(factory)
    await asyncio.gather(*(sc.get_store_by_phone_number_id(f"p{i}") for i in range(10)))
    check("2 lifetime: 10 concurrent first calls build exactly ONE client", len(factory.calls) == 1, str(len(factory.calls)))
    check("2 lifetime: built from Stage 1's get_settings() (SUPABASE_URL, SUPABASE_KEY)",
          factory.calls == [(cfg.get_settings().SUPABASE_URL, cfg.get_settings().SUPABASE_KEY)], str(factory.calls))
    await sc.get_store_by_phone_number_id("again")
    check("2 lifetime: later calls reuse it (still 1 construction)", len(factory.calls) == 1)

    factory = CountingFactory(db)
    reset(factory, cache=False)
    for _ in range(3):
        await sc.get_store_by_phone_number_id("x")
    check("2 lifetime: CACHE_CLIENT_ACROSS_CALLS=False builds a fresh client per call and stores none",
          len(factory.calls) == 3 and sc._client is None, f"{len(factory.calls)} / {sc._client}")

    factory = CountingFactory(db)
    reset(factory)
    sc._client = db.client()
    await sc.get_store_by_phone_number_id("x")
    check("2 lifetime: an injected sc._client is honoured (existing-test pattern) and nothing is built", not factory.calls)
    factory = CountingFactory(db)
    reset(factory, cache=False)
    sc._client = db.client()
    await sc.get_store_by_phone_number_id("x")
    check("2 lifetime: …in the no-cache mode too", not factory.calls)
    reset()


async def t_stage1_link() -> None:
    saved = os.environ.pop("SUPABASE_URL")
    cfg._settings = None
    try:
        factory = CountingFactory(MemoryDB())
        reset(factory)
        raised = None
        try:
            await sc.get_store_by_phone_number_id("x")
        except RuntimeError as exc:
            raised = str(exc)
        check("3 Stage 1: a missing SUPABASE_URL surfaces get_settings()'s clear RuntimeError",
              raised is not None and "SUPABASE_URL" in raised, str(raised))
        check("3 Stage 1: …and nothing broken is cached or built", sc._client is None and not factory.calls)
    finally:
        os.environ["SUPABASE_URL"] = saved
        cfg._settings = None
        reset()


async def t_queries() -> None:
    db = MemoryDB()
    reset()
    sc._client = db.client()
    store_id = await db.insert_store(phone_number_id="PN1", store_name="S", package="pro")
    phone = "15550000001"

    await sc.get_store_by_phone_number_id("PN1")
    check("4 queries: store lookup is select('*') .eq(phone_number_id) .limit(1)",
          db.log[-1] == ("stores", "select", {"columns": "*", "filters": [("phone_number_id", "PN1")], "limit": 1}), str(db.log[-1]))

    db.log.clear()
    await sc.is_new_conversation_and_touch(store_id, phone)
    select_call, upsert_call = db.log
    check("4 queries: window lookup selects 'last_message_at, token_count' filtered by BOTH store_id and customer_phone, limit 1",
          select_call == ("conversation_windows", "select",
                          {"columns": "last_message_at, token_count",
                           "filters": [("store_id", store_id), ("customer_phone", phone)], "limit": 1}), str(select_call))
    new_payload = upsert_call[2]["payload"]
    check("4 queries: upsert uses on_conflict='store_id,customer_phone'", upsert_call[2]["on_conflict"] == "store_id,customer_phone")
    check("4 queries: a NEW conversation's upsert resets token_count to 0 in the same write",
          set(new_payload) == {"store_id", "customer_phone", "last_message_at", "token_count"} and new_payload["token_count"] == 0, str(new_payload))

    await db.patch_window(store_id, phone, token_count=40)
    db.log.clear()
    result = await sc.is_new_conversation_and_touch(store_id, phone)
    cont_payload = db.log[1][2]["payload"]
    check("4 queries: a CONTINUING conversation's upsert OMITS token_count (so the running total survives)",
          set(cont_payload) == {"store_id", "customer_phone", "last_message_at"} and result == (False, 40), f"{cont_payload} {result}")
    check("4 queries: …and the stored total really was preserved", (await db.get_window(store_id, phone))["token_count"] == 40)
    check("4 queries: last_message_at is sent as timezone-aware UTC ISO text", cont_payload["last_message_at"].endswith("+00:00"))

    db.log.clear()
    await sc.add_conversation_tokens(store_id, phone, 25, 40)
    check("4 queries: add_conversation_tokens = update({'token_count': before + used}) filtered by store_id AND customer_phone",
          db.log == [("conversation_windows", "update",
                      {"payload": {"token_count": 65}, "filters": [("store_id", store_id), ("customer_phone", phone)]})], str(db.log))

    db.log.clear()
    await sc.update_store_usage(store_id, 7)
    await sc.update_store_usage(store_id, 8, conversations_remaining=292, last_used_model="m/x")
    await sc.update_store_usage(store_id, 9, conversations_remaining=0)
    payloads = [entry[2]["payload"] for entry in db.log]
    check("4 queries: update_store_usage sends only the fields given; 0 for conversations_remaining IS sent; always filtered by id",
          payloads == [{"monthly_conversation_count": 7},
                       {"monthly_conversation_count": 8, "conversations_remaining": 292, "last_used_model": "m/x"},
                       {"monthly_conversation_count": 9, "conversations_remaining": 0}]
          and all(entry[2]["filters"] == [("id", store_id)] for entry in db.log), str(payloads))
    reset()


async def t_scenarios() -> None:
    debug = load_by_path("stage2_debug_supabase", DEBUG_PATH)
    for op in debug.OPS:
        db = MemoryDB()
        reset()
        sc._client = db.client()
        result = await debug.run_op(op, sc, db, "unit", runtime="memory")
        expected = debug.EXPECTED[op]
        check(f"5 scenario '{op}': no crash", result["crashed"] is None, str(result["crashed"]))
        check(f"5 scenario '{op}': observations == EXPECTED", result["observations"] == expected,
              "\n".join(f"{k}: got {result['observations'].get(k)!r} want {expected.get(k)!r}"
                        for k in expected if (result["observations"] or {}).get(k) != expected.get(k)))
        check(f"5 scenario '{op}': test rows were cleaned up", not db.stores and not db.windows)
    reset()


async def t_parity_with_existing_tests() -> None:
    """Ports of test_conversation_window_layer / the token-cap reset test, onto the async module."""
    from datetime import datetime, timedelta, timezone

    db = MemoryDB()
    reset()
    sc._client = db.client()
    store_id, phone = await db.insert_store(store_name="W"), "+15559998888"

    check("6 parity: a customer's first-ever message starts a new conversation",
          await sc.is_new_conversation_and_touch(store_id, phone) == (True, 0))
    check("6 parity: a second message within the 2h window is NOT a new conversation",
          (await sc.is_new_conversation_and_touch(store_id, phone))[0] is False)
    stale = datetime.now(timezone.utc) - timedelta(hours=3)
    await db.patch_window(store_id, phone, last_message_at=stale.replace(tzinfo=None).isoformat())
    check("6 parity: a message after the 2h window expires starts a new conversation",
          await sc.is_new_conversation_and_touch(store_id, phone) == (True, 0))

    running = 0
    while running < sc.CONVERSATION_TOKEN_CAP:
        await sc.add_conversation_tokens(store_id, phone, 500, running)
        running += 500
        if running < sc.CONVERSATION_TOKEN_CAP:
            assert (await sc.is_new_conversation_and_touch(store_id, phone))[0] is False
    check("6 parity: once CONVERSATION_TOKEN_CAP is reached the next message forces a reset to token_count 0",
          await sc.is_new_conversation_and_touch(store_id, phone) == (True, 0))
    reset()


async def t_webhook_compat() -> None:
    patterns = {
        "get_store_by_phone_number_id": ((("PN",), {})),
        "is_new_conversation_and_touch": ((("store-id", "1555"), {})),
        "add_conversation_tokens": ((("store-id", "1555", 50, 0), {})),
        "update_store_usage": ((("store-id", 3), {"conversations_remaining": 7, "last_used_model": "m"})),
    }
    bad = []
    for name, (args, kwargs) in patterns.items():
        function = getattr(sc, name)
        try:
            inspect.signature(function).bind(*args, **kwargs)
        except TypeError as exc:
            bad.append((name, str(exc)))
        if not inspect.iscoroutinefunction(function):
            bad.append((name, "not a coroutine function"))
    check("7 Stage 6: every call pattern webhook.py uses still binds and is awaitable", not bad, str(bad))


async def t_chain_to_stage3() -> None:
    try:
        import test_stage3_offline as s3
        import httpx  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        check("8 Stage 3 chain: Meta simulator importable", False, f"{type(exc).__name__}: {exc}")
        return

    meta_client = s3.load_module("cfcopy", s3.TARGETS["cf-worker-copy"])
    db = MemoryDB()
    reset()
    sc._client = db.client()
    await db.insert_store(phone_number_id=s3.VALID_PHONE_NUMBER_ID, access_token=s3.VALID_TOKEN, package="pro")
    await db.insert_store(phone_number_id="OTHER-PN", access_token="wrong-token", package="basic")

    store = await sc.get_store_by_phone_number_id(s3.VALID_PHONE_NUMBER_ID)
    fake = s3.FakeMeta()
    returned, raised, logs, _ = await s3.call(
        meta_client, fake, token=store["access_token"], pnid=store["phone_number_id"], to=s3.ALLOWED_RECIPIENT)
    request = fake.requests[0]
    check("8 Stage 3 chain: the fetched row's access_token + phone_number_id drive a Meta-accepted send",
          returned is None and raised is None and logs[0][0] == "INFO" and "succeeded" in logs[0][1]
          and request["headers"]["authorization"] == f"Bearer {s3.VALID_TOKEN}"
          and f"/{s3.VALID_PHONE_NUMBER_ID}/messages" in request["path"], str(logs))

    other = await sc.get_store_by_phone_number_id("OTHER-PN")
    fake = s3.FakeMeta()
    _, raised, logs, _ = await s3.call(meta_client, fake, token=other["access_token"], pnid=s3.VALID_PHONE_NUMBER_ID, to=s3.ALLOWED_RECIPIENT)
    check("8 Stage 3 chain: a store row carrying a bad token is logged by meta_client, not raised",
          raised is None and logs and logs[0][0] == "WARNING" and "190" in logs[0][1], str(logs))
    reset()


async def main() -> int:
    t_static()
    await t_lifetime()
    await t_stage1_link()
    await t_queries()
    await t_scenarios()
    await t_parity_with_existing_tests()
    await t_webhook_compat()
    await t_chain_to_stage3()

    failed = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok or not detail else f"\n        -> {detail}"))
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
