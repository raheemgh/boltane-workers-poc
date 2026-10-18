"""
Typed application settings — Cloudflare Workers build.

Cloudflare Python Workers do NOT expose environment variables/secrets
via os.environ the way a normal Python process would (confirmed by
this migration's audit/PoC, and by Cloudflare's own docs below). The
Render version's app/config.py (unchanged, still running there) reads
via `Settings()` — a module-level singleton built from os.environ at
raw import time. That pattern cannot work here at all.

Two different, both genuinely Cloudflare-documented, ways to reach a
Worker's env exist, and they don't cover the same ground:

  1. `request.scope["env"]` — the pattern Cloudflare's own FastAPI
     package docs demonstrate (developers.cloudflare.com/workers/
     languages/python/packages/fastapi/): inside a route handler,
     `env = request.scope["env"]`. Confirmed, FastAPI-specific — but
     only reachable from code that actually has a Request in hand.

  2. `from workers import env` — a module-scope-importable env,
     confirmed by Cloudflare's Django integration docs (.../packages/
     django/), used there specifically because Django's settings.py
     needs secrets BEFORE the ASGI/WSGI app object even exists, i.e.
     before there is any Request to pull scope off of. This mirrors
     the better-documented JS `cloudflare:workers` env import (see
     the "Import env to access bindings in your Worker's global
     scope" changelog, 2025-03-17): plain vars/secrets are readable
     from anywhere in a Worker's code, request or no request — it's
     only full I/O-bound *binding method calls* that need a live
     request context.

Cloudflare's docs never show these two combined with FastAPI
specifically, so which one is "idiomatic" for a FastAPI app is
genuinely not settled by the docs — flagging that plainly, as asked,
rather than presenting either as confirmed-for-FastAPI when it isn't.

This file settles it structurally, not stylistically. Two real
call sites in this codebase have no Request to work with at all:
app/services/supabase_client.py builds its Supabase client at module
scope, and app/services/ai_client.py's call_ai() runs from a FastAPI
BackgroundTask *after* the response — and the request that started
it — has already gone out. Threading a `settings` parameter through
every function in those two files just to keep pattern 1 everywhere
would mean editing their actual business logic (out of scope for this
stage) and would break test_package_system.py / test_usage_protection.py's
existing fakes, none of which know or should know about settings at
all. Pattern 2 — one get_settings() callable identically from a route
handler or from three call-frames deep inside a background task — is
what makes settings-access-only changes to those files possible, so
it's used everywhere here, uniformly, rather than splitting
request-bound and non-request-bound code across two different ways of
reading the same five values.

No env_file / python-dotenv here — there's no persistent filesystem
in a deployed Worker to read a .env from. `pywrangler dev` has its
own, unrelated local mechanism (a `.dev.vars` file) that populates
`env` for local development; see cf-worker/.dev.vars.example.
"""

from pydantic import BaseModel


class Settings(BaseModel):
    # Must match the "Verify Token" entered in the Meta App Dashboard
    # when registering the webhook URL.
    META_VERIFY_TOKEN: str

    # Meta App Secret — used to verify the X-Hub-Signature-256 header
    # on every incoming POST /webhook, proving the request really
    # came from Meta and wasn't forged by someone who found the URL.
    META_APP_SECRET: str

    # Supabase project credentials (stores table lookups/updates).
    SUPABASE_URL: str
    SUPABASE_KEY: str

    # Shared platform-wide OpenRouter key, used when a store hasn't
    # supplied its own (BYOK) api_key.
    OPENROUTER_DEFAULT_KEY: str

    # Not meaningful on Workers — Workers' fetch-based outbound
    # transport has no concept of a SOCKS5 proxy, and this deployment
    # target never needs one anyway (the Render version's docstring
    # explains OPENROUTER_SOCKS5_PROXY exists only for the
    # Syria-VPN/local-device case). Field kept, always None here, so
    # ai_client.py's `settings.OPENROUTER_SOCKS5_PROXY or None` line
    # can stay textually unchanged rather than needing a
    # Workers-specific branch.
    OPENROUTER_SOCKS5_PROXY: str | None = None


# NOT pydantic_settings.BaseSettings, deliberately. BaseSettings' whole
# purpose is auto-scanning os.environ/.env — behavior this file must
# never trigger, since every field below is always passed explicitly.
# Keeping BaseSettings around would cost a dependency for a code path
# that can never legitimately run on this deployment target, and would
# misleadingly imply Settings still reads process env somehow.

_settings: Settings | None = None

# The five required fields, by name — used only to produce a clear
# "these exact vars/secrets are missing" error below, regardless of
# whether an unset property on the real Pyodide env proxy raises
# AttributeError or silently converts to None (undocumented either
# way, as far as I've found) — either behavior gets caught here with
# the same clear message, rather than either a raw AttributeError from
# deep in Pyodide FFI internals, or a `None` quietly reaching
# pydantic's own (still perfectly legible, but one layer further from
# the actual cause) "field required" error.
_REQUIRED_FIELDS = [
    "META_VERIFY_TOKEN",
    "META_APP_SECRET",
    "SUPABASE_URL",
    "SUPABASE_KEY",
    "OPENROUTER_DEFAULT_KEY",
]


def get_settings() -> Settings:
    """
    Builds Settings from the Worker's `env` (see module docstring for
    why `from workers import env` rather than `request.scope["env"]`)
    on first call, caches it, and returns the cached instance on every
    call after that within this isolate.

    Deliberately a plain function, not a FastAPI Depends() — the whole
    point is that supabase_client.py and ai_client.py, which are not
    route handlers and in ai_client's case run after the request that
    triggered them has already been responded to, need to call this
    exactly the same way a route handler does, with no Request to pass
    down through several intermediate function calls.

    Safe to cache at module scope despite Cloudflare's own
    best-practices warning against storing state in global scope —
    that warning is about per-request-*varying* data leaking across
    requests (a user id, a request body). Env vars and secrets are
    deployment-level and static for the lifetime of a Worker version:
    every request and every background task sees the same five values
    regardless of which one happened to trigger the first build.
    """
    global _settings
    if _settings is None:
        from workers import env

        missing = [name for name in _REQUIRED_FIELDS if not getattr(env, name, None)]
        if missing:
            raise RuntimeError(
                "Missing required Worker env var(s)/secret(s): "
                + ", ".join(missing)
                + ". Set them with `pywrangler secret put <NAME>` / the "
                "dashboard for a deployed Worker, or in .dev.vars for "
                "local `pywrangler dev` (see .dev.vars.example)."
            )

        _settings = Settings(
            META_VERIFY_TOKEN=env.META_VERIFY_TOKEN,
            META_APP_SECRET=env.META_APP_SECRET,
            SUPABASE_URL=env.SUPABASE_URL,
            SUPABASE_KEY=env.SUPABASE_KEY,
            OPENROUTER_DEFAULT_KEY=env.OPENROUTER_DEFAULT_KEY,
            OPENROUTER_SOCKS5_PROXY=getattr(env, "OPENROUTER_SOCKS5_PROXY", None) or None,
        )
    return _settings
