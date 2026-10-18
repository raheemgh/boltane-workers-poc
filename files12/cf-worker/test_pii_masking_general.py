#!/usr/bin/env python3
"""
Tests for the generalised (international) phone masking in
src/app/services/pii_masking.py. Lives at cf-worker/test_pii_masking_general.py.
Standard library only; no network:

    python test_pii_masking_general.py

Sections:
  1  must mask      one table of real-world formats from many countries,
                    ASCII and Arabic-Indic/Persian digits, each inside an
                    Arabic sentence: the WHOLE number is replaced, no digit
                    of it is left behind, and restore() gives back the exact
                    original text
  2  must not mask  prices, quantities, years, short order numbers, dates,
                    times, coordinates, thousands-grouped amounts, IPv4
  3  boundaries     letters glued to a number, punctuation around it, two
                    numbers side by side, repeated numbers, placeholders
                    numbered in order
  4  v1 parity      everything the Syrian-only v1 masked is still masked;
                    the measured v1 failures are now fixed (compares with
                    the Render original when it is present)
  5  robustness     fuzz: restore(mask(x)) == x for arbitrary text, a
                    masked number never leaves a digit behind, linear time
  6  known ambiguity  documented false positives (characterisation, on purpose)
"""

import importlib.util
import random
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
NEW_PATH = HERE / "src" / "app" / "services" / "pii_masking.py"
V1_PATH = REPO_ROOT / "app" / "services" / "pii_masking.py"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pm = load("pii_new", NEW_PATH)
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


# ---------------------------------------------------------------------------
# 1. must mask
# ---------------------------------------------------------------------------
MUST_MASK = [
    # Syria
    "0999123456", "0999 123 456", "0999-123-456", "+963 999 123 456", "+963-999-123-456",
    "00963999123456", "963999123456", "963 999 123 456", "999123456", "999 123 456",
    "011 2345678", "+963 11 234 5678", "0112345678",
    # Syrian / Gulf numbers typed with Arabic-Indic or Persian digits
    "٠٩٩٩١٢٣٤٥٦٧", "٠٩٩٩ ١٢٣ ٤٥٦", "+٩٦٣ ٩٩٩ ١٢٣ ٤٥٦", "۰۹۹۹۱۲۳۴۵۶", "00٩٦٣٩٩٩١٢٣٤٥٦",
    # these two are only recognised because Arabic-Indic digits are converted before the 0 / 00 rules apply
    "٠٧٩١١ ١٢٣٤٥٦", "٠٠٩٧١ ٥٠ ١٢٣ ٤٥٦٧",
    # Gulf / Levant / Egypt / Turkey
    "+971 50 123 4567", "0501234567", "+966 50 123 4567", "+965 5012 3456", "+974 3312 3456",
    "+962 7 9012 3456", "+961 3 123 456", "+20 100 123 4567", "+90 532 123 45 67", "05321234567",
    # Europe / US / Asia
    "+1 (415) 555-2671", "(415) 555-2671", "415-555-2671", "415.555.2671", "4155552671",
    "+44 7911 123456", "07911 123456", "+44 (0) 20 7946 0958", "+49 151 23456789",
    "+33 6 12 34 56 78", "06 12 34 56 78", "+91 98765 43210", "+7 912 345-67-89",
    "+86 138 0013 8000", "+81 90 1234 5678", "00441234567890",
]

SENTENCES = ["رقمي {n} شكراً", "اتصل بي على {n}", "{n}", "my number is {n}, thanks", "تواصل: {n}."]


def t_must_mask() -> None:
    bad = []
    for number in MUST_MASK:
        for sentence in SENTENCES:
            text = sentence.format(n=number)
            result = pm.mask_phone_numbers(text)
            expected = sentence.format(n="[PHONE_1]")
            if result.masked_text != expected or result._found != [number] or result.restore(result.masked_text) != text:
                bad.append((text, result.masked_text, result._found))
    check(f"1 must mask: {len(MUST_MASK)} formats x {len(SENTENCES)} sentences — whole number replaced, "
          "nothing left behind, exact round trip", not bad, "\n        ".join(map(str, bad[:4])))


# ---------------------------------------------------------------------------
# 2. must not mask
# ---------------------------------------------------------------------------
MUST_NOT_MASK = [
    "السعر 150000 ل.س", "الإجمالي 1,250,000 ل.س", "أريد 3 قطع", "2025", "٢٠٢٥", "رقم الطلب 1234567",
    "رقم الطلب 12345678", "بتاريخ 2025-03-15", "بتاريخ 15/03/2025", "بتاريخ 15-03-2025",
    "بتاريخ 15.03.2025", "الساعة 10:30", "من 9 إلى 17", "الموقع 33.513807 36.276528",
    "الخادم 192.168.100.200", "الإصدار v2.1.3", "السعر 1250.50", "3 x 250", "10 20 30",
    "طلب رقم 4521 وطلب رقم 4522", "العمر 25 سنة", "الغرفة 304",
]


def t_must_not_mask() -> None:
    bad = []
    for text in MUST_NOT_MASK:
        result = pm.mask_phone_numbers(text)
        if result.masked_text != text or result._found:
            bad.append((text, result.masked_text))
    check(f"2 must not mask: {len(MUST_NOT_MASK)} non-phone numbers are left untouched", not bad, "\n        ".join(map(str, bad)))


# ---------------------------------------------------------------------------
# 3. boundaries
# ---------------------------------------------------------------------------
def t_boundaries() -> None:
    mask = pm.mask_phone_numbers

    r = mask("0999123456وشكرا")
    check("3 boundary: Arabic letters glued AFTER a number", r.masked_text == "[PHONE_1]وشكرا", r.masked_text)
    r = mask("رقمي0999123456")
    check("3 boundary: Arabic letters glued BEFORE a number", r.masked_text == "رقمي[PHONE_1]", r.masked_text)
    r = mask("0999123456و0988123456")
    check("3 boundary: two numbers joined by the Arabic conjunction 'و'",
          r.masked_text == "[PHONE_1]و[PHONE_2]" and r._found == ["0999123456", "0988123456"], r.masked_text)
    r = mask("0999123456 0988123456")
    check("3 boundary: two numbers side by side are masked separately",
          r.masked_text == "[PHONE_1] [PHONE_2]" and r._found == ["0999123456", "0988123456"], r.masked_text)
    r = mask("+971 50 123 4567 / 0999123456")
    check("3 boundary: a '/' between numbers separates them", r.masked_text == "[PHONE_1] / [PHONE_2]", r.masked_text)
    r = mask("اتصل (0999123456) أو 0988123456.")
    check("3 boundary: surrounding brackets/full stops stay outside the match",
          r.masked_text == "اتصل ([PHONE_1]) أو [PHONE_2].", r.masked_text)
    r = mask("0999123456 ثم 0999123456")
    check("3 boundary: the same number twice gets two placeholders and both restore",
          r.masked_text == "[PHONE_1] ثم [PHONE_2]" and r.restore(r.masked_text) == "0999123456 ثم 0999123456")
    r = mask("طلبان 3 و 4 على 0999123456")
    check("3 boundary: small numbers before a phone number do not get swallowed",
          r.masked_text == "طلبان 3 و 4 على [PHONE_1]", r.masked_text)
    r = mask("")
    check("3 boundary: empty text", r.masked_text == "" and r._found == [])
    r = mask("لا أرقام هنا")
    check("3 boundary: text without digits is untouched and restore() is a no-op",
          r.masked_text == "لا أرقام هنا" and r.restore("رد [PHONE_9]") == "رد [PHONE_9]")
    r = mask("رقمي 0999123456")
    check("3 boundary: restore() puts the numbers back into a model reply that reuses the placeholder",
          r.restore("سأتصل بك على [PHONE_1] غداً") == "سأتصل بك على 0999123456 غداً")


# ---------------------------------------------------------------------------
# 4. v1 parity
# ---------------------------------------------------------------------------
def t_v1_parity() -> None:
    v1_forms = ["0999 123 456", "+963-999-123-456", "0999123456", "00963999123456", "+963 999 123 456", "999123456"]
    bad = [n for n in v1_forms if pm.mask_phone_numbers(f"رقمي {n}").masked_text != "رقمي [PHONE_1]"]
    check("4 v1 parity: every Syrian form v1's own docstring/pattern covered is still fully masked", not bad, str(bad))

    if not V1_PATH.exists():
        check("4 v1 parity: Render original present for the comparison", False, f"{V1_PATH} not found")
        return
    v1 = load("pii_v1", V1_PATH)

    # Everything v1 masked, v2 must mask at least as much (the masked span is never SMALLER).
    # v1 swallowed ONE preceding space when the number began with a bare 9xx ("x999123456 y");
    # v2 leaves surrounding text alone. Compared ignoring spaces for that reason only.
    regress = []
    for number in v1_forms:
        old, new = v1.mask_phone_numbers(f"x {number} y"), pm.mask_phone_numbers(f"x {number} y")
        if old.masked_text.replace(" ", "") != new.masked_text.replace(" ", ""):
            regress.append((number, old.masked_text, new.masked_text))
    check("4 v1 parity: on v1's own forms the masked output equals v1's (v1's swallowed space aside)", not regress, str(regress))

    fixed = {
        "Arabic-Indic digits": ("رقمي ٠٩٩٩١٢٣٤٥٦", "رقمي [PHONE_1]"),
        "glued to an Arabic letter": ("0999123456وشكرا", "[PHONE_1]وشكرا"),
        "UK number (v1 leaked '07')": ("07911 123456", "[PHONE_1]"),
        "UAE number": ("+971 50 123 4567", "[PHONE_1]"),
        "Damascus landline": ("011 2345678", "[PHONE_1]"),
    }
    for label, (text, want) in fixed.items():
        old, new = v1.mask_phone_numbers(text).masked_text, pm.mask_phone_numbers(text).masked_text
        check(f"4 v1 fix — {label}: v1 gave {old!r}, v2 gives {new!r}", new == want and old != want, f"v2={new!r}")


# ---------------------------------------------------------------------------
# 5. robustness
# ---------------------------------------------------------------------------
def t_robustness() -> None:
    rng = random.Random(20261001)
    # No literal "[PHONE_n]" in the alphabet: customer text that already contains one is the single
    # documented exception to the round trip (see section 6).
    alphabet = list("0123456789٠١٢٣٤٥٦٧٨٩۰۱۲۳ +-().  \t,/:abcXYZ_و") + ["\u00a0", "\n"]
    broken = []
    for _ in range(40000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
        result = pm.mask_phone_numbers(text)
        if result.restore(result.masked_text) != text:
            broken.append(text)
            break
    check("5 robustness: restore(mask(x)) == x for 40,000 random strings of digits/separators/Arabic/Latin", not broken,
          repr(broken[:1]))

    leaks = []
    for _ in range(3000):
        number = rng.choice(MUST_MASK)
        text = f"{rng.choice(['', 'a ', 'رقمي ', '(', '- '])}{number}{rng.choice(['', ' b', ' شكراً', ')', '.'])}"
        masked = pm.mask_phone_numbers(text).masked_text
        remaining = "".join(ch for ch in re.sub(r"\[PHONE_\d+\]", "", masked) if ch.isdigit())
        if remaining:
            leaks.append((text, masked))
            break
    check("5 robustness: after masking a known number no digit of it remains in the text", not leaks, str(leaks[:1]))

    # Real input is capped near 1,200 characters by the token firewall; these sizes are far beyond that.
    for size in (10_000, 50_000):
        adversarial = ("1 " * size)[: size * 2]
        started = time.perf_counter()
        pm.mask_phone_numbers(adversarial)
        elapsed = time.perf_counter() - started
        check(f"5 robustness: {size * 2:,}-char pathological input (digit groups with separators) is fast "
              f"({elapsed * 1000:.0f} ms)", elapsed < 3.0, f"{elapsed:.2f}s")
    started = time.perf_counter()
    pm.mask_phone_numbers("9" * 200_000)
    check("5 robustness: a 200,000-digit run is linear time", time.perf_counter() - started < 3.0)


# ---------------------------------------------------------------------------
# 6. known ambiguity (characterisation)
# ---------------------------------------------------------------------------
def t_known_ambiguity() -> None:
    check("6 known ambiguity [characterisation]: 'order 12345678901' (11-digit id) IS masked — an 11-digit id is "
          "indistinguishable from a phone number, and masking is reversible",
          pm.mask_phone_numbers("order 12345678901").masked_text == "order [PHONE_1]")
    check("6 known ambiguity [characterisation]: '100 200 300' looks exactly like 3-3-3 phone grouping and IS masked",
          pm.mask_phone_numbers("100 200 300").masked_text == "[PHONE_1]")
    literal = pm.mask_phone_numbers("my [PHONE_1] is 0999123456")
    check("6 known limitation [characterisation]: customer text that ALREADY contains the literal token "
          "'[PHONE_1]' is restored together with the real placeholder (round trip not exact; harmless — "
          "it only ever inserts the customer's own number)",
          literal.restore(literal.masked_text) != "my [PHONE_1] is 0999123456")
    check("6 known gap [characterisation]: a 16-digit card number is NOT masked (out of scope: not a phone number)",
          pm.mask_phone_numbers("4111111111111111").masked_text == "4111111111111111")


def main() -> int:
    for test in (t_must_mask, t_must_not_mask, t_boundaries, t_v1_parity, t_robustness, t_known_ambiguity):
        test()
    failed = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok or not detail else f"\n        -> {detail}"))
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
