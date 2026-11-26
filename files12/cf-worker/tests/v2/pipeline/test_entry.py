"""test_entry - the selector V1 calls: when V2 steps aside, what it stores, and what it answers Meta.

Level:   L1
Stage:   V2-02B
"""

import types
import unittest

import v2_inbox_model as vm
from app.v2.core.errors import ErrorCode, V2Error
from app.v2.pipeline import entry

PN = "100200300400"
OFF_PN = "999999999"
PHONE = "963999000111"
ON = types.SimpleNamespace(V2_FLAGS="v2_pipeline", V2_ALLOWLIST=PN)


def constraint(sqlstate):
    return V2Error(ErrorCode.DB_CONSTRAINT, sqlstate=sqlstate)


def database():
    stores = {PN: {"id": "s1", "features": {}}, OFF_PN: {"id": "s2", "features": {"v2_pipeline": False}}}
    return vm.FakeDb(vm.InboxModel(stores), errors=constraint)


def payload(pn=PN, messages=(), statuses=(), more=()):
    value = {"messaging_product": "whatsapp",
             "metadata": {"display_phone_number": "15550000000", "phone_number_id": pn}}
    if messages:
        value["contacts"] = [{"profile": {"name": "Sara"}, "wa_id": PHONE}]
        value["messages"] = list(messages)
    if statuses:
        value["statuses"] = list(statuses)
    changes = [{"field": "messages", "value": value}, *more]
    return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": changes}]}


def text(wamid, body="hello there"):
    return {"from": PHONE, "id": wamid, "timestamp": "1700000000", "type": "text", "text": {"body": body}}


def state(msg_id, name, **extra):
    return {"id": msg_id, "status": name, "timestamp": "1700000001", "recipient_id": PHONE, **extra}


class RouteTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        entry._reported.clear()

    async def test_route__with_the_flag_off_it_steps_aside_and_never_touches_the_database(self):
        db = database()
        envs = (None, types.SimpleNamespace(), types.SimpleNamespace(V2_ALLOWLIST=PN),
                types.SimpleNamespace(V2_FLAGS="llm_reasoning_off", V2_ALLOWLIST=PN))
        for env in envs:
            self.assertIsNone(await entry.route_delivery(payload(messages=[text("w1")]), env, None, db=db), env)
        self.assertEqual(db.calls, [])

    async def test_route__flag_on_with_nobody_allowed_steps_aside(self):
        db = database()
        for allowlist in (None, "", "  "):
            env = types.SimpleNamespace(V2_FLAGS="v2_pipeline", V2_ALLOWLIST=allowlist)
            self.assertIsNone(await entry.route_delivery(payload(messages=[text("w1")]), env, db=db))
        self.assertEqual(db.calls, [])

    async def test_route__a_number_outside_the_allowlist_steps_aside(self):
        db = database()
        self.assertIsNone(await entry.route_delivery(payload(pn="555000111", messages=[text("w1")]), ON, db=db))
        self.assertEqual(db.calls, [])

    async def test_route__star_allows_every_number(self):
        env = types.SimpleNamespace(V2_FLAGS="v2_pipeline", V2_ALLOWLIST="*")
        verdict = await entry.route_delivery(payload(messages=[text("w1")]), env, db=database())
        self.assertEqual(verdict.status, 200)

    async def test_route__an_allowed_message_is_stored_and_acknowledged(self):
        db = database()
        verdict = await entry.route_delivery(payload(messages=[text("w1")]), ON, object(), db=db)
        self.assertEqual((verdict.status, verdict.body), (200, {"status": "accepted", "inserted": 1, "duplicates": 0}))
        row = db.model.rows[0]
        self.assertEqual((row["wamid"], row["status"], row["from_phone"], row["store_id"]),
                         ("w1", "pending", PHONE, "s1"))
        self.assertEqual(row["event"]["text"], "hello there")  # the text is stored in the event, nowhere else
        for shown in (str(verdict.body), verdict.note):
            self.assertNotIn("hello", shown)
            self.assertNotIn(PHONE, shown)

    async def test_route__the_same_delivery_again_is_a_duplicate(self):
        db = database()
        body = payload(messages=[text("w1")])
        await entry.route_delivery(body, ON, db=db)
        verdict = await entry.route_delivery(body, ON, db=db)
        self.assertEqual(verdict.body, {"status": "accepted", "inserted": 0, "duplicates": 1})
        self.assertEqual(len(db.model.rows), 1)

    async def test_route__every_message_of_a_delivery_is_stored(self):
        db = database()
        verdict = await entry.route_delivery(payload(messages=[text("w1"), text("w2"), text("w3")]), ON, db=db)
        self.assertEqual(verdict.body["inserted"], 3)
        self.assertEqual([r["wamid"] for r in db.model.rows], ["w1", "w2", "w3"])
        self.assertEqual(len(db.calls), 1)  # one database call for the whole delivery

    async def test_route__sent_and_read_statuses_are_ignored_without_a_database_call(self):
        db = database()
        verdict = await entry.route_delivery(payload(statuses=[state("m1", "sent"), state("m1", "read")]), ON, db=db)
        self.assertEqual((verdict.status, verdict.body), (200, {"status": "ignored"}))
        self.assertEqual(db.calls, [])

    async def test_route__a_delivered_status_is_kept_finished(self):
        db = database()
        await entry.route_delivery(payload(statuses=[state("m1", "delivered"), state("m1", "sent")]), ON, db=db)
        self.assertEqual([(r["wamid"], r["status"], r["kind"]) for r in db.model.rows],
                         [("m1:delivered", "done", "status")])

    async def test_route__group_messages_are_not_stored(self):
        db = database()
        group = {**text("g1"), "group_id": "120363000"}
        verdict = await entry.route_delivery(payload(messages=[group]), ON, db=db)
        self.assertEqual(verdict.body, {"status": "ignored"})
        self.assertEqual(db.model.rows, [])

    async def test_route__a_delivery_with_two_numbers_stays_with_v1(self):
        second = {"field": "messages", "value": {"messaging_product": "whatsapp",
                  "metadata": {"phone_number_id": "100200300999"}, "messages": [text("w9")]}}
        db = database()
        self.assertIsNone(await entry.route_delivery(payload(messages=[text("w1")], more=[second]), ON, db=db))
        self.assertEqual(db.calls, [])

    async def test_route__garbage_steps_aside(self):
        db = database()
        for junk in (None, [], "x", 5, {}, {"object": "page"}, {"object": "whatsapp_business_account", "entry": 3}):
            self.assertIsNone(await entry.route_delivery(junk, ON, db=db), junk)
        self.assertEqual(db.calls, [])

    async def test_route__an_unknown_store_is_acknowledged_with_its_code(self):
        env = types.SimpleNamespace(V2_FLAGS="v2_pipeline", V2_ALLOWLIST="555000111")
        verdict = await entry.route_delivery(payload(pn="555000111", messages=[text("w1")]), env, db=database())
        self.assertEqual((verdict.status, verdict.body),
                         (200, {"status": "ignored", "code": ErrorCode.INBOUND_STORE_UNKNOWN.value}))

    async def test_route__a_store_that_switched_v2_off_goes_back_to_v1(self):
        db = database()
        env = types.SimpleNamespace(V2_FLAGS="v2_pipeline", V2_ALLOWLIST=OFF_PN)
        self.assertIsNone(await entry.route_delivery(payload(pn=OFF_PN, messages=[text("w1")]), env, db=db))
        self.assertEqual(db.model.rows, [])

    async def test_route__a_database_outage_asks_meta_to_retry(self):
        for code in (ErrorCode.DB_UNAVAILABLE, ErrorCode.DB_UNEXPECTED):
            db = database()
            db.fail_with = V2Error(code)
            verdict = await entry.route_delivery(payload(messages=[text("w1")]), ON, db=db)
            self.assertEqual((verdict.status, verdict.body), (503, {"status": "retry", "code": code.value}))
            self.assertIn(code.value, verdict.note)

    async def test_route__a_refused_write_is_acknowledged_so_meta_stops_retrying(self):
        db = database()
        db.fail_with = V2Error(ErrorCode.DB_CONSTRAINT, sqlstate="22P05")
        verdict = await entry.route_delivery(payload(messages=[text("w1")]), ON, db=db)
        self.assertEqual((verdict.status, verdict.body), (200, {"status": "dropped", "code": "DB_CONSTRAINT"}))

    async def test_route__one_refused_event_among_good_ones_is_counted_as_dropped(self):
        answer = {"store_found": True, "store_id": "s1", "routed": True, "results": [
            {"wamid": "w1", "inserted": True, "id": "a"}, {"wamid": "w2", "inserted": False, "id": None, "error": "23514"}]}

        class OneBad:
            async def rpc(self, name, params):
                return answer

        verdict = await entry.route_delivery(payload(messages=[text("w1"), text("w2")]), ON, db=OneBad())
        self.assertEqual(verdict.body, {"status": "accepted", "inserted": 1, "duplicates": 0, "dropped": 1})

    async def test_route__missing_database_settings_reach_the_guard_in_v1(self):
        with self.assertRaises(V2Error) as caught:
            await entry.route_delivery(payload(messages=[text("w1")]), ON)
        self.assertIs(caught.exception.code, ErrorCode.CONFIG_MISSING)

    async def test_route__a_misspelled_flag_is_reported_once_not_silently_ignored(self):
        env = types.SimpleNamespace(V2_FLAGS="v2_pipline", V2_ALLOWLIST="*")
        with self.assertLogs("boltane.v2.entry", "WARNING") as logged:
            self.assertIsNone(await entry.route_delivery(payload(messages=[text("w1")]), env, db=database()))
        self.assertEqual(len(logged.records), 1)
        self.assertIn("CONFIG_FLAG_UNKNOWN", logged.output[0])
        self.assertIn("v2_pipline", logged.output[0])
        with self.assertNoLogs("boltane.v2.entry", "WARNING"):
            await entry.route_delivery(payload(messages=[text("w1")]), env, db=database())


if __name__ == "__main__":
    unittest.main()
