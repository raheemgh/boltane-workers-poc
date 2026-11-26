"""entry - the one door V1 uses to offer a verified webhook delivery to V2: store it, or step aside.

Layer:   pipeline
Stage:   V2-02B
Flags:   v2_pipeline
Raises:  never for database failures (they become verdicts); a bug propagates to the guard in V1's receive_message
Budget:  flag off: 0 subrequests and one set lookup. Flag on: 1 subrequest (inbox_accept), one pass over the payload
Tests:   tests/v2/pipeline/test_entry.py
"""

import logging
from dataclasses import dataclass

from app.v2.core.errors import ErrorCode, V2Error
from app.v2.core.flags import FlagSettings, is_enabled
from app.v2.inbound import inbox
from app.v2.inbound.normalizer import normalize
from app.v2.pipeline import build
from app.v2.ports.db import DbPort

FLAG = "v2_pipeline"
logger = logging.getLogger("boltane.v2.entry")
_reported: set[str] = set()  # configuration problems this isolate already logged


@dataclass(frozen=True)
class Verdict:
    status: int  # the HTTP status V1 answers Meta with
    body: dict  # its JSON body: codes and counts only, never customer text
    note: str  # one safe line for V1's log


def _report_config(settings: FlagSettings) -> None:
    """A misspelled flag opens nothing (ADR-2), so say so once instead of staying silent."""
    problems = [(ErrorCode.CONFIG_FLAG_UNKNOWN, name) for name in settings.unknown_flags]
    problems += [(ErrorCode.CONFIG_INVALID, entry) for entry in settings.invalid_allowlist]
    for code, name in problems:
        if name not in _reported:
            _reported.add(name)
            logger.warning("%s %s", code.value, name)


def _note(what: str, number: str, plan: inbox.Plan, result: inbox.Accepted | None = None) -> str:
    parts = [f"v2 {what} pn={number} kept={len(plan.items)} skipped={plan.skipped}"]
    if result is not None:
        parts.append(f"inserted={result.inserted} dup={result.duplicates} dropped={result.rejected}")
    return " ".join(parts)


def _failed(err: V2Error, number: str, plan: inbox.Plan) -> Verdict:
    """A refused write is the data's fault: answer 200 or Meta repeats it for days. Anything else: 503, Meta retries."""
    what = f"db-error {err.code.value}"
    if err.code is ErrorCode.DB_CONSTRAINT:
        return Verdict(200, {"status": "dropped", "code": err.code.value}, _note(what, number, plan))
    return Verdict(503, {"status": "retry", "code": err.code.value}, _note(what, number, plan))


async def _store(db: DbPort, number: str, plan: inbox.Plan) -> Verdict | None:
    try:
        result = await inbox.accept(db, number, plan.items)
    except V2Error as err:
        return _failed(err, number, plan)
    if not result.store_found:
        body = {"status": "ignored", "code": ErrorCode.INBOUND_STORE_UNKNOWN.value}
        return Verdict(200, body, _note("unknown-store", number, plan))
    if not result.routed:
        return None  # the store switched V2 off: V1 handles the delivery
    body = {"status": "accepted", "inserted": result.inserted, "duplicates": result.duplicates}
    if result.rejected:
        body["dropped"] = result.rejected
    return Verdict(200, body, _note("accepted", number, plan, result))


async def route_delivery(payload: object, env: object, ctx: object = None, *,
                         db: DbPort | None = None) -> Verdict | None:
    """Offer one verified delivery to V2. None means "not mine": V1 handles it exactly as before.

    `ctx` (the Worker's waitUntil handle) is not used yet: V2-08 needs it, and taking it now keeps V1's call stable.
    `db` is for tests; the Worker builds the database port from its env.
    """
    settings = build.settings_from(env)
    _report_config(settings)
    if FLAG not in settings.enabled or not (settings.allow_all or settings.allowlist):
        return None
    delivery = normalize(payload)
    if len(delivery.phone_number_ids) != 1:  # ADR-10, item 5: a delivery with several numbers stays with V1
        return None
    number = delivery.phone_number_ids[0]
    if not is_enabled(FLAG, settings, phone_number_id=number):
        return None
    plan = inbox.plan_items(delivery)
    if not plan.items:  # only sent/read statuses, group traffic, account events
        return Verdict(200, {"status": "ignored"}, _note("ignored", number, plan))
    return await _store(db or build.db_from(env), number, plan)
