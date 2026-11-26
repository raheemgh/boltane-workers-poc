"""inbox - the durable inbox: which events are kept (ADR-10) and thin calls to the four database functions.

Layer:   domain
Stage:   V2-02B
Flags:   none
Raises:  V2Error from the port; DB_UNEXPECTED when the database answers in a shape this code does not know
Budget:  one subrequest per call; accept sends a whole delivery in one call
Tests:   tests/v2/inbound/test_inbox.py
"""

from dataclasses import dataclass

from app.v2.core.errors import ErrorCode, V2Error
from app.v2.core.events import KIND_MESSAGE, KIND_STATUS, STATUS_DELIVERED, STATUS_FAILED, Delivery, InboundEvent
from app.v2.core.safe import as_bool, as_dict, as_list, clean_text
from app.v2.ports.db import DbPort

# The defaults of the SQL functions (sql/migrations/002); tests/v2/schema/test_inbox_sql_static.py keeps them equal.
LOCK_SECONDS = 60  # longer than ctx.waitUntil lives (about 30 s, ADR-8)
PENDING_OLDER_THAN = 45
MAX_ATTEMPTS = 3
CLAIM_LIMIT = 10

KEPT_PENDING = "pending"  # a message: waits to be answered
KEPT_DONE = "done"  # a delivered or failed status: kept for the record and finished at once
KEPT_STATUSES = (STATUS_DELIVERED, STATUS_FAILED)


@dataclass(frozen=True)
class Plan:
    items: tuple[dict, ...]  # what inbox_accept receives
    skipped: int  # events the retention policy does not keep


def _kept_as(event: InboundEvent) -> str | None:
    """The status an event is stored with, or None if it is not kept (ADR-10, item 4)."""
    if event.data.get("group_id"):
        return None  # the bot is 1:1: group traffic is not stored
    if event.kind == KIND_MESSAGE:
        return KEPT_PENDING
    if event.kind == KIND_STATUS and event.data.get("status") in KEPT_STATUSES:
        return KEPT_DONE
    return None


def plan_items(delivery: Delivery) -> Plan:
    """Apply the retention policy to one delivery."""
    items, skipped = [], 0
    for event in delivery.events:
        kept = _kept_as(event)
        if kept is None:
            skipped += 1
            continue
        items.append({"wamid": event.key, "kind": event.kind, "from_phone": event.phone,
                      "from_bsuid": event.bsuid, "status": kept, "event": event.data})
    return Plan(tuple(items), skipped)


@dataclass(frozen=True)
class Row:
    wamid: str | None
    inserted: bool
    error: str | None  # a SQLSTATE when the database refused this event's data


@dataclass(frozen=True)
class Accepted:
    store_found: bool
    store_id: str | None
    routed: bool  # False: the store switched V2 off and nothing was stored
    rows: tuple[Row, ...]

    @property
    def inserted(self) -> int:
        return sum(1 for row in self.rows if row.inserted)

    @property
    def rejected(self) -> int:
        return sum(1 for row in self.rows if row.error)

    @property
    def duplicates(self) -> int:
        return len(self.rows) - self.inserted - self.rejected


def _row(raw: object) -> Row:
    body = as_dict(raw)
    return Row(clean_text(body.get("wamid"), 255), body.get("inserted") is True, clean_text(body.get("error"), 5))


def _parse(raw: object) -> Accepted:
    body = as_dict(raw)
    found, routed, results = as_bool(body.get("store_found")), as_bool(body.get("routed")), body.get("results")
    if found is None or routed is None or not isinstance(results, list):
        raise V2Error(ErrorCode.DB_UNEXPECTED, reason="inbox_accept answer")
    return Accepted(found, clean_text(body.get("store_id"), 64), routed, tuple(_row(item) for item in results))


async def accept(db: DbPort, phone_number_id: str, items: tuple[dict, ...]) -> Accepted:
    """Store the kept events of one delivery in one database call: new ones are inserted, repeats are not."""
    raw = await db.rpc("inbox_accept", {"p_phone_number_id": phone_number_id, "p_events": list(items)})
    return _parse(raw)


async def claim(db: DbPort, event_id: str, lock_seconds: int = LOCK_SECONDS) -> bool:
    """Take one event for processing. True only for the caller that got it."""
    return await db.rpc("inbox_claim", {"p_id": event_id, "p_lock_seconds": lock_seconds}) is True


async def finish(db: DbPort, event_id: str, status: str, error_code: str | None = None) -> bool:
    """Close a claimed event as done, failed or ignored. False if it was not being processed."""
    answer = await db.rpc("inbox_finish", {"p_id": event_id, "p_status": status, "p_error_code": error_code})
    return answer is True


async def claim_stale(db: DbPort, *, kind: str = KIND_MESSAGE, limit: int = CLAIM_LIMIT,
                      pending_older_than: int = PENDING_OLDER_THAN, lock_seconds: int = LOCK_SECONDS,
                      max_attempts: int = MAX_ATTEMPTS) -> list[dict]:
    """The reaper's call: take the stale events (rows of inbound_events); ones out of attempts are failed."""
    raw = await db.rpc("inbox_claim_stale", {"p_kind": kind, "p_limit": limit,
                                              "p_pending_older_than_seconds": pending_older_than,
                                              "p_lock_seconds": lock_seconds, "p_max_attempts": max_attempts})
    return [as_dict(row) for row in as_list(raw)]
