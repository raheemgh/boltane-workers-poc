"""
Stage 4 — offline tests for the Workers port of ai_client.py.
No network, no real credentials, runs in a couple of seconds.

Run it from the cf-worker folder:

    PYTHONPATH=tooling/local-test-support:src python test_stage4_offline.py

Same approach as test_usage_protection.py: httpx.AsyncClient is replaced by
a small fake that plays back scripted OpenRouter responses (streamed SSE
lines, HTTP errors, connection errors). The fake follows OpenRouter's
documented streaming shape: ": OPENROUTER PROCESSING" keep-alive comment
lines, "data: {...}" events, a last usage-only chunk (choices == []),
"data: [DONE]", and mid-stream errors delivered as an event inside an
HTTP 200.

What each part proves
  1. behaviour tests   — the ported call_ai does what the Render one did:
                         masking round-trip, request body (max_tokens,
                         stream_options), BYOK key, fallback chain, 402,
                         network retry, usage accounting, plus the three
                         documented-not-changed edge cases (see ai_client's
                         docstring).
  2. Workers-only      — no proxy= is ever passed to httpx; exactly one
                         warning when OPENROUTER_SOCKS5_PROXY is set.
  3. AST parity        — _stream_completion, _post_with_network_retry,
                         _resolve_models and _NetworkError are
                         AST-identical to the Render original.
  4. Render parity     — the SAME scripted scenarios are run against the
                         Render original and the Workers port; replies,
                         models, token counts and outgoing request bodies
                         must be equal. The one intended difference
                         (non-Syrian phone numbers) is asserted as such.
  5. sabotage          — the suite is re-run against deliberately broken
                         copies of the port; every break must be caught.

IMPORTANT — what this does NOT prove: the scenarios use hand-written
Arabic text, not real store prompts or real customer messages, and
nothing here talks to OpenRouter. That is the job of the real-call step.
Parts 3 and 4 need the Render original; they look for it at
../app/services/ (or RENDER_AI_CLIENT_PATH / RENDER_PII_PATH) and print
SKIPPED if it is not there.
"""

import ast
import asyncio
import contextlib
import json
import logging
import os
import sys
import types
from pathlib import Path

# Settings are read lazily through get_settings() -> `from workers import
# env` (the local stub, backed by os.environ). Dummy values only.
os.environ.setdefault("META_VERIFY_TOKEN", "test-token")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-supabase-key")
os.environ.setdefault("OPENROUTER_DEFAULT_KEY", "test-openrouter-key")
os.environ.pop("OPENROUTER_SOCKS5_PROXY", None)

import httpx  # noqa: E402

# ai_client logs warnings on purpose (429s, retries, 402s); keep them out of
# the test output. capture_logs() below still sees every record.
logging.getLogger("boltane.ai_client").addHandler(logging.NullHandler())

import app.config as cfg  # noqa: E402
from app.services import ai_client as real_ac  # noqa: E402
from app.services.token_limits import MAX_OUTPUT_TOKENS_PER_MESSAGE  # noqa: E402

HERE = Path(__file__).resolve().parent
RENDER_AI_CLIENT = Path(os.environ.get("RENDER_AI_CLIENT_PATH", HERE.parent / "app" / "services" / "ai_client.py"))
RENDER_PII = Path(os.environ.get("RENDER_PII_PATH", HERE.parent / "app" / "services" / "pii_masking.py"))
WORKERS_AI_CLIENT = Path(real_ac.__file__)

UNTOUCHED_FUNCTIONS = ["_NetworkError", "_stream_completion", "_post_with_network_retry", "_resolve_models"]
DEFAULT_KEY = os.environ["OPENROUTER_DEFAULT_KEY"]


# ---------------------------------------------------------------------------
# Scripted OpenRouter
# ---------------------------------------------------------------------------

def sse(pieces, *, usage=None, finish="stop", reasoning=None, error=None, junk=False):
    """One streamed 200 response, as a list of SSE lines."""
    lines = [": OPENROUTER PROCESSING", ""]
    for r in reasoning or []:
        lines += ["data: " + json.dumps({"choices": [{"delta": {"reasoning": r}}]}), ""]
    for i, p in enumerate(pieces):
        lines += ["data: " + json.dumps({"choices": [{"delta": {"content": p}}]}, ensure_ascii=False), ""]
        if junk and i == 0:
            lines += ["data: {this is not json", ""]  # chunk-boundary artifact
    if error:
        lines += ["data: " + json.dumps({"error": error, "choices": [{"delta": {"content": ""}, "finish_reason": "error"}]}), ""]
    else:
        lines += ["data: " + json.dumps({"choices": [{"delta": {"content": ""}, "finish_reason": finish}]}), ""]
    if usage:
        lines += ["data: " + json.dumps({"choices": [], "usage": usage}), ""]
    lines += ["data: [DONE]", ""]
    return {"kind": "sse", "lines": lines}


def http_status(code, body="{}"):
    return {"kind": "status", "code": code, "body": body}


def net_error():
    return {"kind": "raise", "exc": httpx.ConnectError("connection reset")}


def chunks(text, n=7):
    return [text[i:i + n] for i in range(0, len(text), n)]


def usage(total=100):
    return {"prompt_tokens": total - 20, "completion_tokens": 20, "total_tokens": total}


class Script:
    """Steps are consumed one per OpenRouter request. A step may also be a
    callable(request_body) -> step, for replies that depend on the request."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []
        self.client_kwargs = []
        self.sleeps = []


class _Resp:
    def __init__(self, step):
        self.status_code = 200 if step["kind"] == "sse" else step["code"]
        self.text = step.get("body", "")
        self._lines = step.get("lines", [])

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _Ctx:
    def __init__(self, step):
        self.step = step

    async def __aenter__(self):
        if self.step["kind"] == "raise":
            raise self.step["exc"]
        return _Resp(self.step)

    async def __aexit__(self, *_a):
        return False


def _client_class(script):
    class _Client:
        def __init__(self, *_a, **kwargs):
            script.client_kwargs.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        def stream(self, method, url, headers=None, json=None):
            script.requests.append({"url": url, "headers": headers, "body": json})
            assert script.steps, "unexpected extra OpenRouter request"
            step = script.steps.pop(0)
            if callable(step):
                step = step(json)
            return _Ctx(step)

    return _Client


@contextlib.contextmanager
def patched_network(script):
    original_client, original_sleep = httpx.AsyncClient, asyncio.sleep

    async def fake_sleep(delay):
        script.sleeps.append(delay)

    httpx.AsyncClient = _client_class(script)
    asyncio.sleep = fake_sleep
    try:
        yield script
    finally:
        httpx.AsyncClient = original_client
        asyncio.sleep = original_sleep


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextlib.contextmanager
def three_model_chain(ac):
    """low-tier is a ONE-model chain now, which cannot show fallback behaviour; give it three
    models for the tests that need to see the chain being walked."""
    original = ac.PACKAGE_DEFAULT_MODELS["low-tier"]
    ac.PACKAGE_DEFAULT_MODELS["low-tier"] = ["test/one:free", "test/two:free", "test/three:free"]
    try:
        yield ac.PACKAGE_DEFAULT_MODELS["low-tier"]
    finally:
        ac.PACKAGE_DEFAULT_MODELS["low-tier"] = original


@contextlib.contextmanager
def capture_logs():
    handler, logger = _Capture(), logging.getLogger("boltane.ai_client")
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


async def call(ac, script, message="مرحبا", *, package="basic", store_ai_model=None,
               store_api_key=None, system="أنت مساعد متجر لطيف."):
    with patched_network(script):
        return await ac.call_ai(system, message, package=package, store_ai_model=store_ai_model,
                                store_api_key=store_api_key, store_id="store-test")


def reset_settings(ac, proxy=None):
    if proxy is None:
        os.environ.pop("OPENROUTER_SOCKS5_PROXY", None)
    else:
        os.environ["OPENROUTER_SOCKS5_PROXY"] = proxy
    cfg._settings = None
    if hasattr(ac, "_socks5_warning_logged"):
        ac._socks5_warning_logged = False


# ---------------------------------------------------------------------------
# Scenario text (hand-written, NOT real customer data)
# ---------------------------------------------------------------------------

MSG_SYRIAN = "مرحبا، رقمي 0999 123 456 هل تفتحون يوم الجمعة؟"
MSG_MIXED = "رقمي 0999 123 456 ورقم شريكي ٠٩٨٨١٢٣٤٥٦ وأحياناً +971 50 123 4567 — متى تفتحون؟"
LONG_REPLY = " ".join(f"الجملة رقم {i}: نفتح من التاسعة صباحاً حتى العاشرة مساءً طوال الأسبوع." for i in range(1, 26))


def echo_reply(prefix="تمام، سأتصل بك على: "):
    """A step that answers with the (masked) user message it received."""
    def _step(body):
        user_text = body["messages"][-1]["content"]
        return sse(chunks(prefix + user_text, 5), usage=usage(123))
    return _step


# ---------------------------------------------------------------------------
# Tests (each takes the module under test and its source text)
# ---------------------------------------------------------------------------

async def test_static_workers_rules(ac, src):
    tree = ast.parse(src)
    proxy_kw = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                for k in n.keywords if k.arg == "proxy"]
    assert not proxy_kw, f"proxy= must never be passed to httpx (line {proxy_kw})"
    assert "from app.config import settings" not in src, "must use get_settings(), not the Render singleton"
    assert "get_settings" in src


async def test_model_ids_are_real_looking(ac, src):
    assert ac.DEEPSEEK_MODEL == "deepseek/deepseek-v4-flash"
    assert ac.FALLBACK_MODELS == [ac.DEEPSEEK_MODEL], "Stage 4 decision: low-tier uses the same model as basic/pro"
    assert not any(m.startswith("provider-") for m in ac.FALLBACK_MODELS), "placeholder model id left in the chain"
    assert ac.PACKAGE_DEFAULT_MODELS["low-tier"] == ac.FALLBACK_MODELS
    assert ac.PACKAGE_DEFAULT_MODELS["basic"] == ac.PACKAGE_DEFAULT_MODELS["pro"] == [ac.DEEPSEEK_MODEL]


async def test_ast_parity_with_render(ac, src):
    if not RENDER_AI_CLIENT.exists():
        print("SKIPPED: Render original not found at", RENDER_AI_CLIENT)
        return

    def funcs(text):
        return {n.name: ast.dump(n) for n in ast.parse(text).body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}

    render, work = funcs(RENDER_AI_CLIENT.read_text("utf-8")), funcs(src)
    for name in UNTOUCHED_FUNCTIONS:
        assert render[name] == work[name], f"{name} differs from the Render original"
    assert render["_resolve_api_key"] != work["_resolve_api_key"]  # the intended get_settings() change


async def test_happy_path_masking_roundtrip(ac, src):
    system = "أنت مساعد متجر. للتواصل مع الإدارة: 0999 111 222"
    script = Script([echo_reply()])
    reply, model, tokens = await call(ac, script, MSG_MIXED, system=system)

    body = script.requests[0]["body"]
    sent_user = body["messages"][1]["content"]
    for raw in ("123 456", "٠٩٨٨١٢٣٤٥٦", "50 123 4567"):
        assert raw not in sent_user, f"phone number leaked to OpenRouter: {raw}"
    assert "[PHONE_1]" in sent_user and "[PHONE_3]" in sent_user
    assert body["messages"][0] == {"role": "system", "content": system}, "system_prompt must never be masked"
    assert body["messages"][1]["role"] == "user"
    assert reply == "تمام، سأتصل بك على: " + MSG_MIXED, "placeholders must be restored in the reply"
    assert model == ac.DEEPSEEK_MODEL and tokens == 123


async def test_request_shape_and_keys(ac, src):
    script = Script([sse(["ok"], usage=usage())])
    await call(ac, script)
    req = script.requests[0]
    assert req["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert req["headers"]["Authorization"] == f"Bearer {DEFAULT_KEY}"
    body = req["body"]
    assert body["max_tokens"] == MAX_OUTPUT_TOKENS_PER_MESSAGE
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["model"] == ac.DEEPSEEK_MODEL

    script = Script([sse(["ok"], usage=usage())])
    await call(ac, script, store_api_key="sk-store-own")
    assert script.requests[0]["headers"]["Authorization"] == "Bearer sk-store-own"


async def test_long_reply_not_truncated(ac, src):
    assert len(LONG_REPLY) > 1500
    script = Script([sse(chunks(LONG_REPLY, 7), usage=usage(380), junk=True)])
    reply, model, tokens = await call(ac, script)
    assert reply == LONG_REPLY, "a long streamed reply must arrive intact"
    assert tokens == 380


async def test_documented_max_tokens_cutoff(ac, src):
    partial = "نفتح من التاسعة حتى"
    script = Script([sse([partial], finish="length", usage=usage(340))])
    reply, model, tokens = await call(ac, script)
    assert reply == partial and model == ac.DEEPSEEK_MODEL and tokens == 340  # returned as-is (documented)


async def test_documented_midstream_error(ac, src):
    err = {"code": 502, "message": "provider disconnected"}
    script = Script([sse(["الجواب الجزئي"], error=err, usage=usage(60))])
    reply, model, tokens = await call(ac, script)
    assert reply == "الجواب الجزئي" and model == ac.DEEPSEEK_MODEL  # treated as success (documented)

    script = Script([sse([], error=err), sse(["تمام"], usage=usage(70))])
    reply, model, tokens = await call(ac, script, store_ai_model="store/own-model")
    assert reply == "تمام" and model == ac.DEEPSEEK_MODEL, "an error with no text counts as an empty stream"


async def test_documented_reasoning_only_stream(ac, src):
    script = Script([sse([], reasoning=["hmm", "let me think"], usage=usage(310)), sse(["مرحبا"], usage=usage(90))])
    reply, model, tokens = await call(ac, script, store_ai_model="store/reasoner")
    assert [r["body"]["model"] for r in script.requests] == ["store/reasoner", ac.DEEPSEEK_MODEL]
    assert reply == "مرحبا" and tokens == 90


async def test_fallback_chain(ac, src):
    with three_model_chain(ac) as chain:
        script = Script([http_status(429), http_status(503), sse(["ok"], usage=usage())])
        reply, model, tokens = await call(ac, script, package="low-tier")
        assert [r["body"]["model"] for r in script.requests] == chain
        assert model == chain[2]

        script = Script([http_status(429), http_status(502), http_status(500)])
        reply, model, tokens = await call(ac, script, package="low-tier")
        assert (reply, model, tokens) == (ac.GRACEFUL_FAILURE_REPLY, None, 0) and len(script.requests) == 3

    script = Script([http_status(429), sse(["ok"], usage=usage())])
    await call(ac, script, package="basic", store_ai_model="some/own-model")
    assert [r["body"]["model"] for r in script.requests] == ["some/own-model", ac.DEEPSEEK_MODEL]


async def test_402_stops_immediately(ac, src):
    for key, word in (("sk-store-own", "BYOK"), (None, "PLATFORM")):
        script = Script([http_status(402, "insufficient credit"), sse(["x"], usage=usage())])
        with capture_logs() as records, three_model_chain(ac):
            reply, model, tokens = await call(ac, script, package="low-tier", store_api_key=key)
        assert (reply, model, tokens) == (ac.GRACEFUL_FAILURE_REPLY, None, 0)
        assert len(script.requests) == 1, "a 402 must never cycle through the fallback list"
        assert any("OUT OF CREDIT (402)" in r.getMessage() and word in r.getMessage() for r in records)


async def test_network_retry_same_model(ac, src):
    script = Script([net_error(), net_error(), sse(["ok"], usage=usage())])
    reply, model, tokens = await call(ac, script)
    assert [r["body"]["model"] for r in script.requests] == [ac.DEEPSEEK_MODEL] * 3
    assert script.sleeps == [2.0, 4.0] and reply == "ok"

    with three_model_chain(ac) as chain:
        script = Script([net_error(), net_error(), net_error(), sse(["ok"], usage=usage())])
        reply, model, tokens = await call(ac, script, package="low-tier")
        assert [r["body"]["model"] for r in script.requests] == [chain[0]] * 3 + [chain[1]]
        assert script.sleeps == [2.0, 4.0] and model == chain[1]


async def test_usage_accounting(ac, src):
    cases = [(None, 0), ({"prompt_tokens": 40, "completion_tokens": 20}, 60), (usage(75), 75)]
    for u, expected in cases:
        script = Script([sse(["ok"], usage=u)])
        _, _, tokens = await call(ac, script)
        assert tokens == expected, f"usage {u} -> {tokens}, expected {expected}"


async def test_socks5_setting_ignored_with_one_warning(ac, src):
    try:
        reset_settings(ac, proxy="socks5://127.0.0.1:10808")
        script = Script([sse(["a"], usage=usage()), sse(["b"], usage=usage())])
        with capture_logs() as records:
            r1 = await call(ac, script)
            r2 = await call(ac, script)
        assert r1[0] == "a" and r2[0] == "b", "calls must still work with the setting present"
        assert script.client_kwargs and all("proxy" not in k for k in script.client_kwargs)
        warnings = [r for r in records if r.levelno == logging.WARNING and "SOCKS5" in r.getMessage()]
        assert len(warnings) == 1, f"expected exactly one warning, got {len(warnings)}"

        reset_settings(ac, proxy=None)
        script = Script([sse(["a"], usage=usage())])
        with capture_logs() as records:
            await call(ac, script)
        assert not [r for r in records if "SOCKS5" in r.getMessage()], "no warning when the setting is empty"
    finally:
        reset_settings(ac, proxy=None)


BEHAVIOUR_TESTS = [
    test_static_workers_rules, test_model_ids_are_real_looking, test_ast_parity_with_render,
    test_happy_path_masking_roundtrip, test_request_shape_and_keys, test_long_reply_not_truncated,
    test_documented_max_tokens_cutoff, test_documented_midstream_error, test_documented_reasoning_only_stream,
    test_fallback_chain, test_402_stops_immediately, test_network_retry_same_model, test_usage_accounting,
    test_socks5_setting_ignored_with_one_warning,
]


def run_suite(ac, src, *, quiet=False):
    for t in BEHAVIOUR_TESTS:
        asyncio.run(t(ac, src))
        if not quiet:
            print("PASS:", t.__name__)


# ---------------------------------------------------------------------------
# Render parity
# ---------------------------------------------------------------------------

def load_render_module():
    if not (RENDER_AI_CLIENT.exists() and RENDER_PII.exists()):
        return None
    pii = types.ModuleType("render_pii_v1")
    sys.modules["render_pii_v1"] = pii
    exec(compile(RENDER_PII.read_text("utf-8"), str(RENDER_PII), "exec"), pii.__dict__)

    src = RENDER_AI_CLIENT.read_text("utf-8")
    old_settings = "from app.config import settings\n"
    old_pii = "from app.services.pii_masking import mask_phone_numbers\n"
    assert old_settings in src and old_pii in src, "unexpected Render ai_client header"
    src = src.replace(old_settings, (
        "import os as _os, types as _t\n"
        "settings = _t.SimpleNamespace(OPENROUTER_DEFAULT_KEY=_os.environ['OPENROUTER_DEFAULT_KEY'],"
        " OPENROUTER_SOCKS5_PROXY=None)\n"))
    src = src.replace(old_pii, "from render_pii_v1 import mask_phone_numbers\n")
    mod = types.ModuleType("render_ai_client")
    exec(compile(src, str(RENDER_AI_CLIENT), "exec"), mod.__dict__)
    return mod


PARITY_SCENARIOS = [
    ("happy path + Syrian number masking",
     lambda: [echo_reply()], dict(message=MSG_SYRIAN)),
    ("long reply",
     lambda: [sse(chunks(LONG_REPLY, 7), usage=usage(380), junk=True)], {}),
    ("429 then success, store model first",
     lambda: [http_status(429), sse(["ok"], usage=usage())], dict(store_ai_model="store/own-model")),
    ("max_tokens cut-off",
     lambda: [sse(["نفتح من التاسعة حتى"], finish="length", usage=usage(340))], {}),
    ("mid-stream error with partial text",
     lambda: [sse(["جزئي"], error={"code": 502}, usage=usage(60))], {}),
    ("mid-stream error with no text",
     lambda: [sse([], error={"code": 502}), sse(["تمام"], usage=usage(70))], dict(store_ai_model="store/own-model")),
    ("reasoning-only stream",
     lambda: [sse([], reasoning=["hmm"], usage=usage(310)), sse(["مرحبا"], usage=usage(90))],
     dict(store_ai_model="store/reasoner")),
    ("402 on a BYOK key",
     lambda: [http_status(402), sse(["x"], usage=usage())], dict(store_api_key="sk-store-own")),
    ("network errors then success",
     lambda: [net_error(), net_error(), sse(["ok"], usage=usage())], {}),
    ("every model fails",
     lambda: [http_status(429), http_status(500)], dict(store_ai_model="store/own-model")),
    ("no usage field",
     lambda: [sse(["ok"])], {}),
]


async def _run_scenario(mod, steps, kwargs):
    script = Script(steps)
    kwargs = dict(kwargs)
    message = kwargs.pop("message", "مرحبا")
    result = await call(mod, script, message, **kwargs)
    return result, [(r["body"], r["headers"]) for r in script.requests], script.sleeps


def run_render_parity():
    render = load_render_module()
    if render is None:
        print("SKIPPED: Render parity run (Render original not found at", RENDER_AI_CLIENT, ")")
        return
    for name, make_steps, kwargs in PARITY_SCENARIOS:
        a = asyncio.run(_run_scenario(render, make_steps(), kwargs))
        b = asyncio.run(_run_scenario(real_ac, make_steps(), kwargs))
        assert a == b, f"Render and Workers differ on scenario '{name}':\nRender : {a}\nWorkers: {b}"
        print(f"PASS: parity with Render — {name}")

    # The one intended difference: pii_masking v2 masks non-Syrian numbers, v1 does not.
    a = asyncio.run(_run_scenario(render, [echo_reply()], dict(message=MSG_MIXED)))
    b = asyncio.run(_run_scenario(real_ac, [echo_reply()], dict(message=MSG_MIXED)))
    sent_render, sent_workers = a[1][0][0]["messages"][1]["content"], b[1][0][0]["messages"][1]["content"]
    assert "50 123 4567" in sent_render, (
        f"expected the Render pii_masking at {RENDER_PII} to still be v1 (sends non-Syrian numbers raw)")
    assert "50 123 4567" not in sent_workers
    assert b[0][0] == "تمام، سأتصل بك على: " + MSG_MIXED
    print("PASS: intended difference confirmed — Workers masks non-Syrian numbers that Render sends raw")


# ---------------------------------------------------------------------------
# Sabotage: the suite must FAIL on deliberately broken copies
# ---------------------------------------------------------------------------

MUTATIONS = [
    ("passes proxy= to httpx",
     "httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)",
     'httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, proxy="socks5://x")'),
    ("drops max_tokens",
     '"max_tokens": MAX_OUTPUT_TOKENS_PER_MESSAGE,', ""),
    ("stops masking phone numbers",
     "masked = mask_phone_numbers(customer_message)",
     'masked = type("M", (), {"masked_text": customer_message, "restore": lambda self, t: t})()'),
    ("never tries a second model",
     "for attempt, model in enumerate(models, start=1):", "for attempt, model in enumerate(models[:1], start=1):"),
    ("402 cycles through the fallback list",
     "if status_code == INSUFFICIENT_CREDIT_STATUS_CODE:", "if False:"),
    ("corrupts streamed text",
     "full_reply += delta", "full_reply += delta.strip()"),
    ("ignores reported token usage",
     'tokens_used = usage.get("total_tokens")', "tokens_used = 0"),
    ("no network retry",
     "NETWORK_RETRY_DELAYS_SECONDS = [2.0, 4.0]", "NETWORK_RETRY_DELAYS_SECONDS = []"),
    ("warns on every call",
     "    if _socks5_warning_logged:\n        return\n", ""),
]


def run_sabotage():
    src = WORKERS_AI_CLIENT.read_text("utf-8")
    for label, old, new in MUTATIONS:
        assert src.count(old) == 1, f"mutation '{label}': expected exactly one match, found {src.count(old)}"
        mutant_src = src.replace(old, new)
        mutant = types.ModuleType("ai_client_mutant")
        exec(compile(mutant_src, "<mutant>", "exec"), mutant.__dict__)
        try:
            run_suite(mutant, mutant_src, quiet=True)
        except Exception as exc:  # noqa: BLE001 — any failure means the suite noticed
            print(f"PASS: sabotage detected — {label}  ({type(exc).__name__})")
        else:
            raise AssertionError(f"sabotage NOT detected: {label}")


def main():
    source = WORKERS_AI_CLIENT.read_text("utf-8")
    run_suite(real_ac, source)
    run_render_parity()
    run_sabotage()
    print("\nALL PASS")


if __name__ == "__main__":
    main()
