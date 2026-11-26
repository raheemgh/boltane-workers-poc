"""normalizer - turns one Meta webhook delivery into typed events. Never raises.

Layer:   domain
Stage:   V2-02A
Flags:   none
Raises:  never
Budget:  0 subrequests; one pass over the payload, no regex, no deep copies; CPU on a Worker not yet measured (V2-11)
Tests:   tests/v2/inbound/test_normalizer.py
"""

import hashlib
import json
from dataclasses import replace

from app.v2.core.errors import ErrorCode
from app.v2.core.events import (
    KIND_MESSAGE,
    KIND_STATUS,
    SCHEMA_VERSION,
    AccountEvent,
    Delivery,
    InboundEvent,
    Issue,
    Who,
)
from app.v2.core.safe import as_bool, as_dict, as_int, as_list, clean_text, compact
from app.v2.inbound.content import read_content
from app.v2.inbound.identity import MESSAGE_KEYS, STATUS_KEYS, clean_bsuid, clean_phone, read_who

WHATSAPP_OBJECT = "whatsapp_business_account"
MAX_EVENTS = 100  # Meta batches a handful; a delivery with more is cut here and the rest is reported as issues
MAX_ACCOUNT_CHARS = 8192  # a bigger account event keeps only its field names


def _dump(obj: object) -> str | None:
    """Compact canonical JSON, or None if the value is too deep to dump (cannot happen for parsed JSON)."""
    try:
        return json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str)
    except (RecursionError, TypeError, ValueError):
        return None


# --- messages, statuses, account events ----------------------------------------------------------------------


def _extras(message: dict) -> dict:
    """A reply or forward, the ad that started the chat (ctwa_clid), and the group id: only when present."""
    out: dict = {}
    context = as_dict(message.get("context"))
    reply_to = clean_text(context.get("id"), 255)
    if reply_to:
        out["reply_to"] = reply_to
    if context.get("forwarded") is True or context.get("frequently_forwarded") is True:
        out["forwarded"] = True
    referral = as_dict(message.get("referral"))
    ad = compact({"ctwa_clid": clean_text(referral.get("ctwa_clid"), 256),
                   "source_id": clean_text(referral.get("source_id"), 64),
                   "source_type": clean_text(referral.get("source_type"), 16)})
    if ad:
        out["referral"] = ad
    group = clean_text(message.get("group_id"), 64)
    if group:
        out["group_id"] = group
    return out


def _system_who(who: Who, change: dict) -> Who:
    """A system message names the user whose id changed in system.user_id / system.wa_id (the new values).

    Meta omits `from` when the BSUID changed and no phone can be shared, so the system block is the only identity.
    """
    return replace(
        who,
        phone=clean_phone(change.get("wa_id")) or who.phone,
        bsuid=clean_bsuid(change.get("user_id")) or who.bsuid,
        parent_bsuid=clean_bsuid(change.get("parent_user_id")) or who.parent_bsuid,
    )


def _message_event(message: dict, pnid: str, contacts: list) -> InboundEvent | None:
    wamid = clean_text(message.get("id"), 255)
    who = read_who(message, contacts, MESSAGE_KEYS)
    if message.get("type") == "system":
        who = _system_who(who, as_dict(message.get("system")))
    if not wamid or not (who.phone or who.bsuid):
        return None  # no id to dedupe on, or nobody to answer: not an event we can keep
    data = {"v": SCHEMA_VERSION, "kind": KIND_MESSAGE, "wamid": wamid, "phone_number_id": pnid,
            "from": who.to_dict()}
    stamp = as_int(message.get("timestamp"))
    if stamp is not None:
        data["ts"] = stamp
    data.update(read_content(message))
    data.update(_extras(message))
    return InboundEvent(KIND_MESSAGE, wamid, pnid, who.phone, who.bsuid, data)


def _status_errors(status: dict) -> list[dict]:
    out = []
    for item in as_list(status.get("errors"))[:3]:
        row = as_dict(item)
        code = row.get("code")
        if isinstance(code, int) and not isinstance(code, bool):
            out.append(compact({"code": code, "title": clean_text(row.get("title"), 160)}))
    return out


def _pricing(status: dict) -> dict:
    pricing = as_dict(status.get("pricing"))
    return compact({
        "billable": as_bool(pricing.get("billable")),
        "model": clean_text(pricing.get("pricing_model"), 16),
        "type": clean_text(pricing.get("type"), 24),
        "category": clean_text(pricing.get("category"), 32),
    })


def _status_event(status: dict, pnid: str, contacts: list) -> InboundEvent | None:
    msg_id = clean_text(status.get("id"), 255)
    state = clean_text(status.get("status"), 24)
    if not msg_id or not state:
        return None
    who = read_who(status, contacts, STATUS_KEYS)
    data = {"v": SCHEMA_VERSION, "kind": KIND_STATUS, "wamid": msg_id, "status": state,
            "phone_number_id": pnid, "to": who.to_dict()}
    stamp = as_int(status.get("timestamp"))
    if stamp is not None:
        data["ts"] = stamp
    errors = _status_errors(status)
    if errors:
        data["errors"] = errors
    pricing = _pricing(status)
    if pricing:
        data["pricing"] = pricing
    if status.get("recipient_type") == "group":
        data["group_id"] = clean_text(status.get("recipient_id"), 64)
    # One message gets several statuses (sent, delivered, read): the status is part of the dedupe key.
    return InboundEvent(KIND_STATUS, f"{msg_id}:{state}", pnid, who.phone, who.bsuid, data)


def _phone_number_id(value: dict) -> str | None:
    raw = as_dict(value.get("metadata")).get("phone_number_id")
    text = str(raw) if isinstance(raw, int) and not isinstance(raw, bool) else raw
    ok = isinstance(text, str) and text.isascii() and text.isdigit() and 5 <= len(text) <= 30
    return text if ok else None


def _account_event(entry: dict, field: str, value: dict) -> AccountEvent:
    text = _dump(value)
    small = text is not None and len(text) <= MAX_ACCOUNT_CHARS
    kept = value if small else {"truncated": True, "keys": sorted(str(key) for key in value)[:20]}
    seed = f"{entry.get('id')}|{entry.get('time')}|{field}|{text}"
    key = "acct:" + hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:32]
    return AccountEvent(field, _phone_number_id(value), key, kept)


# --- the walk ------------------------------------------------------------------------------------------------


class _Acc:
    """Collects what one delivery holds. Private to this module."""

    def __init__(self) -> None:
        self.events: list[InboundEvent] = []
        self.account: list[AccountEvent] = []
        self.issues: list[Issue] = []
        self.numbers: list[str] = []
        self.seen: set[str] = set()

    def issue(self, where: str) -> None:
        self.issues.append(Issue(ErrorCode.INBOUND_MALFORMED, where))

    def add(self, event: InboundEvent | None, where: str) -> None:
        if event is None:
            self.issue(where)
        elif event.key in self.seen:
            return  # the same id twice inside one delivery: keep the first
        elif len(self.events) >= MAX_EVENTS:
            self.issue(where + " over-limit")
        else:
            self.seen.add(event.key)
            self.events.append(event)

    def note_number(self, pnid: str) -> None:
        if pnid not in self.numbers:
            self.numbers.append(pnid)

    def finish(self) -> Delivery:
        return Delivery(tuple(self.events), tuple(self.account), tuple(self.issues), tuple(self.numbers))


def _read_messages_field(acc: _Acc, value: dict, where: str) -> None:
    pnid = _phone_number_id(value)
    if pnid is None:
        acc.issue(where + ".value.metadata")
        return
    acc.note_number(pnid)
    contacts = as_list(value.get("contacts"))
    for index, message in enumerate(as_list(value.get("messages"))):
        acc.add(_message_event(as_dict(message), pnid, contacts), f"{where}.messages[{index}]")
    for index, status in enumerate(as_list(value.get("statuses"))):
        acc.add(_status_event(as_dict(status), pnid, contacts), f"{where}.statuses[{index}]")


def _read_change(acc: _Acc, entry: dict, change: dict, where: str) -> None:
    field = clean_text(change.get("field"), 64)
    value = as_dict(change.get("value"))
    if field == "messages":
        _read_messages_field(acc, value, where)
    elif field:
        acc.account.append(_account_event(entry, field, value))
    else:
        acc.issue(where + ".field")


def normalize(payload: object) -> Delivery:
    """One Meta delivery in, typed events out. Never raises: what cannot be read becomes an Issue."""
    body = as_dict(payload)
    if body.get("object") != WHATSAPP_OBJECT:
        return Delivery(issues=(Issue(ErrorCode.INBOUND_MALFORMED, "object"),))
    acc = _Acc()
    for e_index, raw_entry in enumerate(as_list(body.get("entry"))):
        entry = as_dict(raw_entry)
        for c_index, change in enumerate(as_list(entry.get("changes"))):
            _read_change(acc, entry, as_dict(change), f"entry[{e_index}].changes[{c_index}]")
    return acc.finish()
