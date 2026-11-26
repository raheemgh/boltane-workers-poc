"""identity - reads who sent a message or who a status went to: phone, BSUID, parent BSUID, username, name.

Layer:   domain
Stage:   V2-02A
Flags:   none
Raises:  never
Budget:  no regex; a handful of dict reads per message
Tests:   tests/v2/inbound/test_identity.py
"""

from app.v2.core.events import Who
from app.v2.core.safe import as_dict, clean_text

MIN_PHONE, MAX_PHONE = 6, 15  # the same range as the wa_customers_phone_check constraint
MAX_BSUID = 144  # 2 letters + "." + up to 128 characters (Meta), with room for the "ENT." of a parent id

# Which fields of a message / of a status carry the identity (Meta docs: business-scoped user IDs).
MESSAGE_KEYS = ("from", "from_user_id", "from_parent_user_id")
STATUS_KEYS = ("recipient_id", "recipient_user_id", "recipient_parent_user_id")


def clean_phone(value: object) -> str | None:
    """Digits only, 6 to 15 of them. A leading "+" is tolerated and dropped. Group ids and BSUIDs are not phones."""
    if not isinstance(value, str):
        return None
    digits = value[1:] if value.startswith("+") else value
    ok = digits.isascii() and digits.isdigit() and MIN_PHONE <= len(digits) <= MAX_PHONE
    return digits if ok else None


def clean_bsuid(value: object) -> str | None:
    """A BSUID: two capital letters, a dot, then alphanumeric parts (a parent id has ENT as a part)."""
    if not isinstance(value, str) or not value.isascii() or len(value) > MAX_BSUID:
        return None
    country, dot, rest = value.partition(".")
    if not dot or len(country) != 2 or not country.isalpha() or not country.isupper():
        return None
    return value if all(part.isalnum() for part in rest.split(".")) else None


def split_identifier(value: object) -> tuple[str | None, str | None]:
    """A field that used to hold a phone number may now hold a BSUID: sort it into (phone, bsuid)."""
    phone = clean_phone(value)
    return (phone, None) if phone else (None, clean_bsuid(value))


def _contact_for(contacts: list, phone: str | None, bsuid: str | None) -> dict:
    """The contacts[] entry that describes this sender: by BSUID, else by phone, else the only one."""
    rows = [row for row in contacts if isinstance(row, dict)]
    for row in rows:
        if bsuid and clean_bsuid(row.get("user_id")) == bsuid:
            return row
    for row in rows:
        if phone and split_identifier(row.get("wa_id"))[0] == phone:
            return row
    return rows[0] if len(rows) == 1 else {}


def read_who(source: dict, contacts: list, keys: tuple[str, str, str]) -> Who:
    """Identity from the fields named in `keys` of `source`, completed from the matching contacts[] entry.

    Meta may omit the phone for a user who adopted a username, and always sends the BSUID. A phone is therefore
    optional, and a BSUID found in a field that used to hold a phone is still recognised.
    """
    phone_key, bsuid_key, parent_key = keys
    phone, bsuid = split_identifier(source.get(phone_key))
    bsuid = clean_bsuid(source.get(bsuid_key)) or bsuid or clean_bsuid(source.get("user_id"))
    parent = clean_bsuid(source.get(parent_key)) or clean_bsuid(source.get("parent_user_id"))
    contact = _contact_for(contacts, phone, bsuid)
    wa_phone, wa_bsuid = split_identifier(contact.get("wa_id"))
    profile = as_dict(contact.get("profile"))
    return Who(
        phone=phone or wa_phone,
        bsuid=bsuid or wa_bsuid or clean_bsuid(contact.get("user_id")),
        parent_bsuid=parent or clean_bsuid(contact.get("parent_user_id")),
        username=clean_text(profile.get("username"), 64),
        name=clean_text(profile.get("name"), 80),
    )
