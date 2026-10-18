"""
PII masking before any text leaves this process to OpenRouter.

v2 scope: phone numbers from ANY country, in any common written form.
(v1 matched Syrian mobile numbers only, written with ASCII digits only —
see "What changed from v1" below.) Same public API as v1:
mask_phone_numbers(text) -> MaskResult, MaskResult.restore(text).

Names are deliberately NOT masked yet. Reliably telling an Arabic
given name apart from an ordinary word needs either a real NER model
or a maintained name dictionary; a naive regex would both miss real
names and flag ordinary words, corrupting conversation context either
way.

Deliberately NOT transliterating to ASCII/Latin letters first either.
Python's `re` module already handles Arabic Unicode text natively, so
there's nothing to gain there, and the hard part was never "the text
isn't ASCII" — it's telling PII apart from ordinary words, which a
letter-for-letter remapping doesn't help with. It would also make
restoring the exact original text less reliable, not more: several
distinct Arabic letters commonly collapse onto the same Latin letter
in a transliteration scheme, so the mapping loses information it
can't get back. (Digits are different: they are only READ in their
ASCII form to classify a candidate; the text that is replaced and later
restored is always the customer's original characters.)

What counts as a phone number (checked on the digits after converting
any Unicode decimal digit — Arabic-Indic ٠-٩, Persian ۰-۹ — to 0-9):

  international   starts with "+" or "00"; 8-15 digits after the prefix
                  (E.164 allows at most 15). Spaces, dots, dashes and
                  parentheses between groups are all fine:
                  +971 50 123 4567, +1 (415) 555-2671, 00963999123456,
                  +44 (0) 20 7946 0958
  national        one leading 0; 9-11 digits in at most 5 groups:
                  0999 123 456, 0501234567, 011 2345678, 07911 123456
  bare            no prefix. Either ONE run of 9-15 digits
                  (963999123456, 999123456, 4155552671) or 3-6 groups
                  of 2-4 digits (the first may be a single digit) joined
                  by ONE consistent separator, 9-15 digits in total
                  (999 123 456, 415-555-2671, 1 415 555 2671), or the
                  US form (415) 555-2671
  neighbours      two numbers side by side ("0999123456 0988123456")
                  are masked separately — each candidate run is split
                  into the longest valid numbers.

Why it errs towards masking: masking is fully reversible (restore()
puts the customer's own text back into the reply), so a false positive
only costs the model a little context for that one token, while a false
negative sends real personal data to a third party. The things that are
deliberately left alone: anything shorter than 9 digits (prices,
quantities, years, order numbers, 8-digit dates), thousands-grouped
amounts ("1,250,000" — comma is not a separator here), dd-mm-yyyy and
yyyy-mm-dd dates, times, coordinates, dotted IPv4 addresses.

Known, accepted ambiguity: a bare 9+ digit number that is not a phone
number (a long order id, an 11-digit national id — which IS personal
data) is masked too; three bare groups like "100 200 300" look exactly
like 3-3-3 phone grouping and are masked. Not covered at all: payment
card numbers (16+ digits), e-mail addresses, street addresses, names.

What changed from v1 (measured against v1, not assumed):
  * v1 matched only Syrian mobiles 9xx xxx xxx — every other country's
    numbers went through untouched (Gulf customers, expats, +971/+966…).
  * v1's literals "0" and "9" were ASCII-only, so a Syrian number typed
    with Arabic-Indic digits (٠٩٩٩١٢٣٤٥٦) was NOT masked.
  * v1 ended with \\b, which does not fire between a digit and an Arabic
    letter, so "0999123456وشكرا" (or a number glued to "و") was missed.
  * v1 matched a Syrian-looking tail INSIDE longer numbers, masking only
    part of them: "07911 123456" became "07[PHONE_1]", leaking digits.
"""

import re
import unicodedata
from dataclasses import dataclass, field

# A run of digit groups with only space / NBSP / dot / dash / parentheses
# between them, optionally starting with "+" and/or "(". \d is Unicode-aware,
# so Arabic-Indic and Persian digits are candidates too. The only boundary
# is "not touching another digit" — letters on either side are fine (an
# Arabic "و" glued to a number, a Latin tag in front of it, …).
_SEPARATORS = r"[ \t\u00a0\u202f.\-()]"
_PHONE_PATTERN = re.compile(rf"(?<!\d)\+?\(?\d(?:{_SEPARATORS}*\d)*(?!\d)")

_DIGIT_GROUP = re.compile(r"\d+")
_IPV4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_US_PAREN_FORM = re.compile(r"\(\d{3}\)[ .\-\u00a0]*\d{3}[ .\-\u00a0]*\d{4}")

# Longest run of digit groups examined as ONE span (keeps the splitting step
# cheap on adversarial input; a real phone number has at most ~8 groups).
_MAX_GROUPS_PER_SPAN = 12


def _ascii_digits(text: str) -> str:
    """Convert every Unicode decimal digit (category Nd) to 0-9; leave all else as is."""
    return "".join(
        str(unicodedata.digit(ch)) if unicodedata.category(ch) == "Nd" else ch for ch in text
    )


def _looks_like_phone(core: str) -> bool:
    """`core` is already ASCII-digit-normalised (see _phone_spans)."""
    groups = _DIGIT_GROUP.findall(core)
    digits = "".join(groups)
    count = len(digits)

    if core.lstrip("(").startswith("+"):
        return 8 <= count <= 15 and len(groups) <= 8
    if digits.startswith("00"):
        return 8 <= count - 2 <= 15 and len(groups) <= 8
    if digits.startswith("0"):
        return 9 <= count <= 11 and len(groups) <= 5

    if len(groups) == 1:
        return 9 <= count <= 15
    if "(" in core:
        return bool(_US_PAREN_FORM.fullmatch(core))

    separators = set(re.findall(r"\D", core))
    return (
        len(separators) == 1
        and 3 <= len(groups) <= 6
        and 9 <= count <= 15
        and 1 <= len(groups[0]) <= 4
        and all(2 <= len(group) <= 4 for group in groups[1:])
    )


def _phone_spans(candidate: re.Match) -> list[tuple[int, int]]:
    """Longest valid phone numbers inside one candidate run, as absolute (start, end) offsets."""
    text = candidate.group(0)
    # Unicode digits map 1:1 to ASCII digits, so offsets in `norm` are offsets in `text`.
    norm = _ascii_digits(text)
    if _IPV4.fullmatch(norm):
        return []

    groups = list(_DIGIT_GROUP.finditer(norm))
    lead = norm[: groups[0].start()]  # what precedes the first digit: "", "+", "(", "+(" …
    spans: list[tuple[int, int]] = []

    i = 0
    while i < len(groups):
        last = min(len(groups) - 1, i + _MAX_GROUPS_PER_SPAN - 1)
        for j in range(last, i - 1, -1):
            end = groups[j].end()
            if i == 0:
                # Keep a leading "+" (it is part of the number). Keep a leading "(" ONLY for the
                # US form "(415) 555-2671"; otherwise it is ordinary punctuation, e.g. "(0999123456)".
                start = 0 if ("+" in lead or _US_PAREN_FORM.fullmatch(norm[:end])) else groups[0].start()
            else:
                start = groups[i].start()
            if _looks_like_phone(norm[start:end]):
                spans.append((candidate.start() + start, candidate.start() + end))
                i = j + 1
                break
        else:
            i += 1
    return spans


@dataclass
class MaskResult:
    masked_text: str
    _found: list[str] = field(default_factory=list)

    def restore(self, text: str) -> str:
        """Replaces every [PHONE_n] placeholder in `text` with the
        original number it stood for. Safe to call on text with no
        placeholders at all — it's a no-op then."""
        for i, original in enumerate(self._found, start=1):
            text = text.replace(f"[PHONE_{i}]", original)
        return text


def mask_phone_numbers(text: str) -> MaskResult:
    found: list[str] = []
    pieces: list[str] = []
    cursor = 0

    for candidate in _PHONE_PATTERN.finditer(text):
        for start, end in _phone_spans(candidate):
            found.append(text[start:end])
            pieces.append(text[cursor:start])
            pieces.append(f"[PHONE_{len(found)}]")
            cursor = end

    pieces.append(text[cursor:])
    return MaskResult(masked_text="".join(pieces), _found=found)
