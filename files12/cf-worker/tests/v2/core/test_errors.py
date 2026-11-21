"""test_errors - tests for the V2 error taxonomy: complete specs, stable names, prefix rules.

Level:   L1
Stage:   V2-01
"""

import unittest

from app.v2.core.errors import PREFIX_CATEGORY, SPECS, Category, ErrorCode, V2Error, spec_for


class ErrorTaxonomyTest(unittest.TestCase):
    def test_errors__every_code_has_a_spec(self):
        self.assertEqual(set(SPECS), set(ErrorCode))

    def test_errors__value_equals_name_and_is_upper_snake(self):
        for code in ErrorCode:
            self.assertEqual(code.value, code.name)
            self.assertRegex(code.value, r"^[A-Z]+(_[A-Z]+)+$")

    def test_errors__category_follows_the_prefix(self):
        for code in ErrorCode:
            prefix = next(p for p in PREFIX_CATEGORY if code.value.startswith(p))
            self.assertIs(SPECS[code].category, PREFIX_CATEGORY[prefix], code.value)

    def test_errors__every_category_is_used(self):
        self.assertEqual({spec.category for spec in SPECS.values()}, set(Category))

    def test_errors__meta_classes_from_the_v2_03_card_exist(self):
        wanted = {"META_AUTH", "META_WINDOW_CLOSED", "META_PERMANENT", "META_TEMPORARY",
                  "META_PAYMENT", "META_OPTOUT", "META_UNSUPPORTED", "META_BSUID"}
        self.assertEqual({c.value for c in ErrorCode if c.value.startswith("META_")}, wanted)

    def test_errors__retry_and_alert_hints(self):
        self.assertTrue(spec_for(ErrorCode.META_TEMPORARY).retryable)
        self.assertFalse(spec_for(ErrorCode.META_PERMANENT).retryable)
        self.assertTrue(spec_for(ErrorCode.META_AUTH).alert)
        self.assertFalse(spec_for(ErrorCode.GATE_RATE_LIMITED).alert)
        self.assertTrue(spec_for(ErrorCode.INTERNAL_BUG).alert)

    def test_errors__every_spec_explains_itself(self):
        for code, spec in SPECS.items():
            self.assertTrue(spec.meaning.endswith("."), code.value)

    def test_errors__exception_message_is_the_code_only(self):
        error = V2Error(ErrorCode.DB_UNAVAILABLE, stage="gate")
        self.assertEqual(str(error), "DB_UNAVAILABLE")
        self.assertIs(error.code, ErrorCode.DB_UNAVAILABLE)
        self.assertIs(error.spec.category, Category.DATA)

    def test_errors__context_is_a_copy(self):
        error = V2Error(ErrorCode.INTERNAL_BUG, stage="send")
        error.context["stage"] = "changed"
        self.assertEqual(V2Error(ErrorCode.INTERNAL_BUG, stage="send").context, {"stage": "send"})


if __name__ == "__main__":
    unittest.main()
