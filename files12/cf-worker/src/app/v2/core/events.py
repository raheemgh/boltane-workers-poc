"""events - the typed result of reading one Meta webhook delivery: events, account events, issues.

Layer:   core
Stage:   V2-02A
Flags:   none
Raises:  never
Budget:  none
Tests:   tests/v2/core/test_events.py
"""

from dataclasses import dataclass

from app.v2.core.errors import ErrorCode

SCHEMA_VERSION = 1  # data["v"] of every stored event; raise it when the stored shape changes

# inbound_events.kind. Plain text on purpose: the set grows (ADR-3).
KIND_MESSAGE = "message"
KIND_STATUS = "status"

# Message types (data["type"]) after normalizing. Anything Meta sends that is not listed becomes "unsupported".
TYPE_TEXT = "text"
TYPE_INTERACTIVE = "interactive"  # a tapped reply button or list row: its title is the text
TYPE_BUTTON = "button"  # a tapped template quick-reply button
TYPE_LOCATION = "location"
TYPE_REACTION = "reaction"
TYPE_IMAGE = "image"
TYPE_AUDIO = "audio"
TYPE_VIDEO = "video"
TYPE_DOCUMENT = "document"
TYPE_STICKER = "sticker"
TYPE_CONTACTS = "contacts"
TYPE_ORDER = "order"
TYPE_SYSTEM = "system"  # the user changed phone number or BSUID: identity news, not a question
TYPE_WELCOME = "request_welcome"
TYPE_UNSUPPORTED = "unsupported"
MESSAGE_TYPES = frozenset(
    {TYPE_TEXT, TYPE_INTERACTIVE, TYPE_BUTTON, TYPE_LOCATION, TYPE_REACTION, TYPE_IMAGE, TYPE_AUDIO,
     TYPE_VIDEO, TYPE_DOCUMENT, TYPE_STICKER, TYPE_CONTACTS, TYPE_ORDER, TYPE_SYSTEM, TYPE_WELCOME,
     TYPE_UNSUPPORTED}
)

# Delivery statuses Meta reports for a message we sent.
STATUS_SENT = "sent"
STATUS_DELIVERED = "delivered"
STATUS_READ = "read"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class Who:
    """Who a message came from (or a status went to). Any field may be missing: a phone is never assumed (ADR-6)."""

    phone: str | None = None  # digits only, no "+"
    bsuid: str | None = None  # business-scoped user id, e.g. US.13491208655302741918
    parent_bsuid: str | None = None  # e.g. US.ENT.11815799212886844830
    username: str | None = None
    name: str | None = None

    def to_dict(self) -> dict:
        pairs = (("phone", self.phone), ("bsuid", self.bsuid), ("parent_bsuid", self.parent_bsuid),
                 ("username", self.username), ("name", self.name))
        return {key: value for key, value in pairs if value}


@dataclass(frozen=True)
class InboundEvent:
    """One message or one status. `data` is exactly what is stored in inbound_events.event (JSON-ready)."""

    kind: str
    key: str  # the dedupe key = inbound_events.wamid. A message: its id. A status: "<message id>:<status>".
    phone_number_id: str
    phone: str | None
    bsuid: str | None
    data: dict


@dataclass(frozen=True)
class AccountEvent:
    """A webhook field that is not about one conversation (quality, template, account alerts, ...)."""

    field: str
    phone_number_id: str | None  # often absent: many account events belong to the WABA, not to a number
    key: str
    value: dict


@dataclass(frozen=True)
class Issue:
    """Something in the delivery we could not read. `where` is a path like entry[0].changes[0].messages[1]."""

    code: ErrorCode
    where: str


@dataclass(frozen=True)
class Delivery:
    events: tuple[InboundEvent, ...] = ()
    account: tuple[AccountEvent, ...] = ()
    issues: tuple[Issue, ...] = ()
    phone_number_ids: tuple[str, ...] = ()  # distinct, in order of appearance
