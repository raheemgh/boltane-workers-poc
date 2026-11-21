"""store_config - validates the per-store settings columns added by migration 001.

Layer:   core
Stage:   V2-01
Flags:   none
Raises:  never
Budget:  none
Tests:   tests/v2/core/test_store_config.py
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from app.v2.core.flags import REGISTRY

FIELDS = ("features", "timezone", "business_hours", "default_language", "billing_anchor", "owner_phone")
DEFAULT_LANGUAGES = ("ar", "en")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_INTERVALS_PER_DAY = 4

# These patterns are also in the SQL checks and in contracts/store_config.schema.json;
# tests/v2/schema/test_schema_static.py keeps the three copies identical.
TIMEZONE_PATTERN = r"^(UTC|[A-Za-z_]+(/[A-Za-z0-9_+-]+)+)$"
OWNER_PHONE_PATTERN = r"^[0-9]{6,15}$"
OPEN_TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
CLOSE_TIME_PATTERN = r"^(([01][0-9]|2[0-3]):[0-5][0-9]|24:00)$"
DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"


@dataclass(frozen=True)
class ConfigIssue:
    field: str
    code: str  # wrong_type | bad_value | bad_format | unknown_flag | missing | unknown_day


def _issue(field: str, code: str) -> list[ConfigIssue]:
    return [ConfigIssue(field, code)]


def _matches(pattern: str, value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _check_features(value: object) -> list[ConfigIssue]:
    if not isinstance(value, Mapping):
        return _issue("features", "wrong_type")
    issues: list[ConfigIssue] = []
    for key, flag_value in value.items():
        if key not in REGISTRY:
            issues += _issue(f"features.{key}", "unknown_flag")
        elif not isinstance(flag_value, bool):
            issues += _issue(f"features.{key}", "wrong_type")
    return issues


def _check_timezone(value: object) -> list[ConfigIssue]:
    if value is None:
        return []
    if not isinstance(value, str):
        return _issue("timezone", "wrong_type")
    if len(value) > 64 or not _matches(TIMEZONE_PATTERN, value):
        return _issue("timezone", "bad_format")
    return []


def _check_interval(path: str, pair: object) -> list[ConfigIssue]:
    if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
        return _issue(path, "wrong_type")
    opens, closes = pair
    if not (_matches(OPEN_TIME_PATTERN, opens) and _matches(CLOSE_TIME_PATTERN, closes)):
        return _issue(path, "bad_format")
    return _issue(path, "bad_value") if opens == closes else []


def _check_day(day: str, intervals: object) -> list[ConfigIssue]:
    path = f"business_hours.{day}"
    if not isinstance(intervals, list):
        return _issue(path, "wrong_type")
    if len(intervals) > MAX_INTERVALS_PER_DAY:
        return _issue(path, "bad_value")
    issues: list[ConfigIssue] = []
    for index, pair in enumerate(intervals):
        issues += _check_interval(f"{path}[{index}]", pair)
    return issues


def _check_business_hours(value: object) -> list[ConfigIssue]:
    """Null means "not configured". Otherwise all seven days are required; [] means closed all day."""
    if value is None:
        return []
    if not isinstance(value, Mapping):
        return _issue("business_hours", "wrong_type")
    issues = [ConfigIssue(f"business_hours.{day}", "missing") for day in DAYS if day not in value]
    issues += [ConfigIssue(f"business_hours.{key}", "unknown_day") for key in value if key not in DAYS]
    for day in DAYS:
        if day in value:
            issues += _check_day(day, value[day])
    return issues


def _check_language(value: object) -> list[ConfigIssue]:
    if not isinstance(value, str):
        return _issue("default_language", "wrong_type")
    return [] if value in DEFAULT_LANGUAGES else _issue("default_language", "bad_value")


def _check_billing_anchor(value: object) -> list[ConfigIssue]:
    if value is None or type(value) is date:
        return []
    if not isinstance(value, str):
        return _issue("billing_anchor", "wrong_type")
    if not _matches(DATE_PATTERN, value):
        return _issue("billing_anchor", "bad_format")
    try:
        date.fromisoformat(value)
    except ValueError:
        return _issue("billing_anchor", "bad_value")
    return []


def _check_owner_phone(value: object) -> list[ConfigIssue]:
    """Digits only, no "+", like Meta's wa_id."""
    if value is None:
        return []
    if not isinstance(value, str):
        return _issue("owner_phone", "wrong_type")
    return [] if _matches(OWNER_PHONE_PATTERN, value) else _issue("owner_phone", "bad_format")


_CHECKS = {
    "features": _check_features,
    "timezone": _check_timezone,
    "business_hours": _check_business_hours,
    "default_language": _check_language,
    "billing_anchor": _check_billing_anchor,
    "owner_phone": _check_owner_phone,
}


def validate_store_config(config: Mapping[str, object]) -> list[ConfigIssue]:
    """Check the six V2 settings that are present in `config`; other keys are ignored.

    An empty list means valid. Absent keys are fine: the database supplies their defaults.
    """
    if not isinstance(config, Mapping):
        return _issue("", "wrong_type")
    issues: list[ConfigIssue] = []
    for name in FIELDS:
        if name in config:
            issues += _CHECKS[name](config[name])
    return issues
