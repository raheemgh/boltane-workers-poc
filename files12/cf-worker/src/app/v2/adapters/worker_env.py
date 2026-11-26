"""worker_env - reads the Worker env into typed V2 settings: the feature flags and the Supabase connection.

Layer:   adapters
Stage:   V2-02B
Flags:   v2_pipeline
Raises:  V2Error CONFIG_MISSING when SUPABASE_URL or SUPABASE_KEY is absent
Budget:  none; flag settings are parsed once per isolate and cached by the two env strings
Tests:   tests/v2/adapters/test_worker_env.py
"""

from app.v2.core.errors import ErrorCode, V2Error
from app.v2.core.flags import ENV_ALLOWLIST, ENV_FLAGS, FlagSettings, parse_settings

ENV_URL = "SUPABASE_URL"
ENV_KEY = "SUPABASE_KEY"

_cache: dict[tuple[str | None, str | None], FlagSettings] = {}


def read_text(env: object, name: str) -> str | None:
    """A text setting from the Worker env; None when it is absent, empty or not text."""
    value = getattr(env, name, None)
    return value if isinstance(value, str) and value else None


def flag_settings(env: object) -> FlagSettings:
    """What V2_FLAGS and V2_ALLOWLIST say. Unset means everything off and nobody allowed (ADR-2)."""
    key = (read_text(env, ENV_FLAGS), read_text(env, ENV_ALLOWLIST))
    settings = _cache.get(key)
    if settings is None:
        settings = _cache[key] = parse_settings(*key)
    return settings


def supabase_credentials(env: object) -> tuple[str, str]:
    """(url, service key) of the database; raises CONFIG_MISSING naming the missing variable (never its value)."""
    url, key = read_text(env, ENV_URL), read_text(env, ENV_KEY)
    if not url or not key:
        raise V2Error(ErrorCode.CONFIG_MISSING, name=ENV_URL if not url else ENV_KEY)
    return url, key
