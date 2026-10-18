"""
Keep-alive endpoint.

Must return 200 immediately and must never touch Supabase, call the
AI, or run any webhook logic. An external cron pings this every
~5 minutes purely to keep the free-tier host warm, at zero cost.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}
