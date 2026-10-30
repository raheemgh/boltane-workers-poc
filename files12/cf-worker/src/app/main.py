"""
Boltane Core Engine — FastAPI application entry point
(Cloudflare Python Workers build).

The always-on message handler for Boltane's free-tier multi-tenant
WhatsApp bot:
    - GET  /webhook   Meta webhook verification
    - POST /webhook   incoming WhatsApp message handling
    - GET  /health    keep-alive endpoint (no Supabase/AI calls)

Differences from the Render version's app/main.py, and only these:
  1. Workers has no ASGI server process. The runtime picks up the class `Default`
     (via src/entry.py).
  2. (Stage 6) `Default` is written out instead of `Default = asgi.entrypoint(app)`,
     because the webhook needs the request's `ctx` to call ctx.waitUntil() and the
     ready-made entrypoint does not hand it to the app. See app/worker_ctx.py for the
     measurements behind this. Routing is unchanged: asgi.fetch() still serves `app`.

Stage 6: the webhook router is wired in, and the four temporary
diagnostic routers from Stages 1-4 (debug_settings, debug_supabase,
debug_meta, debug_ai) are no longer imported or included here, so none
of their /debug/* routes exist on the deployed Worker. (The debug_*.py
files themselves are left on disk for now — several earlier test files
load them by path; see stage6-runbook.md.)
"""

from fastapi import FastAPI
from workers import WorkerEntrypoint, asgi

from app.routers import health, webhook
from app.worker_ctx import EnvWithCtx

app = FastAPI(
    title="Boltane Core Engine",
    description="Always-on message handler for Boltane's free-tier multi-tenant WhatsApp bot.",
    version="0.1.0",
)

app.include_router(webhook.router)
app.include_router(health.router)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return await asgi.fetch(app, request, EnvWithCtx(self.env, getattr(self, "ctx", None)))
