# Ava — Onboarding Backend

A Cloudflare Worker (Hono) implementing Ava's `POST /chat` contract, a
separate post-chat `POST /complete-setup` form submission, `GET /lookup-signup`,
`POST /upload-pdf`, a manual verification handoff, and two Cloudflare Cron
Triggers (activation + PDF-scan sweep). One entry point: `src/index.ts`.
(The earlier Vercel → Render → Termux/Express builds live on the `main`
branch; `server.ts` and everything Express-specific is gone from this one.)

## Two-step signup: chat, then a form

As of this pass, Ava's conversation and Meta/legal setup are two
**separate** steps, not one:

1. **`POST /chat`** — Ava talks to the client, collects business
   understanding + BYOK preference + an optional referral code, and stops.
   No phone_number_id, no access_token, no legal_name — Ava's conversational
   scope no longer includes any of that. The row lands at
   `status: 'chat_complete'`.
2. **`POST /complete-setup`** — a separate form on the site (built and owned
   by the site team, not this backend) collects `legal_name`,
   `phone_number_id`, and `access_token`, submits them here, and **that's**
   what triggers OTP generation and the Raheem notification email now — not
   the end of the chat. See "Meta setup" below.

## How a `/chat` turn works

1. Frontend sends `{ conversation_history, user_message, business_context?,
   referral_code? }`. `business_context` is optional — see "contact_number
   tracking" below. `referral_code` is also optional and top-level (not
   nested in `business_context`) — see "Referral codes" below for why it's
   a separate field rather than something the AI extracts.
2. **Token budget check** (see "Token budgets" below) — if this attempt's
   estimated cumulative output is already at or past budget, the request
   ends here with a support-contact message, no model call at all.
3. `api/chat.ts` calls OpenRouter (platform key) with `buildConversationSystemPrompt`
   — Ava's persona and collection rules, plus whatever `business_context` is
   known — capped at `REPLY_MAX_OUTPUT_TOKENS` output tokens, and gets back
   `{ reply, done }`.
4. If `done: false`, that's returned to the frontend as-is (a
   `contact_number`-tracked row's `conversation_history` still gets updated
   first — see below — but no other DB write happens).
5. If the conversation model signals `done: true`, in the **same request**:
   - **Another token budget check**, now accounting for the reply that was
     just generated — same immediate-cutoff behavior if it would push past
     budget before the extraction call.
   - A second OpenRouter call (`buildExtractionSystemPrompt`), capped at
     `EXTRACTION_MAX_OUTPUT_TOKENS` output tokens, reads the full transcript
     and returns structured fields (including a *fallback* `referral_code`,
     only used if the request's own top-level `referral_code` field wasn't
     sent — see "Referral codes" below) + a generated `system_prompt` for
     the client's future WhatsApp assistant. **No phone_number_id/access_token**
     in this schema anymore — see "Meta setup" below.
   - Whichever `referral_code` applies (top-level field preferred) gets
     validated against `promo_codes` and the global trial ceiling (see
     "Referral codes" below) before building the row.
   - **`buildStoreRow()`** (in `lib/storeRow.ts`) computes the `package` /
     `is_api_free` / `api_key` / `ai_model` / `referral_code` / `is_trial` /
     `discount_expires_at` fields — it's still the only function that
     decides those, to prevent the polarity bug called out in the spec's
     "known bug history." It does **not** compute `phone_number_id`/
     `access_token`/`legal_name` at all anymore.
   - If a `contact_number`-tracked draft row already exists for this
     session, its core fields are **updated** onto that same row (plus
     `status: 'chat_complete'`) — no second row. Otherwise, `buildPendingSignupRow()`
     inserts a fresh row (the original, pre-`contact_number` path — still
     the fallback when no `business_context.contact_number` was ever given;
     see the "Known limitation" callout below for why this path is now a
     dead end).
   - The response tells the client to continue to the next step on the
     site itself — **no OTP code, no otp_code generated at all here**. That
     now only happens once `POST /complete-setup` succeeds.

## Meta setup (`POST /complete-setup`)

`phone_number_id`, `access_token`, and a new `legal_name` field all moved
**out** of Ava's conversation entirely, into a dedicated post-chat form
submission — `POST /complete-setup`, body:
`{ contact_number, legal_name, phone_number_id, access_token }`. This was a
deliberate scope cut: Ava's conversational job is now just business
understanding + BYOK preference + optional referral code (see
`lib/systemPrompt.ts`) — nothing about connecting a WhatsApp number.

`api/complete-setup.ts` looks the row up by `contact_number` (via the same
`findPendingSignupByContactNumber()` used elsewhere) and requires it to
already exist **and** be at `status: 'chat_complete'`:

- **No row at all** for this `contact_number` → `404`, clear error ("complete
  the onboarding conversation first").
- **Row exists but still `'draft'`** (chat unfinished) → `409`, "conversation
  isn't finished yet."
- **Row exists but already past `'chat_complete'`** (`'otp_sent'` or later —
  i.e. this form was already submitted once) → `409`, "already completed."
  This specific case wasn't spelled out explicitly in the original ask
  beyond "must already exist and be past the chat-completion point" —
  treating a *second* submission as equally invalid is a judgment call, not
  a literal instruction, made because silently re-triggering would generate
  a second `otp_code` and a duplicate notification email, which is a real
  bug, not just a rough edge.
- **Row exists at exactly `'chat_complete'`** → the happy path: `legal_name`/
  `phone_number_id`/`access_token` are written with a plain
  `updatePendingSignup()` (no branching logic — `legal_name` in particular
  is stored **exactly as given**, no validation beyond non-empty, no
  format/legal-registry checking of any kind), then `lib/otpHandoff.ts`'s
  `triggerOtpHandoff()` runs: generates the plaintext 12-digit code
  (`lib/otpCode.ts`), writes `otp_code`/`otp_sent_at`/`status: 'otp_sent'`,
  and fires the same best-effort Raheem notification email
  (`lib/notify.ts`) that used to fire at the end of the chat. The response
  is the same OTP+wait-time instructional message as before (bilingual,
  worded identically regardless of PDF-upload status — see below), just
  returned from this endpoint now instead of `/chat`.

### Known limitation: a `contact_number`-less signup is now an orphan

`POST /complete-setup` can **only** find a row by `contact_number` — there's
no other lookup path. A chat that completes without `business_context.contact_number`
ever being sent (the original, pre-`contact_number` fallback path in
`api/chat.ts`, kept for structural backward-compatibility) lands at
`status: 'chat_complete'` with no `contact_number` at all — meaning nothing
can ever submit `/complete-setup` for it. That row is permanently stuck.
This wasn't asked to be fixed by making `contact_number` mandatory (a
product/UX decision, not made unilaterally here) — flagging it plainly
instead of silently working around it. If this matters, the fix is either
making the site always send `contact_number` before `/chat` can reach
`done: true`, or giving `/complete-setup` a second lookup path.

## `contact_number` tracking, resume, and duplicate registration

`business_context` (optional on every `/chat` call, see `lib/types.ts`'s
`BusinessContext`) can include `project_name`, `contact_number`, `domain`,
`business_nature`, `country`. Only `contact_number` drives backend
behavior beyond the system prompt — it's a plain WhatsApp number the
website already collected, **fully separate from `phone_number_id`**
(Meta's technical Cloud API ID, which Ava still collects later,
in-conversation, unchanged). The system prompt is told about
`contact_number` too but is explicitly warned not to confuse the two —
see `buildKnownFactsBlock()` in `lib/systemPrompt.ts`.

**Statelessness caveat:** this API has no session memory of its own — the
frontend carries all state via `conversation_history`. For `contact_number`
tracking to actually work across an entire conversation (not just the
first message), the frontend needs to keep including
`business_context.contact_number` on every `/chat` call for that session,
not only the first one. The other `business_context` fields only need to
be sent once (or whenever they change) since they only ever feed the
system prompt.

Whenever `contact_number` is present on a request, `api/chat.ts` calls
`findPendingSignupByContactNumber()` (`lib/supabase.ts`) and branches three
ways:

1. **No existing row** — inserts a fresh `status: 'draft'` row
   (`buildDraftSignupRow()`, `lib/storeRow.ts`) with just `contact_number` +
   `business_context` + `conversation_history`. None of the core
   `stores`-shaped fields exist yet — that's the point of a draft (see
   `PendingSignupRow`'s doc comment in `lib/types.ts` for why it's typed as
   `Partial<StoreRow>` rather than `StoreRow`).
2. **Existing row, `otp_code` not yet set** — a free overwrite: reused and
   updated in place (merging `business_context` so fields from an earlier
   turn survive a later turn that only resends `contact_number`), never
   duplicated.
3. **Existing row, `otp_code` already set** — **blocked.** The request
   returns immediately with `{ reply: <bilingual support-contact message>,
   done: true, blocked: true }` — no LLM call, no state change. The support
   contact is `RAHEEM_WHATSAPP_NUMBER` (reused, not a separate env var —
   see `.env.example`'s comment on it).

Every turn that includes `contact_number` keeps that row's
`conversation_history` current (updated once right after the conversation
model's reply comes back, in the same shape `ChatRequest.conversation_history`
already expects — prior history + this turn's user message + this turn's
assistant reply), so a resumed session picks up exactly where the client
left off, not one turn behind.

### `GET /lookup-signup?contact_number=<number>`

Lets the website resume an abandoned conversation. Calls the same
`findPendingSignupByContactNumber()` lookup; returns
`{ found: true, conversation_history, business_context }` only if a
resumable (pre-OTP) draft exists, or `{ found: false }` for **everything
else** — including "never existed" and "exists but OTP already sent",
deliberately indistinguishable. That's a privacy choice: the active
duplicate-registration block above (case 3) is supposed to reveal "a
signup already exists" to someone actively trying to register that number
again, but this passive lookup endpoint has no such context — anyone who
can guess/enumerate a phone number could otherwise use it to probe which
numbers have completed signups. See the handler's own comment for more.

**Security note, stated plainly rather than left implicit:** `contact_number`
is a phone number, not a secret. This endpoint (and the
`findPendingSignupByContactNumber()` lookup generally) is only as safe as
"knowing someone's WhatsApp number" is as access control — there is no
additional auth on it in this pass. A resumable draft's
`conversation_history` can contain whatever the client already typed,
which may include their Meta `access_token` or OpenRouter `api_key` if
they got that far before abandoning. If that's not acceptable, the fix
(rate limiting, a short-lived resume token instead of relying on the raw
number, etc.) needs to be decided and added explicitly — this wasn't
specified, so nothing was invented for it here.

### `stores.contact_number` — not in this repo

Item 1 of this feature ("Add `contact_number` column — pending_signups +
stores") only got done half in this codebase: `pending_signups_table.sql`
has it (with a migration), and `StoreRow`/`buildStoreRow()`/`toStoreRow()`
all carry it through so the activation cron writes it into `stores`
correctly. But `stores_table.sql` and the core engine itself live in a
**separate repo not present here** — the equivalent `contact_number`
column needs to be added there by hand; it wasn't (and couldn't be)
touched as part of this change. **`legal_name` has the exact same
caveat** — `pending_signups_table.sql` has it, `StoreRow`/`toStoreRow()`
carry it through, but `stores_table.sql` needs the matching column added
by hand too, for the same reason.

## Phase 2: staging table (`pending_signups`)

Ava writes to `pending_signups` (schema in `pending_signups_table.sql`), and
`GET /cron/activate-pending` — an external scheduler hits this periodically —
promotes eligible rows into `stores`.

Activation calls `toStoreRow()` (in `lib/storeRow.ts`) to get the
`stores`-shaped subset of a `pending_signups` row. `toStoreRow()`
deliberately does **not** re-derive `package`/`is_api_free`/`api_key`/`ai_model`
from raw data — it only forwards what was already computed at finalization
time via `buildStoreRow()`. One computation, many consumers
(`insertPendingSignupRow`/`updatePendingSignup` in `api/chat.ts`,
activation's `insertStoreRow` later) — see `tests/storeRow.test.ts` for a
test that locks this equivalence in. Since a `pending_signups` row can now
legitimately exist as a bare draft (`contact_number`-tracking, see above)
with none of these fields set yet, `toStoreRow()` also runtime-checks that
every required field is present and throws a clear error if not, rather
than silently writing `null`s into `stores` — this should only ever trip on
a malformed row, since a row can't reach `'verified_ready'` without having
gone through finalization first.

### `is_free` → `package` → referral-code-gated `package`

Both `pending_signups` and `stores` dropped the old `is_free` boolean in
favor of `package text not null check (package in ('basic', 'pro',
'low-tier'))`, mirroring the same change already made on the core engine
side. `is_api_free`/`api_key`/`ai_model` are unrelated and unchanged.

**This has changed again since the first pass**, which had every Ava
signup always land on `'basic'` (a real free-trial month, replacing the
old permanent low-tier branded bot) with a compile-time guarantee via
`StoreRow.package` being typed as the literal `"basic"`. That guarantee is
gone: `package` now defaults to **`'low-tier'`**, and only becomes
`'basic'` when the client gives a `referral_code` that validates against
`promo_codes` — see the "Referral codes" section right below. `StoreRow.package`
is typed as the `Package` union (`"basic" | "pro" | "low-tier"`) again, not
a fixed literal, since the branch is now genuine business logic rather
than something to forbid outright. `pending_signups_table.sql` includes
migrations for both the `is_free` → `package` change and the later
`'basic'` → `'low-tier'` default change — neither backfills *existing*
rows' values, only what new rows default to; see that file's migration
block for the reasoning at each step.

`status` is now `draft` -> `chat_complete` -> `otp_sent` -> `verified_ready`
-> `activated` (`chat_complete` is the newest addition — see "Meta setup"
above for what it means and why it's there).
`phone_number_id` is intentionally **not** unique on `pending_signups`
(unlike `stores`) — a client can abandon and restart Ava's conversation
before ever being verified, producing more than one staging row for the
same number. `stores.phone_number_id`'s unique constraint is the real
guard against duplicate activation. `contact_number` is likewise not
DB-unique — the actual one-active-draft-per-number rule is enforced in
application code (`findPendingSignupByContactNumber()`, `api/chat.ts`'s
three-way branch), not a database constraint.

## Referral codes

**Primary path: a top-level `referral_code` field on the `/chat` request
body**, sent by the frontend directly alongside `conversation_history`/
`user_message`/`business_context` — a plain lookup, zero extra LLM token
cost. This was a bug fix: an earlier pass only picked up `referral_code`
via the AI extraction step, which contradicted the original design (codes
were meant to bypass the model entirely). `api/chat.ts` now prefers this
top-level field; the AI-extracted `ExtractedOnboardingData.referral_code`
(Ava asks once, near the end of the conversation, and accepts "no"
immediately — `lib/systemPrompt.ts`) is kept only as a **secondary
fallback** for a frontend that hasn't wired the top-level field yet, not
the primary mechanism.

### Two-tier design (confirmed)

At finalization (`api/chat.ts`, same place the OTP code gets generated), a
given `referral_code` (top-level, or the extraction fallback) is checked
against `promo_codes` (`promo_codes_table.sql`, `lib/promoCodes.ts`,
`validateAndApplyReferralCode()`) — **and** the global trial ceiling
(`lib/trialCeiling.ts`, see below) must not already be hit.

**A code is invalid only if it doesn't exist or is expired.**
`free_trial_max_uses` (renamed from `max_uses`) is *not* an overall cap
that invalidates a code once exceeded — a code never stops working. It
only decides which of two tiers a valid redemption lands on:

- **Tier 1** (`use_count < free_trial_max_uses`) — full free trial:
  `package = 'basic'`, `is_trial = true`, no `discount_expires_at` (there's
  nothing stacked on top of free).
- **Tier 2** (`use_count >= free_trial_max_uses`) — paying from day one,
  but still `package = 'basic'`: `is_trial = false`,
  `discount_expires_at = now() + 3 months`.

Either way, once valid, `referral_code` is stored permanently on the
signup row and the code's `use_count` is incremented — **both tiers count
toward it**, since `free_trial_max_uses` is a running "how many got the
full trial" counter now, not a total-uses-before-the-code-dies cap.
`free_trial_max_uses = null` means unlimited tier-1 redemptions (same
"null = unlimited" convention the old `max_uses` column had, just applied
to trial slots instead of total uses).

- **Invalid (doesn't exist, expired), simply not given, or the global
  ceiling is already hit** — `package` stays at its `'low-tier'` default;
  nothing about the attempt is recorded (a rejected code doesn't get
  written anywhere, only successful applications do — either tier). When
  the ceiling is what blocked it specifically, the code's own `use_count`
  is **not** incremented either — see below for why.
- **A Supabase error while validating** — fails safe to invalid rather
  than blocking the whole signup over an optional field; logged, not
  surfaced to the client.

`discount_expires_at` isn't read or enforced by anything in this codebase
yet — it's recorded for whatever downstream logic eventually consumes it
(billing, a renewal reminder, etc.).

**Known race condition, accepted rather than engineered around:** the tier
check and the `use_count` increment are two separate round-trips, not one
atomic operation — two requests redeeming the same code at the exact same
instant, right at the tier boundary, could both read the same `use_count`
and both land on tier 1 even though only one of them "should" have,
strictly by increment order. Given this is a low-stakes onboarding
discount (not payment-critical) and the boundary is soft — being off by
one redemption doesn't expose the system the way exceeding a hard cap
would — that's an accepted trade-off for this pass — see
`lib/promoCodes.ts`'s header comment for the atomic read-tier-and-increment
alternative if it ever needs closing.

### Global trial ceiling (`lib/trialCeiling.ts`)

A blunt, deliberately simple safety net **on top of** each code's own
`free_trial_max_uses`, not a replacement for it — protects against total
exposure if more referral codes get added later than originally planned.
Before granting a **tier-1** redemption, `api/chat.ts` also checks
`isTrialCeilingReached()`: it counts rows where `package = 'basic' AND
is_trial = true` across **both** `pending_signups` (this repo) and
`stores` (the core engine's table — a direct count query against it is
still legitimate here, since it's the same Supabase database even though
this repo doesn't own that table's schema). **Tier-2 rows deliberately
don't count** against this ceiling — they're not consuming free-trial
capacity, they're paying from day one. If the tier-1 count is already at
or above `TRIAL_GLOBAL_CEILING = 80`, the referral code is treated as
invalid for *this* signup regardless of its own remaining
`free_trial_max_uses`.

This check runs **before** the code's own `validateAndApplyReferralCode()`
call, on purpose — if the system-wide cap is already hit, there's no
reason to also consume one of that code's limited trial slots for a signup
that won't get the free trial anyway.

`is_trial` now genuinely exists on both tables (`pending_signups` via the
migration in `pending_signups_table.sql`; `stores` via the core engine's
own migration). `countBasicTrials()` still has a defensive fallback for if
a query against `is_trial` fails with an "undefined column" error (deploy-order
skew between this repo and the core engine, or an environment that hasn't
picked up the migration yet) — it falls back to counting every
`package = 'basic'` row directly. That fallback is deliberately
over-inclusive now that tiers exist (it would count tier-2 rows as if they
were trials too), but over-counting is the safe failure mode for a
protective ceiling — it can only make the cap trigger more readily, never
less — so it's an acceptable approximation for a path that's only meant to
be hit during migration skew, not normal operation.

Every time the ceiling actually trips, it's logged via `console.log` (see
`lib/trialCeiling.ts`) so Raheem notices it happening in the Workers logs (`npx wrangler tail`, or the dashboard's Observability tab)
rather than it silently capping signups with no visible trace.

### `stores` and `promo_codes` — what's mirrored, what isn't

`referral_code` and `discount_expires_at` have the same caveat as
`contact_number`: threaded through `StoreRow`/`buildStoreRow()`/`toStoreRow()`
so the activation cron carries them into `stores` correctly, but the
actual `stores_table.sql` column additions live in the **core engine
repo, not present here**, and need to be added there by hand if they
haven't been already. `is_trial` is the one exception — the core engine
already added `stores.is_trial` itself (its own migration, to split
`package='basic'` into two different monthly conversation caps), so
that column genuinely exists on both tables already; this repo's
`buildStoreRow()` just needed to start setting it correctly per tier.
`promo_codes` itself is a **new table** (`promo_codes_table.sql`) with no
core-engine counterpart to mirror — it's managed by hand (or whatever
internal tooling gets built later); nothing in this codebase creates or
edits rows in it, only reads and increments `use_count`.

## `branding_opt_in` — schema only

`pending_signups` also gained a `branding_opt_in boolean not null default
false` column. **Schema only, deliberately** — nothing in this codebase
reads, writes, or threads it through `buildStoreRow()`/`toStoreRow()`/
activation. It exists as a placeholder for a future branding feature, not
something to infer behavior from yet. Added only to `pending_signups`, not
`stores` — unlike `contact_number`/`referral_code`, this wasn't specified
as needing to mirror there, so nothing was assumed.

## Token budgets

Enforced in `lib/tokenBudget.ts` and applied in `api/chat.ts`, at both
model call sites:

- **Reply call** (the conversational turn) — output capped at
  `REPLY_MAX_OUTPUT_TOKENS` (200, within the specified 150-200 range),
  passed straight through to OpenRouter's own `max_tokens` parameter
  (`lib/openrouter.ts`'s `maxOutputTokens`). Exact, hard enforcement.
- **Extraction call** — output capped at `EXTRACTION_MAX_OUTPUT_TOKENS`
  (1200, within the specified 1000-1200 range), same mechanism.
- **Cumulative budget per attempt** — `CUMULATIVE_TOKEN_BUDGET_PER_ATTEMPT`
  (5000), checked before *every* model call (reply and extraction alike).
  If continuing would push estimated cumulative output past 5000, the
  request ends immediately with `{ done: true, budgetExceeded: true,
  reply: <bilingual support-contact message> }` — no model call, same
  support-contact pattern as the duplicate-registration block.
- **No input cap**, anywhere. Truncating what the model can *read* risks
  losing onboarding data mid-conversation; capping what it can *generate*
  doesn't have that problem.

**This is estimation, not exact accounting** — flagging that plainly
rather than implying more precision than it has. This API is fully
stateless (see the file header of `api/chat.ts`); the only state available
to reconstruct "how much has this attempt already spent" from, across
possibly many separate HTTP requests, is whatever `conversation_history`
the frontend resends each turn. `estimateTokens()` is a simple chars/4
heuristic (a common rule-of-thumb when a real tokenizer isn't available),
summed across every past assistant-role message (`estimateAssistantTokensSoFar()`)
— user-message content is deliberately excluded from this sum, consistent
with "no input cap." This is a soft guard against a conversation running
unexpectedly long, not a precise cost ledger.

## Final instructional message wording (OTP + wait time)

`buildFinalInstructionalMessage()` — the message telling the client to text
their code to Raheem and wait — now lives in `api/complete-setup.ts`
(moved there from `api/chat.ts`, since that's where OTP generation happens
now — see "Meta setup" above). It takes **no PDF-related parameter at
all**. That's deliberate, not incidental: uploading a PDF is a completely
separate endpoint/flow (`api/upload-pdf.ts`), and this function's
signature makes it structurally impossible for the message to vary by
`pdf_uploaded` status, rather than just "currently doesn't." See
`tests/completeSetup.test.ts`'s dedicated regression test, which runs the
success path twice against rows differing only in `pdf_uploaded` and
asserts the resulting message (with the random OTP code normalized out) is
byte-identical both times.

## Manual OTP verification (this pass replaces full automation)

Earlier passes tried a fully automated flow: hash the code, self-send it to
the client's own WhatsApp number using their freshly-collected credentials,
verify a reply against the hash, all inside `/chat`. That's a dead end —
Meta's Cloud API generally requires an approved message template (not
free-form text) for the first message in a conversation window, and a
self-send has no prior customer-service window open, so the send would
likely get rejected on first contact. **Not revisited.**

What ships now instead, inside `POST /complete-setup` (see "Meta setup"
above — this moved out of `/chat` entirely; `lib/otpHandoff.ts` is the
shared logic both this section and that one refer to):

1. `api/complete-setup.ts` writes `legal_name`/`phone_number_id`/
   `access_token` onto the already-`chat_complete` `pending_signups` row.
2. `lib/otpHandoff.ts`'s `triggerOtpHandoff()` generates a plaintext
   12-digit code (`lib/otpCode.ts`) and writes it onto that row
   (`otp_code`, `otp_sent_at`, `status: 'otp_sent'`) — no hashing, no
   expiry, no attempt counter, because there's no automated check left to
   protect.
3. The response tells the client to send that exact code as a normal
   WhatsApp text, from their own phone, to **`RAHEEM_WHATSAPP_NUMBER`** —
   and that the bot will be activated within 24 hours.
4. Fires a best-effort notification email to `RAHEEM_NOTIFY_EMAIL` via Resend
   (`lib/notify.ts`) containing `store_name`, `phone_number_id`, the code,
   and — best-effort via a read-only Meta Graph API lookup
   (`lib/meta.ts`, `resolveDisplayPhoneNumber()`) — the client's own
   WhatsApp display number, so Raheem knows what to expect the incoming text
   to look like without having to open Supabase. A failed email is logged
   and swallowed; it never turns into a 500 for the client, since it isn't
   part of the verification mechanism itself, just a convenience nudge.

Raheem then, entirely outside this codebase:

- Watches for that WhatsApp text (or the notification email) and matches the
  code by eye.
- If `pdf_uploaded` is `true` on that row, hand-edits `system_prompt` first.
- Sets `status = 'verified_ready'` directly in the Supabase table editor —
  no endpoint for this, on purpose.

There is no Ref-tag mechanism, no mid-verification chat turn, and no
resend/expiry/lockout logic anywhere in this codebase — `api/chat.ts` has
exactly one route, and `api/complete-setup.ts` has exactly one (the
`chat_complete`-only happy path described in "Meta setup" above).

### Read-only Meta lookup — why it's not the dead end above

`lib/meta.ts`'s `resolveDisplayPhoneNumber()` does a single `GET` on
`/{phone_number_id}?fields=display_phone_number` using the client's own
`access_token`. It sends nothing and isn't subject to the message-template
requirement that killed the self-send approach — it exists purely to make
Raheem's notification email more useful. It's best-effort: on any failure it
returns `null`, the email still sends with a note to match on
`phone_number_id`/code instead, and nothing about the client's own response
depends on it succeeding.

## Activation cron (`GET /cron/activate-pending`)

For every `pending_signups` row where `status = 'verified_ready'`: inserts
the `stores`-shaped subset via `toStoreRow()`, then marks that row
`activated`. Protected by a shared secret — set `CRON_SECRET` in the
environment; the endpoint returns `401` if it's unset (fails closed, never
runs open) or if the caller doesn't supply the same value via
`Authorization: Bearer <secret>` or `?secret=<secret>`.

**How it runs:** a Cloudflare Cron Trigger — `*/15 * * * *` (UTC), declared in
`wrangler.jsonc` and handled by `scheduled()` in `src/cron.ts`. Cloudflare's own
edge calls the Worker directly (no HTTP request, so no secret involved). The
two cron expressions in `wrangler.jsonc` and `src/cron.ts` must match exactly
— a test fails if they drift, and an unrecognized expression throws (a visible
failed invocation) rather than silently doing nothing. Each run logs a
`[cron] activate-pending {...}` summary line (`wrangler tail`).

**Manual run / ops fallback:** `GET /cron/activate-pending` with `CRON_SECRET`
still works — handy for "run it right now". It calls the same
`runActivatePending()` the schedule does. Example:
`curl -H "Authorization: Bearer $CRON_SECRET" https://<worker>.workers.dev/cron/activate-pending`

Free plan note: Cloudflare allows 5 Cron Triggers per **account** (not per
Worker); this Worker uses 2.

## PDF upload + malware scan (sed.sh)

`POST /upload-pdf` (multipart/form-data: `phone_number_id` field + `file`
field). Returns **202** immediately — the malware-scan verdict is resolved
later by a separate cron, never inside this request. Steps:

⚠️ **Temporary: scanning is OFF while sed.sh isn't funded.** When
`SED_SH_API_KEY` is unset, step 3 below is skipped entirely — the file
is stored as normal, and the row goes straight to `status: 'otp_sent'`,
`pdf_scan_status: 'skipped'` (no `scan_pending` detour, no `scanId` to
poll). This is a deliberate switch, not a misconfiguration — the old
hard 500-on-missing-key is gone. Same eligibility check, same
PDF/size/page validation, same 202 response shape either way (just
`status: 'received'` instead of `'scanning'`). Re-adding the key
resumes real scanning immediately, no other change needed. The
`boltane-admin-bot` repo's `admin-alerts` cron is what makes this
tolerable in the meantime — it relays every uploaded PDF straight to
Telegram so you still see the file yourself before approving a signup.

1. **Eligibility** (`findEligiblePendingSignupForPdf()`, `lib/supabase.ts`)
   — finds the client's own most recent staging row
   (`status IN ('otp_sent', 'verified_ready')`). With OTP fully manual,
   there's no automated "verified" checkpoint to gate on — `phone_number_id`
   alone isn't a secret, and this endpoint doesn't currently have a stronger
   access control than that. Flagging plainly rather than implying a
   security property it doesn't have.
2. **PDF-only, size, page count** (`lib/pdfValidation.ts`) — real-PDF check
   is by magic bytes (`%PDF-`), not the spoofable `Content-Type` header.
   Page count uses `pdf-lib` (pure JS, no native deps). Rejects over 4 pages
   or over `MAX_PDF_SIZE_BYTES` (defaults to 10MB).
3. **Hand off to sed.sh** (`lib/malwareScan.ts`) — `POST /malware/upload`
   for a pre-signed URL + `scanId`, then `PUT` the raw bytes to that URL.
   Both calls are fast (a metadata POST and a direct S3 PUT) — safe to do
   synchronously in this request. **The verdict itself is never awaited
   here.**
4. **Store now, verdict unknown** (`lib/pdfFlow.ts`, unchanged function) —
   `storePdfAndOverrideStatus()` uploads the buffer to our own private
   Supabase Storage bucket and sets `pdf_uploaded = true` +
   `pdf_storage_path`, then the row is set to `status = 'scan_pending'`
   with the `scan_id` attached. **This means a file sits in our storage
   before we know it's clean** — see the design-note comment at the top of
   `lib/pdfScanSweep.ts` for exactly why (sed.sh's given spec has no
   "download the file back" endpoint, so there'd be nothing for the cron to
   act on later otherwise). The `otp_sent` -> `verified_ready` ->
   `activated` path can never promote a row stuck at `scan_pending`, so an
   unresolved scan can't accidentally get activated — but the file itself
   is only removed once the sweep confirms it's infected, not before.
5. **Respond 202** — `{ status: "scanning", message: "Your file is being
   checked, we'll be in touch." }`.

### Malware-scan sweep (`GET /cron/sweep-pdf-scans`)

Sibling to `/cron/activate-pending`, same `CRON_SECRET` auth pattern, kept
as a separate endpoint since the two crons act on different row states and
mixing their loops made the output harder to read. Its Cron Trigger is
`*/20 * * * *` (UTC), also in `wrangler.jsonc` / `src/cron.ts`; the manual
`GET /cron/sweep-pdf-scans` + `CRON_SECRET` fallback works exactly as above.

For every `pending_signups` row where `status = 'scan_pending'`
(`lib/pdfScanSweep.ts`, `sweepPdfScans()`): `GET /malware/scan/{scanId}`
**once** per row per sweep pass — no retry loop inside this call, the cron's
own cadence provides the repetition.

- `threatStatus: "RUNNING"` — leave the row untouched, picked up again next
  sweep.
- `status: "clean"` — the file's already correctly stored from upload time;
  just resolves `status` back to `'otp_sent'`, sets `pdf_scan_status =
  'clean'`, clears `scan_id`.
- `status: "infected"` — deletes the stored object
  (`deletePdfFromStorage()`), clears `pdf_uploaded`/`pdf_storage_path`, sets
  `pdf_scan_status = 'infected'` (a persisted rejection marker so Raheem
  sees it happened, not just silence), reverts `status` to `'otp_sent'`,
  clears `scan_id`.

**Both outcomes revert to `'otp_sent'`, never back to `'verified_ready'`** —
even if a row was already verified before the scan started, a changed file
forces a fresh manual review rather than silently resuming. See
`lib/types.ts`'s `SignupStatus` doc comment.

### VirusTotal is scrapped

Confirmed and not revisited: VirusTotal's free tier bars commercial use in
its own ToS and actively flags cloud-IP traffic (i.e. requests from
a cloud provider's own IP ranges get treated with more suspicion than a
residential IP would). `lib/virustotal.ts` is deleted; sed.sh (`lib/malwareScan.ts`)
replaces it entirely. Do not reintroduce VirusTotal anywhere in this
project.

### sed.sh endpoint spec (as given — not independently crawled)

`lib/malwareScan.ts` implements exactly the endpoint spec supplied directly
(base URL, both endpoints, exact request/response shapes, auth header).
`sed.sh/docs/malware` renders client-side and blocks automated crawling
(`robots.txt`), so this could **not** be independently re-verified against
sed.sh's own docs before shipping — **verify field names, auth details, and
current pricing yourself against `https://sed.sh/docs/malware` before
trusting this in production.** If anything differs once this hits a real
account, `lib/malwareScan.ts` is the one file that needs correcting; nothing
else in the codebase depends on sed.sh's exact shapes beyond what it
re-exports.

- Base URL: `https://api.sed.sh`
- `POST /malware/upload` — `{ fileName, fileSize, contentType }`, header
  `Authorization: Bearer <SED_SH_API_KEY>` — returns `{ uploadUrl, scanId,
  expiresIn, estimatedCost }`
- `PUT {uploadUrl}` — raw file binary, **no** `Authorization` header (it's a
  pre-signed S3 URL), `Content-Type: application/pdf`
- `GET /malware/scan/{scanId}` — header `Authorization: Bearer
  <SED_SH_API_KEY>` — returns `{ scanId, status: "clean"|"infected",
  threatStatus: "NO_THREATS_FOUND"|"THREATS_FOUND"|"RUNNING", fileName,
  threats: [] }`

### Workers limits worth knowing (Free plan)

Neither `/upload-pdf` nor the sweep cron ever wait on a scan verdict inside a
request — that's the entire reason the verdict lives in a cron. Cloudflare's
Free plan limits **CPU time** (not time spent waiting on the network) to about
10 ms per request and **subrequests** to 50 per invocation (see Cloudflare's
"Limits" page — they change). The one request that does real CPU work is
`/upload-pdf` (multipart parse + `pdf-lib` page count); locally the PDF parse
costs a few ms, but only a real deploy shows the total. If `/upload-pdf` ever
returns Cloudflare error **1102** ("exceeded CPU"), lower `MAX_PDF_SIZE_BYTES`.
The cron jobs make one Supabase call per row plus one sed.sh call per scan, so
they stay under 50 subrequests only up to roughly 16–24 rows per run — far
above current volume; the rest are picked up on the next run.

## Setup

```bash
npm install
cp .dev.vars.example .dev.vars   # fill in real values (gitignored)
npm run dev                      # wrangler dev -> http://127.0.0.1:8787
npm test                         # vitest: the whole unit suite
npm run build                    # tsc --noEmit for both tsconfigs
```

Local end-to-end checks (each starts a real local `workerd` via `wrangler dev`
plus mock Supabase/OpenRouter servers — no network, no credentials; the result
is "passed locally", never "verified on Cloudflare"):

```bash
npm run check:chat      # POST /chat
npm run check:lock      # POST /complete-setup double-submit lock (Durable Object)
npm run check:upload    # POST /upload-pdf (multipart, size caps, pdf-lib under workerd)
npm run check:cron      # scheduled() + manual /cron/* + GET /lookup-signup
node scripts/smoke-selfcheck.mjs   # proves scripts/smoke-deployed.mjs works
```

Also run `pending_signups_table.sql` against Supabase — it's idempotent and
includes migration statements (`alter table ... drop/add column`, status
value remapping) to bring an already-deployed table from the old
automated-OTP schema to the current one, not just a fresh-table
`create table if not exists`. Also run `promo_codes_table.sql` (new table,
required for referral codes to validate at all — see "Referral codes"
above) and populate it by hand with whatever codes should exist. And
`pdf_storage_bucket.sql` for the private PDF bucket.

## Environment variables

`.dev.vars.example` has the full list with comments (local use). On
Cloudflare they are split two ways (see "Deploying"):

- **`vars` in `wrangler.jsonc`** (plain text, committed): `AVA_CONVERSATION_MODEL`,
  `DEFAULT_AUTO_AI_MODEL`, `MAX_PDF_SIZE_BYTES`, `SUPABASE_PDF_BUCKET` — set to
  the same values the code defaults to.
- **Secrets** (uploaded from `secrets.json`, never in git): everything else —
  the credentials *and* the values specific to Raheem (`ALLOWED_ORIGIN`,
  `SUPABASE_URL`, `RAHEEM_NOTIFY_EMAIL`, `RAHEEM_WHATSAPP_NUMBER`,
  `RESEND_FROM_EMAIL`). Template: `secrets.example.json`.

`PORT` and `OPENROUTER_SOCKS5_PROXY` no longer exist. The ones that need a real
decision before deploying:

- `AVA_CONVERSATION_MODEL` — must be an OpenRouter model that supports
  `response_format: {"type": "json_object"}`, since both of Ava's calls
  rely on strict JSON output.
- `ALLOWED_ORIGIN` — the site's exact origin, e.g. `https://you.github.io`
  (no path, no trailing slash; CORS compares it literally). Unset = no CORS
  header at all (fails closed), so the site can't call the API.
- `RAHEEM_WHATSAPP_NUMBER` — where clients are told to text their code
  (read by `api/complete-setup.ts` and `api/chat.ts` — see "Meta setup"
  above). Also reused as the support-contact number for the `contact_number`
  duplicate-registration block and the token-budget cutoff in `api/chat.ts`.
  Required: both routes answer 500 without it.
- `RESEND_API_KEY` / `RESEND_FROM_EMAIL` / `RAHEEM_NOTIFY_EMAIL` — the
  notification email. All three are required or the send is skipped with a
  logged (non-fatal) error.
- `CRON_SECRET` — required for the manual `/cron/*` routes to ever respond
  with anything but 401. (`scheduled()` doesn't use it.)
- `SED_SH_API_KEY` — optional. When set, `/upload-pdf` runs real malware
  scanning as described above; when unset, scanning is skipped (not an
  error) — see the ⚠️ note in "PDF upload + malware scan" above.
- `OPENROUTER_API_BASE` — optional escape hatch (e.g. a gateway in front of
  OpenRouter). Unset = call OpenRouter directly.
- `MAX_PDF_SIZE_BYTES` — defaults to 10MB, not from a written spec.

## PII masking and the OpenRouter client

- **Phone-number masking** (`lib/piiMasking.ts`) — every conversation
  message is masked before it leaves the Worker, and restored in
  every string field of the model's response (whatever shape that
  response has) before this file returns it. `systemPrompt` is never
  masked — it's Boltane's own instructions, not customer data.
  **v1 scope is phone numbers only.** Names are deliberately not
  masked yet: reliably telling an Arabic given name apart from an
  ordinary word needs either a real NER model or a maintained name
  dictionary — a naive approach would both miss real names and flag
  ordinary words, corrupting conversation context either way. See that
  file's header for why this works directly on Arabic Unicode text
  rather than transliterating to Latin/ASCII first (that step doesn't
  help identify what's PII, and makes exact restoration less reliable,
  not more, since several distinct Arabic letters typically collapse
  onto the same Latin one in a transliteration scheme).
- 60s timeout + 2 retries (2s, then 4s backoff) on a network failure or
  429/5xx in `lib/openrouter.ts` — a single dropped attempt shouldn't fail
  the customer's whole turn.
- Gone on this branch: the SOCKS5 proxy and `stream: true`. They existed only
  because Ava ran on a phone behind a VPN; a Worker calls OpenRouter from
  Cloudflare's network with plain `fetch()`. (History: `CHANGES.md`, "Workers
  migration — Stage 4".)
- Request bodies: JSON bodies are capped at 4.5 MB (`lib/httpBody.ts` → 413
  `{ "error": "Request body too large" }`), as `express.json()` did; PDFs at
  `MAX_PDF_SIZE_BYTES` (+1 MB headroom), enforced while streaming.

✅ `lib/openrouter.ts`'s real request/response/retry path is covered by
`tests/openrouterApiBase`, `tests/openrouterMasking` and
`tests/openrouterRetry` (global `fetch` stubbed; the module itself is not
mocked). `tests/chat.test.ts` still mocks the whole module, by design — it
tests chat.ts's own logic.

## Deploying (Cloudflare Workers)

One Worker (`src/index.ts`): the Hono routes, the `SetupLock` Durable Object,
and `scheduled()`. Deploy from a **GitHub Codespace** — not from Termux
(Cloudflare's tooling is unreliable there) and not from a sandbox with no route
to Cloudflare's API.

**1. Once:** Cloudflare dashboard → My Profile → API Tokens → Create Token →
template **"Edit Cloudflare Workers"**. In the Codespace, export:

```bash
export CLOUDFLARE_API_TOKEN=...      # or add as Codespace secrets
export CLOUDFLARE_ACCOUNT_ID=...
npm install
```

**2. Secrets file** (gitignored — never commit it):

```bash
cp secrets.example.json secrets.json
openssl rand -hex 32                 # paste the output as CRON_SECRET
# edit secrets.json: replace every REPLACE_ME with the real value
# add "SED_SH_API_KEY" only if sed.sh is funded; otherwise leave it out
npm run check:secrets                # must say OK before you continue
```

**3. Deploy** — the secrets go up *with* the deployment, so the Worker is never
live without them:

```bash
npx wrangler deploy --secrets-file secrets.json
```

(Later deploys don't need the file; secrets are never deleted by a deploy. To
change one: `npx wrangler secret put NAME`.) Then delete the local file:
`shred -u secrets.json`.

**4. Smoke test the real URL** — label a pass "verified on Cloudflare":

```bash
npx wrangler tail                    # in a second terminal, keep it open
npm run smoke:deployed -- https://ava-onboarding-backend.<account>.workers.dev \
    --origin https://<you>.github.io
```

The default run is read-only: no rows, no messages, no OpenRouter spend. Add
`--with-chat` for ONE real OpenRouter call (the only check that proves
OpenRouter accepts Cloudflare's network), and `--cron-secret <CRON_SECRET>
--run-cron` to run both jobs once for real. Not checkable from outside:
whether the scheduler fires on time (watch `wrangler tail` for
`[cron] activate-pending` / `[cron] sweep-pdf-scans` lines at :00/:15/:30/:45
and :00/:20/:40), CPU limits under real traffic (error 1102), and
`POST /complete-setup` (writes real rows, calls Meta).

**5. Cutover** (only after step 4 passes): point the GitHub Pages frontend's API
base URL at the `workers.dev` URL. Keep the Termux instance running and
reachable the whole time; watch real traffic with `wrangler tail` for a while;
only then stop it (stop, don't delete). Rolling back = point the frontend back
at the Quick Tunnel URL.

## Confirmed (previously open questions)

- ~~`access_token` is collected in the same Ava conversation, right after
  `phone_number_id`~~ — **superseded**: `access_token`/`phone_number_id`
  no longer come from Ava's conversation at all; they're collected via
  `POST /complete-setup` instead. See "Meta setup" above.

## Explicitly not built (out of scope this pass)

- Manual `system_prompt` hand-editing UI for Raheem — he edits the
  Supabase row directly.
- Rate-limiting on `/upload-pdf` (a client could re-upload repeatedly,
  re-triggering a full VirusTotal scan each time) or upload history (a
  re-upload overwrites the previous file at the same storage path via
  `upsert: true` — no record of prior attempts is kept).
- Any cleanup job for abandoned `otp_sent` rows that never get verified, or
  abandoned `chat_complete` rows that never submit `POST /complete-setup`
  (see this file's "Known limitation" callouts) — they just sit there
  indefinitely either way.

## Known limitation of this scaffold

The extraction step trusts the conversational model to have actually
collected non-vague data before setting `done: true` — the system prompt
instructs it to probe vague business descriptions, but there's no hard
backend-side validation gate. If this turns out to be unreliable in
practice, consider adding a lightweight rule-based check (e.g. minimum
description length) before allowing `done: true` through to extraction.
