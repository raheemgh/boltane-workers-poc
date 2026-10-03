-- Boltane — Supabase schema: promo_codes table
--
-- Source of truth for referral_code validation at Ava signup
-- finalization time (lib/promoCodes.ts, api/chat.ts). See README's
-- "Referral codes" section for the full two-tier behavior:
--
--   - A code is invalid ONLY if it doesn't exist or is expired.
--     free_trial_max_uses is NOT an overall cap anymore — a code never
--     stops working once redeemed enough times, it just stops granting
--     the full free-trial tier.
--   - Every valid redemption grants package='basic', always — just on
--     one of two tiers:
--       tier 1 (use_count < free_trial_max_uses): is_trial=true, full
--         free first month, no discount_expires_at (nothing stacked on
--         top of free).
--       tier 2 (use_count >= free_trial_max_uses): is_trial=false —
--         paying from day one — with discount_expires_at = now() + 3
--         months instead.
--   - use_count increments on every valid redemption, both tiers alike
--     (it's a running "how many got the full trial" counter now, not a
--     total-uses-before-the-code-dies cap).
--
-- This table is written to by the Telegram admin bot's "New creator
-- code" / "Edit creator code" actions (lib/handlers/creatorCodes.ts in
-- boltane-admin-bot) — creator_name, discount_pct, and commission_pct
-- below exist for that bot, not for anything in this codebase, which
-- still only reads rows here (and increments use_count).

create table if not exists promo_codes (
    id                    uuid primary key default gen_random_uuid(),
    code                  text not null unique,
    -- Display name for the content creator this code belongs to, shown
    -- in the admin bot's "Content creator stats" flow. Always supplied
    -- by that bot's creation wizard.
    creator_name          text not null,
    -- null = never expires.
    expires_at            timestamp,
    -- Renamed from max_uses: controls ONLY how many redemptions get the
    -- full free-trial tier (tier 1), not an overall cap on the code's
    -- usable lifetime. null = unlimited tier-1 redemptions (every
    -- redemption gets the full free trial, tier 2 never triggers) —
    -- same "null = unlimited" convention the old max_uses column had,
    -- just applied to trial slots instead of total uses.
    free_trial_max_uses   integer,
    -- Incremented by lib/promoCodes.ts on every VALID redemption
    -- (tier 1 or tier 2 alike — a code is never rejected for being
    -- "used up"). See that file's header comment for the known
    -- (accepted, low-stakes) race condition between the tier check and
    -- this increment — not atomic across concurrent redemptions of the
    -- same code in this pass.
    use_count             integer not null default 0,
    -- Percent discount applied during a tier-2 (paying-from-day-one)
    -- redemption's 3-month window. Set by the admin bot at creation
    -- time (defaults to 10 there, not at the column level, since the
    -- app is the single source of truth for that default).
    discount_pct          integer not null,
    -- Percent of a referred store's current_invoice_amount owed to
    -- this creator as commission, computed by the admin bot's
    -- "Content creator stats" flow. Set once at creation.
    commission_pct        integer not null,
    created_at            timestamp default now()
);

-- Speeds up lib/promoCodes.ts's lookup: select * from promo_codes where code = $1;
-- (redundant with the unique constraint's own index in Postgres, but
-- explicit here for clarity/documentation, matching this repo's style
-- elsewhere — costs nothing extra since Postgres just reuses the
-- unique index instead of creating a second one).
create index if not exists promo_codes_code_idx on promo_codes (code);

-- --- Migration for a table already created under the old (single-tier,
-- --- overall-cap) schema. Idempotent: safe to run again — a plain
-- --- `rename column` is NOT idempotent on its own (fails the second
-- --- run since max_uses no longer exists), so this only renames if the
-- --- old column is still there.
do $$
begin
    if exists (
        select 1 from information_schema.columns
        where table_name = 'promo_codes' and column_name = 'max_uses'
    ) then
        alter table promo_codes rename column max_uses to free_trial_max_uses;
    end if;
end $$;
