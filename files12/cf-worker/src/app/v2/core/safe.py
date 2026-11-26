"""safe - small readers for untrusted JSON that never raise: dict, list, text, int, number.

Layer:   core
Stage:   V2-02A
Flags:   none
Raises:  never
Budget:  pure functions, no regex, no copies of big values
Tests:   tests/v2/core/test_safe.py
"""

import math

MAX_TEXT = 4096  # the WhatsApp text limit


def as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def as_list(value: object) -> list:
    return value if isinstance(value, list) else []


def clean_text(value: object, limit: int = MAX_TEXT) -> str | None:
    """A str that PostgreSQL jsonb accepts: no NUL, no lone surrogate, cut to `limit`. None if not a str.

    A NUL or a lone surrogate in a message makes the database reject the whole write, and Meta would then
    retry that message for days. Removing them here is what keeps one odd message from becoming a poison pill.
    """
    if not isinstance(value, str):
        return None
    if "\x00" in value:
        value = value.replace("\x00", "")
    return value[:limit].encode("utf-8", "replace").decode("utf-8")


def as_int(value: object) -> int | None:
    """Meta sends epoch seconds as digit strings. Accept an int or a short ASCII digit string, nothing else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) < 19:
        return int(value)
    return None


def as_number(value: object) -> int | float | None:
    """A finite int or float (Python's json.loads also accepts NaN and Infinity; those are refused)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def as_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def compact(items: dict) -> dict:
    """The same dict without the entries whose value is None."""
    return {key: value for key, value in items.items() if value is not None}
