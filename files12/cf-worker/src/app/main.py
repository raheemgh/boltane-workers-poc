"""
Boltane Core Engine — FastAPI application entry point
(Cloudflare Python Workers build).

The always-on message handler for Boltane's free-tier multi-tenant
WhatsApp bot:
    - GET  /webhook   Meta webhook verification          (Stage 6 — not wired yet)
    - POST /webhook   incoming WhatsApp message handling (Stage 6 — not wired yet)
    - GET  /health    keep-alive endpoint (no Supabase/AI calls)

Differences from the Render version's app/main.py, and only these:
  1. Workers has no ASGI server process — `Default = asgi.entrypoint(app)`
     at the bottom is what the Workers runtime picks up (via src/entry.py).
  2. The webhook router is NOT included yet: app/routers/webhook.py and
     everything it depends on (config, supabase_client, meta_client,
     ai_client) belong to later migration stages. Stage 6 adds back
     `from app.routers import webhook` and
     `app.include_router(webhook.router)`.
"""

from fastapi import FastAPI
from workers import asgi

from app.routers import debug_ai, debug_meta, debug_settings, debug_supabase, health

app = FastAPI(
    title="Boltane Core Engine",
    description="Always-on message handler for Boltane's free-tier multi-tenant WhatsApp bot.",
    version="0.1.0",
)

# Stage 6: app.include_router(webhook.router)
app.include_router(health.router)
# Stage 1 diagnostic only — see app/routers/debug_settings.py's
# docstring. Remove this line (and that file) once Stage 1 is
# confirmed working, well before anything here is ever deployed.
app.include_router(debug_settings.router)
# Stage 3 diagnostic only — see app/routers/debug_meta.py's docstring.
# Inert unless STAGE3_DEBUG_ENABLED=1 is set in .dev.vars. Remove this
# line (and that file) once Stage 3 is confirmed, before Stage 6.
app.include_router(debug_meta.router)
# Stage 2 diagnostic only — see app/routers/debug_supabase.py's docstring.
# Inert unless STAGE2_DEBUG_ENABLED=1 AND STAGE2_CONFIRM_TEST_PROJECT equals
# SUPABASE_URL. Remove this line (and that file) before Stage 6.
app.include_router(debug_supabase.router)
# Stage 4 diagnostic only — see app/routers/debug_ai.py's docstring.
# Inert unless STAGE4_DEBUG_ENABLED=1 is set in .dev.vars. Remove this
# line (and that file) once Stage 4 is confirmed, before Stage 6.
app.include_router(debug_ai.router)

Default = asgi.entrypoint(app)
