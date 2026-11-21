"""log_schema - the one structured log line per message: allowed fields, value checks, JSON text.

Layer:   core
Stage:   V2-01
Flags:   none
Raises:  never
Budget:  none
Tests:   tests/v2/core/test_log_schema.py
"""

import json
import math
import re
from dataclasses import dataclass

from app.v2.core.errors import ErrorCode

SCHEMA_VERSION = 1
LEVELS = frozenset({"debug", "info", "warning", "error"})
EVENTS = frozenset({"message", "job", "alert", "startup"})
OUTCOMES = frozenset({"replied", "ignored", "blocked", "failed", "duplicate", "deferred"})
STAGES = frozenset(
    {"normalize", "inbox", "gate", "prompt", "llm", "guard", "send", "finalize", "total"}
)
REQUIRED = ("ts", "event")

# Never allowed in a line, whatever the value (ADR-5: no secrets, no message text, no identities).
FORBIDDEN = frozenset(
    {
        "text", "body", "message", "msg", "payload", "content", "customer_text", "raw_body",
        "phone", "wa_id", "bsuid", "from", "to", "token", "access_token", "authorization",
        "secret", "key", "password",
    }
)

_TS = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]{8,15}(Z|[+-][0-9]{2}:[0-9]{2})")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_NAME = re.compile(r"[A-Za-z0-9._:/-]{1,80}")
_CODES = frozenset(code.value for code in ErrorCode)


def _matches(pattern: re.Pattern):
    return lambda value: isinstance(value, str) and pattern.fullmatch(value) is not None


def _one_of(options: frozenset[str]):
    return lambda value: isinstance(value, str) and value in options


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_cost(value: object) -> bool:
    ok_type = isinstance(value, (int, float)) and not isinstance(value, bool)
    return ok_type and math.isfinite(value) and value >= 0


def _is_stages(value: object) -> bool:
    return isinstance(value, dict) and all(k in STAGES and _is_count(v) for k, v in value.items())


VALIDATORS = {
    "ts": _matches(_TS),
    "event": _one_of(EVENTS),
    "level": _one_of(LEVELS),
    "outcome": _one_of(OUTCOMES),
    "error_code": _one_of(_CODES),
    "store_id": _matches(_UUID),
    "model": _matches(_NAME),
    "provider": _matches(_NAME),
    "job": _matches(_NAME),
    "stages_ms": _is_stages,
    "tokens_in": _is_count,
    "tokens_out": _is_count,
    "retries": _is_count,
    "cost_usd": _is_cost,
}


@dataclass(frozen=True)
class LogLine:
    data: dict
    dropped: tuple[tuple[str, str], ...]  # (field, reason): forbidden | unknown | invalid | missing


def _problem(name: str, value: object) -> str | None:
    if name in FORBIDDEN:
        return "forbidden"
    check = VALIDATORS.get(name)
    if check is None:
        return "unknown"
    return None if check(value) else "invalid"


def build_line(**fields: object) -> LogLine:
    """Keep only allowed, well-formed fields. The clock is the caller's: pass `ts` in."""
    data: dict = {"v": SCHEMA_VERSION}
    dropped: list[tuple[str, str]] = []
    for name, value in sorted(fields.items()):
        reason = _problem(name, value)
        if reason is None:
            data[name] = dict(value) if isinstance(value, dict) else value
        else:
            dropped.append((name, reason))
    data.setdefault("level", "info")
    dropped += [(name, "missing") for name in REQUIRED if name not in data]
    return LogLine(data, tuple(dropped))


def render(line: LogLine) -> str:
    """One compact JSON line, keys sorted, ASCII only."""
    return json.dumps(line.data, separators=(",", ":"), sort_keys=True)
