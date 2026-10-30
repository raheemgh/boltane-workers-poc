#!/usr/bin/env python3
"""
Stage 6b — Meta replay simulator. No Meta account, no network, no credentials, no cost.

Lives at cf-worker/test_stage6_meta_sim.py. Run from cf-worker/ in the same venv as the Stage 5 test:

    source ~/venv-tests/bin/activate
    python test_stage6_meta_sim.py                 # every scenario
    python test_stage6_meta_sim.py --list          # just list them
    python test_stage6_meta_sim.py --only A1,B6    # a subset
    python test_stage6_meta_sim.py --show A1,D2    # also print everything observed for those scenarios
    python test_stage6_meta_sim.py --sabotage      # break the engine 14 ways: the suite must FAIL every time

WHAT IT DOES
  Replays about 54 realistic situations through the REAL pipeline — webhook._process_message, supabase_client,
  ai_client, meta_client, the real httpx client — and records exactly what the bot did: which replies the
  customer would have received, which requests reached OpenRouter (and with what text), what was written to the
  database, what was logged.

  Only three things are simulated, and each over a real httpx.MockTransport so requests are really serialised:
    * Meta's Graph API — enforces the documented contract (path, Bearer token, JSON body shape, 4096 limit) and can
      answer with Meta's documented errors (190 expired token, 131030 recipient not allowed, 131047 24-hour window,
      130429 rate limit, 5xx, connection failure). Every request is checked for contract violations.
    * OpenRouter — scripted per scenario: normal streamed answer, 429/402/500, empty stream (reasoning ate the
      budget), answer cut by max_tokens, error in the middle of a 200 stream, connection resets.
    * Supabase — the in-memory emulator from Stage 2/5, with a yield point at every query so concurrent messages
      really interleave, and an outage switch per table.

HOW TO READ THE RESULT
  PASS   behaved as the code is meant to.
  GAP    behaved exactly as the code currently does, and that behaviour is a known weakness. Not a migration defect
         (Render does the same); it is the list of things worth deciding before real customers. Each GAP carries a
         one-line impact statement.
  FAIL   the observation differs from what was written down BEFORE running (from reading the code). Either the
         expectation or the code is not what we thought: send me the output. Exit code 1 only when something FAILs.

WHAT IT CANNOT SHOW
  * how the real Meta behaves (retries, batching, payload fields beyond those the engine reads) — the payloads here
    follow Meta's published webhook shapes as far as the engine reads them;
  * what a real model answers (the model is scripted; what is checked is what the engine SENDS to it);
  * Meta-side configuration (app secret, field subscription, token permissions, number registration);
  * Cloudflare's runtime limits — that is test_stage6_deployed.py's job.
"""

import argparse
import asyncio
import contextlib
import itertools
import json
import logging
import re
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_stage5_firewall as s5          # reuse its tree loader and the Supabase outage wrapper  # noqa: E402

T = s5.T                                    # filled by s5.load_tree("workers")

PNID = "PN-SIM"
CUSTOMER = "15551230000"                    # allowed recipient
WINDOW_CLOSED = "15551230001"               # Meta answers 131047 (more than 24 h since the customer last wrote)
UNLISTED = "15555550100"                    # Meta answers 131030 (recipient not in the allowed list)
VALID_TOKEN = "EAAB-valid-sim-token"
AI_REPLY = "أهلاً! كيف أساعدك؟"
DEEPSEEK = "deepseek/deepseek-v4-flash"
MAX_TEXT = 4096

DEFAULT_STORE = dict(
    phone_number_id=PNID, store_name="sim", system_prompt="You are a shop assistant.",
    access_token=VALID_TOKEN, package="pro", is_trial=False, is_api_free=True,
    monthly_conversation_count=0, api_key=None, ai_model=None,
)
DEFAULT_EXPECT = {"meta_violations": [], "crashed": None}

try:                                        # the same ~1190-character Arabic message the Stage 4/6 tests use
    from test_stage6_deployed import long_message as _long_message
except Exception:                           # noqa: BLE001 — only a convenience import
    def _long_message():
        return "ا" * 1190


# ---------------------------------------------------------------------------
# Meta payload builders (shapes follow Meta's published webhook reference)
# ---------------------------------------------------------------------------

_ids = itertools.count(1)


def wamid() -> str:
    return f"wamid.SIM{next(_ids):04d}"


def message(kind: str, customer: str = CUSTOMER, **fields) -> dict:
    return {"from": customer, "id": wamid(), "timestamp": "1760000000", "type": kind, **fields}


def text(body: str, customer: str = CUSTOMER) -> dict:
    return message("text", customer, text={"body": body})


def status(kind: str, errors: list | None = None) -> dict:
    record = {"id": wamid(), "status": kind, "timestamp": "1760000001", "recipient_id": CUSTOMER}
    if errors:
        record["errors"] = errors
    return record


def value(messages: list | None = None, statuses: list | None = None, pnid: str = PNID) -> dict:
    v = {"messaging_product": "whatsapp",
         "metadata": {"display_phone_number": "15550000000", "phone_number_id": pnid}}
    if messages is not None:
        v["contacts"] = [{"profile": {"name": "Sim"}, "wa_id": (messages[0].get("from") if messages else CUSTOMER)}]
        v["messages"] = messages
    if statuses is not None:
        v["statuses"] = statuses
    return v


def envelope(v: dict) -> dict:
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "WABA-SIM", "changes": [{"field": "messages", "value": v}]}]}


def delivery(*messages: dict, pnid: str = PNID) -> dict:
    return envelope(value(messages=list(messages), pnid=pnid))


# ---------------------------------------------------------------------------
# OpenRouter steps (what the simulated provider answers, one step per request)
# ---------------------------------------------------------------------------

def usage(total: int = 120) -> dict:
    return {"prompt_tokens": total - 20, "completion_tokens": 20, "total_tokens": total}


def chunks(s: str, n: int = 7) -> list:
    return [s[i:i + n] for i in range(0, len(s), n)]


def sse(pieces, *, use=None, finish="stop", reasoning=None, error=None) -> dict:
    lines = [": OPENROUTER PROCESSING", ""]
    for r in reasoning or []:
        lines += ["data: " + json.dumps({"choices": [{"delta": {"reasoning": r}}]}), ""]
    for piece in pieces:
        lines += ["data: " + json.dumps({"choices": [{"delta": {"content": piece}}]}, ensure_ascii=False), ""]
    if error:
        lines += ["data: " + json.dumps({"error": error, "choices": [{"delta": {"content": ""}, "finish_reason": "error"}]}), ""]
    else:
        lines += ["data: " + json.dumps({"choices": [{"delta": {"content": ""}, "finish_reason": finish}]}), ""]
    if use:
        lines += ["data: " + json.dumps({"choices": [], "usage": use}), ""]
    lines += ["data: [DONE]", ""]
    return {"kind": "sse", "lines": lines}


def ok_step(reply: str = AI_REPLY, total: int = 120) -> dict:
    return sse(chunks(reply), use=usage(total))


def echo_step(prefix: str):
    return lambda body: sse(chunks(prefix + body["messages"][-1]["content"], 5), use=usage(150))


def status_step(code: int) -> dict:
    return {"kind": "status", "code": code, "body": '{"error": {"message": "simulated"}}'}


NET_ERROR = {"kind": "raise"}


# ---------------------------------------------------------------------------
# Scenario definition
# ---------------------------------------------------------------------------

@dataclass
class Sc:
    sid: str
    group: str
    title: str
    deliver: list                     # payload dicts, or ("concurrent", [payload, ...])
    expect: dict = field(default_factory=dict)
    store: dict = field(default_factory=dict)
    or_steps: list = field(default_factory=list)
    meta: object = "ok"               # "ok" | "raise" | ("error", http_status, meta_code, title)
    outage: tuple = ()
    fake_sleep: bool = False
    customer: str = CUSTOMER
    gap: str | None = None            # known weakness: expectation met -> GAP (not PASS), this is the impact line
    gap_if: object = None             # optional predicate(obs) -> bool: the gap only counts when it actually shows


GAP_EMPTY_PROMPT = ("a non-text message reaches the model as an EMPTY prompt, is answered anyway, and uses up a "
                    "monthly conversation")


def scenarios() -> list:
    S = []
    add = S.append
    long_ar = _long_message()

    # ---- A: text messages ------------------------------------------------------------------------------------
    add(Sc("A1", "text", "plain Arabic question", [delivery(text("السلام عليكم، شو عندكم اليوم؟"))],
           {"sent": ["ai"], "or_requests": 1, "count": 1, "token_count": 120, "window_rows": 1,
            "or_max_tokens": [300]}))
    add(Sc("A2", "text", "plain English question", [delivery(text("Do you have vegetarian options?"))],
           {"sent": ["ai"], "or_requests": 1, "count": 1}))
    add(Sc("A3", "text", "Arabic + emoji + digits: reply text survives the HTTP round trip byte for byte",
           [delivery(text("بدي ٢ برغر 🍔 وبطاطا 🍟 لو سمحت"))],
           {"sent": ["ai"], "sent_texts": [AI_REPLY], "or_user": ["بدي ٢ برغر 🍔 وبطاطا 🍟 لو سمحت"]}))
    add(Sc("A4", "text", "Syrian phone number is masked before OpenRouter and restored in the reply",
           [delivery(text("رقمي 0999 123 456 للتواصل"))],
           {"or_user": ["رقمي [PHONE_1] للتواصل"],
            "sent_texts": ["تمام، سأتصل بك على: رقمي 0999 123 456 للتواصل"]},
           or_steps=[echo_step("تمام، سأتصل بك على: ")]))
    add(Sc("A5", "text", "international phone number is masked too",
           [delivery(text("تواصل معي على +971 50 123 4567"))],
           {"or_user": ["تواصل معي على [PHONE_1]"]}))
    add(Sc("A6", "text", "exactly 1200 characters is accepted", [delivery(text("a" * 1200))],
           {"sent": ["ai"], "or_requests": 1}))
    add(Sc("A7", "text", "1201 characters is rejected before the model, nothing is recorded", [delivery(text("a" * 1201))],
           {"sent": ["too_long"], "or_requests": 0, "count": 0, "window_rows": 0}))
    add(Sc("A8", "text", "long Arabic message (~1190 characters) is accepted", [delivery(text(long_ar))],
           {"sent": ["ai"], "or_requests": 1}))
    add(Sc("A9", "text", "text message with an EMPTY body", [delivery(text(""))],
           {"sent": ["ai"], "or_user": [""], "count": 1}, gap=GAP_EMPTY_PROMPT, gap_if=lambda o: True))

    # ---- B: non-text messages (Meta sends them in the same `messages` array) ------------------------------------
    non_text = [
        ("B1", "image (the caption is ignored)",
         message("image", image={"caption": "شو رأيك بهالصورة؟", "mime_type": "image/jpeg", "sha256": "abc", "id": "MEDIA1"})),
        ("B2", "voice note", message("audio", audio={"mime_type": "audio/ogg; codecs=opus", "sha256": "abc", "id": "MEDIA2", "voice": True})),
        ("B3", "sticker", message("sticker", sticker={"mime_type": "image/webp", "sha256": "abc", "id": "MEDIA3", "animated": False})),
        ("B4", "shared location", message("location", location={"latitude": 33.5138, "longitude": 36.2765, "name": "Home", "address": "Damascus"})),
        ("B5", "shared contact", message("contacts", contacts=[{"name": {"formatted_name": "Test", "first_name": "Test"},
                                                                   "phones": [{"phone": "+15550001111", "type": "CELL"}]}])),
        ("B6", "reaction (a thumbs-up on a bot reply)", message("reaction", reaction={"message_id": "wamid.PREV", "emoji": "👍"})),
        ("B7", "interactive button reply (the button title is lost)",
         message("interactive", interactive={"type": "button_reply", "button_reply": {"id": "btn1", "title": "نعم"}})),
        ("B8", "unsupported message type (comes with an `errors` array)",
         message("unsupported", errors=[{"code": 131051, "title": "Message type is not supported",
                                         "message": "Message type is not supported",
                                         "error_data": {"details": "Message type is not supported"}}])),
    ]
    for sid, title, msg in non_text:
        add(Sc(sid, "non-text", title, [delivery(msg)],
               {"sent": ["ai"], "or_requests": 1, "or_user": [""], "count": 1},
               gap=GAP_EMPTY_PROMPT, gap_if=lambda o: True))

    # ---- C: payload shapes that are not a customer message ----------------------------------------------------------
    dropped = {"sent": [], "or_requests": 0, "meta_calls": 0, "count": 0, "window_rows": 0,
               "logs": lambda tags: "parse_failed" in tags}
    for sid, kind, errs in (("C1", "sent", None), ("C2", "delivered", None), ("C3", "read", None)):
        add(Sc(sid, "shapes", f"status notification '{kind}' (Meta sends up to 3 per bot reply)",
               [envelope(value(statuses=[status(kind, errs)]))], dict(dropped)))
    add(Sc("C4", "shapes", "status notification 'failed' (e.g. Meta could not deliver our reply)",
           [envelope(value(statuses=[status("failed", [{"code": 131047, "title": "Re-engagement message",
                                                         "message": "Re-engagement message",
                                                         "error_data": {"details": "more than 24 hours"}}])]))],
           dict(dropped), gap="a failed-delivery notification is dropped like any status: nobody (bot, owner, log "
                              "reader) is told that a customer did not get the reply",
           gap_if=lambda o: True))
    add(Sc("C5", "shapes", "payload without `entry`", [{"object": "whatsapp_business_account"}], dict(dropped)))
    add(Sc("C6", "shapes", "`entry` with an empty `changes` list", [{"entry": [{"id": "W", "changes": []}]}], dict(dropped)))
    add(Sc("C7", "shapes", "`messages` is an empty list", [envelope(value(messages=[]))], dict(dropped)))
    no_pnid = envelope(value(messages=[text("hi")]))
    del no_pnid["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"]
    add(Sc("C8", "shapes", "metadata without phone_number_id", [no_pnid], dict(dropped)))
    no_from = text("hi")
    del no_from["from"]
    add(Sc("C9", "shapes", "message without `from`", [envelope(value(messages=[no_from]))], dict(dropped)))
    add(Sc("C10", "shapes", "two messages in ONE delivery: only the first is answered",
           [delivery(text("الرسالة الأولى"), text("الرسالة الثانية"))],
           {"sent": ["ai"], "or_requests": 1, "or_user": ["الرسالة الأولى"]},
           gap="every message after the first one in a single delivery is silently ignored (how often Meta batches "
               "messages this way is not known)", gap_if=lambda o: True))
    two_entries = delivery(text("الأول"))
    two_entries["entry"].append(envelope(value(messages=[text("الثاني")]))["entry"][0])
    add(Sc("C11", "shapes", "two `entry` items in one delivery: only the first is answered", [two_entries],
           {"sent": ["ai"], "or_requests": 1, "or_user": ["الأول"]},
           gap="every `entry` after the first is silently ignored", gap_if=lambda o: True))
    add(Sc("C12", "shapes", "phone_number_id that belongs to no store", [delivery(text("hi"), pnid="PN-UNKNOWN")],
           {"sent": [], "or_requests": 0, "meta_calls": 0, "count": 0, "window_rows": 0,
            "logs": lambda tags: "store_missing" in tags}))

    # ---- D: repetition, concurrency, memory ----------------------------------------------------------------------
    same = delivery(text("بدي اطلب برغر"))
    add(Sc("D1", "repeat", "the very same delivery arrives twice (same message id)", [same, same],
           {"sent": ["ai", "ai"], "or_requests": 2, "count": 1, "token_count": 240},
           gap="no de-duplication by message id: a repeated delivery is answered twice and paid twice",
           gap_if=lambda o: True))
    add(Sc("D2", "repeat", "three quick messages from one customer processed at the same time",
           [("concurrent", [delivery(text("بدي برغر")), delivery(text("وبطاطا")), delivery(text("وكولا"))])],
           {"sent": ["ai", "ai", "ai"], "or_requests": 3, "count": 1},
           gap="concurrent messages race on the conversation token counter (the update is read-then-write): "
               "usage is undercounted", gap_if=lambda o: o.get("token_count") != 360))
    add(Sc("D3", "repeat", "a two-step order: does the second answer know the first message?",
           [delivery(text("بدي 2 برغر")), delivery(text("اسمي أحمد"))],
           {"or_roles": [["system", "user"], ["system", "user"]], "or_user": ["بدي 2 برغر", "اسمي أحمد"],
            "count": 1, "token_count": 240},
           gap="no conversation memory: each message reaches the model alone (system prompt + this message), so a "
               "multi-step order loses its context", gap_if=lambda o: True))

    # ---- E: the Meta side refuses our reply ------------------------------------------------------------------------
    lost = {"sent": [], "meta_calls": 1, "or_requests": 1, "count": 1,
            "logs": lambda tags: "meta_send_failed" in tags}
    lost_gap = ("the reply is lost silently: the customer gets nothing, the monthly conversation is already counted, "
                "and only a log line (not an alert) records it")
    add(Sc("E1", "meta-errors", "store access token expired or wrong (Meta code 190)", [delivery(text("مرحبا"))],
           dict(lost, meta_errors=[190]), store={"access_token": "expired-token"}, gap=lost_gap, gap_if=lambda o: True))
    add(Sc("E2", "meta-errors", "recipient not in the allowed list (131030)", [delivery(text("مرحبا", UNLISTED))],
           dict(lost, meta_errors=[131030]), customer=UNLISTED, gap=lost_gap, gap_if=lambda o: True))
    add(Sc("E3", "meta-errors", "24-hour window closed (131047)", [delivery(text("مرحبا", WINDOW_CLOSED))],
           dict(lost, meta_errors=[131047]), customer=WINDOW_CLOSED, gap=lost_gap, gap_if=lambda o: True))
    add(Sc("E4", "meta-errors", "rate limited by Meta (130429): no retry", [delivery(text("مرحبا"))],
           dict(lost, meta_errors=[130429]), meta=("error", 429, 130429, "Rate limit hit"), gap=lost_gap, gap_if=lambda o: True))
    add(Sc("E5", "meta-errors", "Meta answers 500: no retry", [delivery(text("مرحبا"))],
           dict(lost, meta_errors=[2]), meta=("error", 500, 2, "Service temporarily unavailable"), gap=lost_gap, gap_if=lambda o: True))
    add(Sc("E6", "meta-errors", "connection to Meta fails: swallowed, no crash", [delivery(text("مرحبا"))],
           dict(lost, meta_errors=[]), meta="raise", gap=lost_gap, gap_if=lambda o: True))

    # ---- F: the model side fails ------------------------------------------------------------------------------------
    graceful = {"sent": ["graceful"], "count": 1, "token_count": 0}
    graceful_gap = ("a failed model call still uses up a monthly conversation (the customer is told to try again "
                    "later, the quota is spent)")
    add(Sc("F1", "model-errors", "OpenRouter 429 on the only model", [delivery(text("مرحبا"))],
           dict(graceful, or_requests=1), or_steps=[status_step(429)], gap=graceful_gap, gap_if=lambda o: True))
    add(Sc("F2", "model-errors", "OpenRouter 402 (out of credit)", [delivery(text("مرحبا"))],
           dict(graceful, or_requests=1, logs=lambda tags: "ai_402" in tags), or_steps=[status_step(402)],
           gap=graceful_gap, gap_if=lambda o: True))
    add(Sc("F3", "model-errors", "OpenRouter 500", [delivery(text("مرحبا"))],
           dict(graceful, or_requests=1), or_steps=[status_step(500)], gap=graceful_gap, gap_if=lambda o: True))
    add(Sc("F4", "model-errors", "connection to OpenRouter resets three times (same model retried after 2 s and 4 s)",
           [delivery(text("مرحبا"))], dict(graceful, or_requests=3, sleeps=[2.0, 4.0]),
           or_steps=[NET_ERROR, NET_ERROR, NET_ERROR], fake_sleep=True, gap=graceful_gap, gap_if=lambda o: True))
    add(Sc("F5", "model-errors", "empty stream: the model spent its whole budget thinking",
           [delivery(text("مرحبا"))], dict(graceful, or_requests=1),
           or_steps=[sse([], reasoning=["hmm", "let me think"], use=usage(310))], gap=graceful_gap, gap_if=lambda o: True))
    cut = "نفتح من التاسعة حتى"
    add(Sc("F6", "model-errors", "answer cut by max_tokens is sent as it is",
           [delivery(text("متى تفتحون؟"))], {"sent_texts": [cut], "token_count": 340, "count": 1},
           or_steps=[sse([cut], finish="length", use=usage(340))],
           gap="a reply cut off mid-sentence by the 300-token cap is delivered to the customer unchanged",
           gap_if=lambda o: True))
    part = "الجواب الجزئي"
    add(Sc("F7", "model-errors", "error in the middle of a 200 stream, with partial text",
           [delivery(text("مرحبا"))], {"sent_texts": [part], "count": 1},
           or_steps=[sse([part], error={"code": 502, "message": "provider disconnected"}, use=usage(60))],
           gap="an error that arrives inside a 200 stream is not detected: the partial text is sent as the answer",
           gap_if=lambda o: True))
    byok = {"is_api_free": False, "api_key": "sk-store-own", "ai_model": "store/own-model"}
    add(Sc("F8", "model-errors", "BYOK store out of credit (402): its own key only, no fallback to the platform key",
           [delivery(text("مرحبا"))],
           {"sent": ["graceful"], "or_models": ["store/own-model"], "or_auth": ["Bearer sk-store-own"], "remaining": None},
           store=byok, or_steps=[status_step(402)]))
    add(Sc("F9", "model-errors", "BYOK store's own model is rate limited: falls back to DeepSeek with the SAME key",
           [delivery(text("مرحبا"))],
           {"sent": ["ai"], "or_models": ["store/own-model", DEEPSEEK],
            "or_auth": ["Bearer sk-store-own", "Bearer sk-store-own"]},
           store=byok, or_steps=[status_step(429), ok_step()]))

    # ---- G: quota, storage, branding ---------------------------------------------------------------------------------
    capped = {"package": "basic", "is_trial": False, "monthly_conversation_count": 300}
    add(Sc("G1", "quota", "store at its monthly cap, new conversation", [delivery(text("مرحبا"))],
           {"sent": ["limit"], "or_requests": 0, "count": 300, "window_rows": 1}, store=capped))
    add(Sc("G2", "quota", "store at its cap: first message blocked, the customer's next one gets through",
           [delivery(text("مرحبا")), delivery(text("هل من أحد؟"))],
           {"sent": ["limit", "ai"], "or_requests": 1, "count": 300}, store=capped,
           gap="a blocked conversation still writes its window, so the customer's next message counts as 'continuing' "
               "and skips the monthly cap: the cap only blocks the first message of each visit",
           gap_if=lambda o: True))
    add(Sc("G3", "quota", "BYOK low-tier store with a huge counter is never capped",
           [delivery(text("مرحبا"))], {"sent": ["ai+footer"], "or_requests": 1, "remaining": None},
           store={"package": "low-tier", "is_api_free": False, "api_key": "sk-own", "monthly_conversation_count": 999999}))
    add(Sc("G4", "storage", "Supabase down for the store lookup", [delivery(text("مرحبا"))],
           {"sent": [], "or_requests": 0, "meta_calls": 0, "logs": lambda tags: "store_lookup_failed" in tags},
           outage=("stores",),
           gap="when the store lookup fails (for example a paused Supabase project) the customer's message is dropped "
               "with no reply at all", gap_if=lambda o: True))
    add(Sc("G5", "storage", "conversation_windows table down: fails safe (treated as a new conversation)",
           [delivery(text("مرحبا"))],
           {"sent": ["ai"], "or_requests": 1, "count": 1, "window_rows": 0,
            "logs": lambda tags: "window_lookup_failed" in tags and "token_update_failed" in tags},
           outage=("conversation_windows",)))
    add(Sc("G6", "storage", "store row without an access_token", [delivery(text("مرحبا"))],
           dict(lost, meta_errors=[190]), store={"access_token": None}, gap=lost_gap, gap_if=lambda o: True))
    add(Sc("G7", "branding", "low-tier store: footer on the reply and the mention-only-if-asked instruction",
           [delivery(text("مرحبا"))],
           {"sent": ["ai+footer"], "or_system": lambda systems: len(systems) == 1 and "Boltane" in systems[0]},
           store={"package": "low-tier", "is_trial": True}))
    return S


# ---------------------------------------------------------------------------
# Simulated network: OpenRouter + Meta Graph API over httpx.MockTransport
# ---------------------------------------------------------------------------

class State:
    def __init__(self, sc: Sc):
        self.or_steps = list(sc.or_steps)
        self.meta_mode = sc.meta
        self.or_requests, self.meta_requests = [], []
        self.meta_violations, self.meta_errors, self.sent, self.sleeps = [], [], [], []


def _violations(request, body) -> list:
    found = []
    if request.method != "POST":
        found.append("method")
    if not re.match(r"^/v\d+\.\d+/[^/]+/messages$", request.url.path):
        found.append("path")
    if not request.headers.get("authorization", "").startswith("Bearer "):
        found.append("authorization")
    if "application/json" not in request.headers.get("content-type", ""):
        found.append("content-type")
    if not isinstance(body, dict):
        return found + ["body-not-json"]
    if body.get("messaging_product") != "whatsapp":
        found.append("messaging_product")
    if not isinstance(body.get("to"), str) or not body["to"]:
        found.append("to")
    if body.get("type") != "text":
        found.append("type")
    text_body = (body.get("text") or {}).get("body")
    if not isinstance(text_body, str) or not text_body or len(text_body) > MAX_TEXT:
        found.append("text.body")
    return found


def _meta_error(status_code: int, code: int, title: str):
    return T.httpx.Response(status_code, json={"error": {"message": f"(#{code}) {title}", "type": "OAuthException",
                                                         "code": code, "fbtrace_id": "ASIM"}})


def _meta_response(state: State, request):
    try:
        body = json.loads(request.content.decode("utf-8"))
    except ValueError:
        body = None
    state.meta_requests.append(body)
    state.meta_violations.extend(_violations(request, body))
    mode = state.meta_mode
    if mode == "raise":
        raise T.httpx.ConnectError("simulated connection failure", request=request)
    if isinstance(mode, tuple):
        _, http_status, code, title = mode
        state.meta_errors.append(code)
        return _meta_error(http_status, code, title)
    if request.headers.get("authorization", "") != f"Bearer {VALID_TOKEN}":
        state.meta_errors.append(190)
        return _meta_error(401, 190, "Access token has expired or is invalid")
    match = re.match(r"^/v\d+\.\d+/(?P<pnid>[^/]+)/messages$", request.url.path)
    if not match or match.group("pnid") != PNID:
        state.meta_errors.append(100)
        return _meta_error(400, 100, "Unsupported post request")
    to = (body or {}).get("to")
    if to == UNLISTED:
        state.meta_errors.append(131030)
        return _meta_error(400, 131030, "Recipient phone number not in allowed list")
    if to == WINDOW_CLOSED:
        state.meta_errors.append(131047)
        return _meta_error(400, 131047, "Re-engagement message")
    state.sent.append((to, ((body or {}).get("text") or {}).get("body", "")))
    return T.httpx.Response(200, json={"messaging_product": "whatsapp", "contacts": [{"input": to, "wa_id": to}],
                                       "messages": [{"id": "wamid.SIMOK"}]})


def _openrouter_response(state: State, request):
    body = json.loads(request.content.decode("utf-8"))
    state.or_requests.append({"model": body.get("model"), "auth": request.headers.get("authorization"),
                              "messages": body.get("messages") or [], "max_tokens": body.get("max_tokens")})
    step = state.or_steps.pop(0) if state.or_steps else ok_step()
    if callable(step):
        step = step(body)
    if step["kind"] == "status":
        return T.httpx.Response(step["code"], text=step.get("body", "{}"))
    if step["kind"] == "raise":
        raise T.httpx.ConnectError("simulated connection reset", request=request)
    return T.httpx.Response(200, headers={"content-type": "text/event-stream"},
                            content="\n".join(step["lines"]).encode("utf-8"))


def _dispatch(state: State, request):
    host = request.url.host
    if host == "openrouter.ai":
        return _openrouter_response(state, request)
    if host == "graph.facebook.com":
        return _meta_response(state, request)
    return T.httpx.Response(404, text=f"unexpected host {host}")


@contextlib.contextmanager
def patched_network(state: State, fake_sleep: bool):
    httpx = T.httpx
    real_client, real_sleep = httpx.AsyncClient, asyncio.sleep

    class _Client(real_client):
        def __init__(self, *args, **kwargs):
            kwargs.pop("proxy", None)
            kwargs["transport"] = httpx.MockTransport(lambda request: _dispatch(state, request))
            super().__init__(*args, **kwargs)

    async def sleep_recorder(delay, *args, **kwargs):
        state.sleeps.append(delay)

    httpx.AsyncClient = _Client
    if fake_sleep:
        asyncio.sleep = sleep_recorder
    try:
        yield
    finally:
        httpx.AsyncClient = real_client
        asyncio.sleep = real_sleep


# ---------------------------------------------------------------------------
# Supabase side: in-memory emulator + a yield point at every query + outage switch
# ---------------------------------------------------------------------------

@types.coroutine
def _yield_once():
    yield


class _YieldingBuilder:
    def __init__(self, builder):
        self._b = builder

    def __getattr__(self, name):
        attr = getattr(self._b, name)
        if name == "execute":
            async def run():
                await _yield_once()
                return await attr()
            return run

        def chain(*args, **kwargs):
            attr(*args, **kwargs)
            return self
        return chain


class _YieldingClient:
    def __init__(self, inner):
        self._inner = inner

    def table(self, name):
        return _YieldingBuilder(self._inner.table(name))


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------

LOG_TAGS = (("could not parse webhook payload", "parse_failed"), ("no store found", "store_missing"),
            ("Supabase lookup failed", "store_lookup_failed"), ("conversation-window lookup failed", "window_lookup_failed"),
            ("failed to update conversation token count", "token_update_failed"), ("is too long", "too_long"),
            ("over its", "cap_block"), ("Meta send failed", "meta_send_failed"), ("Meta send succeeded", "meta_send_ok"),
            ("OUT OF CREDIT", "ai_402"), ("OpenRouter models failed", "ai_all_failed"),
            ("call_ai failed unexpectedly", "call_ai_crash"))


def kind_of(reply: str) -> str:
    footer = T.wh.BOLTANE_BRANDING_FOOTER
    has_footer = reply.endswith(footer)
    base = reply[: -len(footer)] if has_footer else reply
    known = ((T.wh.MONTHLY_LIMIT_REACHED_MESSAGE, "limit"), (T.wh.MESSAGE_TOO_LONG_REPLY, "too_long"),
             (T.ac.GRACEFUL_FAILURE_REPLY, "graceful"), (AI_REPLY, "ai"))
    label = next((name for ref, name in known if base == ref), "text")
    return label + ("+footer" if has_footer else "")


async def run_scenario(sc: Sc) -> dict:
    state = State(sc)
    db = T.MemoryDB()
    store_id = await db.insert_store(**{**DEFAULT_STORE, **sc.store})
    client = _YieldingClient(db.client())
    if sc.outage:
        client = s5._Outage(client, sc.outage)
    T.sc._client, T.sc._init_lock, T.sc.CACHE_CLIENT_ACROSS_CALLS = client, None, True

    collector = s5._Collect()
    loggers = [logging.getLogger(n) for n in ("boltane.webhook", "boltane.ai_client", "boltane.meta_client")]
    old_levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.DEBUG)
        lg.addHandler(collector)

    crashed = None
    try:
        with patched_network(state, sc.fake_sleep):
            for item in sc.deliver:
                if isinstance(item, tuple) and item[0] == "concurrent":
                    await asyncio.gather(*(T.wh._process_message(p) for p in item[1]))
                else:
                    await T.wh._process_message(item)
    except Exception as exc:  # noqa: BLE001 — a crash is an observation
        crashed = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        for lg, level in zip(loggers, old_levels):
            lg.removeHandler(collector)
            lg.setLevel(level)

    store = await db.get_store(store_id)
    window = await db.get_window(store_id, sc.customer)
    return {
        "sent": [kind_of(t) for _, t in state.sent],
        "sent_texts": [t for _, t in state.sent],
        "meta_calls": len(state.meta_requests),
        "meta_errors": state.meta_errors,
        "meta_violations": state.meta_violations,
        "or_requests": len(state.or_requests),
        "or_user": [r["messages"][-1]["content"] for r in state.or_requests if r["messages"]],
        "or_roles": [[m["role"] for m in r["messages"]] for r in state.or_requests],
        "or_system": [r["messages"][0]["content"] for r in state.or_requests if r["messages"]],
        "or_models": [r["model"] for r in state.or_requests],
        "or_auth": [r["auth"] for r in state.or_requests],
        "or_max_tokens": [r["max_tokens"] for r in state.or_requests],
        "count": store["monthly_conversation_count"],
        "remaining": store["conversations_remaining"],
        "token_count": window["token_count"] if window else None,
        "window_rows": await db.count_windows(store_id, sc.customer),
        "sleeps": state.sleeps,
        "logs": sorted({tag for text_, tag in LOG_TAGS for m in collector.messages if text_ in m}),
        "crashed": crashed,
    }


def judge(sc: Sc, obs: dict) -> tuple[str, list]:
    mismatches = []
    for key, want in {**DEFAULT_EXPECT, **sc.expect}.items():
        got = obs.get(key)
        ok = want(got) if callable(want) else got == want
        if not ok:
            mismatches.append((key, "<predicate>" if callable(want) else want, got))
    if mismatches:
        return "FAIL", mismatches
    if sc.gap and (sc.gap_if is None or sc.gap_if(obs)):
        return "GAP", []
    return "PASS", []


def printable(value) -> str:
    text_ = json.dumps(value, ensure_ascii=False)
    return text_ if len(text_) <= 160 else text_[:157] + "..."


async def run_all(selected: list) -> list:
    results = []
    for sc in selected:
        obs = await run_scenario(sc)
        verdict, mismatches = judge(sc, obs)
        results.append({"id": sc.sid, "group": sc.group, "title": sc.title, "verdict": verdict, "gap": sc.gap,
                        "mismatches": [(k, str(w), g) for k, w, g in mismatches], "observed": obs})
    return results


# ---------------------------------------------------------------------------
# Sabotage: the suite must FAIL when the engine is deliberately broken
# ---------------------------------------------------------------------------

MUTATIONS = (
    ("wh", "input-length cap removed", "if estimated_input_tokens > MAX_INPUT_TOKENS_PER_MESSAGE:", "if False:"),
    ("wh", "monthly cap never blocks", "if is_api_free and is_new_conversation and conversation_count >= cap:", "if False:"),
    ("wh", "monthly counter never increments",
     "new_count = conversation_count + 1 if is_new_conversation else conversation_count", "new_count = conversation_count"),
    ("wh", "low-tier mention instruction dropped", "system_prompt += BOLTANE_MENTION_INSTRUCTION", "pass"),
    ("wh", "low-tier branding footer dropped", "reply += BOLTANE_BRANDING_FOOTER", "pass"),
    ("ac", "phone numbers no longer masked", "masked = mask_phone_numbers(customer_message)",
     'masked = type("M", (), {"masked_text": customer_message, "restore": lambda self, t: t})()'),
    ("ac", "masked numbers not restored in the reply", "return masked.restore(reply_text), model, tokens_used",
     "return reply_text, model, tokens_used"),
    ("ac", "no network retry", "NETWORK_RETRY_DELAYS_SECONDS = [2.0, 4.0]", "NETWORK_RETRY_DELAYS_SECONDS = []"),
    ("ac", "BYOK key ignored (platform key always used)",
     "return store_api_key or get_settings().OPENROUTER_DEFAULT_KEY", "return get_settings().OPENROUTER_DEFAULT_KEY"),
    ("ac", "max_tokens dropped from the OpenRouter request", '"max_tokens": MAX_OUTPUT_TOKENS_PER_MESSAGE,', ""),
    ("mc", "Meta request body: wrong messaging_product", '"messaging_product": "whatsapp",', '"messaging_product": "wa",'),
    ("mc", "Meta request: wrong Authorization scheme", '"Authorization": f"Bearer {access_token}",',
     '"Authorization": f"Token {access_token}",'),
    ("mc", "Meta request: reply text truncated to 20 characters",
     '"text": {"preview_url": False, "body": message},', '"text": {"preview_url": False, "body": message[:20]},'),
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


async def _token_total_overwritten(store_id, customer_phone, tokens_used, token_count_before):
    """Sabotage stand-in for add_conversation_tokens: overwrites the running total instead of adding to it."""
    client = await T.sc._get_client()
    await (client.table("conversation_windows").update({"token_count": tokens_used})
           .eq("store_id", store_id).eq("customer_phone", customer_phone).execute())


def run_sabotage(selected: list) -> int:
    originals = {"wh": T.wh, "ac": T.ac, "mc": T.mc}
    bound = {name: getattr(T.wh, name) for name in ("call_ai", "send_whatsapp", "add_conversation_tokens")}

    def restore():
        T.wh = originals["wh"]
        for name, fn in bound.items():
            setattr(T.wh, name, fn)

    baseline = asyncio.run(run_all(selected))
    base_fail = [r["id"] for r in baseline if r["verdict"] == "FAIL"]
    print(f"baseline (unmodified engine): {sum(1 for r in baseline if r['verdict'] == 'FAIL')} FAIL "
          f"{base_fail if base_fail else ''}")

    rows = []
    plan = [(owner, label, old, new) for owner, label, old, new in MUTATIONS]
    plan.append(("patch", "conversation token total overwritten instead of accumulated", None, None))
    for owner, label, old, new in plan:
        try:
            if owner == "wh":
                T.wh = _mutant(originals["wh"], old, new)
            elif owner == "ac":
                T.wh.call_ai = _mutant(originals["ac"], old, new).call_ai
            elif owner == "mc":
                T.wh.send_whatsapp = _mutant(originals["mc"], old, new).send_whatsapp
            else:
                T.wh.add_conversation_tokens = _token_total_overwritten
            results = asyncio.run(run_all(selected))
            failed = [r["id"] for r in results if r["verdict"] == "FAIL"]
        except Exception as exc:  # noqa: BLE001 — blowing up also counts as noticing
            failed = [f"exception:{type(exc).__name__}"]
        finally:
            restore()
        rows.append((label, failed))

    missed = 0
    for label, failed in rows:
        if failed:
            shown = ", ".join(failed[:6]) + (" ..." if len(failed) > 6 else "")
            print(f"  DETECTED      {label}   -> FAIL in: {shown}")
        else:
            missed += 1
            print(f"  NOT DETECTED  {label}   (the suite stayed green on a broken engine)")
    print(f"\n{len(rows) - missed}/{len(rows)} deliberate breakages detected; baseline had {len(base_fail)} FAIL")
    return 1 if missed or base_fail else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 6b Meta replay simulator (see the module docstring).")
    parser.add_argument("--only", help="comma-separated scenario ids, e.g. A1,B6")
    parser.add_argument("--list", action="store_true", help="list the scenarios and exit")
    parser.add_argument("--sabotage", action="store_true",
                        help="break the engine on purpose (14 ways) and confirm the suite notices every time")
    parser.add_argument("--show", help="comma-separated scenario ids whose full observation is printed")
    parser.add_argument("--out", default="stage6_meta_sim_report.json")
    args = parser.parse_args()

    everything = scenarios()
    if args.list:
        for sc in everything:
            print(f"{sc.sid:<4} {sc.group:<13} {sc.title}")
        return 0
    wanted = {part.strip() for part in args.only.split(",")} if args.only else None
    selected = [sc for sc in everything if wanted is None or sc.sid in wanted]
    if wanted and not selected:
        sys.exit(f"no scenario matches {sorted(wanted)}; use --list")

    s5.load_tree("workers")
    if args.sabotage:
        return run_sabotage(selected)
    results = asyncio.run(run_all(selected))

    for r in results:
        print(f"{r['verdict']:<5} {r['id']:<4} {r['group']:<13} {r['title']}")
        for key, want, got in r["mismatches"]:
            print(f"        {key}: expected {want}, got {printable(got)}")
    for r in results:
        if args.show and r["id"] in {part.strip() for part in args.show.split(",")}:
            print(f"\n--- {r['id']} observed:\n{json.dumps(r['observed'], indent=1, ensure_ascii=False)}")
    counts = {v: sum(1 for r in results if r["verdict"] == v) for v in ("PASS", "GAP", "FAIL")}
    print(f"\n{len(results)} scenarios: {counts['PASS']} PASS, {counts['GAP']} GAP, {counts['FAIL']} FAIL")

    gaps = {}
    for r in results:
        if r["verdict"] == "GAP":
            gaps.setdefault(r["gap"], []).append(r["id"])
    if gaps:
        print("\nKNOWN GAPS (current behaviour, same on Render; the list worth deciding before real customers):")
        for number, (impact, ids) in enumerate(sorted(gaps.items(), key=lambda kv: kv[1][0]), start=1):
            print(f" {number}. [{', '.join(ids)}] {impact}")

    Path(HERE / args.out).write_text(json.dumps({"scenarios": results}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
