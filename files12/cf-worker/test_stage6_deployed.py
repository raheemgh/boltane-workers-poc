#!/usr/bin/env python3
"""
Stage 6 — test client for a DEPLOYED Cloudflare Worker (standard library only).

Lives at cf-worker/test_stage6_deployed.py. It talks to the Worker over the
internet exactly like Meta would, and it NEVER writes to Supabase (read-only).

    cd /workspaces/boltane-workers-poc/files12/cf-worker
    python test_stage6_deployed.py check   --url https://<your-worker>.workers.dev
    python test_stage6_deployed.py deliver --url https://<your-worker>.workers.dev --count 10

Commands
  check    No Meta/Supabase access needed for most of it:
             GET /health, GET /webhook (wrong token -> 403; right token -> echo, if
             META_VERIFY_TOKEN is known), POST /webhook with a missing / wrong /
             malformed signature -> 403, a body tampered after signing -> 403 (needs
             META_APP_SECRET), a correctly signed delivery for an UNKNOWN
             phone_number_id -> 200 (needs META_APP_SECRET; the pipeline just looks the
             store up, finds nothing and stops), and the four /debug/* routes -> 404.
             A 403 only counts if the body is OUR text ("invalid signature" /
             "verification token mismatch"), so a Cloudflare-generated 403 cannot pass.

  deliver  --count N signed deliveries, one after another, shaped like Meta's payload,
           to the test store (phone_number_id = META_TEST_PHONE_NUMBER_ID). For each:
             * HTTP status and response time (must be 200 and under --max-response-seconds)
             * then it polls the conversation_windows row in Supabase to see how far the
               BACKGROUND pipeline got after the response had already been sent:
                 touched  = last_message_at advanced      (pipeline started)
                 finished = token_count changed and > 0   (call_ai succeeded and
                            add_conversation_tokens ran — the step right before send)
               and classifies it COMPLETE / STARTED_NOT_FINISHED / NOT_STARTED.
           It cannot see the WhatsApp message itself: at the end YOU count the replies on
           the recipient phone (the script tells you how many to expect).
           Reading Supabase is optional (--no-supabase); without it only the response
           side is checked and the verdict says so.

Settings come from cf-worker/.dev.vars (same file pywrangler uses) and/or environment
variables, environment winning. The values must be the SAME as the secrets set on the
deployed Worker, and SUPABASE_URL/KEY must be the project that Worker reads:
    META_VERIFY_TOKEN  META_APP_SECRET  SUPABASE_URL  SUPABASE_KEY
    META_TEST_PHONE_NUMBER_ID   the stores.phone_number_id of the test store
    META_TEST_RECIPIENT         the customer number the bot replies to
(--customer overrides META_TEST_RECIPIENT.)

Exit code 0 only when every check / delivery passed.
"""

import argparse
import hashlib
import hmac
import json
import os
import random
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEV_VARS = HERE / ".dev.vars"
USER_AGENT = "boltane-stage6-test/1.0"   # a custom UA: some edge rules block the default Python-urllib one

EXPECTED_BAD_SIGNATURE_BODY = "invalid signature"
EXPECTED_BAD_TOKEN_BODY = "verification token mismatch"
WAITUNTIL_WINDOW_SECONDS = 30            # Cloudflare's documented limit for work after the response

# Hand-written synthetic customer messages (same style as test_stage4_real.py; no real customers).
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
    """Same construction as test_stage4_real._long_message(): just under the 1200-character input cap."""
    text = ""
    for sentence in _LONG_SENTENCES * 2:
        if len(text) + len(sentence) + 1 > 1190:
            break
        text = (text + " " + sentence).strip()
    return text


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def load_settings() -> dict:
    values = {}
    if DEV_VARS.exists():
        for raw in DEV_VARS.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("META_VERIFY_TOKEN", "META_APP_SECRET", "SUPABASE_URL", "SUPABASE_KEY",
                "META_TEST_PHONE_NUMBER_ID", "META_TEST_RECIPIENT", "WORKER_URL"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


# ---------------------------------------------------------------------------
# HTTP helpers (urllib, no third-party packages)
# ---------------------------------------------------------------------------

def http(method: str, url: str, *, headers: dict | None = None, body: bytes | None = None, timeout: float = 20.0):
    """Returns (status, text, seconds). status 0 = could not connect / timed out."""
    request = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
            return response.status, text, time.perf_counter() - started
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), time.perf_counter() - started
    except Exception as exc:  # noqa: BLE001 — a connection failure is a result, not a crash
        return 0, f"{type(exc).__name__}: {exc}", time.perf_counter() - started


def sign(raw: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def meta_payload(phone_number_id: str, customer: str, text: str, message_id: str) -> dict:
    """Same shape as a real Meta delivery; only the fields _process_message reads matter."""
    return {"object": "whatsapp_business_account", "entry": [{"id": "0", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "0", "phone_number_id": phone_number_id},
        "contacts": [{"profile": {"name": "Stage6 test"}, "wa_id": customer}],
        "messages": [{"from": customer, "id": message_id, "timestamp": str(int(time.time())),
                      "type": "text", "text": {"body": text}}]}}]}]}


def rest_get(settings: dict, table: str, params: dict):
    url = f"{settings['SUPABASE_URL'].rstrip('/')}/rest/v1/{table}?{urllib.parse.urlencode(params)}"
    key = settings["SUPABASE_KEY"]
    status, text, _ = http("GET", url, headers={"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"})
    if status != 200:
        raise RuntimeError(f"Supabase REST {table} -> HTTP {status}: {text[:160]}")
    return json.loads(text)


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def cmd_check(args, settings) -> int:
    base = args.url.rstrip("/")
    rows: list[tuple[str, bool, str]] = []

    def record(name, ok, detail=""):
        rows.append((name, bool(ok), detail))

    status, text, secs = http("GET", base + "/health")
    record("GET /health -> 200 {'status': 'ok'}", status == 200 and '"ok"' in text, f"HTTP {status} {text[:80]!r} ({secs:.2f}s)")

    status, text, secs = http("GET", base + "/webhook?" + urllib.parse.urlencode(
        {"hub.mode": "subscribe", "hub.verify_token": "definitely-wrong-token", "hub.challenge": "42"}))
    record("GET /webhook with a wrong verify token -> 403 with OUR body text",
           status == 403 and text == EXPECTED_BAD_TOKEN_BODY, f"HTTP {status} {text[:80]!r}")

    token = settings.get("META_VERIFY_TOKEN")
    if token:
        challenge = "".join(random.choices(string.digits, k=10))
        status, text, secs = http("GET", base + "/webhook?" + urllib.parse.urlencode(
            {"hub.mode": "subscribe", "hub.verify_token": token, "hub.challenge": challenge}))
        record("GET /webhook with the real verify token -> 200 and the challenge echoed back",
               status == 200 and text == challenge, f"HTTP {status} {text[:60]!r} ({secs:.2f}s)")
    else:
        print("SKIPPED: right-token handshake (META_VERIFY_TOKEN not set)")

    body = json.dumps(meta_payload("PN-STAGE6-NOSUCH", "15551230001", "hello", "wamid.NEG"), ensure_ascii=False).encode("utf-8")
    json_header = {"Content-Type": "application/json"}
    negatives = {
        "no signature header": {},
        "signature made with a wrong secret": {"X-Hub-Signature-256": sign(body, "not-the-real-secret")},
        "hex digest without the sha256= prefix": {"X-Hub-Signature-256": sign(body, "x")[len("sha256="):]},
    }
    app_secret = settings.get("META_APP_SECRET")
    for label, extra in negatives.items():
        status, text, _ = http("POST", base + "/webhook", headers={**json_header, **extra}, body=body)
        record(f"POST /webhook, {label} -> 403 with OUR body text",
               status == 403 and text == EXPECTED_BAD_SIGNATURE_BODY, f"HTTP {status} {text[:80]!r}")

    if app_secret:
        tampered = body.replace(b"hello", b"hellp")
        status, text, _ = http("POST", base + "/webhook", headers={**json_header, "X-Hub-Signature-256": sign(body, app_secret)}, body=tampered)
        record("POST /webhook, valid signature but body changed afterwards -> 403 with OUR body text",
               status == 403 and text == EXPECTED_BAD_SIGNATURE_BODY, f"HTTP {status} {text[:80]!r}")
        status, text, secs = http("POST", base + "/webhook", headers={**json_header, "X-Hub-Signature-256": sign(body, app_secret)}, body=body)
        record("POST /webhook, correctly signed delivery for an unknown phone_number_id -> 200 {'status': 'received'}",
               status == 200 and "received" in text, f"HTTP {status} {text[:80]!r} ({secs:.2f}s)")
    else:
        print("SKIPPED: tampered-body and correctly-signed checks (META_APP_SECRET not set)")

    for method, path in (("GET", "/debug/settings"), ("GET", "/debug/meta-send?case=bad_token"),
                         ("GET", "/debug/supabase?op=select"), ("POST", "/debug/ai")):
        status, text, _ = http(method, base + path, headers=json_header, body=b"{}" if method == "POST" else None)
        record(f"{method} {path.split('?')[0]} -> 404 (temporary debug route is gone)", status == 404, f"HTTP {status} {text[:60]!r}")

    failed = [r for r in rows if not r[1]]
    for name, ok, detail in rows:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n        -> {detail}"))
    print(f"\n{len(rows) - len(failed)}/{len(rows)} checks passed")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# deliver
# ---------------------------------------------------------------------------

def window_state(settings, store_id, customer):
    rows = rest_get(settings, "conversation_windows", {
        "store_id": f"eq.{store_id}", "customer_phone": f"eq.{customer}",
        "select": "last_message_at,token_count", "limit": "1"})
    return rows[0] if rows else None


def evaluate(base_row, current_row) -> tuple[bool, bool]:
    """(touched, finished) — see the module docstring for the rule and why it also holds on a reset."""
    if current_row is None:
        return False, False
    touched = base_row is None or current_row["last_message_at"] != base_row["last_message_at"]
    tokens = current_row.get("token_count") or 0
    finished = touched and tokens > 0 and (base_row is None or tokens != (base_row.get("token_count") or 0))
    return touched, finished


def cmd_deliver(args, settings) -> int:
    base = args.url.rstrip("/")
    missing = [k for k in ("META_APP_SECRET", "META_TEST_PHONE_NUMBER_ID") if not settings.get(k)]
    customer = args.customer or settings.get("META_TEST_RECIPIENT")
    if not customer:
        missing.append("META_TEST_RECIPIENT (or --customer)")
    use_db = not args.no_supabase
    if use_db:
        missing += [k for k in ("SUPABASE_URL", "SUPABASE_KEY") if not settings.get(k)]
    if missing:
        sys.exit("missing settings: " + ", ".join(missing) + "  (put them in cf-worker/.dev.vars or the environment)")

    pnid = settings["META_TEST_PHONE_NUMBER_ID"]
    secret = settings["META_APP_SECRET"]
    run_id = "".join(random.choices(string.ascii_lowercase, k=4))
    store_id = None

    print(f"worker:   {base}")
    print(f"store:    phone_number_id={pnid}   customer={customer}   run={run_id}   deliveries={args.count}")
    if use_db:
        stores = rest_get(settings, "stores", {
            "phone_number_id": f"eq.{pnid}", "limit": "1",
            "select": "id,package,is_trial,is_api_free,monthly_conversation_count,conversations_remaining,access_token"})
        if not stores:
            sys.exit(f"no stores row with phone_number_id={pnid} in {settings['SUPABASE_URL']} — "
                     "every delivery would be dropped at the store lookup. Create the test store row first.")
        store = stores[0]
        store_id = store["id"]
        print("          package={package} is_trial={is_trial} is_api_free={is_api_free} "
              "monthly_conversation_count={monthly_conversation_count} conversations_remaining={conversations_remaining} "
              "access_token={token}".format(**store, token="set (hidden)" if store.get("access_token") else "MISSING"))
        remaining = store.get("conversations_remaining")
        if store.get("is_api_free") and remaining is not None and remaining < args.count + 1:
            print(f"WARNING: only {remaining} conversations left this month on this store — it may start answering with the "
                  "monthly-limit notice, which this script would report as STARTED_NOT_FINISHED.")
    else:
        print("          (--no-supabase: only the HTTP response is checked; the background pipeline is NOT observed)")
    print()

    results = []
    for i in range(1, args.count + 1):
        text = (long_message() if args.long else SHORT_MESSAGES[(i - 1) % len(SHORT_MESSAGES)]) + f" [S6-{run_id}-{i}]"
        raw = json.dumps(meta_payload(pnid, customer, text, f"wamid.STAGE6{run_id}{i}"), ensure_ascii=False).encode("utf-8")
        base_row = window_state(settings, store_id, customer) if use_db else None

        sent_at = datetime.now().strftime("%H:%M:%S")
        status, resp_text, resp_seconds = http("POST", base + "/webhook", body=raw, timeout=30.0, headers={
            "Content-Type": "application/json", "X-Hub-Signature-256": sign(raw, secret)})
        response_ended = time.perf_counter()
        response_ok = status == 200 and "received" in resp_text
        fast = resp_seconds <= args.max_response_seconds

        touched_after = finished_after = None
        touched = finished = False
        if use_db and response_ok:
            while time.perf_counter() - response_ended < args.poll_timeout:
                try:
                    current = window_state(settings, store_id, customer)
                except RuntimeError as exc:
                    print(f"      (Supabase read failed, retrying: {exc})")
                    time.sleep(1.0)
                    continue
                touched, finished = evaluate(base_row, current)
                now = time.perf_counter() - response_ended
                if touched and touched_after is None:
                    touched_after = now
                if finished:
                    finished_after = now
                    break
                time.sleep(0.5)

        if not response_ok:
            verdict = "RESPONSE_FAIL"
        elif not use_db:
            verdict = "RESPONSE_ONLY"
        elif finished:
            verdict = "COMPLETE"
        elif touched:
            verdict = "STARTED_NOT_FINISHED"
        else:
            verdict = "NOT_STARTED"

        results.append({"n": i, "sent_at": sent_at, "http_status": status, "response_seconds": round(resp_seconds, 3),
                        "fast_enough": fast, "touched_after_s": touched_after and round(touched_after, 1),
                        "finished_after_s": finished_after and round(finished_after, 1), "verdict": verdict,
                        "response_text": resp_text[:100], "message_chars": len(text)})
        fmt = lambda v: "never" if v is None else f"+{v:.1f}s"
        print(f"#{i:<3} {sent_at}  HTTP {status}  response {resp_seconds:5.2f}s {'ok  ' if fast else 'SLOW'}  "
              f"started {fmt(touched_after)}  finished {fmt(finished_after)}  -> {verdict}")
        if verdict == "COMPLETE" and finished_after > WAITUNTIL_WINDOW_SECONDS:
            print(f"      note: finished {finished_after:.0f}s after the response — past Cloudflare's documented "
                  f"{WAITUNTIL_WINDOW_SECONDS}s waitUntil window, so it was not cancelled")
        if verdict == "STARTED_NOT_FINISHED":
            print(f"      note: started but token_count never changed within {args.poll_timeout:.0f}s. Either the background "
                  "task was cancelled/killed mid-way, or the AI call ended in the graceful-failure reply (tokens=0), or "
                  "the store hit its monthly cap. Check `wrangler tail` and the WhatsApp reply text to tell which.")
        if verdict == "NOT_STARTED":
            print("      note: last_message_at never advanced — the pipeline never reached the window step "
                  "(dropped right after the response, store not found, or an early failure). Check `wrangler tail`.")
        if i < args.count:
            time.sleep(args.gap)

    out_path = HERE / args.out
    out_path.write_text(json.dumps({"url": base, "run": run_id, "long": args.long, "deliveries": results},
                                   indent=2, ensure_ascii=False), encoding="utf-8")

    count = lambda v: sum(1 for r in results if r["verdict"] == v)
    slow = [r["n"] for r in results if r["http_status"] == 200 and not r["fast_enough"]]
    print(f"\nsummary: {len(results)} deliveries | HTTP 200 + fast: {sum(1 for r in results if r['http_status'] == 200 and r['fast_enough'])}"
          f" | COMPLETE: {count('COMPLETE')} | STARTED_NOT_FINISHED: {count('STARTED_NOT_FINISHED')}"
          f" | NOT_STARTED: {count('NOT_STARTED')} | RESPONSE_FAIL: {count('RESPONSE_FAIL')}")
    if slow:
        print(f"slow responses (> {args.max_response_seconds}s): deliveries {slow}")
    print(f"wrote {out_path.name}")
    if use_db:
        print(f"\nNOW CHECK THE PHONE of {customer}: you should have received {len(results)} replies from the test number "
              f"(the ones tagged by the message order #1..#{len(results)}, around the times above).")
        print("The script proves the background pipeline reached the step just before sending; only the phone proves delivery.")
    ok = all(r["verdict"] in ("COMPLETE", "RESPONSE_ONLY") and r["fast_enough"] for r in results)
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "deliver"):
        p = sub.add_parser(name)
        p.add_argument("--url", help="the deployed Worker's base URL (or set WORKER_URL)")
    d = sub.choices["deliver"]
    d.add_argument("--count", type=int, default=10)
    d.add_argument("--gap", type=float, default=5.0, help="seconds to wait between deliveries (use 60+ to meet cold starts)")
    d.add_argument("--poll-timeout", type=float, default=150.0, help="seconds to watch the background pipeline per delivery")
    d.add_argument("--max-response-seconds", type=float, default=3.0, help="Meta wants the 200 within roughly 3 s")
    d.add_argument("--long", action="store_true", help="send the ~1190-character message (slowest model path)")
    d.add_argument("--customer", help="override META_TEST_RECIPIENT")
    d.add_argument("--no-supabase", action="store_true", help="do not read Supabase; check only the HTTP response")
    d.add_argument("--out", default="stage6_deployed_results.json")
    args = parser.parse_args()

    settings = load_settings()
    args.url = args.url or settings.get("WORKER_URL")
    if not args.url:
        sys.exit("give the Worker URL: --url https://<name>.<subdomain>.workers.dev")
    return cmd_check(args, settings) if args.command == "check" else cmd_deliver(args, settings)


if __name__ == "__main__":
    sys.exit(main())
