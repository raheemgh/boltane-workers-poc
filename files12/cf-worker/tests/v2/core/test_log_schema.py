"""test_log_schema - tests for the structured log line: allowed fields, drops, rendering.

Level:   L1
Stage:   V2-01
"""

import json
import unittest

from app.v2.core.errors import ErrorCode
from app.v2.core.log_schema import FORBIDDEN, SCHEMA_VERSION, build_line, render

TS = "2026-11-21T10:20:30.123Z"
STORE = "3f2b8c1e-5d4a-4b6f-9a7e-0c1d2e3f4a5b"


class LogLineTest(unittest.TestCase):
    def test_log_schema__a_full_line_is_kept_whole(self):
        line = build_line(ts=TS, event="message", store_id=STORE, outcome="replied", model="deepseek/deepseek-v4-flash",
                          provider="DeepInfra", stages_ms={"gate": 12, "llm": 900, "total": 1200}, tokens_in=300,
                          tokens_out=120, cost_usd=0.0004, retries=0, error_code=ErrorCode.META_TEMPORARY.value)
        self.assertEqual(line.dropped, ())
        self.assertEqual(line.data["v"], SCHEMA_VERSION)
        self.assertEqual(line.data["level"], "info")

    def test_log_schema__forbidden_names_are_dropped_whatever_the_value(self):
        line = build_line(ts=TS, event="message", **{name: "x" for name in FORBIDDEN})
        self.assertEqual({name for name, _ in line.dropped}, set(FORBIDDEN))
        self.assertTrue(all(reason == "forbidden" for _, reason in line.dropped))
        self.assertFalse(set(line.data) & set(FORBIDDEN))

    def test_log_schema__unknown_and_invalid_fields_are_dropped(self):
        line = build_line(ts=TS, event="message", colour="red", outcome="great", store_id="not-a-uuid",
                          tokens_in=-1, tokens_out=True, cost_usd=float("nan"), error_code="MADE_UP",
                          stages_ms={"nope": 1}, model="has a space")
        reasons = dict(line.dropped)
        self.assertEqual(reasons.pop("colour"), "unknown")
        self.assertTrue(reasons and all(r == "invalid" for r in reasons.values()))
        self.assertEqual(set(line.data), {"v", "ts", "event", "level"})

    def test_log_schema__unhashable_values_do_not_raise(self):
        line = build_line(ts=TS, event=["message"], outcome={"a": 1}, level=[])
        self.assertIn(("event", "invalid"), line.dropped)

    def test_log_schema__missing_required_fields_are_reported(self):
        line = build_line(outcome="ignored")
        self.assertEqual({d for d in line.dropped}, {("ts", "missing"), ("event", "missing")})

    def test_log_schema__caller_cannot_set_the_version(self):
        self.assertIn(("v", "unknown"), build_line(ts=TS, event="job", v=99).dropped)

    def test_log_schema__render_is_compact_sorted_json(self):
        text = render(build_line(ts=TS, event="job", job="reaper", stages_ms={"total": 5}))
        self.assertEqual(text, json.dumps(json.loads(text), separators=(",", ":"), sort_keys=True))
        self.assertNotIn(" ", text)
        self.assertNotIn("\n", text)

    def test_log_schema__stage_dict_is_copied(self):
        stages = {"gate": 1}
        line = build_line(ts=TS, event="message", stages_ms=stages)
        stages["gate"] = 99
        self.assertEqual(line.data["stages_ms"], {"gate": 1})


if __name__ == "__main__":
    unittest.main()
