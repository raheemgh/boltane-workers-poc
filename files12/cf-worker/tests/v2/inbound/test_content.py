"""test_content - L1 tests for inbound/content.py: what each Meta message type becomes, and that nothing raises.

Level:   L1
Stage:   V2-02A
"""

import unittest

from app.v2.core.events import MESSAGE_TYPES
from app.v2.inbound.content import read_content

WEIRD = (None, True, 0, 1.5, "", "x", [], [1], {}, {"a": 1}, float("nan"), "\x00")
KNOWN = ("text", "interactive", "button", "location", "reaction", "image", "audio", "video", "document",
         "sticker", "contacts", "order", "system", "request_welcome", "unsupported", "ephemeral", "")
REPLY = {"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": "b1", "title": "Yes"}}}
LIST = {"type": "interactive", "interactive": {"type": "list_reply",
                                               "list_reply": {"id": "r1", "title": "Menu", "description": "Today"}}}
SYSTEM = {"type": "system", "system": {"type": "user_changed_user_id", "user_id": "US.2", "previous_user_id": "bad",
                                       "wa_id": "16505559999"}}

CASES = (
    ("text", {"type": "text", "text": {"body": "hi"}}, {"type": "text", "text": "hi"}),
    ("text_with_an_empty_body_is_kept", {"type": "text", "text": {"body": ""}}, {"type": "text", "text": ""}),
    ("text_without_a_body", {"type": "text"}, {"type": "unsupported", "orig_type": "text"}),
    ("button_reply", REPLY, {"type": "interactive", "text": "Yes", "interactive": {"kind": "button_reply", "id": "b1"}}),
    ("list_reply", LIST, {"text": "Menu", "interactive": {"kind": "list_reply", "id": "r1", "description": "Today"}}),
    ("flow_reply", {"type": "interactive", "interactive": {"type": "nfm_reply", "nfm_reply": {"name": "f"}}},
     {"type": "unsupported", "orig_type": "interactive"}),
    ("template_button", {"type": "button", "button": {"text": "Track", "payload": "p1"}},
     {"type": "button", "text": "Track", "interactive": {"kind": "template_button", "id": "p1"}}),
    ("location_with_a_name", {"type": "location", "location": {"latitude": 33.5, "longitude": 36.3, "name": "Shop",
                                                              "address": "Main st"}},
     {"type": "location", "text": "[location] Shop, Main st (33.5, 36.3)"}),
    ("location_bare", {"type": "location", "location": {"latitude": 33.5, "longitude": 36.3}},
     {"text": "[location] (33.5, 36.3)", "location": {"lat": 33.5, "lon": 36.3}}),
    ("reaction", {"type": "reaction", "reaction": {"message_id": "w1", "emoji": "👍"}},
     {"type": "reaction", "reaction": {"to": "w1", "emoji": "👍"}}),
    ("reaction_removed", {"type": "reaction", "reaction": {"message_id": "w1"}}, {"reaction": {"to": "w1"}}),
    ("image_with_a_caption", {"type": "image", "image": {"id": "i1", "mime_type": "image/jpeg", "caption": "look"}},
     {"type": "image", "text": "look", "media": {"id": "i1", "mime": "image/jpeg"}}),
    ("document", {"type": "document", "document": {"id": "d1", "mime_type": "application/pdf", "filename": "menu.pdf"}},
     {"type": "document", "media": {"id": "d1", "mime": "application/pdf", "filename": "menu.pdf"}}),
    ("video", {"type": "video", "video": {"id": "v1", "mime_type": "video/mp4"}}, {"type": "video", "media": {"id": "v1"}}),
    ("order", {"type": "order"}, {"type": "order"}),
    ("welcome_request", {"type": "request_welcome"}, {"type": "request_welcome"}),
    ("system_keeps_only_valid_ids", SYSTEM,
     {"type": "system", "system": {"type": "user_changed_user_id", "user_id": "US.2", "phone": "16505559999"}}),
    ("unknown_type_with_errors", {"type": "ephemeral", "errors": [{"code": 131051}, {"code": "x"}, "junk", {"code": True}]},
     {"type": "unsupported", "orig_type": "ephemeral", "meta_errors": [131051]}),
)


def covers(actual: object, expected: object) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and covers(actual[k], v) for k, v in expected.items())
    return actual == expected


class ContentTests(unittest.TestCase):
    def test_read_content__each_case_reads_what_it_should(self):
        for label, message, want in CASES:
            with self.subTest(case=label):
                got = read_content(message)
                self.assertTrue(covers(got, want), f"{got!r} does not cover {want!r}")

    def test_read_content__media_and_reactions_have_no_text_unless_captioned(self):
        for kind in ("audio", "sticker", "reaction", "contacts", "order", "system"):
            self.assertNotIn("text", read_content({"type": kind}), kind)

    def test_read_content__texts_are_cleaned_and_cut(self):
        self.assertEqual(len(read_content({"type": "text", "text": {"body": "a" * 5000}})["text"]), 4096)
        long_title = dict(REPLY, interactive={"type": "button_reply", "button_reply": {"id": "b", "title": "t" * 300}})
        self.assertEqual(len(read_content(long_title)["text"]), 200)
        self.assertEqual(read_content({"type": "text", "text": {"body": "a\x00b"}})["text"], "ab")

    def test_read_content__a_phone_is_shared_only_when_the_user_asked_to_share(self):
        card = {"phones": [{"phone": "+1 650 555 1234", "wa_id": "16505551234"}]}
        other = read_content({"type": "contacts", "contacts": [dict(card, origin="other")]})
        message_level = read_content({"type": "contacts", "origin": "contact_request", "contacts": [card]})
        card_level = read_content({"type": "contacts", "contacts": [dict(card, origin="contact_request")]})
        self.assertNotIn("shared_phone", other)
        self.assertEqual(message_level["shared_phone"], "16505551234")
        self.assertEqual(card_level["shared_phone"], "16505551234")

    def test_read_content__a_broken_body_never_raises_and_the_type_stays_in_the_closed_set(self):
        for kind in KNOWN:
            for weird in WEIRD:
                message = {"type": kind, kind: weird, "errors": weird, "context": weird}
                with self.subTest(kind=kind, weird=repr(weird)):
                    self.assertIn(read_content(message)["type"], MESSAGE_TYPES)

    def test_read_content__a_type_that_is_not_text_is_unsupported(self):
        for kind in (None, 5, ["text"], {"a": 1}, True):
            self.assertEqual(read_content({"type": kind})["type"], "unsupported")


if __name__ == "__main__":
    unittest.main()
