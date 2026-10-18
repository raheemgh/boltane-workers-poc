"""
Stage 1 characterization tests for app/config.py's get_settings().

Complements Done-criteria bullet 1 (the /debug/settings route, proven
against a real Worker via `pywrangler dev` + real .dev.vars — this
script can't reach that, plain CPython has no workerd). What this DOES
prove, without any real Cloudflare account: get_settings() reads every
field correctly from `env`, caches after the first build, handles the
one optional field's presence/absence correctly, and fails with a
clear error rather than silently defaulting when a required field is
missing — using the local-only `workers` stub at
tooling/local-test-support/workers.py (see that file's own docstring;
it is never vendored/deployed).

Run it yourself with:

    cd cf-worker
    PYTHONPATH=tooling/local-test-support:src python test_stage1_settings.py

Pure stdlib + pydantic, no network, no real credentials.
"""

import os

REQUIRED_VARS = {
    "META_VERIFY_TOKEN": "test-verify-token",
    "META_APP_SECRET": "test-app-secret",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_KEY": "test-supabase-key",
    "OPENROUTER_DEFAULT_KEY": "test-openrouter-key",
}


def _reset(env_vars: dict) -> None:
    """
    Resets both get_settings()'s module-level cache and the stub's
    backing os.environ, so each test starts from a clean slate
    regardless of what an earlier test in this same process left
    behind — get_settings() is cached-after-first-call by design (see
    its docstring), so a stale cache would otherwise leak between
    tests here exactly the way it's meant NOT to leak between requests
    in the real Worker.
    """
    import app.config as config

    config._settings = None
    for key in REQUIRED_VARS:
        os.environ.pop(key, None)
    os.environ.pop("OPENROUTER_SOCKS5_PROXY", None)
    os.environ.update(env_vars)


def test_builds_from_env_and_returns_actual_values():
    _reset(REQUIRED_VARS)
    from app.config import get_settings

    settings = get_settings()
    assert settings.META_VERIFY_TOKEN == "test-verify-token"
    assert settings.META_APP_SECRET == "test-app-secret"
    assert settings.SUPABASE_URL == "https://example.supabase.co"
    assert settings.SUPABASE_KEY == "test-supabase-key"
    assert settings.OPENROUTER_DEFAULT_KEY == "test-openrouter-key"
    assert settings.OPENROUTER_SOCKS5_PROXY is None
    print("PASS: get_settings() returns the actual configured values for every required field")


def test_optional_socks5_proxy_field():
    _reset({**REQUIRED_VARS, "OPENROUTER_SOCKS5_PROXY": "socks5://127.0.0.1:10808"})
    from app.config import get_settings

    assert get_settings().OPENROUTER_SOCKS5_PROXY == "socks5://127.0.0.1:10808"
    print("PASS: OPENROUTER_SOCKS5_PROXY is read when set")


def test_caches_after_first_build():
    _reset(REQUIRED_VARS)
    from app.config import get_settings

    first = get_settings()
    # Change the backing env after the first build — a genuinely new
    # env object each call would pick this up; the cached singleton
    # must NOT.
    os.environ["META_VERIFY_TOKEN"] = "changed-after-first-build"
    second = get_settings()
    assert second is first
    assert second.META_VERIFY_TOKEN == "test-verify-token"
    print("PASS: get_settings() caches after the first build within a process/isolate")


def test_missing_required_field_raises_clearly():
    env_vars = dict(REQUIRED_VARS)
    del env_vars["SUPABASE_KEY"]
    _reset(env_vars)
    from app.config import get_settings

    try:
        get_settings()
    except RuntimeError as exc:
        # get_settings() checks all five required fields up front
        # specifically so this is always a RuntimeError naming the
        # missing field, not a raw AttributeError from Pyodide FFI
        # internals or a pydantic "field required" one step removed
        # from the actual cause.
        assert "SUPABASE_KEY" in str(exc)
        print(f"PASS: a missing required field fails clearly ({exc})")
    else:
        raise AssertionError("get_settings() must not succeed with a required field missing")


def test_ai_client_and_supabase_client_import_cleanly():
    """
    Confirms the mechanical settings-access edits didn't break imports
    for the two service modules that don't have a Request to work
    with — the specific risk this stage's design is built around.
    """
    _reset(REQUIRED_VARS)
    import app.services.ai_client  # noqa: F401
    import app.services.supabase_client  # noqa: F401
    import app.routers.webhook  # noqa: F401

    print("PASS: webhook.py, ai_client.py, supabase_client.py all import cleanly under the new pattern")


def main() -> None:
    test_builds_from_env_and_returns_actual_values()
    test_optional_socks5_proxy_field()
    test_caches_after_first_build()
    test_missing_required_field_raises_clearly()
    test_ai_client_and_supabase_client_import_cleanly()
    print("\nALL PASS")


if __name__ == "__main__":
    main()
