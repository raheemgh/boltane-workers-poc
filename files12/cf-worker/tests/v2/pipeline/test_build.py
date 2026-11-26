"""test_build - the wiring: flag settings and the database port come out of the Worker env.

Level:   L1
Stage:   V2-02B
"""

import types
import unittest

from app.v2.adapters.supabase_rest import SupabaseRest
from app.v2.core.errors import ErrorCode, V2Error
from app.v2.pipeline import build
from app.v2.ports.db import DbPort


class BuildTest(unittest.TestCase):
    def test_build__settings_come_from_the_env(self):
        env = types.SimpleNamespace(V2_FLAGS="v2_pipeline", V2_ALLOWLIST="100200300")
        self.assertIn("v2_pipeline", build.settings_from(env).enabled)

    def test_build__the_database_is_supabase_over_the_port(self):
        db = build.db_from(types.SimpleNamespace(SUPABASE_URL="https://x.supabase.co", SUPABASE_KEY="k"))
        self.assertIsInstance(db, SupabaseRest)
        self.assertIsInstance(db, DbPort)

    def test_build__no_credentials_is_config_missing(self):
        with self.assertRaises(V2Error) as caught:
            build.db_from(types.SimpleNamespace())
        self.assertIs(caught.exception.code, ErrorCode.CONFIG_MISSING)


if __name__ == "__main__":
    unittest.main()
