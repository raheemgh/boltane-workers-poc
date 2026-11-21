"""errors - the single V2 error taxonomy: stable codes, categories, retry and alert hints.

Layer:   core
Stage:   V2-01
Flags:   none
Raises:  never
Budget:  none
Tests:   tests/v2/core/test_errors.py
"""

from dataclasses import dataclass
from enum import Enum


class Category(str, Enum):
    CONFIG = "config"
    DATA = "data"
    INBOUND = "inbound"
    GATE = "gate"
    LLM = "llm"
    META = "meta"
    INTERNAL = "internal"


class ErrorCode(str, Enum):
    """Stable codes. A released value never changes; new codes are only added."""

    CONFIG_MISSING = "CONFIG_MISSING"
    CONFIG_INVALID = "CONFIG_INVALID"
    CONFIG_FLAG_UNKNOWN = "CONFIG_FLAG_UNKNOWN"
    DB_UNAVAILABLE = "DB_UNAVAILABLE"
    DB_CONSTRAINT = "DB_CONSTRAINT"
    DB_UNEXPECTED = "DB_UNEXPECTED"
    INBOUND_MALFORMED = "INBOUND_MALFORMED"
    INBOUND_STORE_UNKNOWN = "INBOUND_STORE_UNKNOWN"
    GATE_RATE_LIMITED = "GATE_RATE_LIMITED"
    GATE_CAP_REACHED = "GATE_CAP_REACHED"
    GATE_BLOCKED = "GATE_BLOCKED"
    GATE_COST_GUARD = "GATE_COST_GUARD"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_EMPTY = "LLM_EMPTY"
    LLM_UPSTREAM = "LLM_UPSTREAM"
    LLM_AUTH = "LLM_AUTH"
    META_AUTH = "META_AUTH"
    META_WINDOW_CLOSED = "META_WINDOW_CLOSED"
    META_PERMANENT = "META_PERMANENT"
    META_TEMPORARY = "META_TEMPORARY"
    META_PAYMENT = "META_PAYMENT"
    META_OPTOUT = "META_OPTOUT"
    META_UNSUPPORTED = "META_UNSUPPORTED"
    META_BSUID = "META_BSUID"
    INTERNAL_DEADLINE = "INTERNAL_DEADLINE"
    INTERNAL_BUG = "INTERNAL_BUG"


# The code prefix decides the category; the tests keep both in step.
PREFIX_CATEGORY = {
    "CONFIG_": Category.CONFIG,
    "DB_": Category.DATA,
    "INBOUND_": Category.INBOUND,
    "GATE_": Category.GATE,
    "LLM_": Category.LLM,
    "META_": Category.META,
    "INTERNAL_": Category.INTERNAL,
}


@dataclass(frozen=True)
class ErrorSpec:
    category: Category
    retryable: bool  # retrying the same operation may succeed
    alert: bool  # should reach the admin alerts (V2-07)
    meaning: str


def _spec(code: str, retryable: bool, alert: bool, meaning: str) -> tuple[ErrorCode, ErrorSpec]:
    category = next(cat for prefix, cat in PREFIX_CATEGORY.items() if code.startswith(prefix))
    return ErrorCode(code), ErrorSpec(category, retryable, alert, meaning)


SPECS = dict(
    [
        _spec("CONFIG_MISSING", False, True, "A required env var or setting is absent."),
        _spec("CONFIG_INVALID", False, True, "A setting or store config value fails validation."),
        _spec("CONFIG_FLAG_UNKNOWN", False, False, "V2_FLAGS names a flag that is not registered."),
        _spec("DB_UNAVAILABLE", True, True, "The database did not answer (network, timeout, 5xx)."),
        _spec("DB_CONSTRAINT", False, True, "A constraint rejected a write the code expected to pass."),
        _spec("DB_UNEXPECTED", False, True, "Any other database failure."),
        _spec("INBOUND_MALFORMED", False, False, "A webhook delivery could not be normalized."),
        _spec("INBOUND_STORE_UNKNOWN", False, False, "No store matches the phone_number_id."),
        _spec("GATE_RATE_LIMITED", False, False, "The customer sent too many messages too fast."),
        _spec("GATE_CAP_REACHED", False, False, "The store's monthly conversation cap is used up."),
        _spec("GATE_BLOCKED", False, False, "The customer is on the store's blocklist."),
        _spec("GATE_COST_GUARD", False, False, "The session reached its USD cost guard."),
        _spec("LLM_TIMEOUT", True, False, "The model call passed its deadline."),
        _spec("LLM_EMPTY", True, False, "The model stream ended with no text."),
        _spec("LLM_UPSTREAM", True, False, "OpenRouter or the provider returned an error."),
        _spec("LLM_AUTH", False, True, "The OpenRouter key was rejected or has no credit."),
        _spec("META_AUTH", False, True, "Meta rejected the access token (code 190)."),
        _spec("META_WINDOW_CLOSED", False, False, "The 24h customer-service window is closed."),
        _spec("META_PERMANENT", False, False, "Meta refused this message for good."),
        _spec("META_TEMPORARY", True, False, "Meta failed in a way a retry may fix."),
        _spec("META_PAYMENT", False, True, "Meta reports a payment problem on the account."),
        _spec("META_OPTOUT", False, False, "The customer opted out of messages."),
        _spec("META_UNSUPPORTED", False, False, "The message type is not supported."),
        _spec("META_BSUID", False, False, "Meta needs a different recipient identity (BSUID)."),
        _spec("INTERNAL_DEADLINE", True, False, "The per-message time budget ran out."),
        _spec("INTERNAL_BUG", False, True, "A bug: something that must not happen happened."),
    ]
)


def spec_for(code: ErrorCode) -> ErrorSpec:
    return SPECS[code]


class V2Error(Exception):
    """Carries a code and small scalar context. The message is the code itself, never free text."""

    def __init__(self, code: ErrorCode, **context: object) -> None:
        super().__init__(code.value)
        self.code = code
        self.context = dict(context)

    @property
    def spec(self) -> ErrorSpec:
        return SPECS[self.code]
