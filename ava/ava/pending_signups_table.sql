-- Boltane — Supabase schema: pending_signups table (Phase 2 staging table)
--
-- Ava writes here at the end of her onboarding conversation instead of
-- writing directly to `stores`. OTP verification is fully MANUAL as of
-- this pass: Ava generates a plaintext 12-digit code and asks the
-- client to text it to Raheem on WhatsApp; Raheem matches it by eye
-- and sets status = 'verified_ready' directly in the Supabase table
-- editor (hand-editing system_prompt first if pdf_uploaded is true).
-- GET /cron/activate-pending then promotes 'verified_ready' rows into
-- `stores` — see toStoreRow() in lib/storeRow.ts and the README.
--
-- Core columns mirror `stores` exactly, both in shape and in the
-- package / is_api_free / api_key / ai_model semantics documented in
-- stores_table.sql. The single source of truth for computing those
-- four columns is buildStoreRow() in lib/storeRow.ts. Nothing here (or
-- in the activation cron) should re-derive them independently —
-- activation only forwards the values already written by Ava via
-- toStoreRow() in lib/storeRow.ts, so the two tables can't drift apart.

create table if not exists pending_signups (
    id                 uuid primary key default gen_random_uuid(),

    -- Core fields — identical shape to stores.
    phone_number_id    text,
    store_name         text,
    system_prompt      text,
    access_token       text,
    -- Collected via POST /complete-setup (the site's post-chat form),
    -- same as phone_number_id/access_token — NOT gathered by Ava's
    -- conversation (see api/chat.ts, lib/systemPrompt.ts). Nullable:
    -- null until that form is submitted. Also mirrored onto `stores`
    -- (same caveat as contact_number below — needs the equivalent
    -- column added there separately). Stored as given, no validation
    -- beyond non-empty at the API boundary.
    legal_name         text,
    -- Mirrors stores_table.sql's package column exactly (replaces the
    -- old is_free boolean). Defaults to 'low-tier' — it only becomes
    -- 'basic' when a valid referral_code is given (see referral_code
    -- below and lib/promoCodes.ts) — buildStoreRow() (lib/storeRow.ts)
    -- is the only place that decides this.
    package            text not null default 'low-tier'
                           check (package in ('basic', 'pro', 'low-tier')),
    is_api_free        boolean default true,
    api_key            text,
    ai_model           text,
    -- A plain WhatsApp number, typed early via business_context —
    -- fully separate from phone_number_id (Meta's technical ID,
    -- obtained later in-conversation). Also mirrored onto `stores`
    -- (stores_table.sql, in the core engine repo — not present in
    -- this repo, needs the equivalent column added there separately).
    -- NOT unique at the DB level: see the note near the bottom of this
    -- file for why (same reasoning as phone_number_id not being
    -- unique here either) — the actual one-active-draft-per-number
    -- rule is enforced in application code
    -- (findPendingSignupByContactNumber() in lib/supabase.ts,
    -- api/chat.ts), not by a DB constraint.
    contact_number     text,
    -- Set only when the client gave a referral_code that validated
    -- against promo_codes (lib/promoCodes.ts) — null otherwise, even
    -- if the client attempted an invalid one (the rejected attempt
    -- itself isn't recorded, only successful applications are). Also
    -- mirrored onto `stores` (same caveat as contact_number above —
    -- needs the equivalent column added there separately).
    referral_code      text,
    -- Mirrors the core engine's `stores.is_trial` column (added there
    -- to split package='basic' into two different monthly conversation
    -- caps). Only meaningful when package = 'basic' — null for
    -- 'low-tier'/'pro' rows. true = tier 1 (full free trial), false =
    -- tier 2 (paying from day one, still 'basic') — see
    -- lib/promoCodes.ts's two-tier logic. Unlike `stores.is_trial`
    -- (which defaults to true there, since every stores row already
    -- has a real package by the time it exists), this column has NO
    -- default here — a draft row genuinely has no package yet, so
    -- null is the only correct starting value; buildStoreRow()
    -- (lib/storeRow.ts) always sets this explicitly once a row is
    -- finalized.
    is_trial           boolean,
    -- now() + 3 months at the moment a TIER-2 referral_code redemption
    -- was applied (paying from day one, still gets a 3-month
    -- discount); null for tier 1 (nothing stacked on top of a full
    -- free trial) and for any non-referral 'low-tier' row. Not
    -- enforced/consumed by anything yet — just recorded for whatever
    -- downstream logic eventually reads it.
    discount_expires_at timestamp,
    -- Schema only, no logic wired to it yet anywhere in this codebase
    -- (intentionally — this column is a placeholder for a future
    -- branding feature, not something buildStoreRow()/toStoreRow()
    -- currently read, write, or carry through activation).
    branding_opt_in    boolean not null default false,
    -- See the ALTER migration near the bottom of this file for the full
    -- explanation — set by boltane-admin-bot once it relays the PDF to
    -- Telegram and deletes it from Storage.
    pdf_removed_from_storage boolean not null default false,

    -- Manual-OTP columns. otp_code is plaintext by design — Raheem
    -- reads it and matches it against a WhatsApp text by eye; there is
    -- no automated verification step to hash against.
    otp_code           text,
    otp_sent_at        timestamp,

    -- Malware-scan handoff columns (sed.sh — see lib/malwareScan.ts).
    -- scan_id is set the moment a PDF is handed off and cleared once
    -- the cron sweep (lib/pdfScanSweep.ts) resolves a verdict; a
    -- non-null scan_id means "still waiting on sed.sh." pdf_scan_status
    -- persists the last verdict even after scan_id clears, so a
    -- rejected upload leaves a visible trace in the table view instead
    -- of silently reverting to "no PDF."
    scan_id            text,
    pdf_scan_status    text,  -- 'clean' | 'infected' | null

    -- Staging-only columns for the PDF-review flow.
    pdf_uploaded       boolean not null default false,
    pdf_storage_path   text,

    -- contact_number-tracking columns (see api/chat.ts, GET
    -- /lookup-signup). business_context is the raw structured object
    -- as last given by the website (project name, contact_number,
    -- domain, business nature, country — lib/types.ts's
    -- BusinessContext); conversation_history is the running transcript
    -- (same shape as ChatRequest.conversation_history), kept current
    -- every turn so an abandoned session can be resumed exactly where
    -- the client left off. Both are only ever populated when
    -- contact_number is known — a signup that never went through
    -- business_context has null in both.
    business_context      jsonb,
    conversation_history  jsonb,

    -- 'draft'          — contact_number known, conversation still in
    --                    progress; no core fields, no OTP yet. The
    --                    resumable state GET /lookup-signup looks for.
    -- 'chat_complete'  — Ava's conversation finished (store_name,
    --                    system_prompt, package/is_api_free/api_key/
    --                    ai_model, referral_code/is_trial/
    --                    discount_expires_at all set) but legal_name/
    --                    phone_number_id/access_token are still null —
    --                    waiting on POST /complete-setup (the site's
    --                    post-chat form), which is also what generates
    --                    the OTP code and advances status to 'otp_sent'.
    -- 'otp_sent'       — code generated; client told to text it to Raheem;
    --                    covers the whole waiting-on-Raheem window, PDF
    --                    upload included (pdf_uploaded is the marker for
    --                    that, not a separate status) — also the state a
    --                    row returns to once a PDF scan resolves either way
    -- 'scan_pending'   — a PDF was just uploaded and handed to sed.sh for
    --                    scanning; the cron sweep resolves this back to
    --                    'otp_sent' once a verdict comes back
    -- 'verified_ready' — Raheem matched the code by hand and set this
    --                    directly in the table editor (after hand-editing
    --                    system_prompt too, if pdf_uploaded = true)
    -- 'activated'      — promoted to stores by GET /cron/activate-pending
    status             text not null default 'draft',

    created_at         timestamp default now()
);

-- Speeds up findPendingSignupByContactNumber() (api/chat.ts's
-- dedupe/draft-reuse check on every contact_number-bearing request,
-- and GET /lookup-signup):
-- select * from pending_signups where contact_number = $1
--   order by created_at desc limit 1;
create index if not exists pending_signups_contact_number_idx
    on pending_signups (contact_number, created_at desc)
    where contact_number is not null;

-- Speeds up the malware-scan sweep's polling query:
-- select * from pending_signups where status = 'scan_pending';
create index if not exists pending_signups_scan_pending_idx
    on pending_signups (status)
    where status = 'scan_pending';

-- Speeds up the activation cron's polling query:
-- select * from pending_signups where status = 'verified_ready';
create index if not exists pending_signups_verified_ready_idx
    on pending_signups (status)
    where status = 'verified_ready';

-- Speeds up phone_number_id lookups (e.g. /upload-pdf eligibility):
-- select * from pending_signups
--   where phone_number_id = $1 and status in ('otp_sent', 'verified_ready')
--   order by created_at desc limit 1;
create index if not exists pending_signups_phone_status_idx
    on pending_signups (phone_number_id, status, created_at desc);

-- Note: unlike stores.phone_number_id, this column is intentionally NOT
-- unique here — a client can abandon and restart Ava's conversation
-- before ever being verified, producing more than one staging row for
-- the same number. stores.phone_number_id keeps its unique constraint,
-- so the activation cron's insert into stores remains the final
-- integrity check against duplicate activation.

-- --- Migration for a table already created under the old (automated-OTP)
-- --- schema — `create table if not exists` above is a no-op against an
-- --- existing table, so the actual shape change has to happen here.
-- --- Idempotent: safe to run again.
alter table pending_signups drop column if exists otp_code_hash;
alter table pending_signups drop column if exists otp_expires_at;
alter table pending_signups drop column if exists otp_verified_at;
alter table pending_signups drop column if exists otp_attempts;
alter table pending_signups drop column if exists activate_at;

alter table pending_signups add column if not exists otp_code text;
alter table pending_signups add column if not exists otp_sent_at timestamp;

-- Malware-scan handoff columns (sed.sh replaced VirusTotal — see
-- lib/malwareScan.ts; VirusTotal's free tier is scrapped entirely,
-- its ToS bars commercial use and it actively flags cloud-IP traffic).
alter table pending_signups add column if not exists scan_id text;
alter table pending_signups add column if not exists pdf_scan_status text;

-- is_free -> package migration (mirrors the same change on
-- stores_table.sql / the core engine). Every existing row was
-- necessarily on the old always-true is_free path, so 'basic' is the
-- correct backfill for all of them, not just new signups.
alter table pending_signups add column if not exists package text;
update pending_signups set package = 'basic' where package is null;
alter table pending_signups alter column package set not null;
alter table pending_signups alter column package set default 'basic';
do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'pending_signups_package_check'
    ) then
        alter table pending_signups
            add constraint pending_signups_package_check
            check (package in ('basic', 'pro', 'low-tier'));
    end if;
end $$;
alter table pending_signups drop column if exists is_free;

-- contact_number tracking columns (business_context, resumable
-- drafts — see api/chat.ts, GET /lookup-signup). Existing rows all
-- predate this feature, so null/null/null (no contact_number, no
-- saved context, no saved transcript) is correct for every one of
-- them — nothing to backfill.
alter table pending_signups add column if not exists contact_number text;
alter table pending_signups add column if not exists business_context jsonb;
alter table pending_signups add column if not exists conversation_history jsonb;

-- The default only changes for NEW rows going forward — deliberately
-- NOT touching existing rows' status values here. An existing
-- 'otp_sent'/'scan_pending'/'verified_ready'/'activated' row is
-- already past the point 'draft' describes; only a genuinely-new
-- insert should ever start at 'draft'.
alter table pending_signups alter column status set default 'draft';

-- Collapse any old-schema rows into the new 3-value status set before
-- anything queries against it — run once, safe to re-run (no-ops on a
-- table that never had these old values).
update pending_signups set status = 'otp_sent'
    where status in ('pending_manual_review');
update pending_signups set status = 'verified_ready'
    where status = 'awaiting_activation';
-- 'expired' had no code path that ever set it, so no mapping needed —
-- if you find any, decide by hand rather than silently dropping them.

drop index if exists pending_signups_activation_idx;

-- package default: 'basic' -> 'low-tier' (referral-code gating — see
-- lib/promoCodes.ts, lib/storeRow.ts). Deliberately NOT touching
-- existing rows' package values here, same reasoning as the status
-- default change above — a row that already has 'basic' presumably
-- earned it under the old always-basic rule and shouldn't be silently
-- downgraded; this only changes what NEW rows default to.
alter table pending_signups alter column package set default 'low-tier';

-- Referral-code columns (lib/promoCodes.ts, api/chat.ts). Existing
-- rows predate this feature — null/null/null is correct for all of them.
alter table pending_signups add column if not exists referral_code text;
alter table pending_signups add column if not exists is_trial boolean;
alter table pending_signups add column if not exists discount_expires_at timestamp;

-- Schema-only placeholder — see the create-table comment above.
alter table pending_signups add column if not exists branding_opt_in boolean not null default false;

-- legal_name: Meta setup (phone_number_id, access_token) and legal_name
-- all moved out of Ava's conversation into POST /complete-setup (see
-- api/complete-setup.ts, lib/otpHandoff.ts). Existing rows predate this
-- split entirely — they were already past this point under the old
-- single-step flow, so null here is correct/harmless for all of them.
alter table pending_signups add column if not exists legal_name text;

-- Set by boltane-admin-bot's admin-alerts cron once it has relayed the
-- uploaded PDF to Telegram (via a short-lived Storage signed URL) and
-- confirmed Telegram actually received it — only then does that cron
-- delete the object from Storage, to stay well under the project's
-- storage cap. pdf_storage_path is deliberately left untouched (still
-- points at where the file *was*, for audit purposes) — this column is
-- what actually says whether the bytes are still there. false/existing
-- rows are correct as-is: nothing to relay if pdf_uploaded is also
-- false, and any row already relayed under an older pass would have
-- had no way to record it before this column existed.
alter table pending_signups add column if not exists pdf_removed_from_storage boolean not null default false;
