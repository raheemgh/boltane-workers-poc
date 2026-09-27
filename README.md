# boltane-workers-poc

Throwaway proof-of-concept. Tests exactly 3 unconfirmed items from the
Core Engine -> Cloudflare Workers feasibility audit, nothing else. Does
NOT touch the real Core Engine repo and isn't meant to be deployed
long-term or reused as-is.

## Setup

1. Copy the secrets template and fill in real values:
   ```bash
   cp .dev.vars.example .dev.vars
   ```
   Edit `.dev.vars` and fill in `OPENROUTER_API_KEY` (Ava's existing key
   is fine for this one-off test), `SUPABASE_URL`, `SUPABASE_KEY`, and
   `SUPABASE_TEST_TABLE` (any small real table -- `stores` by default).
   **Do not commit `.dev.vars`.**

2. Install deps and start the local dev server (uses the real
   Pyodide/workerd runtime -- no Cloudflare deployment needed for this):
   ```bash
   uv sync
   uv run pywrangler dev
   ```
   It should print a local URL, typically `http://localhost:8787`.

## Test 1 -- BackgroundTasks + ctx.waitUntil()

This is the most important one -- the whole webhook architecture
depends on the answer.

Fire 10 separate requests (each one is its own request/response cycle,
exactly like 10 separate Meta webhook deliveries would be):

```bash
for i in $(seq 1 10); do
  curl -s "http://localhost:8787/test/background?call_id=$i"
  echo
done
```

Each call should return **immediately** (near-instant `"status":
"response sent"`), NOT after a 2-second wait -- that's the "responds
fast" half working.

Then wait ~5 seconds and check what actually landed:

```bash
sleep 5
curl -s http://localhost:8787/test/background-check | python3 -m json.tool
```

**What to report back:**
- Did all 10 calls return near-instantly?
- Does `/test/background-check` show `"count": 10` with all 10
  `call_id`s present, or fewer?
- Also check the `pywrangler dev` terminal output for 10
  `BACKGROUND TASK COMPLETED: ...` log lines -- this is the more
  reliable signal if the in-memory list behaves oddly.
- If some are missing: were they missing consistently (same call_ids
  every run) or randomly (different ones each run)?

If this comes back 10/10 reliably, that's a strong signal
`workers.asgi` bridges `BackgroundTasks` into `ctx.waitUntil()`
correctly. If it's flaky or consistently short, the real pipeline
needs a different mechanism entirely (e.g. a Cloudflare Queue).

*Caveat:* `pywrangler dev` is a high-fidelity local simulation of the
real runtime, but isolate lifecycle/teardown timing on the actual
deployed edge can differ from local dev. If this test passes locally,
it's still worth one `pywrangler deploy` (free tier) + a repeat of this
exact test against the live URL before fully trusting the result, given
how central this one is.

## Test 2 -- OpenRouter SSE streaming

```bash
curl -s "http://localhost:8787/test/stream" | python3 -m json.tool
```

Repeat this ~10 times, including at least once with a longer prompt to
force a longer streamed response:

```bash
curl -s --get "http://localhost:8787/test/stream" \
  --data-urlencode "prompt=Write a 900-word short story about a train that only runs at midnight." \
  | python3 -m json.tool
```

**What to report back:**
- Does `"ok"` come back `true` each time?
- Does `"reply_full"` look complete and coherent, or does it cut off
  mid-sentence/mid-word?
- Does `"chunk_count"` look reasonable for the response length (more
  chunks for the longer prompt)?
- Any `"error"` text at all -- paste it exactly, even if only some
  calls fail.

## Test 3 -- supabase-py async client

```bash
curl -s http://localhost:8787/test/supabase | python3 -m json.tool
```

**What to report back:**
- `"ok": true` with a real `"row_count"`, or the exact `"error"` text
  if it fails.
- If it fails, the exact exception type/message matters a lot here --
  it'll tell us whether it's a threading issue slipping in somewhere
  underneath (unlikely, since this uses the async client specifically),
  a missing/incompatible sub-dependency, or something else entirely
  (e.g. a plain auth/URL mistake, which is worth ruling out first).

## After you run it

Send back the actual output (paste the JSON, the terminal log lines,
and any exact error text) for all three tests. That real signal is
what Step 3 (staged migration plan) and Step 4 (deployment notes) get
built on next -- not assumptions.
