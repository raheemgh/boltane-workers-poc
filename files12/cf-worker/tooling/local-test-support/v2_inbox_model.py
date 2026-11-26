"""v2_inbox_model - reference model of the inbox SQL functions, a fake database port, and the shared scenarios.

Layer:   tooling
Stage:   V2-02B
Flags:   none
Raises:  never
Budget:  none
Tests:   tests/v2/inbound/test_inbox.py
"""

import itertools

ALLOWED_STATUS = ("pending", "processing", "done", "failed", "ignored")
FINISH_STATUS = ("done", "failed", "ignored")
NEVER = float("-inf")  # a missing lock counts as an expired one, like coalesce(locked_until, '-infinity') in SQL
PN_ON, PN_OFF, PN_NONE = "pn-on", "pn-off", "pn-none"  # a V2 store, a store that switched V2 off, no store
PHONE = "963999000111"


class ModelError(Exception):
    """What the database would raise. `sqlstate` is the five-character code."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def default_stores() -> dict:
    return {PN_ON: {"id": "store-on", "features": {}}, PN_OFF: {"id": "store-off", "features": {"v2_pipeline": False}}}


class InboxModel:
    """In-memory twin of inbox_accept, inbox_claim, inbox_finish and inbox_claim_stale (sql/migrations/002)."""

    def __init__(self, stores: dict | None = None) -> None:
        self.stores = default_stores() if stores is None else stores
        self.rows: list[dict] = []
        self.now = 1000.0  # seconds; advance() moves it, like time passing between two calls
        self._ids = itertools.count(1)

    def advance(self, seconds: float) -> None:
        self.now += seconds

    # --- inbox_accept ---------------------------------------------------------------------------------------
    def accept(self, phone_number_id, events) -> dict:
        store = self.stores.get(phone_number_id)
        if store is None:
            return {"store_found": False, "store_id": None, "routed": False, "results": []}
        found = {"store_found": True, "store_id": store["id"]}
        if store["features"].get("v2_pipeline") is False:
            return {**found, "routed": False, "results": []}
        return {**found, "routed": True, "results": [self._insert(store["id"], item) for item in events or []]}

    @staticmethod
    def _violation(item: dict, status: str) -> str | None:
        """The SQLSTATE the table constraints give: 23502 not null, 23514 check (checked in that order)."""
        if item.get("wamid") is None or item.get("event") is None:
            return "23502"
        no_identity = item.get("from_phone") is None and item.get("from_bsuid") is None
        kind = "message" if item.get("kind") is None else item["kind"]
        if status not in ALLOWED_STATUS or not isinstance(item["event"], dict) or (kind == "message" and no_identity):
            return "23514"
        return None

    def _insert(self, store_id: str, item: dict) -> dict:
        wamid = item.get("wamid")
        status = "pending" if item.get("status") is None else item["status"]
        error = self._violation(item, status)
        if error:
            return {"wamid": wamid, "inserted": False, "id": None, "error": error}
        if any(r["store_id"] == store_id and r["wamid"] == wamid for r in self.rows):
            return {"wamid": wamid, "inserted": False, "id": None}  # on conflict do nothing
        row = {"id": f"id-{next(self._ids)}", "store_id": store_id, "wamid": wamid,
               "kind": "message" if item.get("kind") is None else item["kind"],
               "from_phone": item.get("from_phone"), "from_bsuid": item.get("from_bsuid"), "status": status,
               "attempts": 0, "locked_until": None, "received_at": self.now, "error_code": None,
               "finished_at": self.now if status == "done" else None, "event": item["event"]}
        self.rows.append(row)
        return {"wamid": wamid, "inserted": True, "id": row["id"]}

    # --- claim, finish, reaper ------------------------------------------------------------------------------
    def _expired(self, row: dict) -> bool:
        return (NEVER if row["locked_until"] is None else row["locked_until"]) < self.now

    def _lock(self, row: dict, lock_seconds: int) -> None:
        row.update(status="processing", attempts=row["attempts"] + 1, locked_until=self.now + max(lock_seconds, 1))

    def claim(self, event_id: str, lock_seconds: int = 60) -> bool:
        row = next((r for r in self.rows if r["id"] == event_id), None)
        if row is None or not (row["status"] == "pending" or (row["status"] == "processing" and self._expired(row))):
            return False
        self._lock(row, lock_seconds)
        return True

    def finish(self, event_id: str, status: str, error_code: str | None = None) -> bool:
        if status not in FINISH_STATUS:
            raise ModelError("22023")
        row = next((r for r in self.rows if r["id"] == event_id), None)
        if row is None or row["status"] != "processing":
            return False
        row.update(status=status, error_code=error_code, finished_at=self.now, locked_until=None)
        return True

    def _stale(self, row: dict, pending_older: int) -> bool:
        if row["status"] == "pending":
            return row["received_at"] < self.now - pending_older
        return row["status"] == "processing" and self._expired(row)

    def claim_stale(self, kind="message", limit=10, pending_older=45, lock_seconds=60, max_attempts=3) -> list:
        mine = [r for r in self.rows if r["kind"] == kind and self._stale(r, pending_older)]
        for row in mine:
            if row["attempts"] >= max_attempts:
                row.update(status="failed", error_code="INTERNAL_DEADLINE", finished_at=self.now, locked_until=None)
        todo = [r for r in mine if r["status"] != "failed"]
        todo.sort(key=lambda r: r["received_at"])
        for row in todo[: max(limit, 0)]:
            self._lock(row, lock_seconds)
        return [dict(r) for r in todo[: max(limit, 0)]]

    def snapshot(self) -> dict:
        return {r["wamid"]: f"{r['status']}:{r['attempts']}:{r['error_code'] or ''}" for r in self.rows}


class FakeDb:
    """A DbPort over the model. `errors` builds the exception a real adapter raises for a SQLSTATE."""

    def __init__(self, model: InboxModel | None = None, errors=ModelError) -> None:
        self.model = InboxModel() if model is None else model
        self.errors = errors
        self.calls: list[tuple[str, dict]] = []
        self.fail_with: Exception | None = None  # set it to make every call fail (an outage)

    async def rpc(self, name: str, params: dict) -> object:
        self.calls.append((name, params))
        if self.fail_with is not None:
            raise self.fail_with
        try:
            return self._dispatch(name, params)
        except ModelError as exc:
            raise self.errors(exc.sqlstate) from exc

    def _dispatch(self, name: str, params: dict) -> object:
        model = self.model
        if name == "inbox_accept":
            return model.accept(params["p_phone_number_id"], params["p_events"])
        if name == "inbox_claim":
            return model.claim(params["p_id"], params.get("p_lock_seconds", 60))
        if name == "inbox_finish":
            return model.finish(params["p_id"], params["p_status"], params.get("p_error_code"))
        if name == "inbox_claim_stale":
            return model.claim_stale(params["p_kind"], params["p_limit"], params["p_pending_older_than_seconds"],
                                     params["p_lock_seconds"], params["p_max_attempts"])
        raise LookupError(name)


# --- the shared scenarios: one table, run on the model (L1) and on the real functions (L3) ---------------------


def msg(wamid: str, phone: str | None = PHONE) -> dict:
    return {"wamid": wamid, "kind": "message", "from_phone": phone, "from_bsuid": None, "status": "pending",
            "event": {"v": 1, "wamid": wamid}}


def delivered(wamid: str) -> dict:
    return {"wamid": wamid + ":delivered", "kind": "status", "from_phone": PHONE, "from_bsuid": None,
            "status": "done", "event": {"v": 1, "wamid": wamid}}


def accept_step(events: list, pn: str = PN_ON, *, found=True, routed=True, inserted=(), errors=None) -> tuple:
    expected = {"found": found, "routed": routed, "inserted": list(inserted),
                "errors": list(errors) if errors is not None else [None] * len(inserted)}
    return ("accept", {"pn": pn, "events": events}, expected)


def claim(wamid: str, expect: bool, lock: int = 60) -> tuple:
    return ("claim", {"wamid": wamid, "lock": lock}, expect)


def finish(wamid: str, expect: bool, status: str = "done", error: str | None = None) -> tuple:
    return ("finish", {"wamid": wamid, "status": status, "error": error}, expect)


def stale(expect: list, **limits) -> tuple:
    return ("claim_stale", limits, expect)


def advance(seconds: int) -> tuple:
    return ("advance", {"seconds": seconds}, None)


def snapshot(expect: dict) -> tuple:
    return ("snapshot", {}, expect)


SCENARIOS = {
    "dedupe_by_wamid": [
        accept_step([msg("m1"), msg("m2")], inserted=[True, True]),
        accept_step([msg("m2"), msg("m3")], inserted=[False, True]),
        snapshot({"m1": "pending:0:", "m2": "pending:0:", "m3": "pending:0:"}),
    ],
    "unknown_store_and_store_switch": [
        accept_step([msg("m1")], PN_NONE, found=False, routed=False),
        accept_step([msg("m1")], PN_OFF, routed=False),
        snapshot({}),
    ],
    "one_bad_event_never_costs_the_good_ones": [
        accept_step([msg("m1"), msg("m2", phone=None), msg("m3"), {**msg("m4"), "wamid": None},
                     {**msg("m5"), "status": "weird"}],
                    inserted=[True, False, True, False, False],
                    errors=[None, "23514", None, "23502", "23514"]),
        snapshot({"m1": "pending:0:", "m3": "pending:0:"}),
    ],
    "status_events_are_kept_finished": [
        accept_step([delivered("w1")], inserted=[True]),
        snapshot({"w1:delivered": "done:0:"}),
        advance(100),
        stale([]),
    ],
    "claim_and_finish": [
        accept_step([msg("m1")], inserted=[True]),
        claim("m1", True), claim("m1", False),
        finish("m1", True), finish("m1", False), claim("m1", False),
        snapshot({"m1": "done:1:"}),
    ],
    "finish_needs_processing_and_keeps_the_error_code": [
        accept_step([msg("m1"), msg("m2")], inserted=[True, True]),
        finish("m1", False),
        claim("m2", True), finish("m2", True, status="failed", error="LLM_TIMEOUT"),
        snapshot({"m1": "pending:0:", "m2": "failed:1:LLM_TIMEOUT"}),
    ],
    "a_repeat_never_resets_a_claimed_event": [
        accept_step([msg("m1")], inserted=[True]),
        claim("m1", True),
        accept_step([msg("m1")], inserted=[False]),
        snapshot({"m1": "processing:1:"}),
    ],
    "an_expired_lock_can_be_claimed_again": [
        accept_step([msg("m1")], inserted=[True]),
        claim("m1", True), advance(59), claim("m1", False), advance(2), claim("m1", True),
        snapshot({"m1": "processing:2:"}),
    ],
    "reaper_takes_old_pending_once": [
        accept_step([msg("m1")], inserted=[True]),
        stale([]), advance(46), stale(["m1"]), stale([]),
        snapshot({"m1": "processing:1:"}),
    ],
    "reaper_gives_up_after_max_attempts": [
        accept_step([msg("m1")], inserted=[True]),
        advance(46), stale(["m1"], max_attempts=2),
        advance(61), stale(["m1"], max_attempts=2),
        advance(61), stale([], max_attempts=2),
        snapshot({"m1": "failed:2:INTERNAL_DEADLINE"}),
    ],
    "reaper_takes_the_oldest_first_up_to_the_limit": [
        accept_step([msg("m1")], inserted=[True]), advance(1),
        accept_step([msg("m2")], inserted=[True]), advance(1),
        accept_step([msg("m3")], inserted=[True]), advance(50),
        stale(["m1", "m2"], limit=2), stale(["m3"], limit=2),
    ],
}


def norm_accept(raw: dict) -> dict:
    """The comparable shape of an inbox_accept answer (same for the model, the Python layer and the real SQL)."""
    rows = raw["results"]
    return {"found": raw["store_found"], "routed": raw["routed"],
            "inserted": [r["inserted"] for r in rows], "errors": [r.get("error") for r in rows]}


def run_on_model(steps: list, model: InboxModel | None = None) -> list:
    """Run one scenario on the model; returns [(step index, op, got, expected)] for the steps that differ."""
    model = InboxModel() if model is None else model

    def event_id(wamid: str) -> str:
        return next(r["id"] for r in model.rows if r["wamid"] == wamid)

    def call(op: str, args: dict) -> object:
        if op == "accept":
            return norm_accept(model.accept(args["pn"], args["events"]))
        if op == "claim":
            return model.claim(event_id(args["wamid"]), args["lock"])
        if op == "finish":
            return model.finish(event_id(args["wamid"]), args["status"], args["error"])
        if op == "claim_stale":
            return sorted(r["wamid"] for r in model.claim_stale(**args))
        if op == "advance":
            return model.advance(args["seconds"])
        return model.snapshot()

    return compare(steps, call)


def compare(steps: list, call) -> list:
    """Shared by every transport: call(op, args) gives the comparable result of one step."""
    problems = []
    for index, (op, args, expected) in enumerate(steps):
        got = call(op, args)
        if got != expected:
            problems.append((index, op, got, expected))
    return problems
