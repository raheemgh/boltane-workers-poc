// lib/types.ts
//
// Central type definitions. StoreRow mirrors stores_table.sql exactly.
// Columns owned by the core engine (monthly_message_count,
// messages_remaining, last_used_model, usage_reset_at) are deliberately
// NOT part of StoreRow — if any future code tries to set them from
// Ava's insert path, TypeScript will error at compile time.
import type { MalwareScanStatus } from "./malwareScan";

export interface ConversationMessage {
  role: "user" | "assistant";
  content: string;
}

export interface ChatRequest {
  conversation_history: ConversationMessage[];
  user_message: string;
  // Optional, structured context the website already collected before
  // handing off to Ava (see lib/systemPrompt.ts's merge and the
  // "contact_number tracking" section below). Nominally sent on the
  // first /chat call of a session — but since this API has no session
  // memory of its own, contact_number specifically needs to keep
  // arriving on every call for draft-row tracking / resume / dedupe to
  // keep working across the whole conversation, not just turn one. The
  // other fields are only really needed once (they just feed the
  // system prompt) and can be omitted on later calls.
  business_context?: BusinessContext;
  // PRIMARY path for referral codes — a plain top-level field, exactly
  // like contact_number is inside business_context: a direct lookup,
  // zero extra LLM token cost. This is what api/chat.ts actually
  // prefers at finalization time (see lib/promoCodes.ts). The
  // AI-extracted ExtractedOnboardingData.referral_code (the client
  // typing a code into the chat itself) is kept only as a secondary
  // fallback for when the frontend hasn't wired this field yet — not
  // the primary path, on purpose (see git history: extraction-only was
  // a bug, not the original design).
  referral_code?: string | null;
}

/**
 * Structured context the client's website already has before Ava's
 * conversation starts — e.g. from a signup form or landing page. All
 * fields optional; only what's actually known should be sent.
 * contact_number is the one field with real backend behavior attached
 * to it (see supabase.ts's findPendingSignupByContactNumber() and
 * api/chat.ts) — the rest only ever get folded into the system prompt.
 */
export interface BusinessContext {
  project_name?: string | null;
  contact_number?: string | null;
  domain?: string | null;
  business_nature?: string | null;
  country?: string | null;
}

export interface ChatResponse {
  reply: string;
  done: boolean;
  // true only on the contact_number duplicate-registration block (see
  // README's "Duplicate registration" section) — done is also true on
  // that response (nothing more to do), but blocked tells the frontend
  // this ISN'T the normal "you're onboarded, here's your OTP" ending.
  blocked?: boolean;
  // true only when the per-attempt cumulative token budget (see
  // lib/tokenBudget.ts, README's "Token budgets" section) was hit
  // before a model call could be made. done is also true here — the
  // conversation is being ended, not paused — but this tells the
  // frontend it's a budget cutoff, not a normal completion.
  budgetExceeded?: boolean;
}

/**
 * The row Ava writes to Supabase `stores`.
 *
 * `package` used to be typed as the literal `"basic"` — every Ava
 * signup started on Basic, full stop, and that was a compile-time
 * guarantee against ever making it conditional on anything. That
 * guarantee is gone as of the referral-code gate below: `package` is
 * now genuinely `"basic"` XOR `"low-tier"` depending on whether a
 * valid referral_code was given (see lib/promoCodes.ts and
 * api/chat.ts's finalization step — NOT computed inside buildStoreRow()
 * itself, since that would require buildStoreRow() to do async DB
 * validation, breaking its "pure function" shape; buildStoreRow() just
 * takes the already-decided package straight from its caller now).
 * `is_api_free`/`api_key`/`ai_model` are unrelated to `package` and
 * unchanged by any of this.
 */
export type Package = "basic" | "pro" | "low-tier";

export interface StoreRow {
  phone_number_id: string;
  store_name: string;
  system_prompt: string;
  access_token: string;
  package: Package;
  is_api_free: boolean;
  api_key: string | null;
  ai_model: string | null;
  // A plain WhatsApp number, typed early (business_context, before the
  // conversation even reaches phone_number_id) — fully separate from
  // phone_number_id (Meta's technical Cloud API ID, obtained later in
  // the conversation once the client finishes their own Meta setup).
  // Nullable: older rows / any signup that never went through
  // business_context won't have one.
  contact_number: string | null;
  // Set only when a referral_code was given AND validated successfully
  // (lib/promoCodes.ts) — an invalid or missing code leaves this null
  // and package at 'low-tier'; a valid one stores the code permanently
  // here (it survives activation into `stores`, unlike a promo_codes
  // row's own use_count bookkeeping) and sets package to 'basic'.
  referral_code: string | null;
  // Mirrors the core engine's `stores.is_trial` column (added there to
  // split package='basic' into two different monthly conversation
  // caps). Only meaningful when package === 'basic' — null for
  // 'low-tier'/'pro' rows, where it doesn't apply. true = tier 1 (full
  // free trial, referral.tier === 1), false = tier 2 (paying from day
  // one but still 'basic', referral.tier === 2). Decided once, at
  // finalization, by buildStoreRow() — see lib/promoCodes.ts for the
  // tier logic itself.
  is_trial: boolean | null;
  // now() + 3 months at the moment a tier-2 referral_code redemption
  // was applied (paying from day one, still gets a 3-month discount);
  // null for tier 1 (nothing stacked on top of a full free trial) and
  // for any non-referral 'low-tier' row. Not enforced/consumed anywhere
  // yet in this pass — just recorded for whatever downstream logic
  // eventually reads it.
  discount_expires_at: string | null;
  // Collected via POST /complete-setup (the site team's post-chat
  // form), same as phone_number_id/access_token — NOT part of Ava's
  // conversation. Nullable: a row that hasn't reached that step yet
  // (still 'chat_complete' or earlier) has no legal_name yet. Stored
  // as given, no validation beyond non-empty at the API boundary (see
  // api/complete-setup.ts) — no format/legal-registry checking here.
  legal_name: string | null;
  // Schema-only column (pending_signups_table.sql), boolean not null
  // default false. Deliberately NOT set by buildStoreRow()/toStoreRow()
  // — see README's "branding_opt_in — schema only" section; this is a
  // placeholder for a future branding feature, nothing reads or writes
  // it yet. Typed as optional (rather than required, DB-default-backed)
  // so adding it here doesn't force every existing StoreRow object
  // literal in this codebase to start setting it.
  branding_opt_in?: boolean;
}

/**
 * What the extraction LLM call must produce from the full conversation,
 * once the conversation-phase model has signalled done: true.
 *
 * phone_number_id/access_token are deliberately NOT here — Meta setup
 * moved entirely out of Ava's conversation into a separate post-chat
 * form (POST /complete-setup). Ava's scope is now just: business
 * understanding + BYOK preference + optional referral_code. See
 * lib/systemPrompt.ts and api/complete-setup.ts.
 *
 * Note: `ai_assistant_name` has no column of its own in `stores` — it
 * only feeds into the generated `system_prompt` text.
 */
export interface ExtractedOnboardingData {
  store_name: string;
  ai_assistant_name: string;
  business_description: string;
  byok: boolean;
  api_key: string | null;
  ai_model: string | null;
  // Optional — only set if the client mentioned a referral/promo code
  // during the conversation. Validated against `promo_codes` at
  // finalization time (lib/promoCodes.ts, api/chat.ts) — extraction
  // itself just reports what the client said, it doesn't validate
  // anything.
  referral_code: string | null;
}

export interface ExtractionResult extends ExtractedOnboardingData {
  system_prompt: string;
}

/**
 * The outcome of validating a referral_code against `promo_codes`
 * (lib/promoCodes.ts). Always has `code` (the code that was checked,
 * even if invalid — useful for logging/debugging why a signup landed
 * on 'low-tier'); `valid` decides everything else. buildStoreRow()
 * (lib/storeRow.ts) takes this as an input rather than computing it,
 * since the DB lookup itself is async and buildStoreRow() stays a pure
 * synchronous function — see api/chat.ts for where validation actually
 * happens.
 *
 * Two-tier design: a code is invalid only if it doesn't exist or is
 * expired — free_trial_max_uses no longer makes a code stop working
 * once exceeded, it only decides which tier a valid redemption lands
 * on. `tier` carries that decision through to buildStoreRow() so it can
 * set `is_trial`/`discount_expires_at` correctly:
 *   tier 1 — full free trial: is_trial=true, no discount_expires_at
 *            (there's nothing stacked on top of free).
 *   tier 2 — paying from day one, but still package='basic': is_trial=false,
 *            discount_expires_at = now() + 3 months.
 */
export interface ReferralApplication {
  code: string;
  valid: boolean;
  // Only set when valid: true — which tier this redemption landed in.
  tier?: 1 | 2;
  // Only set when tier === 2.
  discountExpiresAt?: string | null;
}

/**
 * Phase 2 staging state:
 *   'draft'          — a contact_number is known (business_context) but
 *                      the conversation hasn't finished — no OTP, no
 *                      core fields yet. Exists purely so
 *                      GET /lookup-signup can resume an abandoned
 *                      session and so contact_number duplicate-checks
 *                      have something to key off before the client
 *                      ever reaches "done". See supabase.ts's
 *                      findPendingSignupByContactNumber() and
 *                      api/chat.ts.
 *   'chat_complete'  — Ava's conversation finished: store_name,
 *                      system_prompt, package/is_api_free/api_key/
 *                      ai_model, referral_code/is_trial/discount_expires_at
 *                      are all set. Still missing legal_name,
 *                      phone_number_id, access_token — those come from
 *                      the site's separate post-chat form
 *                      (POST /complete-setup), which is also what
 *                      generates the OTP code and moves the row to
 *                      'otp_sent'. A row can sit here indefinitely if
 *                      the client never submits that form.
 *   'otp_sent'       — code generated, client told to text it to Raheem
 *                      (also the resting state while a PDF sits in
 *                      'scan_pending' or between scans — see below)
 *   'scan_pending'    — a PDF was just uploaded and handed to sed.sh for
 *                      scanning (lib/malwareScan.ts); the row sits here
 *                      until the cron sweep (lib/pdfScanSweep.ts) polls
 *                      a verdict and moves it back to 'otp_sent'. This
 *                      state exists purely to keep the activation cron
 *                      from ever promoting a row while its PDF's
 *                      verdict is still unknown — see the "your call"
 *                      note in lib/pdfScanSweep.ts for why it always
 *                      reverts to 'otp_sent' rather than trying to
 *                      restore whatever status came before.
 *   'verified_ready' — Raheem matched the code by hand and set this
 *                      directly in the Supabase table editor (also
 *                      after hand-editing system_prompt, if
 *                      pdf_uploaded is true)
 *   'activated'      — promoted to `stores` by the activation cron
 */
export type SignupStatus =
  | "draft"
  | "chat_complete"
  | "otp_sent"
  | "scan_pending"
  | "verified_ready"
  | "activated";

/**
 * The row Ava writes to Supabase `pending_signups`.
 *
 * Extends Partial<StoreRow>, NOT StoreRow directly — a draft row
 * (contact_number known, conversation still in progress) genuinely has
 * none of StoreRow's other fields yet; they're all nullable text
 * columns in the DB for exactly this reason. By the time a row reaches
 * 'verified_ready' (and thus toStoreRow() in lib/storeRow.ts, which
 * asserts this at runtime), every StoreRow field is expected to be
 * populated — buildStoreRow() computed them all at once, at
 * finalization, same as before this change; only the timing of when a
 * row starts existing in Supabase moved earlier.
 *
 * otp_code is plaintext by design (manual verification — Raheem reads
 * it and matches it against a WhatsApp text by eye, see lib/otpCode.ts
 * and README). scan_id/pdf_scan_status are owned by the malware-scan
 * handoff (lib/malwareScan.ts, api/upload-pdf.ts, lib/pdfScanSweep.ts).
 * pdf_uploaded/pdf_storage_path are set by lib/pdfFlow.ts. The
 * verified_ready/activated transitions are still owned by Raheem
 * (manual) and the activation cron, respectively.
 */
export interface PendingSignupRow extends Partial<StoreRow> {
  status: SignupStatus;
  otp_code?: string | null;
  otp_sent_at?: string | null;
  pdf_uploaded?: boolean;
  pdf_storage_path?: string | null;
  // Set the moment a PDF is handed to sed.sh; cleared once the cron
  // sweep resolves a verdict (either outcome) — a non-null scan_id is
  // exactly "we're still waiting on sed.sh for this one."
  scan_id?: string | null;
  // Last scan verdict, kept even after scan_id clears, so a rejected
  // upload has a visible trace in the Supabase table view instead of
  // just silently reverting to "no PDF" with no explanation.
  pdf_scan_status?: MalwareScanStatus | null;
  // Set by boltane-admin-bot's admin-alerts cron after it relays the
  // file to Telegram and deletes it from Storage (storage-cap
  // workaround while sed.sh isn't funded). Nothing in this repo reads
  // or writes this — it's admin-bot's bookkeeping, present here only
  // so this type stays a true mirror of the pending_signups schema.
  pdf_removed_from_storage?: boolean;
  // Raw business_context as last given (see ChatRequest) — kept so a
  // resumed session (GET /lookup-signup) still has it even if the
  // frontend doesn't resend the full object on every turn.
  business_context?: BusinessContext | null;
  // The full running transcript — same shape the frontend would send
  // back as ChatRequest.conversation_history on its next call (prior
  // history + this turn's user message + this turn's assistant reply).
  // Updated every turn a contact_number is present, draft or not, so
  // GET /lookup-signup can hand back an exact resume point.
  conversation_history?: ConversationMessage[] | null;
  created_at?: string;
  id?: string;
}

/**
 * POST /complete-setup — the site team's post-chat form submission.
 * Meta setup (phone_number_id, access_token) and legal_name all live
 * here now, entirely outside Ava's conversation — see
 * api/complete-setup.ts. Looked up by contact_number against the
 * pending_signups row Ava already created/updated during the chat
 * (must be at status 'chat_complete').
 */
export interface CompleteSetupRequest {
  contact_number: string;
  legal_name: string;
  phone_number_id: string;
  access_token: string;
}

export interface CompleteSetupResponse {
  reply: string;
}
