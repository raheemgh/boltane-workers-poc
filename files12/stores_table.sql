-- Boltane — Supabase schema: stores table
--
-- gen_random_uuid() has been a built-in PostgreSQL function since v13
-- (Supabase runs on a newer version), so no extension needs to be
-- enabled for the id column's default.
--
-- package vs is_api_free — two independent flags, do not conflate:
--   package     = which pricing tier the store is on. One of
--                 'low-tier', 'basic', 'pro'. Drives Boltane branding
--                 (low-tier only, unconditionally), the monthly
--                 conversation cap, and the default AI model chain —
--                 see resolve_monthly_conversation_cap() in webhook.py
--                 (package='basic' needs is_trial too, see below) and
--                 PACKAGE_DEFAULT_MODELS in ai_client.py. Defaults
--                 'basic'.
--   is_api_free = true: store runs on Boltane's shared free platform
--                 API key (the default — subject to the package's
--                 monthly conversation cap). false: store brought its
--                 own OpenRouter key (BYOK) — fully exempt from the
--                 cap regardless of package, since it draws on the
--                 store's own OpenRouter balance, not the shared
--                 quota. ai_model is left null at signup for BYOK
--                 stores, filled in manually by us afterward.
--
-- is_trial only matters when package = 'basic', which now covers two
-- different things sharing one package value: a referral-code-granted
-- free trial (is_trial = true, 100 conversations/month) and a
-- converted, actually-paying Basic subscriber (is_trial = false, 300
-- conversations/month — unchanged from before). There's no billing
-- automation, so this is a manually-set flag: Ava sets it true at
-- signup for a valid referral code, false otherwise, and Raheem flips
-- it to false by hand once a trial store starts actually paying.
-- Defaults true (the smaller, stricter cap) so a row created without
-- this explicitly set never silently grants the larger paid allowance.
-- See resolve_monthly_conversation_cap() in webhook.py.

create table if not exists stores (
    id                          uuid primary key default gen_random_uuid(),
    phone_number_id             text unique,
    store_name                  text,
    system_prompt               text,
    ai_model                    text,
    api_key                     text,
    access_token                text,
    package                     text not null default 'basic' check (package in ('basic', 'pro', 'low-tier')),
    is_api_free                 boolean default true,
    is_trial                    boolean not null default true,
    monthly_conversation_count  integer default 0,
    conversations_remaining     integer,
    last_used_model             text,
    usage_reset_at              timestamp,
    created_at                  timestamp default now()
);

-- conversations_remaining / last_used_model are observability-only —
-- the app never reads them to make decisions, they just make the two
-- numbers visible directly in the table. conversations_remaining is
-- null for BYOK stores (is_api_free=false), since no cap applies to
-- them.
alter table stores add column if not exists conversations_remaining integer;
alter table stores add column if not exists last_used_model text;

-- Defaults true (the stricter, smaller-cap trial rate) — see the
-- header comment above for why. Existing rows backfill to true here
-- too; if any current 'basic' stores are actually already-paying
-- (not trials), flip those specific rows to false by hand after
-- running this migration.
alter table stores add column if not exists is_trial boolean not null default true;

-- Tracks a rolling "conversation window" per (store, customer) pair,
-- so usage is metered in conversations rather than raw messages:
-- every message from the same customer_phone to the same store within
-- the window counts as one conversation. The window is 2 hours (see
-- CONVERSATION_WINDOW_HOURS in supabase_client.py) — this is Boltane's
-- own definition tuned to actually separate distinct customer visits,
-- NOT Meta's 24h WhatsApp billing-conversation window; the two are
-- unrelated despite the similar concept.
--
-- token_count is the second half of the "token firewall": the
-- cumulative input+output tokens (OpenRouter's real prompt_tokens +
-- completion_tokens, not an estimate) used across the current
-- conversation window. Once it reaches CONVERSATION_TOKEN_CAP (see
-- token_limits.py), the next message forces a reset — same effect as
-- the 2h window expiring naturally. See is_new_conversation_and_touch()
-- and add_conversation_tokens() in supabase_client.py — together they
-- are the only things that read or write this table.
create table if not exists conversation_windows (
    store_id        uuid references stores(id) on delete cascade,
    customer_phone  text not null,
    last_message_at timestamp not null default now(),
    token_count     integer not null default 0,
    primary key (store_id, customer_phone)
);

-- Migration: adds token_count to a conversation_windows table created
-- before the token firewall existed. Idempotent.
alter table conversation_windows add column if not exists token_count integer not null default 0;

-- ---------------------------------------------------------------------
-- Migration: replaces the earlier is_free / monthly_message_count /
-- messages_remaining system with package / monthly_conversation_count /
-- conversations_remaining, and adds conversation_windows. Safe to run
-- even if the table doesn't exist yet or already has these changes —
-- every statement below is idempotent.
-- ---------------------------------------------------------------------

-- Add the new package column if it isn't there yet. Existing rows get
-- the default ('basic') applied automatically since the column is
-- NOT NULL with a default.
alter table stores add column if not exists package text not null default 'basic';

-- Attach the check constraint separately, guarded, so this doesn't
-- fail on a table that already has the package column but not yet the
-- constraint (e.g. a partially-applied earlier run of this file).
do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'stores_package_check'
    ) then
        alter table stores add constraint stores_package_check
            check (package in ('basic', 'pro', 'low-tier'));
    end if;
end $$;

-- Rename the usage columns to the new conversation-based unit. Each
-- rename is guarded so it only runs once, on a table still carrying
-- the old column name.
do $$
begin
    if exists (
        select 1 from information_schema.columns
        where table_name = 'stores' and column_name = 'monthly_message_count'
    ) then
        alter table stores rename column monthly_message_count to monthly_conversation_count;
    end if;

    if exists (
        select 1 from information_schema.columns
        where table_name = 'stores' and column_name = 'messages_remaining'
    ) then
        alter table stores rename column messages_remaining to conversations_remaining;
    end if;
end $$;

-- Drop the old is_free column entirely — fully replaced by package
-- (low-tier ~ the old is_free=true branding behavior; basic/pro ~ the
-- old is_free=false behavior, now split into two paid tiers).
alter table stores drop column if exists is_free;

-- ---------------------------------------------------------------------
-- Migration: brings this file's DDL in line with what ava-backend's
-- activation cron and the Telegram admin bot already read/write against
-- the live `stores` table (per boltane_database_reference.md) — this
-- repo's own copy of the schema had drifted and was missing all five
-- of these. The core engine itself doesn't currently read any of them;
-- added for schema parity, not because webhook.py needs them yet.
-- ---------------------------------------------------------------------

-- Plain WhatsApp number collected early by Ava — NOT the same as
-- phone_number_id (Meta's technical Cloud API ID). Reference/lookup
-- only, nothing in this repo keys off it.
alter table stores add column if not exists contact_number text;

-- Written once at signup by ava-backend (lib/storeRow.ts) and never
-- changed after — permanent audit trail of who referred this store,
-- even if the promo_codes row it points to later expires or exhausts
-- its free-trial slots. Plain text match against promo_codes.code, not
-- a foreign key (see boltane_database_reference.md's "How the tables
-- connect" section).
alter table stores add column if not exists referral_code text;

-- End of the 3-month 10% discount window — only set for a tier-2
-- referral redemption (paying from day one, still package='basic').
-- Not enforced/consumed by this repo yet.
alter table stores add column if not exists discount_expires_at timestamp;

-- Reserved for a future "keep branding for a discount" feature — not
-- wired to any logic here or in ava-backend yet. Defaults false so an
-- existing row never silently opts in.
alter table stores add column if not exists branding_opt_in boolean default false;

-- Official/legal account-holder name, collected via ava-backend's
-- POST /complete-setup (outside Ava's conversation). Nullable: rows
-- that predate this column, or that haven't reached that step, won't
-- have one.
alter table stores add column if not exists legal_name text;

-- ---------------------------------------------------------------------
-- Migration: adds current_invoice_amount / invoice_due_date /
-- subscription_status, which boltane-admin-bot's invoice handlers
-- (lib/handlers/invoice.ts, viewCustomer.ts) already read and write
-- against the live `stores` table, and which that repo's own
-- migration (0001_admin_bot_additions.sql) assumes already exist —
-- it only ALTERs a check constraint on subscription_status, it never
-- creates the column. Without this block that migration errors
-- outright on a database that has never had these columns.
--
-- Constraint is deliberately the original 2-value ('paid'/'unpaid')
-- form here, matching boltane_database_reference.md — the admin bot's
-- own migration is what widens it to add 'cancelled', by dropping and
-- re-adding a constraint with this exact name. Run this file before
-- that one.
-- ---------------------------------------------------------------------
alter table stores add column if not exists current_invoice_amount numeric;
alter table stores add column if not exists invoice_due_date date;
alter table stores add column if not exists subscription_status text;
do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'stores_subscription_status_check'
    ) then
        alter table stores add constraint stores_subscription_status_check
            check (subscription_status in ('paid', 'unpaid') or subscription_status is null);
    end if;
end $$;
