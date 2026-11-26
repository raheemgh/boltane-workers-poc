"""test_normalizer - L1 tests for inbound/normalizer.py: Meta fixtures, edge cases, never-raises sweeps, sabotage.

Level:   L1
Stage:   V2-02A
"""

import json
import random
import re
import types
import unittest
from pathlib import Path

from app.v2.core.errors import ErrorCode
from app.v2.core.events import MESSAGE_TYPES, Delivery
from app.v2.inbound import normalizer as real

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "meta"
SOURCE = Path(real.__file__)
WS = "wamid.HBgLMTY1MDM4Nzk0MzkVAgASGBQzQUFERjg0NDEzNDdFODU3MUMxMAA="
U1 = "US.13491208655302741918"
P1 = "US.ENT.11815799212886844830"
NUMBER = "106540352242922"

# (id, fixture, facts to equal, data of the first event that must be covered). One row per fixture file.
SCENARIOS = (
    ("N01", "text_bsuid_username_no_phone",
     {"kinds": ["message"], "types": ["text"], "texts": ["Does it come in another color?"], "phones": [None],
      "bsuids": [U1], "pnids": [NUMBER], "issues": []},
     {"from": {"parent_bsuid": P1, "username": "realsheenanelson", "name": "Sheena Nelson"}}),
    ("N02", "text_bsuid_with_phone", {"phones": ["16505551234"], "bsuids": [U1], "texts": ["Hello, is the shop open?"]}, None),
    ("N03", "text_username_phone_available", {"phones": ["16505551234"], "bsuids": [U1]},
     {"from": {"username": "pablomorales", "parent_bsuid": P1}}),
    ("N04", "text_legacy_phone_only", {"phones": ["963999000111"], "bsuids": [None], "texts": ["مرحبا، شو عندكم اليوم؟"]}, None),
    ("N05", "batch_two_entries_three_messages",
     {"texts": ["one", "two", "three"], "keys": ["wamid.B1", "wamid.B2", "wamid.B3"],
      "phones": ["16505551234", "16505551234", "963999000222"]}, None),
    ("N06", "status_delivered_phone_username",
     {"kinds": ["status"], "types": ["delivered"], "keys": [WS + ":delivered"], "phones": ["16505551234"], "bsuids": [U1]},
     {"pricing": {"billable": True, "category": "marketing", "model": "PMP", "type": "regular"}}),
    ("N07", "status_delivered_to_bsuid_no_phone", {"types": ["delivered"], "phones": [None], "bsuids": [U1]}, None),
    ("N08", "status_failed_131049", {"types": ["failed"], "keys": [WS + ":failed"], "phones": ["16505551234"], "bsuids": [None]},
     {"errors": [{"code": 131049, "title": "This message was not delivered to maintain healthy ecosystem engagement."}]}),
    ("N09", "status_sent_and_read_batch", {"types": ["sent", "read"], "keys": [WS + ":sent", WS + ":read"]}, None),
    ("N10", "system_user_changed_number", {"types": ["system"], "phones": ["16505559999"], "bsuids": [U1]},
     {"system": {"type": "user_changed_number", "previous_user_id": "US.10230984751029384756", "phone": "16505559999"}}),
    ("N11", "system_user_changed_user_id", {"types": ["system"], "phones": [None], "bsuids": [U1], "issues": []},
     {"system": {"type": "user_changed_user_id", "previous_user_id": "US.10230984751029384756"}}),
    ("N12", "contacts_request_contact_info", {"types": ["contacts"], "bsuids": [U1]},
     {"contacts_count": 1, "shared_phone": "16505551234"}),
    ("N13", "unsupported_131051", {"types": ["unsupported"], "bsuids": ["BO.10000000000000000001"]}, {"meta_errors": [131051]}),
    ("N14", "interactive_131000_no_body", {"types": ["unsupported"]},
     {"orig_type": "interactive", "meta_errors": [131000], "reply_to": "wamid.CTX1"}),
    ("N15", "interactive_button_reply", {"types": ["interactive"], "texts": ["نعم"]},
     {"interactive": {"kind": "button_reply", "id": "btn1"}, "reply_to": "wamid.PREV"}),
    ("N16", "location_shared",
     {"types": ["location"], "texts": ["[location] Main Street Beach, Santa Cruz, CA (38.9806263495, -131.9428612257)"]},
     {"location": {"lat": 38.9806263495, "lon": -131.9428612257, "name": "Main Street Beach"}}),
    ("N17", "image_with_caption", {"types": ["image"], "texts": ["شو رأيك بهالصورة؟"]}, {"media": {"id": "MEDIA1", "mime": "image/jpeg"}}),
    ("N18", "audio_voice_note", {"types": ["audio"], "texts": [None]}, {"media": {"voice": True}}),
    ("N19", "sticker_animated", {"types": ["sticker"]}, {"media": {"animated": True, "mime": "image/webp"}}),
    ("N20", "reaction_thumbs_up", {"types": ["reaction"]}, {"reaction": {"to": "wamid.PREV", "emoji": "👍"}}),
    ("N21", "referral_ctwa_text", {"types": ["text"]},
     {"referral": {"ctwa_clid": "AR_fake_click_id_0000000000", "source_id": "120200000000000", "source_type": "ad"}}),
)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def facts(delivery: Delivery) -> dict:
    events = delivery.events
    return {
        "kinds": [e.kind for e in events],
        "types": [e.data.get("type") or e.data.get("status") for e in events],
        "texts": [e.data.get("text") for e in events],
        "phones": [e.phone for e in events],
        "bsuids": [e.bsuid for e in events],
        "keys": [e.key for e in events],
        "pnids": list(delivery.phone_number_ids),
        "issues": [issue.code.value for issue in delivery.issues],
    }


def covers(actual: object, expected: object) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and covers(actual[k], v) for k, v in expected.items())
    return actual == expected


def delivery_of(messages: list) -> dict:
    value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": NUMBER}, "messages": messages}
    return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": value}]}]}


def text_message(wamid: str, body: str) -> dict:
    return {"from": "963999000111", "id": wamid, "timestamp": "1", "type": "text", "text": {"body": body}}


def scenario_failures(normalize_fn) -> list[str]:
    failures = []
    for sid, name, want, data0 in SCENARIOS:
        delivery = normalize_fn(load_fixture(name)["payload"])
        got = facts(delivery)
        for key, expected in want.items():
            if got[key] != expected:
                failures.append(f"{sid} {key}: {got[key]!r} != {expected!r}")
        if data0 and not (delivery.events and covers(delivery.events[0].data, data0)):
            failures.append(f"{sid} data not covered: {data0!r}")
    return failures


def extra_failures(normalize_fn) -> list[str]:
    """Behaviours that have no fixture of their own: the same id twice, and the per-delivery limit."""
    failures = []
    twice = delivery_of([text_message("wamid.D1", "a"), text_message("wamid.D1", "b")])
    if [e.data["text"] for e in normalize_fn(twice).events] != ["a"]:
        failures.append("the same id twice in one delivery was not collapsed to the first")
    many = normalize_fn(delivery_of([text_message(f"wamid.M{i}", "x") for i in range(real.MAX_EVENTS + 5)]))
    if len(many.events) != real.MAX_EVENTS or len(many.issues) != 5:
        failures.append("the per-delivery limit is not enforced and reported")
    return failures


def behavior_failures(normalize_fn) -> list[str]:
    return scenario_failures(normalize_fn) + extra_failures(normalize_fn)


def assert_db_safe(case: unittest.TestCase, event) -> None:
    """What the database would refuse must never leave the normalizer: NUL, NaN, a phone that is not 6-15 digits."""
    text = json.dumps(event.data, allow_nan=False, ensure_ascii=False)
    text.encode("utf-8")
    case.assertNotIn("\x00", text)
    case.assertNotIn("\\u0000", json.dumps(event.data))
    case.assertEqual(event.data["kind"], event.kind)
    case.assertEqual(event.data["v"], 1)
    case.assertTrue(0 < len(event.key) <= 300)
    if event.phone is not None:
        case.assertRegex(event.phone, r"^[0-9]{6,15}$")
    if event.bsuid is not None:
        case.assertRegex(event.bsuid, r"^[A-Z]{2}\.[A-Za-z0-9.]+$")


class FixtureTests(unittest.TestCase):
    def test_fixtures__every_file_names_its_source_and_date(self):
        files = sorted(FIXTURES.glob("*.json"))
        self.assertGreaterEqual(len(files), 21)
        for path in files:
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(doc["_source"] and all(isinstance(s, str) and s for s in doc["_source"]), path.name)
            self.assertRegex(doc["_fetched"], r"^\d{4}-\d{2}-\d{2}$", path.name)
            self.assertIn(doc["_verified"], ("meta-doc", "simulator-only"), path.name)
            self.assertIsInstance(doc["payload"], dict, path.name)

    def test_fixtures__every_file_has_a_scenario_and_ids_are_unique(self):
        names = {path.stem for path in FIXTURES.glob("*.json")}
        self.assertEqual(names, {row[1] for row in SCENARIOS})
        self.assertEqual(len({row[0] for row in SCENARIOS}), len(SCENARIOS))

    def test_scenarios__the_real_normalizer_meets_every_expectation(self):
        self.assertEqual(behavior_failures(real.normalize), [])

    def test_events__are_json_ready_and_database_safe(self):
        for sid, name, _want, _data0 in SCENARIOS:
            for event in real.normalize(load_fixture(name)["payload"]).events:
                with self.subTest(scenario=sid):
                    assert_db_safe(self, event)
                    if event.kind == "message":
                        self.assertIn(event.data["type"], MESSAGE_TYPES)

    def test_privacy__media_urls_and_contact_cards_are_not_stored(self):
        for name in ("sticker_animated", "location_shared", "contacts_request_contact_info", "image_with_caption"):
            for event in real.normalize(load_fixture(name)["payload"]).events:
                blob = json.dumps(event.data, ensure_ascii=False)
                for secret in ("lookaside", "foursquare", "vcard", "+16505551234", "sha256"):
                    self.assertNotIn(secret, blob, name)


class EdgeCaseTests(unittest.TestCase):
    def test_normalize__not_a_delivery_gives_one_issue_and_no_event(self):
        for payload in (None, [], "x", 1, {}, {"object": "page"}, {"object": ["whatsapp_business_account"]}):
            result = real.normalize(payload)
            self.assertEqual((result.events, [i.where for i in result.issues]), ((), ["object"]), payload)
            self.assertEqual(result.issues[0].code, ErrorCode.INBOUND_MALFORMED)

    def test_normalize__poison_characters_are_removed(self):
        result = real.normalize(delivery_of([text_message("wamid.P1", "a\x00b\ud83dc")]))
        self.assertEqual(result.events[0].data["text"], "ab?c")
        assert_db_safe(self, result.events[0])

    def test_normalize__a_message_without_identity_or_id_is_an_issue(self):
        no_who = {"id": "wamid.X", "timestamp": "1", "type": "text", "text": {"body": "hi"}}
        no_id = {"from": "963999000111", "timestamp": "1", "type": "text", "text": {"body": "hi"}}
        result = real.normalize(delivery_of([no_who, no_id]))
        self.assertEqual(result.events, ())
        self.assertEqual([i.where for i in result.issues],
                         ["entry[0].changes[0].messages[0]", "entry[0].changes[0].messages[1]"])

    def test_normalize__missing_phone_number_id_is_an_issue(self):
        payload = delivery_of([text_message("wamid.N1", "hi")])
        del payload["entry"][0]["changes"][0]["value"]["metadata"]
        result = real.normalize(payload)
        self.assertEqual((result.events, result.phone_number_ids), ((), ()))
        self.assertTrue(result.issues[0].where.endswith(".value.metadata"))

    def test_normalize__an_integer_phone_number_id_is_read_as_text(self):
        payload = delivery_of([text_message("wamid.N2", "hi")])
        payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = 106540352242922
        self.assertEqual(real.normalize(payload).phone_number_ids, (NUMBER,))

    def test_normalize__several_numbers_are_listed_in_order_of_appearance(self):
        first, second = delivery_of([text_message("wamid.A", "a")]), delivery_of([text_message("wamid.B", "b")])
        second["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "999999999999"
        first["entry"].append(second["entry"][0])
        self.assertEqual(real.normalize(first).phone_number_ids, (NUMBER, "999999999999"))

    def test_normalize__unknown_and_malformed_types_become_unsupported(self):
        for kind in ("weird", "", None, 5, ["text"], {"a": 1}):
            message = dict(text_message("wamid.U1", "x"), type=kind)
            data = real.normalize(delivery_of([message])).events[0].data
            self.assertEqual(data["type"], "unsupported", kind)

    def test_normalize__bad_coordinates_make_a_location_unsupported(self):
        for lat in (float("nan"), float("inf"), "38.9", None, True):
            message = {"from": "963999000111", "id": "wamid.L1", "type": "location",
                       "location": {"latitude": lat, "longitude": 1.5}}
            self.assertEqual(real.normalize(delivery_of([message])).events[0].data["type"], "unsupported", lat)

    def test_normalize__a_bsuid_in_the_from_field_is_not_stored_as_a_phone(self):
        message = dict(text_message("wamid.B1", "hi"), **{"from": U1})
        event = real.normalize(delivery_of([message])).events[0]
        self.assertEqual((event.phone, event.bsuid), (None, U1))

    def test_normalize__group_messages_and_group_statuses_keep_the_group_id(self):
        message = dict(text_message("wamid.G1", "hi"), group_id="120363025246125486@g.us")
        self.assertEqual(real.normalize(delivery_of([message])).events[0].data["group_id"], "120363025246125486@g.us")
        status = {"id": "wamid.S1", "status": "delivered", "recipient_type": "group", "recipient_id": "1203630@g.us"}
        payload = delivery_of([])
        payload["entry"][0]["changes"][0]["value"]["statuses"] = [status]
        event = real.normalize(payload).events[0]
        self.assertEqual((event.phone, event.data["group_id"]), (None, "1203630@g.us"))

    def test_normalize__account_events_are_typed_and_keyed_by_content(self):
        def delivery(time: int, value: dict) -> dict:
            change = {"field": "message_template_status_update", "value": value}
            return {"object": "whatsapp_business_account", "entry": [{"id": "1", "time": time, "changes": [change]}]}
        value = {"event": "APPROVED", "message_template_name": "welcome"}
        one, again = real.normalize(delivery(10, value)), real.normalize(delivery(10, dict(value)))
        later = real.normalize(delivery(11, value))
        self.assertEqual(one.events, ())
        self.assertEqual(one.account[0].field, "message_template_status_update")
        self.assertTrue(one.account[0].key.startswith("acct:"))
        self.assertEqual(one.account[0].key, again.account[0].key)
        self.assertNotEqual(one.account[0].key, later.account[0].key)
        self.assertIsNone(one.account[0].phone_number_id)

    def test_normalize__a_huge_account_event_keeps_only_its_field_names(self):
        value = {"alert_type": "x", "blob": "y" * (real.MAX_ACCOUNT_CHARS + 1)}
        change = {"field": "account_alerts", "value": value}
        payload = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [change]}]}
        self.assertEqual(real.normalize(payload).account[0].value, {"truncated": True, "keys": ["alert_type", "blob"]})

    def test_normalize__a_change_without_a_field_is_an_issue(self):
        payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {}}, {"field": 5}]}]}
        self.assertEqual([i.where for i in real.normalize(payload).issues],
                         ["entry[0].changes[0].field", "entry[0].changes[1].field"])


REPLACEMENTS = (None, True, 0, -1, 1.5, "", "x", "\x00", "\ud83d", [], {}, [None], {"a": 1}, "US.1", "1" * 400)
SWEEP = ("text_bsuid_username_no_phone", "status_failed_131049", "system_user_changed_number",
         "contacts_request_contact_info", "interactive_131000_no_body", "location_shared", "referral_ctwa_text")
KEYS = ("object", "entry", "id", "changes", "field", "value", "messages", "statuses", "contacts", "metadata",
        "phone_number_id", "from", "from_user_id", "user_id", "wa_id", "profile", "name", "username", "type", "text",
        "body", "timestamp", "status", "recipient_id", "errors", "code", "context", "interactive", "button_reply",
        "title", "location", "latitude", "longitude", "system", "image", "caption", "reaction", "emoji", "referral",
        "origin", "phones", "group_id", "recipient_type")
LEAVES = (None, True, False, 0, 7, -3, 2.5, "", "x", "1234567", "US.1", "whatsapp_business_account", "messages",
          "text", "image", "interactive", "system", "contacts", "delivered", "failed", "\x00", "wamid.R", NUMBER)


def paths(node: object, prefix: tuple = ()):
    """Every position (as a key path) of a JSON-like tree, containers included."""
    yield prefix
    if isinstance(node, dict):
        for key, child in node.items():
            yield from paths(child, prefix + (key,))
    elif isinstance(node, list):
        for index, child in enumerate(node):
            yield from paths(child, prefix + (index,))


def replaced(tree: object, path: tuple, value: object) -> object:
    clone = json.loads(json.dumps(tree))
    if not path:
        return value
    holder = clone
    for step in path[:-1]:
        holder = holder[step]
    holder[path[-1]] = value
    return clone


def random_tree(rng: random.Random, depth: int) -> object:
    pick = rng.random()
    if depth <= 0 or pick < 0.35:
        return rng.choice(LEAVES)
    if pick < 0.7:
        return {rng.choice(KEYS): random_tree(rng, depth - 1) for _ in range(rng.randint(0, 5))}
    return [random_tree(rng, depth - 1) for _ in range(rng.randint(0, 4))]


class NeverRaisesTests(unittest.TestCase):
    def test_never_raises__replacing_every_position_of_real_payloads(self):
        for name in SWEEP:
            tree = load_fixture(name)["payload"]
            for path in paths(tree):
                with self.subTest(fixture=name, path=path):
                    for value in REPLACEMENTS:
                        self.assertIsInstance(real.normalize(replaced(tree, path, value)), Delivery)

    def test_never_raises__random_trees_and_what_comes_out_is_database_safe(self):
        rng = random.Random(20261123)
        for _ in range(3000):
            change = {"field": rng.choice(("messages", "messages", "other")), "value": random_tree(rng, 5)}
            payload = {"object": "whatsapp_business_account", "entry": [{"changes": [change]}, random_tree(rng, 3)]}
            result = real.normalize(payload)
            for event in result.events:
                assert_db_safe(self, event)
            self.assertIsInstance(real.normalize(random_tree(rng, 4)), Delivery)


MUTATIONS = (
    ("only the first message of a delivery",
     'enumerate(as_list(value.get("messages")))', 'enumerate(as_list(value.get("messages"))[:1])'),
    ("only the first entry of a delivery",
     'enumerate(as_list(body.get("entry")))', 'enumerate(as_list(body.get("entry"))[:1])'),
    ("a status key without the status",
     'f"{msg_id}:{state}"', "msg_id"),
    ("a phone is required",
     "not (who.phone or who.bsuid)", "not who.phone"),
    ("system messages keep no identity of their own",
     '        who = _system_who(who, as_dict(message.get("system")))', "        pass"),
    ("a system message ignores the new phone",
     'phone=clean_phone(change.get("wa_id")) or who.phone', "phone=who.phone"),
    ("no dedupe inside a delivery",
     "elif event.key in self.seen:", "elif False:"),
    ("no per-delivery limit",
     "elif len(self.events) >= MAX_EVENTS:", "elif False:"),
)


def load_mutant(old: str, new: str):
    source = SOURCE.read_text(encoding="utf-8")
    assert source.count(old) == 1, f"mutation target must appear once: {old!r}"
    module = types.ModuleType("normalizer_mutant")
    exec(compile(source.replace(old, new), str(SOURCE), "exec"), module.__dict__)
    return module


class SabotageTests(unittest.TestCase):
    """Break the module on purpose in 8 ways: the behaviour checks must notice every one (conventions 7.3)."""

    def test_sabotage__the_checks_pass_on_the_real_module(self):
        self.assertEqual(behavior_failures(real.normalize), [])

    def test_sabotage__every_mutation_is_caught(self):
        for label, old, new in MUTATIONS:
            with self.subTest(mutation=label):
                self.assertNotEqual(behavior_failures(load_mutant(old, new).normalize), [], label)


if __name__ == "__main__":
    unittest.main()
