## Deployment (Render — Docker web service, free tier)

### Required environment variables (set in Render's dashboard under Environment, not committed to code)
- `SUPABASE_URL`
- `SUPABASE_KEY`
- `META_VERIFY_TOKEN`
- `META_APP_SECRET`
- `OPENROUTER_DEFAULT_KEY`

If the code introduced any other env var name along the way, add it here too before deploying. Do NOT set `PORT` manually — Render supplies it automatically and the Dockerfile already reads it.

### Keep-alive
Render's free web services spin down after ~15 minutes of inactivity, and the next request pays a ~30–60s cold-start penalty. An external cron service — cron-job.org, a GitHub Actions scheduled workflow, or UptimeRobot (pick one, all free) — must send a `GET` request to `/health` every ~5 minutes to keep it awake. This is configured outside the app itself (in the cron service's dashboard), not in code. This is an unofficial workaround, not something Render guarantees long-term, and running nonstop uses roughly all of the free tier's 750 included monthly compute hours.

### Notes
- Render web services get a public `onrender.com` URL by default — this is required here, since Meta's webhook can only send its own `X-Hub-Signature-256` header (not a private-access token), so the endpoint must be publicly reachable. `POST /webhook` already verifies that signature on every request, so public reachability doesn't mean unauthenticated — unsigned requests are rejected with 403.
- Runtime: Docker (Render auto-detects the Dockerfile).
- Free-tier RAM is small (512MB on Render's free instance type) but comfortably sufficient here: this service does no local AI inference and no local data storage — it's a thin async relay between Meta, Supabase, and OpenRouter. Avoid caching full per-store conversation history in memory.
