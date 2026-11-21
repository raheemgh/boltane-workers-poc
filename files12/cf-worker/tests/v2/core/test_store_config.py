"""test_store_config - tests for validate_store_config: each of the six settings, good and bad.

Level:   L1
Stage:   V2-01
"""

import unittest
from datetime import date

from app.v2.core.store_config import ConfigIssue, validate_store_config

WEEK = {d: [["09:00", "22:00"]] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


def codes(config):
    return sorted((i.field, i.code) for i in validate_store_config(config))


class StoreConfigTest(unittest.TestCase):
    def test_store_config__a_full_valid_config_has_no_issues(self):
        config = {"features": {"llm_reasoning_off": True}, "timezone": "Asia/Damascus", "business_hours": WEEK,
                  "default_language": "en", "billing_anchor": "2026-11-05", "owner_phone": "963999000111"}
        self.assertEqual(validate_store_config(config), [])

    def test_store_config__empty_and_unrelated_keys_are_fine(self):
        self.assertEqual(validate_store_config({}), [])
        self.assertEqual(validate_store_config({"store_name": 5, "package": "x"}), [])

    def test_store_config__nullable_fields_accept_none(self):
        config = {"timezone": None, "business_hours": None, "billing_anchor": None, "owner_phone": None}
        self.assertEqual(validate_store_config(config), [])

    def test_store_config__not_nullable_fields_reject_none(self):
        self.assertEqual(codes({"features": None, "default_language": None}),
                         [("default_language", "wrong_type"), ("features", "wrong_type")])

    def test_store_config__not_a_mapping(self):
        self.assertEqual(validate_store_config([]), [ConfigIssue("", "wrong_type")])

    def test_store_config__features_must_be_registered_bools(self):
        self.assertEqual(codes({"features": {"nope": True, "llm_reasoning_off": "yes"}}),
                         [("features.llm_reasoning_off", "wrong_type"), ("features.nope", "unknown_flag")])

    def test_store_config__language(self):
        self.assertEqual(codes({"default_language": "fr"}), [("default_language", "bad_value")])
        self.assertEqual(codes({"default_language": 1}), [("default_language", "wrong_type")])

    def test_store_config__timezone(self):
        for good in ("UTC", "Asia/Damascus", "America/Argentina/Buenos_Aires", "Etc/GMT+3"):
            self.assertEqual(validate_store_config({"timezone": good}), [], good)
        for bad in ("Damascus", "", "Asia/", "a b/c", "UTC\n", "A/" + "b" * 70):
            self.assertEqual(codes({"timezone": bad}), [("timezone", "bad_format")], bad)

    def test_store_config__owner_phone_is_digits_only(self):
        for bad in ("+963999000111", "12345", "1" * 16, "96399 9000111", "963999000111\n"):
            self.assertEqual(codes({"owner_phone": bad}), [("owner_phone", "bad_format")], bad)
        self.assertEqual(codes({"owner_phone": 963999000111}), [("owner_phone", "wrong_type")])

    def test_store_config__billing_anchor(self):
        self.assertEqual(validate_store_config({"billing_anchor": date(2026, 11, 5)}), [])
        self.assertEqual(codes({"billing_anchor": "2026-02-30"}), [("billing_anchor", "bad_value")])
        self.assertEqual(codes({"billing_anchor": "05/11/2026"}), [("billing_anchor", "bad_format")])
        self.assertEqual(codes({"billing_anchor": 20261105}), [("billing_anchor", "wrong_type")])

    def test_store_config__hours_need_all_seven_days(self):
        short = {k: v for k, v in WEEK.items() if k != "fri"}
        self.assertEqual(codes({"business_hours": short}), [("business_hours.fri", "missing")])
        self.assertEqual(codes({"business_hours": {**WEEK, "xyz": []}}), [("business_hours.xyz", "unknown_day")])

    def test_store_config__hours_closed_overnight_and_midnight_are_valid(self):
        hours = {**WEEK, "fri": [], "sat": [["18:00", "02:00"]], "sun": [["08:00", "12:00"], ["16:00", "24:00"]]}
        self.assertEqual(validate_store_config({"business_hours": hours}), [])

    def test_store_config__hours_bad_intervals(self):
        cases = {
            "mon": ([["9:00", "22:00"]], "bad_format"),
            "tue": ([["24:00", "10:00"]], "bad_format"),
            "wed": ([["10:00", "10:00"]], "bad_value"),
            "thu": ([["01:00", "02:00"]] * 5, "bad_value"),
            "fri": ([["01:00"]], "wrong_type"),
            "sat": ("open", "wrong_type"),
        }
        for day, (value, code) in cases.items():
            issues = codes({"business_hours": {**WEEK, day: value}})
            self.assertEqual(len(issues), 1, day)
            self.assertEqual(issues[0][1], code, day)
            self.assertTrue(issues[0][0].startswith(f"business_hours.{day}"))


if __name__ == "__main__":
    unittest.main()
