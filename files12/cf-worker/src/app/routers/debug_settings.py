"""
Stage 1 diagnostic route ONLY. Proves get_settings() actually resolves
every Settings field from the real Worker env under `pywrangler dev`
with `.dev.vars` set — not defaults, not None, not an import-time
error.

Not meant to survive past this stage: it echoes every configured
value, secrets included, to whoever can reach the URL. Fine for a
`pywrangler dev` localhost check; NOT fine deployed anywhere
reachable. Remove this file and its one include_router() line in
app/main.py once Stage 1 is confirmed working — Stage 6 replaces the
health-only router set here with the real webhook router anyway.
"""

from fastapi import APIRouter

from app.config import get_settings

router = APIRouter()


@router.get("/debug/settings")
async def debug_settings() -> dict:
    return get_settings().model_dump()
