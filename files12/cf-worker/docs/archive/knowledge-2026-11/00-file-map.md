# Boltane Core Engine — File Map

Maps the flattened files you downloaded back to their real location in the
repo, plus what each one contains and which migration stage touches it.

| Original path in the repo | File you have | Contains | Stage that touches it |
|---|---|---|---|
| `app/main.py` | `main.py` | FastAPI app instantiation, wires up the routers | 0 (ported verbatim, entrypoint pattern changes) |
| `app/config.py` | `config.py` | `Settings(BaseSettings)` — env var / secrets config | **1** (rewrite target) |
| `app/routers/health.py` | `routers_health.py` | `GET /health` — trivial keep-alive endpoint | 0 (ported verbatim) |
| `app/routers/webhook.py` | `routers_webhook.py` | `GET/POST /webhook` — Meta verification challenge, hmac signature check, schedules the background pipeline | **6** (integration target) |
| `app/services/ai_client.py` | `services_ai_client.py` | OpenRouter streaming chat completion, prompt construction, response parsing | **4** (rewrite target) |
| `app/services/meta_client.py` | `services_meta_client.py` | Sends WhatsApp replies via the Meta Graph API (`httpx.AsyncClient().post()`) | **3** (port target) |
| `app/services/pii_masking.py` | `services_pii_masking.py` | PII masking logic — pure Python, no I/O | 0 (ported verbatim, no changes needed) |
| `app/services/supabase_client.py` | `services_supabase_client.py` | Store lookup / conversation_windows upsert / usage update — sync client wrapped in `asyncio.to_thread()` (the confirmed threading blocker) | **2** (rewrite target) |
| `app/services/token_limits.py` | `services_token_limits.py` | Token/budget firewall logic — pure Python, no I/O | 0 (ported verbatim); **5** verifies its live integration |
| `requirements.txt` | `requirements.txt` | Current Render dependency list | 0 (reference for building `pyproject.toml`) |
| `Dockerfile` | `Dockerfile.txt` | Render container build config | Not migrated — Render-only, stays as-is |
| `DEPLOYMENT.md` | `DEPLOYMENT.md` | Render deployment notes, incl. the keep-alive cron workaround | Step 4 (deployment notes) references this |
| `stores_table.sql` | `stores_table.sql` | Supabase schema — `stores` / `conversation_windows` tables | **2** (schema context for the rewrite) |
| `test_package_system.py` | `test_package_system.py` | Existing tests — "package system" behavior | 0 (should pass unmodified); **7** (final adaptation) |
| `test_usage_protection.py` | `test_usage_protection.py` | Existing tests — usage/budget firewall behavior | 0 (should pass unmodified); **5**, **7** |
| `test_webhook_timing.py` | `test_webhook_timing.py` | Existing tests — fast-response-then-background-completion timing, via a real `uvicorn` subprocess | **7** (needs a redesign — the subprocess approach doesn't translate to Workers) |
| `.env.example` | `env.example.txt` | Template of the expected env vars/secrets | **1** (list of fields `Settings` needs) |

## Not included (skipped on purpose — trivial or irrelevant)
- `app/__init__.py`, `app/models/__init__.py`, `app/routers/__init__.py`,
  `app/services/__init__.py` — empty package markers, nothing to port.
- `.dockerignore` — Render-specific, not relevant to the Workers migration.

## Cloudflare Workers tree — `cf-worker/` (added in Stage 0)
The Workers build lives in `cf-worker/`, next to the untouched Render code. Its
module root is `cf-worker/src/`, and inside it the `app/...` paths are identical
to the Render repo, so `from app.services.token_limits import ...` works unchanged.
Flat filenames use the prefix `cfw_` + the same convention as above (drop `app/`,
`/` -> `_`) so they never collide with the Render flat files. When a later stage
ports a file, its Workers copy is a NEW `cfw_*` file — the Render flat file stays.

| Path in the repo | File in Project Knowledge | Contains | Stage |
|---|---|---|---|
| `cf-worker/pyproject.toml` | `cfw_pyproject.toml` | Workers deps (fastapi/httpx/supabase) + dev group (workers-py, workers-runtime-sdk) | 0 |
| `cf-worker/wrangler.jsonc` | `cfw_wrangler.jsonc` | `main`, `compatibility_date`, `python_workers` flag | 0 |
| `cf-worker/src/entry.py` | `cfw_entry.py` | one-line shim: wrangler `main`, re-exports `Default` from `app.main` | 0 |
| `cf-worker/src/app/main.py` | `cfw_main.py` | FastAPI app + `Default = asgi.entrypoint(app)`; health router (0), `/debug/settings` (1, temporary — see that file), webhook router added in Stage 6 | 0, 1, 6 |
| `cf-worker/src/app/routers/health.py` | `cfw_routers_health.py` | `GET /health` — verbatim copy | 0 |
| `cf-worker/src/app/services/token_limits.py` | `cfw_services_token_limits.py` | verbatim copy (sha256-identical to the Render file) | 0 |
| `cf-worker/src/app/services/pii_masking.py` | `cfw_services_pii_masking.py` | verbatim copy (sha256-identical to the Render file) | 0 |
| `cf-worker/test_stage0_ports.py` | `cfw_test_stage0_ports.py` | characterization tests for the two verbatim ports (the existing suites don't call them directly) | 0 |

Not uploaded to Knowledge (trivial, like the Render ones): `cf-worker/.gitignore`,
`cf-worker/src/app/__init__.py`, `.../routers/__init__.py`, `.../services/__init__.py`.

## Stage 1 additions/changes
| Path in the repo | File in Project Knowledge | Contains | Notes |
|---|---|---|---|
| `cf-worker/src/app/config.py` | `cfw_config.py` | Rewritten `Settings` (plain `pydantic.BaseModel`, not `BaseSettings`) + `get_settings()` — lazy, cached, built from `from workers import env`, never `os.environ` | NEW copy this stage — Stage 0 explicitly left config.py unported |
| `cf-worker/src/app/routers/webhook.py` | `cfw_routers_webhook.py` | Same as the Render original except 3 lines: the `settings` import and its two usages now call `get_settings()` | NEW copy this stage — business logic untouched, diffed against the Render original to confirm |
| `cf-worker/src/app/services/ai_client.py` | `cfw_services_ai_client.py` | Same as the Render original except 3 lines, same pattern as webhook.py | NEW copy this stage |
| `cf-worker/src/app/services/supabase_client.py` | `cfw_services_supabase_client.py` | Module-level eager `_client = create_client(...)` replaced with lazy, cached `_get_client()`; its 5 call sites updated to call it | NEW copy this stage — see chat message for why this one's diff is larger than the other two |
| `cf-worker/src/app/routers/debug_settings.py` | `cfw_routers_debug_settings.py` | Temporary `GET /debug/settings` diagnostic route — echoes every Settings field. Remove before Stage 6 / before any real deployment | NEW this stage, temporary |
| `cf-worker/.dev.vars.example` | `cfw_dev_vars_example.txt` | Template for `pywrangler dev`'s local `.dev.vars` file — same 5 keys as `env.example.txt` | NEW this stage |
| `cf-worker/tooling/local-test-support/workers.py` | `cfw_tooling_workers_stub.py` | LOCAL-TEST-ONLY stub for `from workers import env`, backed by `os.environ` — never vendored/deployed | NEW this stage, dev tooling only — see the file's own docstring |
| `cf-worker/test_stage1_settings.py` | `cfw_test_stage1_settings.py` | Characterization tests for `get_settings()`: real values, caching, optional field, clear failure on a missing required field | NEW this stage |

## Stage 1 correction — `meta_client.py` was missing
`routers_webhook.py` imports `app.services.meta_client.send_whatsapp` — that
file was never copied into `cf-worker/`, so `import app.routers.webhook`
failed with `ModuleNotFoundError`. Fixed by porting it **verbatim** (it never
touches `settings` — takes `access_token` as a parameter, same as
`token_limits.py`/`pii_masking.py` in Stage 0), sha256-identical to the
Render original.

Note: the master file-map above lists `app/services/meta_client.py` as a
Stage 3 "port target". Since it needs zero changes to run on Workers, this
verbatim copy already IS what Stage 3 would have produced for this file —
Stage 3 shouldn't need to touch it again, just confirm that.

| Path in the repo | File in Project Knowledge | Contains | Stage |
|---|---|---|---|
| `cf-worker/src/app/services/meta_client.py` | `cfw_services_meta_client.py` | verbatim copy (sha256-identical to the Render file) | 1 (fix — originally slated for 3, needed no changes) |

## Stage 2 additions/changes — `supabase_client.py` async rewrite
`cf-worker/src/app/services/supabase_client.py` (`cfw_services_supabase_client.py`)
is REWRITTEN: sync client + thread offloading replaced by `acreate_client` and native
`await`. Built on Stage 1's `get_settings()`; the four public signatures are unchanged.
Client policy: built lazily, cached per isolate (`CACHE_CLIENT_ACROSS_CALLS = True`),
one-line fallback to per-call clients if the reuse probe ever fails.

| Path in the repo | File in Project Knowledge | Contains | Notes |
|---|---|---|---|
| `cf-worker/src/app/services/supabase_client.py` | `cfw_services_supabase_client.py` | the async port | REPLACES the Stage 1 version |
| `cf-worker/src/app/routers/debug_supabase.py` | `cfw_routers_debug_supabase.py` | scenarios (`run_op`), `EXPECTED`, `HttpDB` verifier, temporary `GET /debug/supabase` | NEW, temporary — needs `STAGE2_DEBUG_ENABLED=1` and `STAGE2_CONFIRM_TEST_PROJECT == SUPABASE_URL`; remove before Stage 6 |
| `cf-worker/tooling/local-test-support/memory_supabase.py` | `cfw_tooling_memory_supabase.py` | in-memory PostgREST-like emulator (test-only, next to the `workers` stub) | NEW |
| `cf-worker/test_stage2_supabase_unit.py` | `cfw_test_stage2_supabase_unit.py` | offline tests: static, client lifetime, Stage 1 link, exact queries, scenarios, parity with existing tests, webhook call patterns, Stage 3 chain | NEW |
| `cf-worker/test_stage2_supabase_real.py` | `cfw_test_stage2_supabase_real.py` | real TEST-project CLI: `render` / `async` / `workers [--reuse-probe]` / `compare` | NEW |
| `cf-worker/src/app/main.py` | `cfw_main.py` | also includes the `debug_supabase` router; requires the Stage 3 `debug_meta` file too | MODIFIED — supersedes the Stage 3 version |

`.dev.vars` (local only, TEST project!) gains: `STAGE2_DEBUG_ENABLED`,
`STAGE2_CONFIRM_TEST_PROJECT`. Generated at test time: `stage2_{render,async,workers}.json`.


## Stage 3 additions/changes — `meta_client.py` verification
`cf-worker/src/app/services/meta_client.py` (`cfw_services_meta_client.py`) is
**unchanged** in Stage 3: it imports only `logging` and `httpx`, never reads
`settings`/`os.environ`, and stays sha256-identical to the Render original.
Stage 3 adds only test scaffolding around it.

| Path in the repo | File in Project Knowledge | Contains | Notes |
|---|---|---|---|
| `cf-worker/src/app/routers/debug_meta.py` | `cfw_routers_debug_meta.py` | `run_case()` scenario runner + temporary `GET /debug/meta-send?case=...` route | NEW, temporary — inert unless `STAGE3_DEBUG_ENABLED=1`; remove before Stage 6 / any deployment |
| `cf-worker/test_stage3_meta.py` | `cfw_test_stage3_meta.py` | CLI: `check` (hash + no-settings proof), `render`, `workers`, `compare` | NEW |
| `cf-worker/test_stage3_offline.py` | `cfw_test_stage3_offline.py` | Offline conformance test: a Meta-spec simulator under the real httpx (`MockTransport`), run against both copies of `meta_client.py`. No network/credentials needed | NEW |
| `cf-worker/src/app/main.py` | `cfw_main.py` | Now also includes the `debug_meta` router (2 lines + comment) | MODIFIED — replaces the Stage 0/1 version |
| `cf-worker/.dev.vars` | (not uploaded — holds secrets) | + `STAGE3_DEBUG_ENABLED`, `META_TEST_ACCESS_TOKEN`, `META_TEST_PHONE_NUMBER_ID`, `META_TEST_RECIPIENT` | local only; never committed |

Generated at test time, not source: `cf-worker/stage3_render.json`,
`cf-worker/stage3_workers.json` (contain phone numbers — don't commit).

## PII masking generalisation — `pii_masking.py` (supersedes its Stage 0 "verbatim" status)
`cf-worker/src/app/services/pii_masking.py` (`cfw_services_pii_masking.py`) is **no longer
verbatim**. v1 masked Syrian mobiles (ASCII digits) only; v2 masks phone numbers from any
country, in international / national / bare forms, with Arabic-Indic and Persian digits, and
fixes three measured v1 defects (see the module docstring). Public API unchanged:
`mask_phone_numbers(text) -> MaskResult`, `MaskResult.restore(text)`; `ai_client.py` needs no change.

| Path in the repo | File in Project Knowledge | Notes |
|---|---|---|
| `cf-worker/src/app/services/pii_masking.py` | `cfw_services_pii_masking.py` | REPLACES the Stage 0 copy. The Render original is deliberately untouched, so the two trees now differ on non-Syrian numbers until/unless Render is updated too (Render is retired by this migration). |
| `cf-worker/test_pii_masking_general.py` | `cfw_test_pii_masking_general.py` | NEW, stdlib only; compares against the Render original when present |

Consequence for Stage 4: "same handling of pii_masking as Render" now holds for Syrian-number
inputs only; for other numbers the Workers tree masks MORE than Render, by design.
`test_stage0_ports.py` may assert v1-only behaviour — re-run it after replacing the file.

## Stage 4 additions/changes — `ai_client.py` port
`cf-worker/src/app/services/ai_client.py` (`cfw_services_ai_client.py`) is the Render original with exactly these differences:
settings read through `get_settings()` (Stage 1 pattern; the Stage 1 version of this file was never in Knowledge, so this copy
carries that change too); `httpx.AsyncClient` is NEVER given `proxy=` (a non-empty `OPENROUTER_SOCKS5_PROXY` logs one warning per
isolate and is otherwise ignored); and the low-tier chain `FALLBACK_MODELS` is `[DEEPSEEK_MODEL]` instead of three placeholder
`:free` IDs (decision of Stage 4 — pending the owner's explicit confirmation; one-line change; measurements in
`stage4-findings-and-backlog.md`). `_stream_completion`, `_post_with_network_retry`, `_resolve_models` and `_NetworkError` are
AST-identical to the Render original (checked by the offline test).

| Path in the repo | File in Project Knowledge | Contains | Notes |
|---|---|---|---|
| `cf-worker/src/app/services/ai_client.py` | `cfw_services_ai_client.py` | the Workers port described above | NEW copy this stage |
| `cf-worker/src/app/routers/debug_ai.py` | `cfw_routers_debug_ai.py` | temporary `POST /debug/ai` — runs the real `call_ai` inside Workers and returns reply + per-call trace (serving provider, finish reason, usage, request-body hash) | NEW, temporary — inert unless `STAGE4_DEBUG_ENABLED=1`; remove before Stage 6 (together with its two lines in `main.py`) |
| `cf-worker/src/app/main.py` | `cfw_main.py` | now also includes the `debug_ai` router | MODIFIED — supersedes the Stage 3 version |
| `cf-worker/test_stage4_offline.py` | `cfw_test_stage4_offline.py` | no-network suite (35 PASS lines): scripted OpenRouter streaming, masking round-trip, fallback/402/network-retry, documented edge cases, AST parity + parity run against the Render original, 9 sabotage checks | NEW |
| `cf-worker/test_stage4_real.py` | `cfw_test_stage4_real.py` | real OpenRouter calls: `render` / `async` / `workers [ids]` / `compare` / `traces`; key read from `.dev.vars` | NEW |
| `cf-worker/probe_stage4_tokens.py` | `cfw_probe_stage4_tokens.py` | diagnostic probe (modes: none, `variants`, `free`, `model <slug>`); not part of the product | NEW, diagnostic |
| — | `stage4-findings-and-backlog.md` | measurements (thinking tokens vs the 300 cap, free-model results, Nex), documented-not-changed items, post-migration candidates in feature-intake form | NEW, documentation |

Stage 4 findings in one place (details in `stage4-findings-and-backlog.md`):
- Behaviour is the same on Render, CPython and Workers: for identical requests (same body hash) the same provider served the call and results varied randomly in both directions; only difference seen is a `content-encoding: identity` header on Workers.
- DeepSeek V4 Flash thinking tokens count inside `max_tokens=300`: a share of replies come back empty (→ `GRACEFUL_FAILURE_REPLY` on a one-model chain) or cut off. Not fixed in the migration; `reasoning: {"enabled": false}` fixed every test and is a post-migration candidate.
- `finish_reason` is not captured; mid-stream errors inside HTTP 200 are not detected; the engine sends no chat history; `estimate_tokens` underestimates Arabic by ~1.6x.
- Intended difference from Render: `pii_masking.py` v2 masks non-Syrian phone numbers that the Render original sends to OpenRouter unmasked.

Files to put in Knowledge for this stage: `cfw_services_ai_client.py`, `cfw_routers_debug_ai.py`, `cfw_main.py`, `cfw_test_stage4_offline.py`,
`cfw_test_stage4_real.py`, `cfw_probe_stage4_tokens.py`, `stage4-findings-and-backlog.md`, and this `00-file-map.md` (replaces the old one).

## Stage 5 additions/changes — token/budget firewall integration test
No source file changed in this stage: `token_limits.py` stays byte-identical to the Render original, and the firewall's
call sites in `webhook.py` are structurally identical to Render (AST-compared by the test). Stage 5 adds a test and a
findings document only.

| Path in the repo | File in Project Knowledge | Contains | Notes |
|---|---|---|---|
| `cf-worker/test_stage5_firewall.py` | `cfw_test_stage5_firewall.py` | End-to-end firewall test: the REAL `_process_message` + real `supabase_client` (over the in-memory emulator) + real `ai_client` + real `meta_client`; only Supabase and the network are replaced. 9 scenarios (S01–S09), Render parity (`record render` / `record workers` / `compare`), 14-mutation sabotage run | NEW. Default run = Workers tree; needs `tooling/local-test-support/memory_supabase.py` (Stage 2) |
| — | `stage5-findings-and-backlog.md` | Real pipeline order, what was proven, the cap-bypass gap (S07, identical on Render), other observations, monthly-reset answer, feature-intake row | NEW, documentation |

Stage 5 findings in one place (details in `stage5-findings-and-backlog.md`):
- Pipeline order: get_store -> input cap -> touch (window write) -> monthly cap -> call_ai -> update_usage -> add_tokens -> send.
- Known gap, same on Render: the window is written BEFORE the monthly-cap check, so a blocked customer's next message counts as "continuing" and skips the cap. Pinned as scenario S07; fix proposed as a post-Stage-6 flag (`block_without_window_touch`, default false) — awaiting the owner's decision.
- Monthly reset: nothing in the Core Engine files resets `monthly_conversation_count`; `usage_reset_at` exists in the schema but nothing reads or writes it. The admin-bot spec refers to an existing usage-reset logic whose location is unknown, so no second resetter is added yet. Decision: a calendar-month reset checked inside the Core Engine after Stage 6 as flag `monthly_cycle_reset` (default false); the 100% alert lives in the admin bot's cron, not in the Core Engine. Contract/questions: `handoff-for-admin-bot-chat.md` (for the admin-bot chat, not a Knowledge file for this stage).

Files to put in Knowledge for this stage: `cfw_test_stage5_firewall.py`, `stage5-findings-and-backlog.md`, and this `00-file-map.md` (replaces the old one).

## Stage 6 additions/changes — webhook wiring + deployed-Worker test (status: superseded in part by Stage 6e below)
`cf-worker/src/app/routers/webhook.py` (`cfw_routers_webhook.py`) is **unchanged**: it differs from the Render original only in the
three `settings` -> `get_settings()` lines made in Stage 1 (checked with `diff` and by AST in `test_stage6_routes.py`). The GET handshake,
the HMAC check and the `BackgroundTasks` scheduling were therefore already in place. Stage 6 changes `main.py` and adds tests + a runbook.

| Path in the repo | File in Project Knowledge | Contains | Notes |
|---|---|---|---|
| `cf-worker/src/app/main.py` | `cfw_main.py` | now includes `webhook.router` + `health.router`; the four temporary debug routers are no longer imported or included | MODIFIED — REPLACES the Stage 4 version |
| `cf-worker/test_stage6_routes.py` | `cfw_test_stage6_routes.py` | offline: static wiring checks, AST parity of webhook.py/main.py with Render, real-app route tests through FastAPI TestClient (GET handshake, 6 kinds of bad signature -> 403 + nothing scheduled, valid signature -> 200 + scheduled once, /debug/* -> 404), 4 sabotage checks | NEW. Needs fastapi + httpx (the Stage 5 venv) |
| `cf-worker/test_stage6_deployed.py` | `cfw_test_stage6_deployed.py` | stdlib-only client for a DEPLOYED Worker: `check` (signature negatives, handshake, /debug 404) and `deliver` (signed Meta-shaped deliveries, response timing, Supabase polling that classifies COMPLETE / STARTED_NOT_FINISHED / NOT_STARTED) | NEW. Read-only on Supabase. Writes `stage6_deployed_results.json` (don't commit) |
| — | `stage6-runbook.md` | ordered steps with expected output, the Phase A (no Meta) / Phase B (real Meta) split, stop rules, what was not checked | NEW, documentation |
| — | `stage6-meta-simulation-plan.md` | what a Meta simulation can/cannot prove, code-reading findings on real Meta webhook shapes (statuses, non-text, duplicates, no chat history…), the in-process simulator design (6b), where each risk is handled (now vs after Stage 7) | NEW, documentation — proposal, simulator not built yet |
| — | `install_stage6.py` | one-file installer: writes the three Stage 6 files to their repo paths (self-checking, run from cf-worker) | NEW, helper — not needed in Knowledge |

Left on disk on purpose: `cf-worker/src/app/routers/debug_{settings,supabase,meta,ai}.py` and their `cfw_routers_debug_*.py` Knowledge copies.
Nothing includes them any more, so no /debug/* route exists, but `test_stage2_supabase_unit.py`, `test_stage2_supabase_real.py` and
`test_stage3_meta.py` load them by path. Moving them out of `src/` (so they are not bundled) is a Stage 7 item unless decided otherwise.

Stage 6 facts in one place (details in `stage6-runbook.md`):
- Cloudflare limits page (updated 2026-10-12): work after the response via `waitUntil` runs up to 30 s; Free plan = 10 ms CPU per request, startup time 1 s, Worker size 64 MiB uncompressed with NO compressed-size limit (an earlier reading of 3 MB compressed was outdated). CPU and startup have not been measured against Core Engine yet; the pipeline took up to 108 s in Stage 4.
- Real WhatsApp delivery and the Meta-dashboard handshake need Meta credentials and an account holder eligible for the WhatsApp Business Platform; Phase A of the runbook works without them and stops at the send step.

Files to put in Knowledge for this stage (6, 6b, 6c): `stage6-findings-and-backlog.md`, `cfw_test_stage6_meta_sim.py`, `cfw_services_supabase_client.py` (replace), `cfw_pyproject.toml` (replace), `cfw_test_stage2_supabase_unit.py` (replace), `cfw_main.py` (replace), `cfw_test_stage6_routes.py`, `cfw_test_stage6_deployed.py`, `stage6-runbook.md`, `stage6-meta-simulation-plan.md`, and this `00-file-map.md` (replaces the old one — delete the old `00-file-map (4).md` first).

## Stage 6b — bundle-size fix: `supabase` umbrella package replaced by `postgrest` alone (status: deployed and exercised by the user's Stage 6 runs)
Why: `uv run pywrangler deploy --dry-run` measured 19176.79 KiB total / 5075.37 KiB gzip. This was first read as exceeding a 3 MB compressed Free limit; Cloudflare's current limits page (updated 2026-10-12) has NO compressed limit (64 MiB uncompressed), so the size was NOT blocking. The change is kept because it removes ~8.5 MB of unused packages (measured after: 10485.76 KiB total / 2561.00 KiB gzip) and reduces startup work (limit 1 s).
`du` on `python_modules/` showed `cryptography` (7116 KB, plus cffi/pycparser) as the largest item; `grep` of the dist-info METADATA files showed it is
required only via `supabase_auth` -> `pyjwt[crypto]` — the auth client of the umbrella package, which Core Engine never uses.
`postgrest` alone depends on httpx[http2], deprecation, pydantic, yarl (no cryptography, no websockets) and provides the same
`AsyncPostgrestClient.table()` builder and `APIError` class. Measured result: 19176.79 -> 10485.76 KiB total.

| Path in the repo | File in Project Knowledge | Change | Notes |
|---|---|---|---|
| `cf-worker/src/app/services/supabase_client.py` | `cfw_services_supabase_client.py` | imports `AsyncPostgrestClient` instead of `supabase.acreate_client`; adds a local `acreate_client(url, key)` that builds the client with base URL `<url>/rest/v1`, headers `apikey` + `Authorization: Bearer`, schema public, and `DEFAULT_POSTGREST_CLIENT_TIMEOUT` (what the umbrella package passes) | REPLACES the Stage 2 version. AST-checked: every other function (4 public, `_parse_timestamp`, `_new_client`, `_get_client`) is body-identical; only type hints changed |
| `cf-worker/pyproject.toml` | `cfw_pyproject.toml` | dependency `supabase>=2.9.0` -> `postgrest>=2.31.0,<3` | REPLACES the old copy. After applying, `rm -rf python_modules` so no stale vendored package stays |
| `cf-worker/test_stage2_supabase_unit.py` | `cfw_test_stage2_supabase_unit.py` | one static check updated (it looked for the text `from supabase import acreate_client`) | REPLACES the old copy |
| — | `install_stage6b.py` | one-file, all-or-nothing installer for the three changes above (refuses to overwrite a `supabase_client.py` that differs from the Knowledge copy) | NEW, helper — not needed in Knowledge |

Not verified yet (needs the user's runs): the offline Stage 1/2/5 tests after the change; that the effective timeout equals the old one
(`DEFAULT_POSTGREST_CLIENT_TIMEOUT` vs the umbrella package's `postgrest_client_timeout` default — a one-line check is in the runbook step); the real-project Stage 2
comparison (needs the TEST Supabase project active); the new `--dry-run` size.

## Stage 6c — Meta replay simulator (status: first real run by the user: 54 scenarios, 23 PASS / 31 GAP / 0 FAIL; re-run after Stage 6e: same numbers)
| Path in the repo | File in Project Knowledge | Contains | Notes |
|---|---|---|---|
| `cf-worker/test_stage6_meta_sim.py` | `cfw_test_stage6_meta_sim.py` | 54 scenarios replayed through the REAL `_process_message`/`supabase_client`/`ai_client`/`meta_client` over real httpx (MockTransport): Meta Graph contract + documented error codes, scripted OpenRouter (429/402/500/empty stream/cut-off/mid-stream error/resets), in-memory Supabase with a yield point per query and an outage switch. Verdicts PASS / GAP (known weakness, same on Render) / FAIL (expectation differs). `--sabotage` breaks the engine 14 ways and checks the suite notices; `--show ID` prints everything observed for a scenario. Reuses `load_tree` and `_Outage`/`_Collect` from `test_stage5_firewall.py` | NEW. Needs the Stage 5 test file and `tooling/local-test-support` next to it. Writes `stage6_meta_sim_report.json` (don't commit) |
| — | `install_stage6c.py` | one-file installer for the simulator | NEW, helper — not needed in Knowledge |
| — | `stage6-findings-and-backlog.md` | the 11 findings of the first real simulator run (23 PASS / 31 GAP / 0 FAIL) as a V2.0 backlog: gap, impact, proposed fix, flag key (all default false), suggested priority; plus what is still unmeasured | NEW, documentation |

Validated here without fastapi/httpx: scenario table consistent with the real parse step, the real PII masking and the input-cap arithmetic; static name check. NOT run end to end: needs httpx/fastapi in the Codespaces venv.

## Stage 6e — `ctx.waitUntil` scheduling (status: confirmed by the user's runs; two items deferred to V2.0)
Why: on a DEPLOYED Worker, FastAPI `BackgroundTasks` kept the HTTP response waiting until the whole task ended (5.7 / 40.9 / 70.0 s for tasks of 5 / 40 / 70 s), so Meta's 200 would wait for the AI pipeline. `ctx.waitUntil` returned at once but cancels work about 30 s after the response (a 28 s task finished, a 35 s task was cancelled). This contradicts the PoC premise in the Stage 6 task ("BackgroundTasks is bridged into waitUntil") for the response-time part. The request's `ctx` reaches the route inside the ASGI `env` (class `EnvWithCtx`), no module-level state.

| Path in the repo | File in Project Knowledge | Change | Notes |
|---|---|---|---|
| `cf-worker/src/app/worker_ctx.py` | `cfw_worker_ctx.py` | NEW: `EnvWithCtx`, `get_worker_ctx(scope)` — pure Python | NEW |
| `cf-worker/src/app/main.py` | `cfw_main.py` | `class Default(WorkerEntrypoint)` whose `fetch` calls `asgi.fetch(app, request, EnvWithCtx(self.env, self.ctx))` instead of `Default = asgi.entrypoint(app)` | REPLACES the Stage 6 version |
| `cf-worker/src/app/routers/webhook.py` | `cfw_routers_webhook.py` | `receive_message` only: `ctx.waitUntil(asyncio.ensure_future(_process_message(payload)))` when a ctx exists, `background_tasks.add_task` otherwise (Render's path). `_process_message`, constants and the other functions are AST-identical to Render | REPLACES the Stage 1 copy |
| `cf-worker/test_stage6_routes.py` | `cfw_test_stage6_routes.py` | static check for the new `Default`, `receive_message` no longer required to be AST-identical (replaced by explicit checks), new scheduling checks (section 5) | REPLACES the old copy |
| — | `install_stage6e.py`, `install_probe2.py` | all-or-nothing installers | helpers, not needed in Knowledge |

Throwaway probe Worker `boltane-probe` (folder `cf-probe`, versions 1 and 2): measured the behaviour above; delete it with `npx wrangler delete --name boltane-probe` when done.

Results the user ran: `test_stage6_routes.py` 36 PASS; `test_stage5_firewall.py` 29/29; `test_stage6_meta_sim.py` 23 PASS / 31 GAP / 0 FAIL. Deployed Worker: `check` 12/12; `deliver` 18 deliveries (10 at 5 s gaps, 5 at 90 s gaps, 3 long messages): all HTTP 200 under 1 s and COMPLETE, background pipeline finished 1.2–13.8 s after the response; Meta answered code 190 for the deliberately fake token, so the Worker does reach Graph API.

DEFERRED (not done, do not claim): a real WhatsApp message arriving on a phone, and the handshake from the real Meta App Dashboard (Meta's documentation lists Syria among places not eligible for the WhatsApp Business Platform). Known gap: work that runs longer than about 30 s after the response is cancelled (backlog item 12 in `stage6-findings-and-backlog.md`). Cold starts after long idle periods are not measured.

Files to put in Knowledge for this stage: `cfw_worker_ctx.py` (new), `cfw_main.py`, `cfw_routers_webhook.py`, `cfw_test_stage6_routes.py`, `stage6-findings-and-backlog.md`, and this `00-file-map.md` (each REPLACES the old copy; delete the old copy first). `stage6-runbook.md` still describes BackgroundTasks in its introduction; the measurements above supersede it.


## Stage 7 additions — test-suite adaptation + final parity (status: run by the user; documentation draft, pending his confirmation)
No application file changed in this stage (`cf-worker/src` is byte-identical to commit `75c4474`; the suite checks this with `git diff`). Stage 7 adds tests and helper scripts only. The three original test files (`test_package_system.py`, `test_usage_protection.py`, `test_webhook_timing.py`) are NOT in the Codespace repo (preflight search of `/workspaces/boltane-workers-poc`), so the Workers copies below are created from the Knowledge originals. Correction to earlier text in this file: "should pass unmodified" (Stage 0/1) stopped being true at Stage 2 (async supabase client) — two tests need an async Supabase fake.

| Path in the repo | File in Project Knowledge | Contains | Notes |
|---|---|---|---|
| `cf-worker/test_package_system.py` | `cfw_test_package_system.py` | Workers copy: only functional change is `_FakeQueryBuilder.execute` -> `async def` (the Workers supabase_client awaits it); assertions untouched | NEW. Run: `PYTHONPATH=tooling/local-test-support:src python test_package_system.py` — 5/5 on his Codespace |
| `cf-worker/test_usage_protection.py` | `cfw_test_usage_protection.py` | Workers copy: no test body changed (comments/docstring + run command only); imports the async fake above | NEW. 8/8 on his Codespace |
| `cf-worker/test_webhook_timing.py` | `cfw_test_webhook_timing.py` | Redesign for Workers, stdlib only, black-box against a running Worker (`pywrangler dev` or the deployed URL): Phase A = three wrongly signed requests (403, body exactly `invalid signature`, text/plain, and the conversation window in Supabase unchanged); Phase B = differential timing, CONTROL (unknown store) vs TEST (real pipeline), tolerance = measured spread of the controls, minimum-based statistic, INCONCLUSIVE when the pipeline is too short to discriminate | NEW. Exit 0 PASS / 1 FAIL / 2 cannot run / 3 INCONCLUSIVE. Writes `stage7_timing_results.json` (do not commit). `--long` is NOT a valid probe with the default model (see below) |
| `cf-worker/stage7_run_suite.py` | `cfw_stage7_run_suite.py` | runs the adapted tests one function at a time, rebuilds the "before" baseline by putting the one changed line back, checks `cf-worker/src` == HEAD, optional `--regression` summary of every earlier offline test | helper, kept in Knowledge because the second round needs it after every code change. Writes `stage7_suite_report.txt` (do not commit) |
| `cf-worker/stage7_test_store.py` | `cfw_stage7_test_store.py` | `status` / `create` / `delete --yes` for the ONE temporary row `stage6-test-pn` (fake access token) | helper. Deleting the row is POSTPONED to the end of the second round (his decision): `python stage7_test_store.py delete --yes` |
| `cf-worker/stage7_preflight.py` | — | read-only fact collector used at the start of Stage 7 | helper |
| — | `stage7-final-summary.md` | measured results, intentional differences Render -> Workers, open items, cleanup | NEW, documentation |

Stage 7 facts in one place (details in `stage7-final-summary.md`):
- Baseline reconstruction: with the single line put back, 4/5 and 7/8 pass; exactly `test_conversation_window_layer` and `test_supabase_layer_forces_reset_when_token_cap_reached` flip FAIL -> PASS. (A reconstruction, not an original file.)
- All earlier offline tests green on his Codespace (Python 3.14.2): stage0, pii 29/29, stage1, stage2 unit 41/41, stage3 offline 52/52, stage4 offline, stage5 29/29, routes 36/36, meta sim 54 = 23 PASS / 31 GAP / 0 FAIL.
- Render-vs-Workers: `test_stage5_firewall.py record workers` + `record render` + `compare` after the postgrest and waitUntil changes: both sides match EXPECTED, IDENTICAL on all 9 scenarios, 0 problems. The Render side is the Render original under CPython (`files12/app`); the hosted Render service was never touched.
- Timing, local `pywrangler dev` (ceiling 1.0 s): PASS. Deployed Worker (ceiling 3.0 s): PASS (slowest TEST response 0.96 s; work finished 2.5–11.5 s AFTER the response). One short-message delivery in five on each side did not complete: the local dev log shows the DeepSeek V4 Flash empty reply -> `GRACEFUL` (documented in Stage 4); on the deployed Worker the cause was not captured.
- The ~1190-character message (`--long`) ends in an empty reply with the default model (local log confirmed): the deployed 0/5 and 0/1 are consistent with it, not confirmed (a >30 s `waitUntil` cancellation, backlog item 12, is not excluded).
- Not measured: real WhatsApp delivery, cold starts after hours of idle, concurrent messages to one store, a pipeline longer than 30 s on the deployed Worker.

Files to put in Knowledge for this stage: `cfw_test_package_system.py`, `cfw_test_usage_protection.py`, `cfw_test_webhook_timing.py`, `cfw_stage7_run_suite.py`, `cfw_stage7_test_store.py`, `stage7-final-summary.md`, and this `00-file-map.md` (replaces the old one — delete the old copy first).
