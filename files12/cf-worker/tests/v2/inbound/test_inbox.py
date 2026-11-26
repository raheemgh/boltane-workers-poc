"""test_inbox - the inbox: the retention policy (ADR-10), the calls to the database functions, the shared scenarios.

Level:   L1
Stage:   V2-02B
"""

import asyncio
import unittest

import v2_inbox_model as vm
from app.v2.core.errors import ErrorCode, V2Error
from app.v2.core.events import KIND_MESSAGE, KIND_STATUS, Delivery, InboundEvent
from app.v2.inbound import inbox

PN = "100200300400"
PHONE = "963999000111"


def message(wamid, **data):
    return InboundEvent(KIND_MESSAGE, wamid, PN, PHONE, None, {"v": 1, "kind": "message", "wamid": wamid, **data})


def status(msg_id, state, **data):
    return InboundEvent(KIND_STATUS, f"{msg_id}:{state}", PN, PHONE, None,
                        {"v": 1, "kind": "status", "wamid": msg_id, "status": state, **data})


def constraint(sqlstate):
    return V2Error(ErrorCode.DB_CONSTRAINT, sqlstate=sqlstate)


def plan(*events):
    return inbox.plan_items(Delivery(tuple(events), (), (), (PN,)))


class Canned:
    """A database port that always gives the same answer."""

    def __init__(self, answer):
        self.answer = answer

    async def rpc(self, name, params):
        return self.answer


class PolicyTest(unittest.TestCase):
    def test_plan__a_message_waits_as_pending_with_its_identity_and_event(self):
        event = message("wamid.1", text="hi")
        item = plan(event).items[0]
        self.assertEqual(set(item), {"wamid", "kind", "from_phone", "from_bsuid", "status", "event"})
        self.assertEqual((item["wamid"], item["kind"], item["status"]), ("wamid.1", "message", "pending"))
        self.assertEqual((item["from_phone"], item["from_bsuid"]), (PHONE, None))
        self.assertIs(item["event"], event.data)

    def test_plan__delivered_and_failed_statuses_are_kept_finished(self):
        items = plan(status("m1", "delivered"), status("m2", "failed", errors=[{"code": 131047}])).items
        self.assertEqual([(i["wamid"], i["kind"], i["status"]) for i in items],
                         [("m1:delivered", "status", "done"), ("m2:failed", "status", "done")])

    def test_plan__sent_read_and_unknown_statuses_are_not_kept(self):
        result = plan(status("m1", "sent"), status("m2", "read"), status("m3", "deleted"), message("w1"))
        self.assertEqual([i["wamid"] for i in result.items], ["w1"])
        self.assertEqual(result.skipped, 3)

    def test_plan__group_traffic_is_not_kept(self):
        result = plan(message("g1", group_id="120363"), status("m1", "delivered", group_id="120363"))
        self.assertEqual((result.items, result.skipped), ((), 2))

    def test_plan__system_messages_are_kept_because_identity_merging_needs_them(self):
        self.assertEqual(plan(message("s1", type="system")).items[0]["status"], "pending")

    def test_plan__order_is_kept_and_an_empty_delivery_is_an_empty_plan(self):
        self.assertEqual([i["wamid"] for i in plan(message("a"), message("b"), message("c")).items], ["a", "b", "c"])
        self.assertEqual(plan(), inbox.Plan((), 0))


class AcceptTest(unittest.IsolatedAsyncioTestCase):
    async def test_accept__new_events_are_inserted_and_repeats_are_duplicates(self):
        db = vm.FakeDb()
        first = await inbox.accept(db, vm.PN_ON, tuple([vm.msg("m1"), vm.msg("m2")]))
        again = await inbox.accept(db, vm.PN_ON, tuple([vm.msg("m2"), vm.msg("m3")]))
        self.assertEqual((first.inserted, first.duplicates, first.rejected), (2, 0, 0))
        self.assertEqual((again.inserted, again.duplicates, again.rejected), (1, 1, 0))
        self.assertTrue(first.store_found and first.routed)
        self.assertEqual(first.store_id, "store-on")

    async def test_accept__unknown_store_and_a_store_that_switched_v2_off(self):
        db = vm.FakeDb()
        unknown = await inbox.accept(db, vm.PN_NONE, (vm.msg("m1"),))
        off = await inbox.accept(db, vm.PN_OFF, (vm.msg("m1"),))
        self.assertEqual((unknown.store_found, unknown.routed, unknown.rows), (False, False, ()))
        self.assertEqual((off.store_found, off.routed, off.rows), (True, False, ()))
        self.assertEqual(db.model.rows, [])

    async def test_accept__a_refused_event_is_rejected_not_counted_as_a_repeat(self):
        result = await inbox.accept(vm.FakeDb(), vm.PN_ON, (vm.msg("m1"), vm.msg("m2", phone=None), vm.msg("m1")))
        self.assertEqual((result.inserted, result.rejected, result.duplicates), (1, 1, 1))
        self.assertEqual([row.error for row in result.rows], [None, "23514", None])

    async def test_accept__a_database_answer_in_an_unknown_shape_is_db_unexpected(self):
        wrong = (None, [], {}, "ok", {"store_found": True}, {"store_found": True, "routed": True, "results": {}},
                 {"store_found": "yes", "routed": True, "results": []})
        for answer in wrong:
            with self.assertRaises(V2Error) as caught:
                await inbox.accept(Canned(answer), PN, ())
            self.assertIs(caught.exception.code, ErrorCode.DB_UNEXPECTED, answer)

    async def test_accept__a_database_failure_reaches_the_caller(self):
        db = vm.FakeDb()
        db.fail_with = V2Error(ErrorCode.DB_UNAVAILABLE)
        with self.assertRaises(V2Error) as caught:
            await inbox.accept(db, PN, ())
        self.assertIs(caught.exception.code, ErrorCode.DB_UNAVAILABLE)


class WrapperTest(unittest.IsolatedAsyncioTestCase):
    """The names and parameters sent to the database must be the ones the SQL functions declare."""

    async def test_wrappers__function_names_and_parameters(self):
        db = vm.FakeDb(errors=constraint)
        await inbox.accept(db, vm.PN_ON, (vm.msg("m1"),))
        event_id = db.model.rows[0]["id"]
        self.assertTrue(await inbox.claim(db, event_id))
        self.assertTrue(await inbox.finish(db, event_id, "failed", "LLM_TIMEOUT"))
        await inbox.claim_stale(db)
        names = [name for name, _ in db.calls]
        self.assertEqual(names, ["inbox_accept", "inbox_claim", "inbox_finish", "inbox_claim_stale"])
        self.assertEqual(db.calls[0][1], {"p_phone_number_id": vm.PN_ON, "p_events": [vm.msg("m1")]})
        self.assertEqual(db.calls[1][1], {"p_id": event_id, "p_lock_seconds": 60})
        self.assertEqual(db.calls[2][1], {"p_id": event_id, "p_status": "failed", "p_error_code": "LLM_TIMEOUT"})
        self.assertEqual(db.calls[3][1], {"p_kind": "message", "p_limit": 10, "p_pending_older_than_seconds": 45,
                                         "p_lock_seconds": 60, "p_max_attempts": 3})

    async def test_wrappers__only_a_real_true_counts_as_claimed_or_finished(self):
        for answer in (False, None, "true", 1, {}):
            self.assertFalse(await inbox.claim(Canned(answer), "id"), answer)
            self.assertFalse(await inbox.finish(Canned(answer), "id", "done"), answer)

    async def test_wrappers__claim_stale_returns_rows_as_dicts_and_ignores_junk(self):
        self.assertEqual(await inbox.claim_stale(Canned([{"id": "a"}, 5, None, {"id": "b"}])), [{"id": "a"}, {}, {}, {"id": "b"}])
        self.assertEqual(await inbox.claim_stale(Canned(None)), [])

    async def test_wrappers__finishing_with_a_wrong_status_is_db_constraint(self):
        db = vm.FakeDb(errors=constraint)
        with self.assertRaises(V2Error) as caught:
            await inbox.finish(db, "id-1", "pending")
        self.assertIs(caught.exception.code, ErrorCode.DB_CONSTRAINT)
        self.assertEqual(caught.exception.context, {"sqlstate": "22023"})


def through_inbox(db):
    """A scenario transport that goes through inbox.py and the fake port, so the Python layer is under test too."""
    model = db.model

    def event_id(wamid):
        return next(r["id"] for r in model.rows if r["wamid"] == wamid)

    def call(op, args):
        if op == "accept":
            got = asyncio.run(inbox.accept(db, args["pn"], tuple(args["events"])))
            return {"found": got.store_found, "routed": got.routed,
                    "inserted": [r.inserted for r in got.rows], "errors": [r.error for r in got.rows]}
        if op == "claim":
            return asyncio.run(inbox.claim(db, event_id(args["wamid"]), args["lock"]))
        if op == "finish":
            return asyncio.run(inbox.finish(db, event_id(args["wamid"]), args["status"], args["error"]))
        if op == "claim_stale":
            return sorted(r["wamid"] for r in asyncio.run(inbox.claim_stale(db, **args)))
        if op == "advance":
            return model.advance(args["seconds"])
        return model.snapshot()

    return call


class ScenarioTest(unittest.TestCase):
    def test_scenarios__the_model_satisfies_every_scenario(self):
        for name, steps in vm.SCENARIOS.items():
            self.assertEqual(vm.run_on_model(steps), [], name)

    def test_scenarios__the_python_layer_gives_the_same_results(self):
        for name, steps in vm.SCENARIOS.items():
            self.assertEqual(vm.compare(steps, through_inbox(vm.FakeDb())), [], name)

    def test_scenarios__there_are_enough_of_them_to_mean_something(self):
        self.assertGreaterEqual(len(vm.SCENARIOS), 10)
        self.assertTrue(all(len(steps) >= 2 for steps in vm.SCENARIOS.values()))


if __name__ == "__main__":
    unittest.main()
