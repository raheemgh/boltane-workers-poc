"""test_identity - L1 tests for inbound/identity.py: phone, BSUID, parent BSUID, username and contact matching.

Level:   L1
Stage:   V2-02A
"""

import unittest

from app.v2.inbound.identity import (
    MAX_BSUID,
    MESSAGE_KEYS,
    STATUS_KEYS,
    clean_bsuid,
    clean_phone,
    read_who,
    split_identifier,
)

BSUID = "US.13491208655302741918"
PARENT = "US.ENT.11815799212886844830"


class PhoneAndBsuidTests(unittest.TestCase):
    def test_clean_phone__accepts_digits_and_drops_a_plus(self):
        self.assertEqual(clean_phone("16505551234"), "16505551234")
        self.assertEqual(clean_phone("+16505551234"), "16505551234")

    def test_clean_phone__refuses_everything_else(self):
        for value in ("12345", "1" * 16, "٣٣٣٣٣٣٣٣٣", "1234 5678", BSUID, "120363025@g.us", "", None, 16505551234, ["1"]):
            self.assertIsNone(clean_phone(value), value)

    def test_clean_bsuid__accepts_user_and_parent_ids(self):
        self.assertEqual(clean_bsuid(BSUID), BSUID)
        self.assertEqual(clean_bsuid(PARENT), PARENT)

    def test_clean_bsuid__refuses_what_is_not_the_shape(self):
        for value in ("us.123", "USA.123", "U.123", "US.", "US..1", "US.12 3", "US.12-3", "16505551234", "", None, 5, "US.é1"):
            self.assertIsNone(clean_bsuid(value), value)

    def test_clean_bsuid__length_boundary(self):
        self.assertIsNotNone(clean_bsuid("US." + "1" * (MAX_BSUID - 3)))
        self.assertIsNone(clean_bsuid("US." + "1" * (MAX_BSUID - 2)))

    def test_split_identifier__sorts_a_phone_or_a_bsuid(self):
        self.assertEqual(split_identifier("16505551234"), ("16505551234", None))
        self.assertEqual(split_identifier(BSUID), (None, BSUID))
        self.assertEqual(split_identifier("nonsense"), (None, None))


class ReadWhoTests(unittest.TestCase):
    def test_read_who__phone_and_bsuid_from_the_message(self):
        message = {"from": "16505551234", "from_user_id": BSUID}
        contacts = [{"profile": {"name": "Pablo", "username": "pablo"}, "wa_id": "16505551234", "user_id": BSUID}]
        who = read_who(message, contacts, MESSAGE_KEYS)
        self.assertEqual((who.phone, who.bsuid, who.username, who.name), ("16505551234", BSUID, "pablo", "Pablo"))

    def test_read_who__bsuid_only_when_the_phone_is_omitted(self):
        message = {"from_user_id": BSUID, "from_parent_user_id": PARENT}
        who = read_who(message, [{"profile": {"name": "S"}, "user_id": BSUID}], MESSAGE_KEYS)
        self.assertEqual((who.phone, who.bsuid, who.parent_bsuid), (None, BSUID, PARENT))

    def test_read_who__legacy_message_has_a_phone_only(self):
        who = read_who({"from": "963999000111"}, [{"profile": {"name": "Sim"}, "wa_id": "963999000111"}], MESSAGE_KEYS)
        self.assertEqual((who.phone, who.bsuid), ("963999000111", None))

    def test_read_who__a_bsuid_in_the_from_field_is_never_a_phone(self):
        who = read_who({"from": BSUID}, [], MESSAGE_KEYS)
        self.assertEqual((who.phone, who.bsuid), (None, BSUID))

    def test_read_who__picks_the_contact_by_bsuid_then_by_phone(self):
        contacts = [{"profile": {"name": "Other"}, "wa_id": "111111111", "user_id": "US.1"},
                    {"profile": {"name": "Right"}, "wa_id": "222222222", "user_id": "US.2"}]
        self.assertEqual(read_who({"from_user_id": "US.2"}, contacts, MESSAGE_KEYS).name, "Right")
        self.assertEqual(read_who({"from": "222222222"}, contacts, MESSAGE_KEYS).name, "Right")
        self.assertIsNone(read_who({"from": "333333333"}, contacts, MESSAGE_KEYS).name)  # two contacts, no match

    def test_read_who__nobody_known_gives_an_empty_who(self):
        self.assertEqual(read_who({}, [], MESSAGE_KEYS).to_dict(), {})
        self.assertEqual(read_who({"from": None, "from_user_id": 5}, ["x", None, 3], MESSAGE_KEYS).to_dict(), {})

    def test_read_who__status_keys_read_the_recipient_and_ignore_a_group_id(self):
        status = {"recipient_id": "120363025246125486@g.us", "recipient_user_id": BSUID}
        who = read_who(status, [], STATUS_KEYS)
        self.assertEqual((who.phone, who.bsuid), (None, BSUID))


if __name__ == "__main__":
    unittest.main()
