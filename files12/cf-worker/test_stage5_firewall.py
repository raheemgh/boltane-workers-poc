#!/usr/bin/env python3
"""
Stage 5 — token/budget firewall, END TO END through the real pipeline.

Lives at cf-worker/test_stage5_firewall.py. No network, no credentials.

    cd cf-worker
    source ~/venv-tests/bin/activate
    python test_stage5_firewall.py                  # Workers tree: every check + sabotage
    python test_stage5_firewall.py record workers   # -> stage5_workers.json
    python test_stage5_firewall.py record render    # -> stage5_render.json (needs ../app = Render original)
    python test_stage5_firewall.py compare          # Render vs Workers vs EXPECTED

What is REAL here (nothing below is replaced by a fake):
  * webhook._process_message  — the actual pipeline, called directly
  * supabase_client           — the actual functions (Workers: async port; Render: sync + to_thread)
  * ai_client.call_ai         — the actual function (masking, max_tokens, fallback, usage accounting)
  * meta_client.send_whatsapp — the actual function
What is REPLACED (only these two boundaries):
  * Supabase  -> MemoryDB (tooling/local-test-support/memory_supabase.py), an in-memory emulator of
                 the PostgREST behaviour the module uses. Rows are verified through MemoryDB's own
                 independent verifier face, not through supabase_client.
  * Network   -> a fake httpx.AsyncClient that answers OpenRouter (streamed SSE) and Meta, and
                 records every request, so "no OpenRouter call was made" is a count, not a guess.

What this CANNOT show: how a real Supabase project behaves (Stage 2's real test did that), or how
OpenRouter behaves (Stage 4's real test did that). It proves the WIRING of the firewall.

Real pipeline order, as confirmed by the traces below (each name = one step the pipeline took):

    get_store -> [input cap] -> touch (window read+upsert) -> [monthly cap]
              -> call_ai -> update_usage -> add_tokens -> send

Scenarios
  S01  input cap: rejected BEFORE the window is touched; boundary 1200 chars allowed, 1201 rejected
  S02  monthly cap: store at cap, new conversation -> blocked, zero OpenRouter requests
  S03  allowed: new conversation -> usage written by the real update functions, max_tokens=300 sent
  S04  every package's cap (low-tier 40, basic trial 100, basic paid 300, pro 1000): below / at / over;
       BYOK exempt
  S05  token accumulation turn by turn up to CONVERSATION_TOKEN_CAP, then the forced reset
  S06  forced reset counts against the monthly cap; time-window expiry vs a fresh window
  S07  KNOWN GAP (characterisation): a store over its cap only blocks the FIRST message of each
       window — see the note at EXPECTED["S07"]
  S08  a failed AI call still consumes a monthly conversation (characterisation)
  S09  window lookup failure fails SAFE (treated as new -> the cap applies)
  and a sabotage run: the whole suite must FAIL on deliberately weakened copies of the firewall.
"""

import argparse
import ast
import asyncio
import contextlib
import json
import logging
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent          # cf-worker/
REPO_ROOT = HERE.parent                         # holds the Render original under app/
SRC = HERE / "src"
SUPPORT = HERE / "tooling" / "local-test-support"
OUT = {tree: HERE / f"stage5_{tree}.json" for tree in ("render", "workers")}

CUSTOMER_A = "15551230001"
CUSTOMER_B = "15551230002"
PNID = "PN-STAGE5"
AI_REPLY = "أهلاً! كيف أساعدك؟"
DEEPSEEK = "deepseek/deepseek-v4-flash"

T = SimpleNamespace()   # filled by load_tree(): wh, sc, ac, mc, tl, IS_WORKERS, APIError, MemoryDB, httpx


# ---------------------------------------------------------------------------
# Loading one tree (Workers or Render) — they cannot share an interpreter
# ---------------------------------------------------------------------------

def load_tree(tree: str) -> None:
    # Same dummy values the existing tests use (test_package_system.py) — Render's supabase_client builds a
    # client at import time, and these are known to be accepted by supabase-py. Nothing here hits the network.
    for name, value in (("META_VERIFY_TOKEN", "test-token"), ("META_APP_SECRET", "test-app-secret"),
                        ("SUPABASE_URL", "https://example.supabase.co"), ("SUPABASE_KEY", "test-supabase-key"),
                        ("OPENROUTER_DEFAULT_KEY", "test-openrouter-key")):
        os.environ.setdefault(name, value)
    os.environ.pop("OPENROUTER_SOCKS5_PROXY", None)

    root = SRC if tree == "workers" else REPO_ROOT
    if tree == "render" and not (REPO_ROOT / "app" / "routers" / "webhook.py").exists():
        sys.exit(f"Render original not found at {REPO_ROOT / 'app'} — cannot run the 'render' side.")
    sys.path[:0] = [str(SUPPORT), str(root)]   # SUPPORT: memory_supabase (+ the `workers` stub for Workers)

    import httpx
    from memory_supabase import APIError, MemoryDB
    import app.routers.webhook as wh
    import app.services.ai_client as ac
    import app.services.meta_client as mc
    import app.services.supabase_client as sc
    import app.services.token_limits as tl

    expected = (root / "app" / "routers" / "webhook.py").resolve()
    if Path(wh.__file__).resolve() != expected:
        sys.exit(f"imported the wrong webhook module: {wh.__file__} (expected {expected}). "
                 "Unset PYTHONPATH and run again.")

    for name in ("boltane.webhook", "boltane.ai_client", "boltane.meta_client"):
        logging.getLogger(name).addHandler(logging.NullHandler())   # keep expected warnings off the screen

    T.tree, T.httpx, T.APIError, T.MemoryDB = tree, httpx, APIError, MemoryDB
    T.wh, T.ac, T.mc, T.sc, T.tl = wh, ac, mc, sc, tl
    T.IS_WORKERS = hasattr(sc, "_get_client")
    if not T.IS_WORKERS:
        # Render's low-tier chain is three PLACEHOLDER ':free' ids (Stage 4 decision: the Workers port
        # uses DeepSeek). Point Render at the same model so the one intended difference cannot show up.
        ac.PACKAGE_DEFAULT_MODELS["low-tier"] = [ac.DEEPSEEK_MODEL]


# ---------------------------------------------------------------------------
# Fake network (OpenRouter streamed SSE + Meta Graph API)
# ---------------------------------------------------------------------------

class Net:
    def __init__(self, trace: list, tokens: int, steps: list):
        self.trace, self.tokens, self.steps = trace, tokens, list(steps)
        self.or_requests: list = []
        self.meta_sends: list = []


class _Resp:
    def __init__(self, status: int, lines: list):
        self.status_code, self.text, self._lines = status, "", lines

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _Ctx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *_a):
        return False


def _client_class(net: Net):
    class _Client:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        def stream(self, method, url, headers=None, json=None):          # OpenRouter (ai_client)
            net.trace.append("net:openrouter")
            net.or_requests.append(json)
            step = net.steps.pop(0) if net.steps else {"kind": "ok"}
            if step["kind"] == "status":
                return _Ctx(_Resp(step["code"], []))
            total = net.tokens
            dumps = __import__("json").dumps
            lines = [
                "data: " + dumps({"choices": [{"delta": {"content": AI_REPLY}}]}, ensure_ascii=False),
                "data: " + dumps({"choices": [], "usage": {"prompt_tokens": total - 20,
                                                           "completion_tokens": 20, "total_tokens": total}}),
                "data: [DONE]",
            ]
            return _Ctx(_Resp(200, lines))

        async def post(self, url, headers=None, json=None):               # Meta (meta_client)
            net.trace.append("net:meta")
            net.meta_sends.append(json["text"]["body"])
            return SimpleNamespace(status_code=200, text="{}")

    return _Client


# ---------------------------------------------------------------------------
# Supabase side: MemoryDB, plus a sync facade for the Render tree and an outage wrapper
# ---------------------------------------------------------------------------

class _SyncBuilder:
    """Render's supabase_client calls .execute() synchronously (inside asyncio.to_thread). MemoryDB's
    builder has an `async def execute()` that never actually awaits, so it is driven by hand here."""

    def __init__(self, builder):
        self._b = builder

    def __getattr__(self, name):
        attr = getattr(self._b, name)
        if name == "execute":
            def run():
                coro = attr()
                try:
                    coro.send(None)
                except StopIteration as done:
                    return done.value
                raise RuntimeError("the in-memory emulator unexpectedly awaited")
            return run

        def chain(*args, **kwargs):
            attr(*args, **kwargs)
            return self
        return chain


class _SyncClient:
    def __init__(self, inner):
        self._inner = inner

    def table(self, name):
        return _SyncBuilder(self._inner.table(name))


class _Outage:
    def __init__(self, inner, tables):
        self._inner, self._tables = inner, set(tables)

    def table(self, name):
        if name in self._tables:
            raise T.APIError("08006", f"simulated outage on {name}")
        return self._inner.table(name)


def _naive_hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(tzinfo=None).isoformat()


# ---------------------------------------------------------------------------
# World = one store + one in-memory database + one fake network
# ---------------------------------------------------------------------------

TRACE_STEPS = (
    ("get_store_by_phone_number_id", "get_store", "sc"),
    ("is_new_conversation_and_touch", "touch", "sc"),
    ("add_conversation_tokens", "add_tokens", "sc"),
    ("update_store_usage", "update_usage", "sc"),
    ("call_ai", "call_ai", "ac"),
    ("send_whatsapp", "send", "mc"),
)
LOG_MARKERS = (("conversation-window lookup failed", "window_lookup_failed"),
               ("failed to update conversation token count", "token_update_failed"))


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class World:
    async def build(self, *, package, is_trial, is_api_free, count, api_key, tokens, fail_tables, steps):
        self.trace: list = []
        self.net = Net(self.trace, tokens, steps)
        self.db = T.MemoryDB()
        self.store_id = await self.db.insert_store(
            phone_number_id=PNID, store_name="stage5", system_prompt="You are a shop assistant.",
            access_token="test-access-token", package=package, is_trial=is_trial,
            is_api_free=is_api_free, monthly_conversation_count=count, api_key=api_key)
        client = self.db.client()
        if not T.IS_WORKERS:
            client = _SyncClient(client)
        if fail_tables:
            client = _Outage(client, fail_tables)
        T.sc._client = client
        if T.IS_WORKERS:
            T.sc._init_lock = None
            T.sc.CACHE_CLIENT_ACROSS_CALLS = True
        self.logs: list[str] = []
        return self

    @contextlib.contextmanager
    def _instrumented(self):
        originals = {name: getattr(T.wh, name) for name, _, _ in TRACE_STEPS}
        original_client = T.httpx.AsyncClient
        collector, logger = _Collect(), logging.getLogger("boltane.webhook")
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(collector)

        def wrap(label, fn):
            async def inner(*args, **kwargs):
                self.trace.append(label)
                return await fn(*args, **kwargs)
            return inner

        for name, label, owner in TRACE_STEPS:
            setattr(T.wh, name, wrap(label, getattr(getattr(T, owner), name)))
        T.httpx.AsyncClient = _client_class(self.net)
        try:
            yield
        finally:
            T.httpx.AsyncClient = original_client
            for name, fn in originals.items():
                setattr(T.wh, name, fn)
            logger.removeHandler(collector)
            logger.setLevel(old_level)
            self.logs.extend(collector.messages)

    async def step(self, text="what time do you open?", customer=CUSTOMER_A) -> dict:
        """Send one customer message through the real pipeline; return what happened + the DB afterwards."""
        self.trace.clear()
        self.net.meta_sends.clear()
        before = len(self.net.or_requests)
        payload = {"entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": PNID},
            "messages": [{"from": customer, "text": {"body": text}}]}}]}]}
        self.logs.clear()
        with self._instrumented():
            await T.wh._process_message(payload)
        store = await self.db.get_store(self.store_id)
        window = await self.db.get_window(self.store_id, customer)
        return {
            "trace": list(self.trace),
            "reply": reply_kind(self.net.meta_sends[0] if len(self.net.meta_sends) == 1 else None),
            "or_requests": len(self.net.or_requests) - before,
            "count": store["monthly_conversation_count"],
            "remaining": store["conversations_remaining"],
            "model": store["last_used_model"],
            "window_rows": await self.db.count_windows(self.store_id, customer),
            "token_count": window["token_count"] if window else None,
            "logged": sorted({tag for text_, tag in LOG_MARKERS for m in self.logs if text_ in m}),
        }

    async def seed_window(self, customer, token_count, hours_ago=0.0):
        await self.db.insert_window(self.store_id, customer, token_count)
        if hours_ago:
            await self.db.patch_window(self.store_id, customer, last_message_at=_naive_hours_ago(hours_ago))


async def world(*, package="basic", is_trial=False, is_api_free=True, count=0, api_key=None,
                tokens=120, fail_tables=(), steps=()) -> World:
    return await World().build(package=package, is_trial=is_trial, is_api_free=is_api_free, count=count,
                               api_key=api_key, tokens=tokens, fail_tables=fail_tables, steps=steps)


def reply_kind(reply):
    if reply is None:
        return "none"
    footer = T.wh.BOLTANE_BRANDING_FOOTER
    has_footer = reply.endswith(footer)
    base = reply[: -len(footer)] if has_footer else reply
    kinds = ((T.wh.MONTHLY_LIMIT_REACHED_MESSAGE, "limit"), (T.wh.MESSAGE_TOO_LONG_REPLY, "too_long"),
             (T.ac.GRACEFUL_FAILURE_REPLY, "graceful"), (AI_REPLY, "ai"))
    kind = next((label for text, label in kinds if base == text), "other:" + base[:30])
    return kind + ("+footer" if has_footer else "")


def pick(record: dict, *keys) -> dict:
    return {key: record[key] for key in keys}


# Names for the three pipeline shapes the traces must show.
TOO_LONG = ["get_store", "send", "net:meta"]
BLOCKED = ["get_store", "touch", "send", "net:meta"]
ALLOWED = ["get_store", "touch", "call_ai", "net:openrouter", "update_usage", "add_tokens", "send", "net:meta"]
CORE = ("reply", "or_requests", "count", "token_count")


# ---------------------------------------------------------------------------
# Scenarios — each returns plain JSON-able observations
# ---------------------------------------------------------------------------

async def s01_input_cap():
    limit = T.tl.MAX_INPUT_TOKENS_PER_MESSAGE * 4
    obs = {}
    w = await world(count=7)
    obs["over_limit"] = await w.step("a" * (limit + 1))
    w = await world(count=300)
    obs["over_limit_store_at_cap"] = pick(await w.step("a" * (limit + 1)), "trace", "reply", "or_requests", "count", "window_rows")
    w = await world(count=7)
    obs["exactly_at_limit"] = await w.step("a" * limit)
    return obs


async def s02_blocked_at_cap():
    w = await world(count=300)
    return await w.step()


async def s03_allowed_new_conversation():
    w = await world(count=10)
    obs = await w.step()
    body = w.net.or_requests[0]
    obs["request_max_tokens"], obs["request_model"] = body.get("max_tokens"), body.get("model")
    return obs


CAP_CONFIGS = (("low-tier", dict(package="low-tier", is_trial=True), 40),
               ("basic-trial", dict(package="basic", is_trial=True), 100),
               ("basic-paid", dict(package="basic", is_trial=False), 300),
               ("pro", dict(package="pro", is_trial=False), 1000))


async def s04_caps_by_package():
    obs = {}
    for name, kwargs, cap in CAP_CONFIGS:
        w = await world(count=cap - 1, **kwargs)
        below = await w.step(customer=CUSTOMER_A)
        nxt = await w.step(customer=CUSTOMER_B)
        over = await (await world(count=cap + 5, **kwargs)).step()
        obs[name] = {
            "below_cap_allowed": pick(below, "reply", "or_requests", "count", "remaining"),
            "next_new_customer_blocked": pick(nxt, "reply", "or_requests", "count"),
            "over_cap_blocked": pick(over, "reply", "or_requests", "count", "remaining"),
        }
    obs["byok"] = {}
    for package in ("low-tier", "basic", "pro"):
        w = await world(package=package, is_api_free=False, count=999_999, api_key="sk-store-own")
        obs["byok"][package] = pick(await w.step(), "reply", "or_requests", "count", "remaining")
    return obs


async def s05_token_accumulation():
    w = await world(count=10, tokens=1000)
    return [pick(await w.step(), *CORE) for _ in range(4)]


async def s06_forced_reset_and_expiry():
    obs = {}
    for label, count, token_count, hours in (("forced_reset_under_cap", 50, 3000, 0.0),
                                             ("forced_reset_at_cap", 300, 3000, 0.0),
                                             ("just_under_token_cap", 50, 2999, 0.0),
                                             ("window_expired_3h", 50, 500, 3.0),
                                             ("window_fresh_1h", 50, 500, 1.0)):
        w = await world(count=count)
        await w.seed_window(CUSTOMER_A, token_count, hours)
        obs[label] = pick(await w.step(), *CORE)
    return obs


async def s07_known_gap_blocked_window():
    w = await world(count=300, tokens=1500)
    obs = [pick(await w.step(customer=CUSTOMER_A), *CORE) for _ in range(5)]
    obs.append(pick(await w.step(customer=CUSTOMER_B), *CORE))
    return obs


async def s08_failed_ai_call_consumes_quota():
    w = await world(count=10, steps=[{"kind": "status", "code": 500}])
    return await w.step()


async def s09_window_lookup_failure_fails_safe():
    w = await world(count=300, fail_tables=("conversation_windows",))
    at_cap = pick(await w.step(), "trace", "reply", "or_requests", "count", "logged")
    w = await world(count=10, fail_tables=("conversation_windows",))
    under = pick(await w.step(), "trace", "reply", "or_requests", "count", "remaining", "model", "window_rows", "logged")
    return {"store_at_cap": at_cap, "store_under_cap": under}


SCENARIOS = (("S01", s01_input_cap), ("S02", s02_blocked_at_cap), ("S03", s03_allowed_new_conversation),
             ("S04", s04_caps_by_package), ("S05", s05_token_accumulation), ("S06", s06_forced_reset_and_expiry),
             ("S07", s07_known_gap_blocked_window), ("S08", s08_failed_ai_call_consumes_quota),
             ("S09", s09_window_lookup_failure_fails_safe))


# ---------------------------------------------------------------------------
# EXPECTED — derived from the documented Render behaviour (webhook.py / supabase_client.py
# docstrings and test_package_system.py / test_usage_protection.py), not from running the code.
# ---------------------------------------------------------------------------

def _full(trace, reply, orq, count, remaining, model, rows, tc, logged=()):
    return {"trace": trace, "reply": reply, "or_requests": orq, "count": count, "remaining": remaining,
            "model": model, "window_rows": rows, "token_count": tc, "logged": list(logged)}


def _caps(cap, footer=""):
    return {
        "below_cap_allowed": {"reply": "ai" + footer, "or_requests": 1, "count": cap, "remaining": 0},
        "next_new_customer_blocked": {"reply": "limit", "or_requests": 0, "count": cap},
        "over_cap_blocked": {"reply": "limit", "or_requests": 0, "count": cap + 5, "remaining": None},
    }


def _core(reply, orq, count, tc):
    return {"reply": reply, "or_requests": orq, "count": count, "token_count": tc}


EXPECTED = {
    "S01": {
        "over_limit": _full(TOO_LONG, "too_long", 0, 7, None, None, 0, None),
        "over_limit_store_at_cap": {"trace": TOO_LONG, "reply": "too_long", "or_requests": 0, "count": 300, "window_rows": 0},
        "exactly_at_limit": _full(ALLOWED, "ai", 1, 8, 292, DEEPSEEK, 1, 120),
    },
    "S02": _full(BLOCKED, "limit", 0, 300, None, None, 1, 0),
    "S03": {**_full(ALLOWED, "ai", 1, 11, 289, DEEPSEEK, 1, 120), "request_max_tokens": 300, "request_model": DEEPSEEK},
    "S04": {
        "low-tier": _caps(40, "+footer"), "basic-trial": _caps(100), "basic-paid": _caps(300), "pro": _caps(1000),
        "byok": {"low-tier": {"reply": "ai+footer", "or_requests": 1, "count": 1_000_000, "remaining": None},
                 "basic": {"reply": "ai", "or_requests": 1, "count": 1_000_000, "remaining": None},
                 "pro": {"reply": "ai", "or_requests": 1, "count": 1_000_000, "remaining": None}},
    },
    "S05": [_core("ai", 1, 11, 1000), _core("ai", 1, 11, 2000), _core("ai", 1, 11, 3000),
            _core("ai", 1, 12, 1000)],   # 4th message: 3000 >= CONVERSATION_TOKEN_CAP -> forced new conversation
    "S06": {
        "forced_reset_under_cap": _core("ai", 1, 51, 120),
        "forced_reset_at_cap": _core("limit", 0, 300, 0),
        "just_under_token_cap": _core("ai", 1, 50, 3119),
        "window_expired_3h": _core("ai", 1, 51, 120),
        "window_fresh_1h": _core("ai", 1, 50, 620),
    },
    # S07 — KNOWN GAP, pinned on purpose (identical on Render: same logic). A store AT its cap blocks only
    # the first message of each window: the blocked attempt still creates/touches the window row, so the
    # customer's NEXT message is "continuing" and skips the cap check entirely. 3 of 5 messages below reach
    # OpenRouter although the store is at its cap, and the counter never moves. If this is ever fixed,
    # this expectation MUST change in the same commit — that is the point of pinning it.
    "S07": [_core("limit", 0, 300, 0), _core("ai", 1, 300, 1500), _core("ai", 1, 300, 3000),
            _core("limit", 0, 300, 0), _core("ai", 1, 300, 1500), _core("limit", 0, 300, 0)],
    "S08": _full(ALLOWED, "graceful", 1, 11, 289, None, 1, 0),
    "S09": {
        "store_at_cap": {"trace": BLOCKED, "reply": "limit", "or_requests": 0, "count": 300,
                         "logged": ["window_lookup_failed"]},
        "store_under_cap": {"trace": ALLOWED, "reply": "ai", "or_requests": 1, "count": 11, "remaining": 289,
                            "model": DEEPSEEK, "window_rows": 0,
                            "logged": ["token_update_failed", "window_lookup_failed"]},
    },
}


def diff(expected, got, path=""):
    if isinstance(expected, dict) and isinstance(got, dict):
        for key in sorted(set(expected) | set(got)):
            yield from diff(expected.get(key, "<absent>"), got.get(key, "<absent>"), f"{path}.{key}" if path else key)
    elif isinstance(expected, list) and isinstance(got, list) and len(expected) == len(got):
        for i, (e, g) in enumerate(zip(expected, got)):
            yield from diff(e, g, f"{path}[{i}]")
    elif expected != got:
        yield path, expected, got


async def run_all() -> dict:
    results = {}
    for sid, fn in SCENARIOS:
        try:
            results[sid] = await fn()
        except Exception as exc:  # noqa: BLE001 — a crash is an observation, not a reason to stop
            results[sid] = {"crashed": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return results


def problems_vs_expected(results: dict) -> list:
    return [(sid, path, want, got) for sid in EXPECTED for path, want, got in diff(EXPECTED[sid], results.get(sid))]


# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------

def static_checks() -> list:
    out = []

    def check(name, ok, detail=""):
        out.append((name, bool(ok), detail))

    tl, wh = T.tl, T.wh
    check("constants: input cap 300, output cap 300, conversation cap 3000 (unchanged by the port)",
          (tl.MAX_INPUT_TOKENS_PER_MESSAGE, tl.MAX_OUTPUT_TOKENS_PER_MESSAGE, tl.CONVERSATION_TOKEN_CAP) == (300, 300, 3000))
    check("constants: monthly caps low-tier 40 / basic trial 100 / basic paid 300 / pro 1000",
          (wh.resolve_monthly_conversation_cap("low-tier", True), wh.resolve_monthly_conversation_cap("basic", True),
           wh.resolve_monthly_conversation_cap("basic", False), wh.resolve_monthly_conversation_cap("pro", False))
          == (40, 100, 300, 1000))
    check("constants: conversation window is 2 hours", T.sc.CONVERSATION_WINDOW_HOURS == 2)

    render_wh = REPO_ROOT / "app" / "routers" / "webhook.py"
    render_tl = REPO_ROOT / "app" / "services" / "token_limits.py"
    if not render_wh.exists():
        print("SKIPPED: Render original not found at", REPO_ROOT / "app", "— parity checks not run")
        return out

    def funcs(path):
        tree = ast.parse(Path(path).read_text("utf-8"))
        funcs_ = {n.name: ast.dump(n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        consts = {t.id: ast.dump(n.value) for n in tree.body if isinstance(n, ast.Assign)
                  for t in n.targets if isinstance(t, ast.Name)}
        return funcs_, consts

    rf, rc = funcs(render_wh)
    wf, wc = funcs(Path(wh.__file__))
    check("AST parity with Render: _process_message and resolve_monthly_conversation_cap are identical",
          rf["_process_message"] == wf["_process_message"]
          and rf["resolve_monthly_conversation_cap"] == wf["resolve_monthly_conversation_cap"])
    firewall_consts = ("PACKAGE_MONTHLY_CONVERSATION_CAP", "BASIC_TRIAL_MONTHLY_CONVERSATION_CAP",
                       "BASIC_PAID_MONTHLY_CONVERSATION_CAP", "MONTHLY_LIMIT_REACHED_MESSAGE", "MESSAGE_TOO_LONG_REPLY")
    check("AST parity with Render: the cap constants and the two fixed reply texts are identical",
          all(rc[name] == wc[name] for name in firewall_consts))
    check("token_limits.py is byte-identical to the Render original",
          Path(tl.__file__).read_bytes() == render_tl.read_bytes())
    return out


# ---------------------------------------------------------------------------
# Sabotage — the suite must FAIL when the firewall is deliberately weakened
# ---------------------------------------------------------------------------

LIMIT_CHECK = "if is_api_free and is_new_conversation and conversation_count >= cap:"
MUTATIONS = (
    ("wh", "input cap removed", "if estimated_input_tokens > MAX_INPUT_TOKENS_PER_MESSAGE:", "if False:"),
    ("wh", "monthly cap off by one (> instead of >=)", "and conversation_count >= cap:", "and conversation_count > cap:"),
    ("wh", "monthly cap never blocks", LIMIT_CHECK, "if False:"),
    ("wh", "BYOK stores capped too", LIMIT_CHECK, "if is_new_conversation and conversation_count >= cap:"),
    ("wh", "conversation token usage never recorded",
     'await add_conversation_tokens(store["id"], customer_number, tokens_used, conversation_token_count)', "pass"),
    ("wh", "monthly counter never increments",
     "new_count = conversation_count + 1 if is_new_conversation else conversation_count", "new_count = conversation_count"),
    ("wh", "every message counted as a conversation",
     "new_count = conversation_count + 1 if is_new_conversation else conversation_count", "new_count = conversation_count + 1"),
    ("wh", "window lookup failure fails OPEN", "is_new_conversation = True", "is_new_conversation = False"),
    ("sc", "token cap never forces a reset",
     "token_cap_reached = existing_token_count >= CONVERSATION_TOKEN_CAP", "token_cap_reached = False"),
    ("sc", "a forced reset does not zero token_count", 'payload["token_count"] = 0', "pass"),
    ("sc", "the 2h window effectively never expires", "hours=CONVERSATION_WINDOW_HOURS", "hours=CONVERSATION_WINDOW_HOURS * 1000"),
    ("sc", "token total overwritten instead of accumulated", "new_total = token_count_before + tokens_used", "new_total = tokens_used"),
    ("sc", "conversations_remaining never written", 'payload["conversations_remaining"] = conversations_remaining', "pass"),
    ("ac", "max_tokens dropped from the OpenRouter request", '"max_tokens": MAX_OUTPUT_TOKENS_PER_MESSAGE,', ""),
)


def _mutant(module, old, new):
    path = Path(module.__file__)
    source = path.read_text("utf-8")
    if source.count(old) != 1:
        raise AssertionError(f"mutation target found {source.count(old)} times in {path.name}: {old!r}")
    mutant = types.ModuleType(module.__name__ + "_mutant")
    mutant.__file__ = str(path)
    exec(compile(source.replace(old, new), str(path), "exec"), mutant.__dict__)
    return mutant


def run_sabotage() -> list:
    out = []
    original = {"wh": T.wh, "sc": T.sc, "ac": T.ac}
    for owner, label, old, new in MUTATIONS:
        setattr(T, owner, _mutant(original[owner], old, new))
        try:
            results = asyncio.run(run_all())
            caught = problems_vs_expected(results)
        except Exception as exc:  # noqa: BLE001 — blowing up also counts as noticing
            caught = [("exception", type(exc).__name__, "", str(exc)[:80])]
        finally:
            for key, module in original.items():
                setattr(T, key, module)
        out.append((f"sabotage detected — {label}", bool(caught), f"{len(caught)} difference(s)" if caught else
                    "NOT DETECTED: the suite stayed green on a weakened firewall"))
    return out


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def report(rows) -> int:
    failed = [r for r in rows if not r[1]]
    for name, ok, detail in rows:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok or not detail else f"\n        -> {detail}"))
    print(f"\n{len(rows) - len(failed)}/{len(rows)} checks passed")
    return 1 if failed else 0


def cmd_run() -> int:
    load_tree("workers")
    rows = static_checks()
    results = asyncio.run(run_all())
    bad = problems_vs_expected(results)
    for sid, _fn in SCENARIOS:
        mine = [(p, w, g) for s, p, w, g in bad if s == sid]
        rows.append((f"{sid} {_fn.__name__}", not mine,
                     "\n        ".join(f"{p}: expected {w!r}, got {g!r}" for p, w, g in mine[:6])))
    rows += run_sabotage()
    code = report(rows)
    print("\nKNOWN GAP (pinned, identical on Render — see EXPECTED['S07']): a store at its monthly cap blocks only the\n"
          "first message of each 2h window; the next message from the same customer reaches OpenRouter.")
    return code


def cmd_record(tree: str) -> int:
    load_tree(tree)
    results = asyncio.run(run_all())
    OUT[tree].write_text(json.dumps({"tree": tree, "scenarios": results}, indent=2, ensure_ascii=False), encoding="utf-8")
    crashed = [sid for sid, r in results.items() if isinstance(r, dict) and r.get("crashed")]
    print(f"wrote {OUT[tree].name}  ({len(results)} scenarios, {len(crashed)} crashed {crashed})")
    return 1 if crashed else 0


def cmd_compare() -> int:
    sides = {t: json.loads(p.read_text("utf-8"))["scenarios"] for t, p in OUT.items() if p.exists()}
    if not sides:
        sys.exit("no stage5_*.json — run `record workers` and `record render` first")
    print("sides present:", ", ".join(sides))
    problems = 0
    for tree, results in sides.items():
        bad = problems_vs_expected(results)
        print(f"  {tree:8s} vs EXPECTED: {'matches' if not bad else 'DIFFERS'}")
        for sid, path, want, got in bad:
            print(f"      {sid}.{path}: expected {want!r}, got {got!r}")
        problems += bool(bad)
    if len(sides) == 2:
        differences = [(sid, p, a, b) for sid in sorted(sides["render"]) for p, a, b in diff(sides["render"][sid], sides["workers"].get(sid))]
        print(f"  render vs workers: {'IDENTICAL on every scenario' if not differences else 'DIFFERENT'}")
        for sid, path, a, b in differences:
            print(f"      {sid}.{path}: render={a!r} workers={b!r}")
        problems += bool(differences)
    print(f"\n{problems} problem(s)")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 5 firewall test (see the module docstring).")
    parser.add_argument("command", nargs="?", default="run", choices=("run", "record", "compare"))
    parser.add_argument("tree", nargs="?", choices=("workers", "render"))
    args = parser.parse_args()
    if args.command == "record":
        if not args.tree:
            parser.error("record needs a tree: record workers | record render")
        return cmd_record(args.tree)
    if args.tree:
        parser.error(f"'{args.command}' takes no tree argument")
    return cmd_compare() if args.command == "compare" else cmd_run()


if __name__ == "__main__":
    sys.exit(main())
