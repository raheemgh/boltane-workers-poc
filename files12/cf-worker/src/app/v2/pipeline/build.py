"""build - wires the adapters to the ports: the only pipeline module allowed to import adapters.

Layer:   pipeline
Stage:   V2-02B
Flags:   none
Raises:  V2Error CONFIG_MISSING when the Supabase settings are absent
Budget:  none
Tests:   tests/v2/pipeline/test_build.py
"""

from app.v2.adapters.supabase_rest import SupabaseRest
from app.v2.adapters.worker_env import flag_settings, supabase_credentials
from app.v2.core.flags import FlagSettings
from app.v2.ports.db import DbPort


def settings_from(env: object) -> FlagSettings:
    """The V2 flag settings this Worker env asks for."""
    return flag_settings(env)


def db_from(env: object) -> DbPort:
    """The database port for this Worker: Supabase functions called with the service key."""
    url, key = supabase_credentials(env)
    return SupabaseRest(url, key)
