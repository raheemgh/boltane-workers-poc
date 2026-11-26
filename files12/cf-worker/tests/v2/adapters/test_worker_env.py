"""test_worker_env - the Worker env reader: flag settings, the Supabase connection, and odd env objects.

Level:   L1
Stage:   V2-02B
"""

import types
import unittest

from app.v2.adapters import worker_env
from app.v2.core.errors import ErrorCode, V2Error


class Forwarding:
    """Like app.worker_ctx.EnvWithCtx: unknown names are forwarded to the real env."""

    def __init__(self, env):
        self._env = env

    def __getattr__(self, name):
        return getattr(self._env, name)


def env(**values):
    return types.SimpleNamespace(**values)


class ReadTextTest(unittest.TestCase):
    def test_read_text__only_a_non_empty_string_counts(self):
        e = env(A="x", B="", C=5, D=None)
        self.assertEqual(worker_env.read_text(e, "A"), "x")
        for name in ("B", "C", "D", "MISSING"):
            self.assertIsNone(worker_env.read_text(e, name), name)

    def test_read_text__a_missing_env_or_a_forwarding_one_is_fine(self):
        self.assertIsNone(worker_env.read_text(None, "A"))
        self.assertIsNone(worker_env.read_text(Forwarding(env()), "A"))
        self.assertEqual(worker_env.read_text(Forwarding(env(A="x")), "A"), "x")


class FlagSettingsTest(unittest.TestCase):
    def test_flag_settings__unset_means_everything_off_and_nobody_allowed(self):
        for e in (None, env(), Forwarding(env())):
            s = worker_env.flag_settings(e)
            self.assertEqual((s.enabled, s.allowlist, s.allow_all), (frozenset(), frozenset(), False))

    def test_flag_settings__reads_both_variables(self):
        s = worker_env.flag_settings(env(V2_FLAGS="v2_pipeline", V2_ALLOWLIST="100200300"))
        self.assertEqual(s.enabled, frozenset({"v2_pipeline"}))
        self.assertEqual(s.allowlist, frozenset({"100200300"}))

    def test_flag_settings__the_same_strings_give_the_same_cached_object(self):
        one = worker_env.flag_settings(env(V2_FLAGS="v2_pipeline", V2_ALLOWLIST="*"))
        two = worker_env.flag_settings(Forwarding(env(V2_FLAGS="v2_pipeline", V2_ALLOWLIST="*")))
        self.assertIs(one, two)
        self.assertTrue(one.allow_all)

    def test_flag_settings__a_wrong_flag_name_opens_nothing_and_is_listed(self):
        s = worker_env.flag_settings(env(V2_FLAGS="v2_pipline", V2_ALLOWLIST="*"))
        self.assertEqual(s.enabled, frozenset())
        self.assertEqual(s.unknown_flags, ("v2_pipline",))


class CredentialsTest(unittest.TestCase):
    def test_supabase_credentials__both_present(self):
        self.assertEqual(worker_env.supabase_credentials(env(SUPABASE_URL="u", SUPABASE_KEY="k")), ("u", "k"))

    def test_supabase_credentials__a_missing_one_names_the_variable_never_the_value(self):
        for e, name in ((env(SUPABASE_KEY="k-SECRET"), "SUPABASE_URL"), (env(SUPABASE_URL="u"), "SUPABASE_KEY")):
            with self.assertRaises(V2Error) as caught:
                worker_env.supabase_credentials(e)
            self.assertIs(caught.exception.code, ErrorCode.CONFIG_MISSING)
            self.assertEqual(caught.exception.context, {"name": name})

    def test_supabase_credentials__no_env_at_all(self):
        with self.assertRaises(V2Error):
            worker_env.supabase_credentials(None)


if __name__ == "__main__":
    unittest.main()
