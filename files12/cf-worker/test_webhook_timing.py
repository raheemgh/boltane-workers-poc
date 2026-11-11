#!/usr/bin/env python3
"""
Stage 7 — test_webhook_timing.py, redesigned for Cloudflare Workers (standard library only).

WHAT THE ORIGINAL DID, AND WHY IT CANNOT BE PORTED AS IT WAS
  The Render version started `uvicorn app.main:app` as a subprocess, POSTed one signed payload and asserted the
  answer came back in under 1 s "although the background task simulates 5 seconds of work". Two problems:
    1. There is no uvicorn on Workers (and app/main.py imports `workers`, which only exists inside workerd).
    2. The "5 seconds of work" no longer exists: the payload had no phone_number_id, so the real pipeline drops it
       at the parsing step. Ported literally, the test would pass without proving anything.
  It also never looked at what a REJECTED request looks like.

HOW THIS VERSION WORKS (a differential measurement, not a fixed threshold)
  It is a black-box client for a running Worker: either `pywrangler dev` (local) or the deployed Worker.

      python test_webhook_timing.py --url http://127.0.0.1:8787                              # local, ceiling 1.0 s
      python test_webhook_timing.py --url https://boltane-core-engine.boltane.workers.dev    # deployed, ceiling 3.0 s

  Phase A  rejected requests. Three wrongly signed requests for the test store (no signature header / signature
           made with a wrong secret / valid signature but the body changed afterwards). Each must answer
           403 with the exact text "invalid signature" (text/plain) quickly, AND the pipeline must not have run:
           the store's conversation window in Supabase must be byte-for-byte unchanged afterwards.
  Phase B  differential timing. Pairs of correctly signed deliveries, same endpoint, same body size:
             CONTROL  phone_number_id of a store that does not exist -> the pipeline has (almost) nothing to do
             TEST     the test store -> the real pipeline runs (Supabase, OpenRouter, Meta send)
           If the response were held until the pipeline ends (what FastAPI BackgroundTasks did on a deployed
           Worker: 5.7 / 40.9 / 70.0 s measured for tasks of 5 / 40 / 70 s), TEST would be slower than CONTROL by
           about the pipeline's duration. With ctx.waitUntil it is not. Network latency, TLS and cold starts hit
           both alike and cancel out in the difference.
             tolerance  = spread (max - min) of the CONTROL times, never below 0.3 s   <- measured, not chosen
             difference = fastest TEST response (among completed deliveries) - fastest CONTROL response
                          must be <= tolerance. The minimum is used on purpose: network jitter and cold starts only
                          ever ADD delay, so the minimum is the least noisy estimate (a simulation of noisy links gave
                          a false-failure rate under 0.1% with 5 pairs, against up to 1.7% for the median), while a
                          held response delays EVERY TEST delivery, so its minimum is delayed too.
             power      = fastest observed time from sending to completion - fastest CONTROL  must be >= 3 x tolerance,
                          otherwise the pipeline was too short to tell "held" from "not held": verdict INCONCLUSIVE
                          (run again, or with --long, the slowest model path).
           Completion is observed the way test_stage6_deployed.py does: conversation_windows.last_message_at moves
           (pipeline started) and token_count changes (call_ai finished and add_conversation_tokens ran).
  Also kept from the original: every TEST response is HTTP 200 {"status": "received"} and under the absolute ceiling.

SAFETY
  * Only the test store (phone_number_id must start with "stage") and only if its access_token is the deliberately
    fake one (starts with "stage6-fake"): otherwise real WhatsApp messages could be sent. Override: --allow-real-send.
  * Supabase is only READ here (REST GET). The Worker itself writes the usual usage rows for the test store.
  * Never talks to the Render-hosted service. Costs: --runs OpenRouter calls (one DeepSeek call each).
  * Settings come from cf-worker/.dev.vars and/or the environment (environment wins):
    META_APP_SECRET, SUPABASE_URL, SUPABASE_KEY, META_TEST_PHONE_NUMBER_ID, META_TEST_RECIPIENT.

Exit code: 0 PASS, 1 FAIL, 2 could not run (settings / store / Worker unreachable), 3 INCONCLUSIVE.
Results are also written to stage7_timing_results.json (no secrets; do not commit).
"""

import argparse
import hashlib
import hmac
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NamedTuple

HERE = Path(__file__).resolve().parent
DEV_VARS = HERE / ".dev.vars"
USER_AGENT = "boltane-stage7-timing/1.0"
PNID_PREFIX = "stage"
FAKE_TOKEN_PREFIX = "stage6-fake"
UNKNOWN_PNID = "stage7-no-such-store"
REJECT_BODY = "invalid signature"
NOISE_FLOOR_SECONDS = 0.3
POWER_FACTOR = 3.0
POLL_INTERVAL = 0.4

SHORT_MESSAGES = [
    "السلام عليكم، شو عندكم اليوم؟",
    "عندكم برغر دجاج؟ وكم نوع ساندويش مشوي عندكم؟",
    "بدي اطلب 2 برغر لحم و1 شيش طاووق، رقمي 0999 123 456 للتواصل",
    "متى بتفتحوا يوم الجمعة؟",
]
_LONG_SENTENCES = [
    "السلام عليكم، بدي اطلب لعيلتي اليوم وبدي أتأكد من كل شي قبل ما أثبّت الطلب.",
    "عندي ثلاث أولاد، واحد منهم ما بياكل البصل أبداً والتاني بيحب الساندويتش المشوي.",
    "الصغير بدو شي خفيف وما بيحب الصوص الحار، فبدي أعرف شو أنسب إلو من القائمة.",
    "وكمان بدي أعرف إذا الشيش طاووق بيجي مع بطاطا ولا لازم أطلبها لحال.",
    "وبالنسبة للتوصيل، أنا ساكن بمنطقة بعيدة شوي عن المطعم وبدي أعرف إذا بتوصلوا لعندي.",
    "ضيوفنا بيجوا الساعة الثامنة، فبدي الطلب يكون جاهز قبلها بشوي إذا ممكن.",
    "وإذا في طبق جانبي بيناسب الأطفال وبيكفي خمس أشخاص خبروني عنو لأني ما بدي أطلب كمية زيادة.",
    "كمان سؤال أخير، هل ممكن أغيّر الطلب بعد ما أثبّته إذا تغيرت الأمور عندي؟",
]


def long_message() -> str:
    """Just under the 1200-character input cap (same construction as the Stage 4 / Stage 6 tests)."""
    text = ""
    for sentence in _LONG_SENTENCES * 2:
        if len(text) + len(sentence) + 1 > 1190:
            break
        text = (text + " " + sentence).strip()
    return text


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class Resp(NamedTuple):
    status: int
    text: str
    seconds: float
    content_type: str


def http(method: str, url: str, headers: dict | None = None, body: bytes | None = None, timeout: float = 30.0) -> Resp:
    request = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
            return Resp(response.status, text, time.perf_counter() - started, response.headers.get("content-type", ""))
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        ctype = exc.headers.get("content-type", "") if exc.headers else ""
        return Resp(exc.code, text, time.perf_counter() - started, ctype)
    except Exception as exc:  # noqa: BLE001 — a connection failure is a result, not a crash
        return Resp(0, f"{type(exc).__name__}: {str(exc)[:150]}", time.perf_counter() - started, "")


def load_settings() -> dict:
    values: dict = {}
    if DEV_VARS.exists():
        for raw in DEV_VARS.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("META_APP_SECRET", "SUPABASE_URL", "SUPABASE_KEY", "META_TEST_PHONE_NUMBER_ID", "META_TEST_RECIPIENT"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def sign(raw: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


_wamid_counter = [0]


def meta_payload(pnid: str, customer: str, text: str) -> dict:
    """Same shape as a real Meta delivery; only the fields _process_message reads matter."""
    _wamid_counter[0] += 1
    return {"object": "whatsapp_business_account", "entry": [{"id": "0", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "0", "phone_number_id": pnid},
        "contacts": [{"profile": {"name": "Stage7 timing"}, "wa_id": customer}],
        "messages": [{"from": customer, "id": f"wamid.STAGE7T{int(time.time())}{_wamid_counter[0]}",
                      "timestamp": str(int(time.time())), "type": "text", "text": {"body": text}}]}}]}]}


def rest_get(settings: dict, table: str, params: dict):
    url = f"{settings['SUPABASE_URL'].rstrip('/')}/rest/v1/{table}?{urllib.parse.urlencode(params)}"
    key = settings["SUPABASE_KEY"]
    resp = http("GET", url, {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}, timeout=20.0)
    if resp.status != 200:
        raise RuntimeError(f"Supabase REST {table} -> HTTP {resp.status}: {resp.text[:140]}")
    return json.loads(resp.text)


def window_state(settings: dict, store_id: str, customer: str):
    rows = rest_get(settings, "conversation_windows", {
        "store_id": f"eq.{store_id}", "customer_phone": f"eq.{customer}",
        "select": "last_message_at,token_count", "limit": "1"})
    return rows[0] if rows else None


def evaluate(base_row, current_row) -> tuple[bool, bool]:
    """(touched, finished): last_message_at moved = pipeline started; token_count changed and > 0 = it finished."""
    if current_row is None:
        return False, False
    touched = base_row is None or current_row["last_message_at"] != base_row["last_message_at"]
    tokens = current_row.get("token_count") or 0
    finished = touched and tokens > 0 and (base_row is None or tokens != (base_row.get("token_count") or 0))
    return touched, finished


def wait_until_quiet(settings, store_id, customer, max_wait=30.0):
    """Waits until the window row stops changing, so an earlier run's pipeline cannot look like a leak."""
    deadline = time.perf_counter() + max_wait
    previous = window_state(settings, store_id, customer)
    while time.perf_counter() < deadline:
        time.sleep(2.0)
        current = window_state(settings, store_id, customer)
        if current == previous:
            return True
        previous = current
    return False


# ---------------------------------------------------------------------------
# checks bookkeeping
# ---------------------------------------------------------------------------

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok or not detail else f"\n        -> {detail}"))
    return bool(ok)


# ---------------------------------------------------------------------------
# phases
# ---------------------------------------------------------------------------

def phase_rejected(base, settings, store_id, customer, ceiling, settle) -> dict:
    print("\nPhase A — rejected requests (shape and state)")
    pnid = settings["META_TEST_PHONE_NUMBER_ID"]
    secret = settings["META_APP_SECRET"]
    quiet = wait_until_quiet(settings, store_id, customer)
    if not quiet:
        print("      note: the window row was still changing after 30 s (another delivery in flight?) — "
              "the 'state unchanged' check below may be unreliable")
    before = window_state(settings, store_id, customer)

    good_raw = json.dumps(meta_payload(pnid, customer, "stage7 rejection probe"), ensure_ascii=False).encode("utf-8")
    tampered = good_raw.replace(b"probe", b"prube")
    json_header = {"Content-Type": "application/json"}
    variants = [
        ("no signature header", good_raw, {}),
        ("signature made with a wrong secret", good_raw, {"X-Hub-Signature-256": sign(good_raw, "stage7-not-the-secret")}),
        ("valid signature, body changed afterwards", tampered, {"X-Hub-Signature-256": sign(good_raw, secret)}),
    ]
    assert tampered != good_raw
    observed = []
    for label, raw, extra in variants:
        resp = http("POST", base + "/webhook", {**json_header, **extra}, raw)
        observed.append({"variant": label, "status": resp.status, "seconds": round(resp.seconds, 3),
                         "content_type": resp.content_type, "body": resp.text[:60]})
        check(f"{label} -> 403, body exactly {REJECT_BODY!r}, text/plain, within {ceiling:.1f}s",
              resp.status == 403 and resp.text == REJECT_BODY and resp.content_type.startswith("text/plain")
              and resp.seconds <= ceiling,
              f"HTTP {resp.status} body={resp.text[:60]!r} content-type={resp.content_type!r} {resp.seconds:.2f}s")
    time.sleep(settle)
    after = window_state(settings, store_id, customer)
    check("the three rejected requests left the conversation window untouched (nothing was scheduled)",
          after == before, f"before={before} after={after}")
    return {"observed": observed}


class Sample(NamedTuple):
    n: int
    control_seconds: float
    test_seconds: float
    status: int
    body_ok: bool
    verdict: str
    started_after: float | None      # seconds after the response ended
    finished_after: float | None
    total_to_finish: float | None    # seconds from sending the TEST request to observed completion


def post_valid(base: str, secret: str, pnid: str, customer: str, text: str) -> Resp:
    raw = json.dumps(meta_payload(pnid, customer, text), ensure_ascii=False).encode("utf-8")
    return http("POST", base + "/webhook", {"Content-Type": "application/json", "X-Hub-Signature-256": sign(raw, secret)}, raw)


def phase_timing(base, settings, store_id, customer, args) -> list:
    print(f"\nPhase B — differential timing ({args.runs} CONTROL/TEST pairs)")
    pnid, secret = settings["META_TEST_PHONE_NUMBER_ID"], settings["META_APP_SECRET"]
    post_valid(base, secret, UNKNOWN_PNID, customer, "stage7 warm-up (discarded)")   # wakes the webhook path
    time.sleep(1.0)
    samples = []
    for n in range(1, args.runs + 1):
        text = (long_message() if args.long else SHORT_MESSAGES[(n - 1) % len(SHORT_MESSAGES)]) + f" [S7-{n}]"
        control = post_valid(base, secret, UNKNOWN_PNID, customer, text)
        time.sleep(0.7)
        base_row = window_state(settings, store_id, customer)
        t_send = time.perf_counter()
        test = post_valid(base, secret, pnid, customer, text)
        t_response_end = time.perf_counter()
        started_at = finished_at = None
        while time.perf_counter() - t_response_end < args.poll_timeout and test.status == 200:
            try:
                current = window_state(settings, store_id, customer)
            except RuntimeError as exc:
                print(f"      (Supabase read failed, retrying: {exc})")
                time.sleep(1.0)
                continue
            touched, finished = evaluate(base_row, current)
            now = time.perf_counter()
            if touched and started_at is None:
                started_at = now
            if finished:
                finished_at = now
                break
            time.sleep(POLL_INTERVAL)
        verdict = "COMPLETE" if finished_at else "STARTED_NOT_FINISHED" if started_at else "NOT_STARTED"
        sample = Sample(
            n, control.seconds, test.seconds, test.status, test.status == 200 and '"received"' in test.text, verdict,
            None if started_at is None else started_at - t_response_end,
            None if finished_at is None else finished_at - t_response_end,
            None if finished_at is None else finished_at - t_send)
        samples.append(sample)
        fmt = lambda v: "never" if v is None else f"+{v:.1f}s"
        print(f"  #{n}: control {control.seconds:5.2f}s | test HTTP {test.status} {test.seconds:5.2f}s | "
              f"started {fmt(sample.started_after)} finished {fmt(sample.finished_after)} -> {verdict}")
        time.sleep(args.gap)
    return samples


def evaluate_timing(samples: list, ceiling: float, runs: int, power_factor: float) -> str:
    print("\nResults")
    controls = [s.control_seconds for s in samples]
    tests = [s.test_seconds for s in samples]
    complete = [s for s in samples if s.verdict == "COMPLETE"]

    check(f"every TEST delivery answered HTTP 200 {{'status': 'received'}}", all(s.body_ok for s in samples),
          str([(s.n, s.status) for s in samples if not s.body_ok]))
    check(f"every TEST response came back within the {ceiling:.1f}s ceiling (the original test's assertion)",
          all(t <= ceiling for t in tests), f"slowest={max(tests):.2f}s")
    check("the pipeline STARTED after the response for every TEST delivery (window row touched)",
          all(s.verdict != "NOT_STARTED" for s in samples), str([s.n for s in samples if s.verdict == "NOT_STARTED"]))
    need = max(1, runs - 1)
    unfinished = [(s.n, s.verdict) for s in samples if s.verdict != "COMPLETE"]
    if unfinished:
        print(f"  note: deliveries that did not complete: {unfinished}. A delivery that starts but never completes is "
              "the graceful-failure reply (documented: tokens=0), a store at its monthly cap, or a pipeline cancelled "
              "after ~30 s — the Worker's own log (warnings) tells which; this test cannot.")
    check(f"at least {need} of {runs} TEST deliveries COMPLETED (token_count updated = call_ai + add_tokens ran)",
          len(complete) >= need, f"only {len(complete)} completed; not completed: {unfinished}")

    verdict = "PASS"
    if not complete:
        print("  (no completed delivery: the differential check cannot be evaluated)")
        return "FAIL"

    tolerance = max(NOISE_FLOOR_SECONDS, max(controls) - min(controls))
    difference = min(s.test_seconds for s in complete) - min(controls)
    pipeline = min(s.total_to_finish for s in complete) - min(controls)
    background = statistics.median(s.finished_after for s in complete)
    print(f"  CONTROL: fastest {min(controls):.2f}s, median {statistics.median(controls):.2f}s, spread {max(controls) - min(controls):.2f}s")
    print(f"  TEST:    fastest {min(s.test_seconds for s in complete):.2f}s, median {statistics.median(tests):.2f}s  "
          f"(completed deliveries: fastest) -> difference {difference:+.2f}s")
    print(f"  tolerance {tolerance:.2f}s   shortest observed pipeline ~{pipeline:.2f}s   "
          f"work still running after the response: median {background:.2f}s")
    if pipeline < power_factor * tolerance:
        print(f"  INCONCLUSIVE: the shortest pipeline (~{pipeline:.1f}s) is shorter than {power_factor:.0f} x tolerance "
              f"({power_factor * tolerance:.1f}s), so a held response could not be told apart from noise. "
              "Run again, or with --long.")
        verdict = "INCONCLUSIVE"
    else:
        check(f"DIFFERENTIAL: TEST response is not slower than CONTROL by more than the measured tolerance "
              f"({difference:+.2f}s <= {tolerance:.2f}s) although the pipeline takes ~{pipeline:.1f}s",
              difference <= tolerance,
              "" if difference <= tolerance else
              "the response is being held until the pipeline ends (BackgroundTasks behaviour)")
    return verdict


# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 7 webhook timing test (see the module docstring)",
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="Worker base URL: http://127.0.0.1:8787 or the https workers.dev URL")
    parser.add_argument("--runs", type=int, default=5, help="CONTROL/TEST pairs (= OpenRouter calls). default 5, max 12")
    parser.add_argument("--max-response", type=float, help="absolute ceiling in seconds (default 1.0 local, 3.0 deployed)")
    parser.add_argument("--long", action="store_true", help="use the ~1190-character message (slowest model path)")
    parser.add_argument("--gap", type=float, default=2.0, help="seconds between pairs")
    parser.add_argument("--settle", type=float, default=4.0, help="seconds to wait before checking the window after rejected requests")
    parser.add_argument("--poll-timeout", type=float, default=90.0, help="seconds to watch each pipeline")
    parser.add_argument("--allow-real-send", action="store_true", help="skip the fake-access-token guard (can send REAL WhatsApp messages)")
    parser.add_argument("--out", default="stage7_timing_results.json")
    args = parser.parse_args()

    base = args.url.rstrip("/")
    host = urllib.parse.urlparse(base).hostname or ""
    local = host in ("127.0.0.1", "localhost", "::1")
    if not local and not base.startswith("https://"):
        sys.exit("refusing: a non-local URL must be https://")
    if not 1 <= args.runs <= 12:
        sys.exit("--runs must be between 1 and 12 (each one is a paid OpenRouter call)")
    ceiling = args.max_response if args.max_response is not None else (1.0 if local else 3.0)

    settings = load_settings()
    missing = [k for k in ("META_APP_SECRET", "SUPABASE_URL", "SUPABASE_KEY", "META_TEST_PHONE_NUMBER_ID", "META_TEST_RECIPIENT")
               if not settings.get(k)]
    if missing:
        print("missing settings: " + ", ".join(missing) + "  (cf-worker/.dev.vars or the environment)")
        return 2
    pnid, customer = settings["META_TEST_PHONE_NUMBER_ID"], settings["META_TEST_RECIPIENT"]
    if not pnid.startswith(PNID_PREFIX):
        print(f"refusing: phone_number_id {pnid!r} does not look like a test store (must start with {PNID_PREFIX!r})")
        return 2

    try:
        stores = rest_get(settings, "stores", {
            "phone_number_id": f"eq.{pnid}", "limit": "1",
            "select": "id,package,is_api_free,monthly_conversation_count,conversations_remaining,access_token"})
    except RuntimeError as exc:
        print(f"cannot read the store row: {exc}")
        return 2
    if not stores:
        print(f"no stores row with phone_number_id={pnid}. Create it first:  python stage7_test_store.py create")
        return 2
    store = stores[0]
    token = store.pop("access_token", None) or ""
    if not token.startswith(FAKE_TOKEN_PREFIX) and not args.allow_real_send:
        print(f"refusing: the store's access_token is not the fake test token (it must start with {FAKE_TOKEN_PREFIX!r}); "
              "this run could send REAL WhatsApp messages. Use --allow-real-send only if that is what you want.")
        return 2
    store_id = store["id"]
    remaining = store.get("conversations_remaining")
    print(f"worker: {base}   ceiling: {ceiling:.1f}s   {'LOCAL' if local else 'DEPLOYED'}")
    print(f"store:  {pnid}  package={store.get('package')} is_api_free={store.get('is_api_free')} "
          f"count={store.get('monthly_conversation_count')} remaining={remaining}   customer={customer}")
    if store.get("is_api_free") and remaining is not None and remaining < args.runs + 2:
        print(f"WARNING: only {remaining} conversations left this month — the pipeline may answer with the limit notice")

    health = [http("GET", base + "/health", timeout=15.0) for _ in range(2)]
    if any(h.status != 200 for h in health):
        print(f"the Worker does not answer /health with 200: {[(h.status, h.text[:60]) for h in health]}")
        return 2
    rtt = [http("GET", base + "/health", timeout=15.0).seconds for _ in range(5)]
    print(f"/health round trip: median {statistics.median(rtt):.2f}s (min {min(rtt):.2f}s, max {max(rtt):.2f}s) — informational")

    rejected = phase_rejected(base, settings, store_id, customer, ceiling, args.settle)
    samples = phase_timing(base, settings, store_id, customer, args)
    verdict = evaluate_timing(samples, ceiling, args.runs, POWER_FACTOR)

    failed = [c for c in CHECKS if not c[1]]
    if failed:
        verdict = "FAIL"
    Path(HERE / args.out).write_text(json.dumps({
        "url": base, "ceiling": ceiling, "verdict": verdict, "runs": args.runs, "long": args.long,
        "rejected": rejected, "samples": [s._asdict() for s in samples],
        "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in CHECKS]}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed   VERDICT: {verdict}   (saved {args.out})")
    return {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 3}[verdict]


if __name__ == "__main__":
    sys.exit(main())
