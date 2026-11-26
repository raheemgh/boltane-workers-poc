"""test_safe - L1 tests for core/safe.py: the readers never raise and refuse what PostgreSQL would refuse.

Level:   L1
Stage:   V2-02A
"""

import unittest

from app.v2.core.safe import MAX_TEXT, as_bool, as_dict, as_int, as_list, as_number, clean_text, compact

WEIRD = (None, True, False, 0, 1, -1, 1.5, "", "x", [], [1], {}, {"a": 1}, b"x", (1,), float("nan"), float("inf"))


class SafeTests(unittest.TestCase):
    def test_as_dict__returns_only_dicts(self):
        for value in WEIRD:
            self.assertEqual(as_dict(value), value if isinstance(value, dict) else {})

    def test_as_list__returns_only_lists(self):
        for value in WEIRD:
            self.assertEqual(as_list(value), value if isinstance(value, list) else [])

    def test_clean_text__removes_nul_and_lone_surrogates(self):
        self.assertEqual(clean_text("a\x00b"), "ab")
        self.assertEqual(clean_text("x\ud83dy"), "x?y")
        clean_text("\ud83d\x00ok").encode("utf-8")  # what the database driver does; must not raise

    def test_clean_text__keeps_arabic_and_emoji(self):
        text = "مرحبا 👍 كيف الحال؟"
        self.assertEqual(clean_text(text), text)

    def test_clean_text__cuts_to_the_limit(self):
        self.assertEqual(len(clean_text("a" * (MAX_TEXT + 10))), MAX_TEXT)
        self.assertEqual(clean_text("abcdef", 3), "abc")

    def test_clean_text__anything_but_a_str_gives_none(self):
        for value in WEIRD:
            if not isinstance(value, str):
                self.assertIsNone(clean_text(value))

    def test_as_int__takes_ints_and_short_digit_strings_only(self):
        cases = ((5, 5), ("1749416383", 1749416383), (True, None), ("12a", None), ("", None), (1.5, None),
                 ("9" * 19, None), ("٣٣", None), (None, None), ("-5", None))
        for value, expected in cases:
            self.assertEqual(as_int(value), expected, value)

    def test_as_number__refuses_bool_nan_and_infinity(self):
        self.assertEqual(as_number(38.98), 38.98)
        self.assertEqual(as_number(-5), -5)
        for value in (True, None, "1", float("nan"), float("inf"), float("-inf"), [1]):
            self.assertIsNone(as_number(value))

    def test_as_bool__only_real_booleans_pass(self):
        self.assertIs(as_bool(True), True)
        self.assertIs(as_bool(False), False)
        for value in (None, 0, 1, "true", [], {}):
            self.assertIsNone(as_bool(value))

    def test_compact__drops_none_and_keeps_falsy_values(self):
        self.assertEqual(compact({"a": None, "b": 0, "c": "", "d": False, "e": [], "f": 1}),
                         {"b": 0, "c": "", "d": False, "e": [], "f": 1})


if __name__ == "__main__":
    unittest.main()
