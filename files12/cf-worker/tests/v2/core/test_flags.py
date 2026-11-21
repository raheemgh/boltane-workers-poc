"""test_flags - tests for the flag registry and resolver: defaults, env parsing, gating, overrides.

Level:   L1
Stage:   V2-01
"""

import unittest

from app.v2.core.flags import DEFAULT_SETTINGS, FLAGS, REGISTRY, is_enabled, parse_settings


class RegistryTest(unittest.TestCase):
    def test_flags__registry_shape(self):
        self.assertEqual(len(REGISTRY), len(FLAGS))
        for flag in FLAGS:
            self.assertRegex(flag.name, r"^[a-z][a-z0-9_]*$")
            self.assertIs(flag.default, False)
            self.assertTrue(flag.description.strip())
            self.assertRegex(flag.stage, r"^V2-\d\d[A-Z]?$")

    def test_flags__only_the_pipeline_is_gated(self):
        self.assertEqual([f.name for f in FLAGS if f.gated], ["v2_pipeline"])

    def test_flags__everything_is_off_by_default(self):
        for name in REGISTRY:
            self.assertFalse(is_enabled(name, DEFAULT_SETTINGS))
            self.assertFalse(is_enabled(name, DEFAULT_SETTINGS, phone_number_id="123456789012345"))


class ParseTest(unittest.TestCase):
    def test_flags__parse_empty_and_none(self):
        self.assertEqual(parse_settings(None, None), DEFAULT_SETTINGS)
        self.assertEqual(parse_settings("", "  "), DEFAULT_SETTINGS)

    def test_flags__parse_lists_with_mixed_separators_and_case(self):
        s = parse_settings("V2_Pipeline, llm_reasoning_off;llm_reasoning_off", "11111 22222,33333")
        self.assertEqual(s.enabled, frozenset({"v2_pipeline", "llm_reasoning_off"}))
        self.assertEqual(s.allowlist, frozenset({"11111", "22222", "33333"}))

    def test_flags__unknown_names_are_reported_not_enabled(self):
        s = parse_settings("v2_pipeline,typo_flag", None)
        self.assertEqual(s.unknown_flags, ("typo_flag",))
        self.assertEqual(s.enabled, frozenset({"v2_pipeline"}))

    def test_flags__bad_allowlist_entries_are_reported(self):
        s = parse_settings(None, "12345,abc,+96399,*")
        self.assertEqual(s.invalid_allowlist, ("+96399", "abc"))
        self.assertTrue(s.allow_all)
        self.assertEqual(s.allowlist, frozenset({"12345"}))


class GatedTest(unittest.TestCase):
    ON = parse_settings("v2_pipeline", "111111")

    def test_flags__pipeline_needs_flag_and_allowlist(self):
        self.assertTrue(is_enabled("v2_pipeline", self.ON, phone_number_id="111111"))
        self.assertTrue(is_enabled("v2_pipeline", self.ON, phone_number_id=111111))
        self.assertFalse(is_enabled("v2_pipeline", self.ON, phone_number_id="222222"))
        self.assertFalse(is_enabled("v2_pipeline", self.ON))
        self.assertFalse(is_enabled("v2_pipeline", parse_settings(None, "111111"), phone_number_id="111111"))
        self.assertFalse(is_enabled("v2_pipeline", parse_settings("v2_pipeline", None), phone_number_id="111111"))

    def test_flags__star_allows_every_store(self):
        s = parse_settings("v2_pipeline", "*")
        self.assertTrue(is_enabled("v2_pipeline", s, phone_number_id="999999"))
        self.assertTrue(is_enabled("v2_pipeline", s))

    def test_flags__a_store_can_turn_the_pipeline_off_but_never_on(self):
        self.assertFalse(is_enabled("v2_pipeline", self.ON, store_features={"v2_pipeline": False}, phone_number_id="111111"))
        off = parse_settings(None, "111111")
        self.assertFalse(is_enabled("v2_pipeline", off, store_features={"v2_pipeline": True}, phone_number_id="111111"))
        outside = is_enabled("v2_pipeline", self.ON, store_features={"v2_pipeline": True}, phone_number_id="222222")
        self.assertFalse(outside)


class OverrideTest(unittest.TestCase):
    def test_flags__store_value_beats_the_global_list(self):
        on = parse_settings("llm_reasoning_off", None)
        self.assertTrue(is_enabled("llm_reasoning_off", on))
        self.assertFalse(is_enabled("llm_reasoning_off", on, store_features={"llm_reasoning_off": False}))
        self.assertTrue(is_enabled("llm_reasoning_off", DEFAULT_SETTINGS, store_features={"llm_reasoning_off": True}))

    def test_flags__non_bool_overrides_are_ignored(self):
        on = parse_settings("llm_reasoning_off", None)
        for junk in (1, 0, "true", None, [], {}):
            self.assertTrue(is_enabled("llm_reasoning_off", on, store_features={"llm_reasoning_off": junk}))
            self.assertFalse(is_enabled("llm_reasoning_off", DEFAULT_SETTINGS, store_features={"llm_reasoning_off": junk}))

    def test_flags__bad_features_object_is_ignored(self):
        on = parse_settings("llm_reasoning_off", None)
        for junk in (None, [], "x", 5):
            self.assertTrue(is_enabled("llm_reasoning_off", on, store_features=junk))

    def test_flags__unknown_name_is_off(self):
        self.assertFalse(is_enabled("nope", parse_settings("nope", "*"), store_features={"nope": True}))


if __name__ == "__main__":
    unittest.main()
