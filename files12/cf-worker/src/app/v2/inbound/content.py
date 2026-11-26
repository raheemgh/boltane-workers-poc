"""content - reads what one Meta message carries: text, reply title, location, media, reaction, system, ...

Layer:   domain
Stage:   V2-02A
Flags:   none
Raises:  never
Budget:  no regex; a few dict reads per message
Tests:   tests/v2/inbound/test_content.py
"""

from app.v2.core.events import (
    TYPE_AUDIO,
    TYPE_BUTTON,
    TYPE_CONTACTS,
    TYPE_DOCUMENT,
    TYPE_IMAGE,
    TYPE_INTERACTIVE,
    TYPE_LOCATION,
    TYPE_ORDER,
    TYPE_REACTION,
    TYPE_STICKER,
    TYPE_SYSTEM,
    TYPE_TEXT,
    TYPE_UNSUPPORTED,
    TYPE_VIDEO,
    TYPE_WELCOME,
)
from app.v2.core.safe import as_bool, as_dict, as_list, as_number, clean_text, compact
from app.v2.inbound.identity import clean_bsuid, clean_phone


def meta_errors(message: dict) -> list[int]:
    codes = [as_dict(item).get("code") for item in as_list(message.get("errors"))]
    return [code for code in codes if isinstance(code, int) and not isinstance(code, bool)][:3]


def unsupported(message: dict) -> dict:
    """Anything we do not read, and any known type whose body is missing (Meta then sends an errors array)."""
    out: dict = {"type": TYPE_UNSUPPORTED}
    original = clean_text(message.get("type"), 40)
    if original and original != TYPE_UNSUPPORTED:
        out["orig_type"] = original
    codes = meta_errors(message)
    if codes:
        out["meta_errors"] = codes
    return out


def _read_text(message: dict) -> dict:
    body = clean_text(as_dict(message.get("text")).get("body"))
    return {"type": TYPE_TEXT, "text": body} if body is not None else unsupported(message)


def _read_interactive(message: dict) -> dict:
    inter = as_dict(message.get("interactive"))
    kind = inter.get("type")
    reply = as_dict(inter.get(kind)) if kind in ("button_reply", "list_reply") else {}
    title = clean_text(reply.get("title"), 200)
    if title is None:  # an error payload without a body (Meta 131000), or a kind we do not read (flows)
        return unsupported(message)
    info = compact({"kind": kind, "id": clean_text(reply.get("id"), 256),
                     "description": clean_text(reply.get("description"), 200) or None})
    return {"type": TYPE_INTERACTIVE, "text": title, "interactive": info}


def _read_button(message: dict) -> dict:
    button = as_dict(message.get("button"))
    text = clean_text(button.get("text"), 200)
    if text is None:
        return unsupported(message)
    info = compact({"kind": "template_button", "id": clean_text(button.get("payload"), 256)})
    return {"type": TYPE_BUTTON, "text": text, "interactive": info}


def _read_location(message: dict) -> dict:
    where = as_dict(message.get("location"))
    lat, lon = as_number(where.get("latitude")), as_number(where.get("longitude"))
    if lat is None or lon is None:
        return unsupported(message)
    name, address = clean_text(where.get("name"), 120), clean_text(where.get("address"), 240)
    place = address if name and address and name in address else ", ".join(p for p in (name, address) if p)
    text = f"[location] {place} ({lat}, {lon})" if place else f"[location] ({lat}, {lon})"
    place_info = compact({"lat": lat, "lon": lon, "name": name, "address": address})
    return {"type": TYPE_LOCATION, "text": text, "location": place_info}


def _read_reaction(message: dict) -> dict:
    reaction = as_dict(message.get("reaction"))
    info = compact({"to": clean_text(reaction.get("message_id"), 255), "emoji": clean_text(reaction.get("emoji"), 16)})
    return {"type": TYPE_REACTION, "reaction": info}


def _media_reader(kind: str):
    """Reader for image, audio, video, document and sticker. The caption, if any, is the customer's text."""

    def read(message: dict) -> dict:
        media = as_dict(message.get(kind))
        info = compact({
            "id": clean_text(media.get("id"), 256),
            "mime": clean_text(media.get("mime_type"), 100),
            "filename": clean_text(media.get("filename"), 200),
            "voice": as_bool(media.get("voice")),
            "animated": as_bool(media.get("animated")),
        })
        out: dict = {"type": kind, "media": info}
        caption = clean_text(media.get("caption"))
        if caption:
            out["text"] = caption
        return out

    return read


def _shared_phone(message: dict, cards: list) -> str | None:
    """A phone the user shared on purpose (the request-contact-info button): it links a BSUID to a number."""
    asked = message.get("origin") == "contact_request" or any(
        as_dict(card).get("origin") == "contact_request" for card in cards
    )
    if not asked:
        return None
    for card in cards:
        for entry in as_list(as_dict(card).get("phones")):
            phone = clean_phone(as_dict(entry).get("wa_id")) or clean_phone(as_dict(entry).get("phone"))
            if phone:
                return phone
    return None


def _read_contacts(message: dict) -> dict:
    cards = as_list(message.get("contacts"))
    out: dict = {"type": TYPE_CONTACTS, "contacts_count": len(cards)}  # card contents belong to third parties
    shared = _shared_phone(message, cards)
    if shared:
        out["shared_phone"] = shared
    return out


def _read_system(message: dict) -> dict:
    change = as_dict(message.get("system"))
    info = compact({
        "type": clean_text(change.get("type"), 40),
        "user_id": clean_bsuid(change.get("user_id")),
        "previous_user_id": clean_bsuid(change.get("previous_user_id")),
        "parent_user_id": clean_bsuid(change.get("parent_user_id")),
        "previous_parent_user_id": clean_bsuid(change.get("previous_parent_user_id")),
        "phone": clean_phone(change.get("wa_id")),
    })
    return {"type": TYPE_SYSTEM, "system": info}


def _read_order(_message: dict) -> dict:
    return {"type": TYPE_ORDER}


def _read_welcome(_message: dict) -> dict:
    return {"type": TYPE_WELCOME}


_READERS = {
    "text": _read_text,
    "interactive": _read_interactive,
    "button": _read_button,
    "location": _read_location,
    "reaction": _read_reaction,
    "contacts": _read_contacts,
    "system": _read_system,
    "order": _read_order,
    "request_welcome": _read_welcome,
    "image": _media_reader(TYPE_IMAGE),
    "audio": _media_reader(TYPE_AUDIO),
    "video": _media_reader(TYPE_VIDEO),
    "document": _media_reader(TYPE_DOCUMENT),
    "sticker": _media_reader(TYPE_STICKER),
}


def read_content(message: dict) -> dict:
    """What the message carries, by its Meta type: {"type": ..., "text": ..., details}. Unknown types: unsupported."""
    kind = message.get("type")
    reader = _READERS.get(kind) if isinstance(kind, str) else None
    return (reader or unsupported)(message)
