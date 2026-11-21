"""flags - the feature-flag registry and the resolver (global list, allowlist, per-store override).

Layer:   core
Stage:   V2-01
Flags:   none
Raises:  never
Budget:  none
Tests:   tests/v2/core/test_flags.py
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass

ENV_FLAGS = "V2_FLAGS"  # comma list of flag names that are ON for every store
ENV_ALLOWLIST = "V2_ALLOWLIST"  # comma list of phone_number_ids, or "*" for every store
ALLOW_ALL = "*"

_PHONE_NUMBER_ID = re.compile(r"[0-9]{5,30}")
_SPLIT = re.compile(r"[,;\s]+")


@dataclass(frozen=True)
class Flag:
    name: str
    default: bool  # always False: False means the V1 behaviour, literally
    description: str
    stage: str  # the stage that owns the flag
    gated: bool = False  # needs the global flag AND the allowlist; a store can only turn it off


FLAGS = (
    Flag(
        name="v2_pipeline",
        default=False,
        description="Send a store's messages through the V2 pipeline (needs the allowlist too).",
        stage="V2-02",
        gated=True,
    ),
    Flag(
        name="llm_reasoning_off",
        default=False,
        description="Ask OpenRouter to turn model reasoning off.",
        stage="V2-03",
    ),
    Flag(
        name="llm_provider_sort_latency",
        default=False,
        description="Ask OpenRouter to sort providers by latency.",
        stage="V2-03",
    ),
    Flag(
        name="llm_data_collection_deny",
        default=False,
        description="Ask OpenRouter to use only providers that do not collect data.",
        stage="V2-03",
    ),
)

REGISTRY = {flag.name: flag for flag in FLAGS}


@dataclass(frozen=True)
class FlagSettings:
    """What the Worker env says. Built once per isolate by an adapter, passed in, never read here."""

    enabled: frozenset[str] = frozenset()
    allowlist: frozenset[str] = frozenset()
    allow_all: bool = False
    unknown_flags: tuple[str, ...] = ()  # names in V2_FLAGS that are not registered
    invalid_allowlist: tuple[str, ...] = ()  # entries in V2_ALLOWLIST that are not ids


DEFAULT_SETTINGS = FlagSettings()


def _tokens(raw: str | None) -> list[str]:
    return [token for token in _SPLIT.split((raw or "").strip()) if token]


def _parse_flags(raw: str | None) -> tuple[frozenset[str], tuple[str, ...]]:
    enabled: set[str] = set()
    unknown: set[str] = set()
    for token in _tokens(raw):
        name = token.lower()
        if name in REGISTRY:
            enabled.add(name)
        else:
            unknown.add(name)
    return frozenset(enabled), tuple(sorted(unknown))


def _parse_allowlist(raw: str | None) -> tuple[frozenset[str], bool, tuple[str, ...]]:
    ids: set[str] = set()
    invalid: set[str] = set()
    allow_all = False
    for token in _tokens(raw):
        if token == ALLOW_ALL:
            allow_all = True
        elif _PHONE_NUMBER_ID.fullmatch(token):
            ids.add(token)
        else:
            invalid.add(token)
    return frozenset(ids), allow_all, tuple(sorted(invalid))


def parse_settings(flags_raw: str | None, allowlist_raw: str | None) -> FlagSettings:
    """Turn the two env strings into settings. Unset or empty means everything off, nobody allowed."""
    enabled, unknown = _parse_flags(flags_raw)
    allowlist, allow_all, invalid = _parse_allowlist(allowlist_raw)
    return FlagSettings(enabled, allowlist, allow_all, unknown, invalid)


def _store_override(name: str, store_features: Mapping[str, object] | None) -> bool | None:
    """The store's own true/false for `name` (stores.features); anything but a bool is ignored."""
    if not isinstance(store_features, Mapping):
        return None
    value = store_features.get(name)
    return value if isinstance(value, bool) else None


def _allowed(settings: FlagSettings, phone_number_id: object) -> bool:
    return settings.allow_all or (
        phone_number_id is not None and str(phone_number_id) in settings.allowlist
    )


def is_enabled(
    name: str,
    settings: FlagSettings,
    *,
    store_features: Mapping[str, object] | None = None,
    phone_number_id: object = None,
) -> bool:
    """Is flag `name` on for this store? Unknown names are off.

    Normal flag: the store's own true/false wins, else the global list decides.
    Gated flag (the V2 pipeline): on only if it is in the global list AND the store is allowlisted;
    the store's own value can only turn it off.
    """
    flag = REGISTRY.get(name)
    if flag is None:
        return False
    globally_on = name in settings.enabled
    override = _store_override(name, store_features)
    if flag.gated:
        return globally_on and override is not False and _allowed(settings, phone_number_id)
    return globally_on if override is None else override
