"""test_events - L1 tests for core/events.py: constants stay consistent and the value types behave.

Level:   L1
Stage:   V2-02A
"""

import dataclasses
import unittest

from app.v2.core import events
from app.v2.core.errors import ErrorCode


class EventsTests(unittest.TestCase):
    def test_constants__every_type_constant_is_in_the_type_set(self):
        declared = {getattr(events, name) for name in dir(events) if name.startswith("TYPE_")}
        self.assertEqual(declared, set(events.MESSAGE_TYPES))

    def test_constants__values_are_lowercase_text_without_spaces(self):
        names = [n for n in dir(events) if n.startswith(("TYPE_", "KIND_", "STATUS_"))]
        for name in names:
            value = getattr(events, name)
            self.assertTrue(value.islower() and " " not in value, name)

    def test_who__to_dict_leaves_out_empty_fields(self):
        self.assertEqual(events.Who(phone="16505551234", name="").to_dict(), {"phone": "16505551234"})
        self.assertEqual(events.Who().to_dict(), {})

    def test_who__is_frozen(self):
        who = events.Who(phone="1")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            who.phone = "2"

    def test_delivery__defaults_are_empty_tuples(self):
        delivery = events.Delivery()
        self.assertEqual((delivery.events, delivery.account, delivery.issues, delivery.phone_number_ids),
                         ((), (), (), ()))

    def test_issue__carries_a_code_from_the_error_taxonomy(self):
        issue = events.Issue(ErrorCode.INBOUND_MALFORMED, "entry[0]")
        self.assertEqual(issue.code.value, "INBOUND_MALFORMED")


if __name__ == "__main__":
    unittest.main()
