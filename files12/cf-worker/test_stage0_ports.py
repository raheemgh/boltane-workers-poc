"""
Stage 0 characterization tests for the two verbatim-ported modules,
app/services/token_limits.py and app/services/pii_masking.py.

Why this file exists: the existing test suite (test_usage_protection.py,
test_package_system.py) never calls estimate_tokens() or
mask_phone_numbers() directly. It only reaches token_limits.py through
webhook.py / supabase_client.py / ai_client.py, which are later-stage
files that don't exist in cf-worker/ yet. So these tests pin the
current behaviour of the two ported modules directly. They assert what
the ORIGINAL Render modules do — run them against either copy:

    # against the Workers-side ports
    cd cf-worker && PYTHONPATH=src python test_stage0_ports.py

    # against the Render originals (from the repo root)
    PYTHONPATH=. python cf-worker/test_stage0_ports.py

Pure stdlib, no network, no credentials.
"""

from app.services import pii_masking, token_limits
from app.services.pii_masking import mask_phone_numbers
from app.services.token_limits import estimate_tokens


def test_constants_unchanged():
    assert token_limits.MAX_INPUT_TOKENS_PER_MESSAGE == 300
    assert token_limits.MAX_OUTPUT_TOKENS_PER_MESSAGE == 300
    assert token_limits.CONVERSATION_TOKEN_CAP == 3000
    assert token_limits.SYSTEM_PROMPT_TARGET_TOKENS_MIN == 500
    assert token_limits.SYSTEM_PROMPT_TARGET_TOKENS_MAX == 700
    print("PASS: token_limits constants unchanged")


def test_estimate_tokens_rounds_up_and_handles_empty():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0
    assert estimate_tokens("a") == 1
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens("مرحبا") == 2
    print("PASS: estimate_tokens rounds up (~4 chars/token) and treats empty as 0")


def test_estimate_tokens_input_cap_boundary():
    cap = token_limits.MAX_INPUT_TOKENS_PER_MESSAGE
    assert estimate_tokens("a" * (cap * 4)) == cap          # exactly at the cap: allowed
    assert estimate_tokens("a" * (cap * 4 + 1)) == cap + 1  # one char over: rejected by webhook.py
    print("PASS: estimate_tokens boundary around MAX_INPUT_TOKENS_PER_MESSAGE")


def test_mask_common_syrian_number_formats():
    cases = {
        "call 0999 123 456 now": ("call [PHONE_1] now", ["0999 123 456"]),
        "+963-999-123-456": ("[PHONE_1]", ["+963-999-123-456"]),
        "00963 999 123 456": ("[PHONE_1]", ["00963 999 123 456"]),
        "0999123456": ("[PHONE_1]", ["0999123456"]),
        "999123456": ("[PHONE_1]", ["999123456"]),
        "رقمي 0999123456 شكرا": ("رقمي [PHONE_1] شكرا", ["0999123456"]),
    }
    for text, (masked, found) in cases.items():
        result = mask_phone_numbers(text)
        assert result.masked_text == masked, (text, result.masked_text)
        assert result._found == found, (text, result._found)
    print("PASS: mask_phone_numbers masks the documented Syrian number formats")


def test_mask_leaves_non_phone_text_alone():
    result = mask_phone_numbers("no phone here 12345")
    assert result.masked_text == "no phone here 12345"
    assert result._found == []
    print("PASS: text without a phone number is returned unchanged")


def test_mask_numbers_are_numbered_in_order_and_restore_roundtrips():
    text = "two: 0999123456 and 0988.777.666"
    result = mask_phone_numbers(text)
    assert result.masked_text == "two: [PHONE_1] and [PHONE_2]"
    assert result.restore(result.masked_text) == text
    assert result.restore("reply to [PHONE_1] and [PHONE_2]") == "reply to 0999123456 and 0988.777.666"
    print("PASS: placeholders numbered in order; restore() round-trips")


def test_restore_is_noop_without_matching_placeholders():
    result = mask_phone_numbers("x 0999123456 y")
    assert result.restore("nothing to restore") == "nothing to restore"
    assert result.restore("unknown [PHONE_9] stays") == "unknown [PHONE_9] stays"
    assert mask_phone_numbers("plain").restore("plain") == "plain"
    print("PASS: restore() leaves text without matching placeholders untouched")


def main() -> None:
    test_constants_unchanged()
    test_estimate_tokens_rounds_up_and_handles_empty()
    test_estimate_tokens_input_cap_boundary()
    test_mask_common_syrian_number_formats()
    test_mask_leaves_non_phone_text_alone()
    test_mask_numbers_are_numbered_in_order_and_restore_roundtrips()
    test_restore_is_noop_without_matching_placeholders()
    print("\nALL PASS")


if __name__ == "__main__":
    main()
