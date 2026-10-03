# Ava backend — v3 (full session snapshot)

Cumulative snapshot of every fix/change applied to the original
`ava-backend-fixed__2_.zip` throughout this session, in order. Nothing
about the onboarding flow's business rules, pricing, referral tiers, or
token budgets changed — only correctness fixes and the two explicitly
requested behavior changes (BYOK default-to-no, reply conciseness).

## Bug fixes (v2, unchanged from before)
1. Process crash from one request (Express 4 async errors uncaught) —
   `lib/asyncHandler.ts` + `server.ts`; `lib/sanitize.ts` drops malformed
   history entries.
2. Phone placeholder collisions across messages — `createPhoneMasker()`
   in `lib/piiMasking.ts`, used by `lib/openrouter.ts` (system prompt
   masked too).
3. Trial ceiling double-counted activated customers —
   `lib/trialCeiling.ts` excludes `status='activated'` pending rows.
4. Malware sweep: one failing row no longer aborts the sweep; a verdict
   is 'clean' only when explicitly 'clean'.
5. `package-lock.json` regenerated.
6. `toStoreRow()` treats blank strings as missing.
7. 10-15s timeouts on Resend/Graph/sed.sh fetches (60s for PDF upload).
8. `lib/cors.ts` / `lib/supabase.ts` read env per call, not at import.
9. Cron auth uses constant-time compare (`lib/cronAuth.ts`).
10. README stale Vercel references fixed.

## v3 additions (this session, after v2)
11. **`/complete-setup` double-submit guard** — in-memory `inFlightSetups`
    Set rejects a second concurrent submission for the same
    contact_number with 409 (single-process deployment only; flagged in
    the Workers migration audit as needing a KV/Durable-Object lock if
    ever moved off a single long-running process).
12. **Supabase `ws` fix** — `@supabase/supabase-js` always constructs a
    RealtimeClient on `createClient()`, which throws on Node 20 (no
    native WebSocket). Fixed by passing `realtime: { transport: ws }`
    (the `ws` package) — this codebase never uses realtime features,
    this only unblocks client construction.
13. **`OPENROUTER_API_BASE` override** — `lib/openrouter.ts` now reads an
    optional env var to redirect requests to an alternate endpoint (e.g.
    a Cloudflare Worker acting as a reverse proxy to OpenRouter, to route
    around Syria's geo-block without a VPN). Defaults to OpenRouter's
    real URL when unset — zero behavior change unless configured.
14. **System prompt: reply conciseness** — added an explicit instruction
    to keep replies short (max 2 sentences + 1 question), especially
    important for Arabic (more tokens per idea than English), reducing
    truncation risk against the locked `REPLY_MAX_OUTPUT_TOKENS` budget.
15. **System prompt: off-topic/injection resistance** — added an
    instruction to stay on the onboarding topics and not follow
    instructions embedded in a client's message (jailbreak-style
    redirection).
16. **System prompt: BYOK default-to-no** — if the client seems unsure,
    confused, or gives a non-answer about BYOK, Ava now defaults to
    byok=false immediately with one reassuring line, instead of
    explaining what BYOK/API keys are; further questions get redirected
    to support.

## Tests
22 files / 152 tests (117 original + 32 v2 hardening tests + 3 new for
OPENROUTER_API_BASE). `tsc --noEmit` clean, `npm run build:server`
succeeds.

## Deliberately NOT changed — still needs a product decision
BYOK with no key / empty extraction still saved as-is; promo use_count
on conversation reset; contact_number not normalized; no rate limit/
input length cap; extraction token cap (1200) untested against long
Arabic replies; `/lookup-signup` still returns full history; RLS not
enabled in the SQL files; PDF upload resets `verified_ready` status.

---

# Workers migration — Stage 3 (branch `workers`)

`POST /complete-setup` + the double-submit lock, moved to Cloudflare
Workers. Result label: **passed locally** (workerd via `wrangler dev`,
mock Supabase) — NOT "verified on Cloudflare". See "Not proven yet".

## What changed
- `api/complete-setup.ts` → Hono handler. Only the lock acquisition and
  the request/response surface changed; every message, status code and
  validation rule is the same. `finishSetup()` now returns
  `{ status, body }` instead of writing to `res`.
- **`src/setupLock.ts`** (new): the `SetupLock` Durable Object, one per
  `contact_number` (`env.LOCK.idFromName(contactNumber)`). Plan's
  **Option A**: the real `finishSetup()` runs INSIDE the DO, so the lock
  is held for the true duration (Supabase lookup + update + OTP
  handoff), like the old `Set`'s add()/finally-delete() window.
- `lib/httpBody.ts` (new): reads the JSON body like `express.json()`
  did — empty body → `{}` (handler's own "Missing required field(s)"
  400), malformed JSON → 400 `{ error: "Invalid request" }` (never a 500).
- `src/index.ts`: registers the route, exports `SetupLock`, adds
  `app.onError` (JSON 500, no detail leak, CORS header still present).
  This is what replaces `asyncHandler`/`errorMiddleware` (not ported).
- `wrangler.jsonc`: `LOCK` binding + migration `v1` (`new_sqlite_classes`).
- `lib/otpHandoff.ts`, `lib/meta.ts`, `lib/notify.ts`: **unchanged**
  (already plain `fetch` + `AbortSignal.timeout`). The
  `NotifyEmailError`-is-non-fatal try/catch is intact.
- `.gitignore`: added `.wrangler/` and `.dev.vars` (the latter holds
  real secrets in real use).

## `server.ts` is frozen on this branch
It imports `api/` handlers whose signatures are now Hono's, so it is
excluded from `tsconfig.json` and left byte-for-byte untouched.
The live Termux/Render server keeps running from `main`. Stage 7
deletes it. Do not run `npm run dev` / `build:server` on this branch.

## Tests
- `completeSetup.test.ts` (10) — every assertion unchanged, only the
  transport adapted (`tests/helpers/invoke.ts`); all 10 now run through
  the real `SetupLock` class. (The stage plan didn't list this file, but
  it holds the message-wording and verbatim-`legal_name` regressions.)
- `completeSetupInflight.test.ts` — the 2 original intents kept, now
  against the real lock class, plus 2 new (burst of 5; different numbers
  don't block each other). Mutation-checked: an `await` between
  check-and-set fails 2 of the 4.
- `workerApp.test.ts` (6, new) — CORS/OPTIONS/405/400/onError through
  the real app.
- **160/160** (152 before + 8 net new). `tsc --noEmit` clean on both
  `tsconfig.json` and `tsconfig.workers.json`.

## Lock check on real workerd (`node scripts/lock-check.mjs`)
Starts a mock Supabase + `wrangler dev`, then asserts on the real route
AND on side effects (the mock counts OTP writes — exactly one winner must
mean exactly ONE OTP):
- RACE 15/15, BURST 7/7, REACQUIRE pass (after success, after a failed
  attempt, after a race), INDEPENDENT pass (two numbers at once: ~540ms
  wall vs ~490ms solo; serialized would be ~980ms). TTL: N/A.
- **Negative control** (`--negative-control`, breaks the lock on purpose
  and restores the file afterwards): RACE 1/4 passed, BURST 1/3 —
  duplicate OTPs (2 and even 5) were generated, and the test caught it.

## Not proven yet (needs a real deploy — Stage 7)
- Global uniqueness across Cloudflare regions (local workerd is a single
  instance).
- The lock was tested with a MOCK Supabase, not a real test project.
- Workers Free CPU limit per request is not enforced by `wrangler dev`.

---

# Workers migration — Stage 4 (branch `workers`)

`POST /chat` + `lib/openrouter.ts` on Workers. Result label:
**passed locally** (real workerd, mock OpenRouter + mock Supabase). The
one thing it could NOT check — a real OpenRouter completion — is a
required step for you, below.

## What changed
- **`lib/openrouter.ts`**: native `fetch()`. Removed `axios`,
  `socks-proxy-agent`, `OPENROUTER_SOCKS5_PROXY`, `getHttpClient()`, the
  SSE line-parser, `streamToString` and `stream: true` (plan option (a):
  one blocking request). Still masks phone numbers before sending and
  restores them after. 60s timeout via `AbortSignal.timeout`, 2 retries
  (2s, 4s).
- **`OPENROUTER_API_BASE` KEPT, on purpose.** Harmless escape hatch (unset
  = call OpenRouter directly); it also keeps `openrouterApiBase.test.ts`
  meaningful and lets a gateway be put in front of OpenRouter later
  without a code change.
- **Behavior change you should know about — retry on 429/5xx now really
  happens.** In the axios version it was dead code (see the plan's
  "found by tracing" note). Effect: a 429 or 5xx from OpenRouter now
  costs up to 6s of extra waiting (2s + 4s) and 2 extra upstream calls
  before `/chat` answers 502, where before it failed instantly. 4xx
  other than 429 (400/401/402/404) still fail immediately. Content
  problems (model returned non-JSON, empty completion) are not retried,
  same as before. If you'd rather keep the old fail-fast behavior, it is
  one line: make `isRetryableStatus()` return `false`.
- **`api/chat.ts`** → Hono handler; business logic untouched. Only the
  surface changed: CORS/OPTIONS now come from the middleware in
  `src/index.ts`; malformed JSON → 400 `{ error: "Invalid request" }`
  (shared `lib/httpBody.ts`); every `res.status(n).json(x)` →
  `return c.json(x, n)`. Registered as `app.all("/chat", ...)`.
- `.env.example` / `README.md`: SOCKS5 documentation corrected; the
  README's "known gap: no test covers openrouter.ts's real code" is now
  closed and says so.
- New npm scripts: `check:lock`, `check:chat`, `smoke:openrouter`.
- `server.ts`: still untouched and frozen (see Stage 3).

## Tests — 178/178 (was 160), `tsc` clean on both configs
- `openrouterApiBase.test.ts` (3) and `openrouterMasking.test.ts` (4):
  same intents and assertions, transport swapped from a mocked axios to a
  stubbed global `fetch` (`tests/helpers/fetchStub.ts`). The Masking file
  was missing from the original plan; found by grepping for every test
  that mocks `axios`.
- **`openrouterRetry.test.ts` (18, new)**: 429 once → success after one
  retry (fetch called twice); exact 2s/4s backoff timing; give-up after 3
  attempts; 400/401/402/404 not retried; network error, our own timeout
  and a connection dying mid-body all retried; bad content not retried;
  request shape (no `stream`, `json_object`, `max_tokens`, bearer key,
  abort signal); a retried request is byte-identical; the API key never
  appears in an error.
- Mutation-checked: re-creating the old dead-code behavior fails 4 of
  them; removing the network-error retry fails 4.
- `chat.test.ts` (17) and `hardeningChat.test.ts` (6): every assertion
  unchanged, only the transport adapter (`tests/helpers/invoke.ts`).
- `npm ls axios socks-proxy-agent` → empty; grep for either import → none.

## End-to-end on real workerd: `npm run check:chat` — 19/19
Mock OpenRouter + mock Supabase behind the real Worker, asserting on the
wire from the mock's side: exactly one upstream call per turn, bearer key
present, **no `stream` flag**, **the raw phone number never sent
upstream** and restored in the reply, 429 retried through the real stack
(2.1s), 400 not retried (78ms), persistent 500 → 3 attempts then a clean
502 with no upstream detail (6.2s), full completion writing
`chat_complete` and NO `otp_code`, duplicate-signup block with zero
upstream calls, plus CORS/405/400/OPTIONS. Negative control (PII masking
deliberately removed) → 2 checks fail, so the check can see the bug.

## YOUR step before calling Stage 4 done: a real OpenRouter request
From a network that can reach OpenRouter (GitHub Codespaces; not Syria
without a VPN), using a test key with a small credit limit:

    OPENROUTER_API_KEY=sk-or-... npm run smoke:openrouter -- --full

It starts `wrangler dev`, sends real turns through `POST /chat`, prints
HTTP status and milliseconds, and (`--full`) runs the longest call Ava
makes (reply + 1200-token extraction) against a throwaway in-memory
Supabase. If the numbers look comfortable, plan option (a) stands. If a
long non-streamed request is dropped or times out, the fallback is
option (b) in the Stage 4 plan (keep streaming via the Web Streams
reader). The same script takes `--url` to re-check a deployed Worker in
Stage 7.

## Not proven yet
- A real OpenRouter completion (above), and its latency from Cloudflare's
  edge (needs the Stage 7 deploy; local runs measure YOUR network).
- Workers Free CPU limit: Cloudflare's pricing page lists **10 ms CPU per
  invocation** on Free (some third-party pages say 30 ms; check your
  dashboard). `wrangler dev` does not enforce it. Indicative only: the
  turn logic (sanitize + mask + build + parse, 40-message history) costs
  ~0.2 ms CPU in Node; waiting on OpenRouter/Supabase is not CPU time.
  Free also caps subrequests at 50 per invocation; the longest `/chat`
  path makes about a dozen.
- Cross-region Durable Object behavior (Stage 3) is still deploy-only.

# Workers migration — Stages 5, 6 and 7 (branch `workers`)

Result label for everything below: **"passed locally"**. Nothing here has run on
real Cloudflare yet — see "Not proven yet".

## Stage 5 — `POST /upload-pdf`
- `api/upload-pdf.ts` is a Hono handler; `busboy` + `lib/multipart.ts` are gone,
  replaced by `Request.formData()`.
- The one real behavior difference: busboy cut an oversized upload off *while
  streaming*; `formData()` buffers the whole body, and a Worker request body can
  be up to 100 MB against a 128 MB isolate. So the cap is enforced *during* the
  stream (a `Content-Length` pre-check, plus a byte-counting `TransformStream`
  for chunked / missing / lying lengths), then the exact `file.size` check, then
  `validatePdfFile()`'s friendlier 422 — same three outcomes as before.
- `MAX_PDF_SIZE_BYTES` that isn't a positive number now falls back to the 10 MB
  default (`parseInt("abc")` was NaN, and every `x > NaN` is false, so a typo
  silently disabled the cap).
- `lib/pdfValidation.ts`: `isPdfMagicBytes` compares bytes in a loop instead of
  `Buffer.prototype.equals`, which loses its type declaration when Cloudflare's
  and Node's types are loaded together (`tsconfig.workers.json`). Same behavior.
- The `/upload-pdf` handler had no test before; `tests/uploadPdf.test.ts` covers
  the whole decision tree (23 tests).

## Stage 6 — cron, `GET /lookup-signup`
- `scheduled()` (`src/cron.ts`) handles two Cron Triggers: `*/15 * * * *` →
  activation, `*/20 * * * *` → PDF-scan sweep (UTC; `wrangler.jsonc`
  `triggers.crons`, a test fails if the two files drift; an unrecognized
  expression throws instead of silently doing nothing). Each job logs a
  `[cron] <job> {summary}` line. The default export is now
  `{ fetch, scheduled }`; `app` is also a named export.
- The manual `GET /cron/activate-pending` and `/cron/sweep-pdf-scans` stay behind
  `CRON_SECRET` and call the same `runActivatePending()` / `runSweepPdfScans()`.
- `isCronAuthorizedRequest()` for Web `Request`s: repeated `?secret=a&secret=b`
  is rejected (Express gave an array; Hono's `c.req.query()` would return the
  first value). `lookup-signup` rejects a repeated `contact_number` the same way.
- Free plan: 5 Cron Triggers per **account**; this Worker uses 2.
- `lookup-signup`'s privacy property — "no row" and "OTP already sent" return
  byte-identical responses — is now a test.
- `src/workersContract.ts` (compile-time only) asserts the default export is a
  valid `ExportedHandler<Env>` under `tsconfig.workers.json`.

## Stage 7 — integration
- **Retired for good:** `server.ts`, `lib/asyncHandler.ts`, `lib/cors.ts`,
  `tsconfig.build.json`, the `/selftest` route, and the dependencies `express`,
  `dotenv`, `@types/express`, `tsx`. `lib/cronAuth.ts` keeps only the Web-`Request`
  check. Stage 7's optional step 0 (`crypto.subtle.timingSafeEqual`) was
  deliberately **not** done: no gain, and the existing Node `timingSafeEqual` is
  exercised under real workerd by `check:cron`.
- **A behavior the port had silently dropped, now restored:** `server.ts`'s
  `express.json({ limit: "4.5mb" })`. `lib/httpBody.ts` now caps JSON bodies at
  4.5 MB (declared `Content-Length`, then counted while streaming) → `413
  { error: "Request body too large" }` on `/chat` and `/complete-setup`, with the
  CORS header, as before. Without it a 100 MB body would have been buffered whole.
- **Config:** `wrangler.jsonc` `vars` holds only the four non-personal values that
  equal the code defaults. Everything else (credentials *and* `ALLOWED_ORIGIN`,
  `SUPABASE_URL`, `RAHEEM_NOTIFY_EMAIL`, `RAHEEM_WHATSAPP_NUMBER`,
  `RESEND_FROM_EMAIL`) is a secret uploaded from a gitignored `secrets.json`
  (template: `secrets.example.json`) — a deliberate deviation from the plan's
  "plain vars" list, so Raheem's personal contact details never enter git history
  and no placeholder value can be deployed. `npm run check:secrets` validates the
  file before upload. `PORT` and `OPENROUTER_SOCKS5_PROXY` are dead and not migrated.
- **Deploy:** `npx wrangler deploy --secrets-file secrets.json` (verified with
  `--dry-run` on wrangler 4.145): the secrets go up with the deployment.
  `scripts/smoke-deployed.mjs` is a smoke test for the real URL — read-only by
  default; `--with-chat` / `--run-cron` are opt-in because they spend OpenRouter
  credit / run the real jobs. `scripts/smoke-selfcheck.mjs` proves that script works.
- `.env.example` → `.dev.vars.example`; README rewritten for Workers (no more
  Vercel / Render / cron-job.org / Express); this file's older entries are history.

## Tests — 247/247 (was 178), 27 files (was 24), `tsc` clean on both configs
Accounting against the stage-4 zip, file by file:
- **Removed:** `asyncHandler.test.ts` (7). Its three `asyncHandler` tests and the
  `headersSent` test are Express-only. "Unknown error → 500 without leaking
  details" is covered by `workerApp.test.ts`; "4xx keeps its status" by the new
  413/400 tests in `httpBody.test.ts`; "`applyCors` reads `ALLOWED_ORIGIN` per
  request" is now a `workerApp.test.ts` test (6 → 7).
- **Ported, same four cases:** `cronAuth.test.ts` (Express `Request` → Web `Request`).
- **New:** `uploadPdf` 23, `cron` 26, `lookupSignup` 12, `httpBody` 14.
- **Every other file unchanged** (identical counts).
- Each new safeguard has a negative control: removing it makes a test fail
  (size-cap pre-check, streaming counter, NaN fail-open, lookup privacy,
  duplicate-secret rejection, activate-before-insert order, cron drift, silent
  unknown cron, 413 vs 400, `scheduled()` signature under `tsc`).

## End-to-end on real local workerd
`npm run check:lock` ALL CHECKS PASSED · `check:chat` 19/19 · `check:upload` 21/21
· `check:cron` 26/26 · `node scripts/smoke-selfcheck.mjs` 9/9. (`mock-supabase.mjs`
gained generic filters, a storage API and a `stores` table — additively; the
Stage 3 and 4 checks pass unchanged against it.)

## Not proven yet (only a real deploy shows these)
- That Cloudflare's scheduler fires both Cron Triggers on time.
- CPU time on the Free plan (10 ms): `/upload-pdf` does the most work; the PDF
  parse alone measured 1–4 ms in Node, which is not `workerd` and not the whole
  request. Watch for error 1102.
- That OpenRouter accepts requests from Cloudflare's network (`--with-chat`).
- The `SetupLock` Durable Object on real Cloudflare (`concurrency-test.mjs` needs
  the mock Supabase's OTP-write counter, so it can't be pointed at a real URL, and
  `/complete-setup` writes real rows and calls Meta).
- The sed.sh verdict path end to end (its base URL is hard-coded; unit-tested only).
- Subrequests: 50 per invocation on Free, so roughly 16–24 rows per cron run.
