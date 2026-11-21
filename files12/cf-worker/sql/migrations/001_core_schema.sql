-- Boltane V2 - migration 001: core schema (additive only).
-- Stage V2-01. Rollback: sql/rollbacks/001_core_schema.sql
--
-- Rules (ADR-3):
--   * Additive and idempotent: only "create ... if not exists" and "add column if not exists".
--     Running this file twice changes nothing the second time.
--   * V1 never reads or writes any of this. With every V2 flag false nothing here is used.
--   * Every timestamp is timestamptz. Every table has store_id (admin_alerts: nullable, because
--     some alerts belong to the platform and not to one store).
--   * Row level security is ON and there are no policies: only the service key can read or write.
--   * Phone numbers are digits only, no "+", like Meta's wa_id.
--   * Columns that hold customer text or identifiers are marked PII in a comment. The retention
--     sweep (D-11, default 30 days) deletes by message_log.created_at, inbound_events.received_at
--     and wa_customers.last_seen_at.
--   * Constraints only on stable value sets. Sets that will grow (kind, content_type, model) stay
--     plain text and are checked in code.

-- ---------------------------------------------------------------------
-- 1) stores: new settings columns (nullable or defaulted, so V1 and Ava keep working)
-- ---------------------------------------------------------------------
alter table stores add column if not exists features jsonb not null default '{}'::jsonb;
alter table stores add column if not exists timezone text;
alter table stores add column if not exists business_hours jsonb;
alter table stores add column if not exists default_language text not null default 'ar';
alter table stores add column if not exists billing_anchor date;
alter table stores add column if not exists owner_phone text;

do $$
begin
    if not exists (select 1 from pg_constraint
                   where conname = 'stores_features_object_check' and conrelid = 'public.stores'::regclass) then
        alter table stores add constraint stores_features_object_check
            check (jsonb_typeof(features) = 'object');
    end if;
    if not exists (select 1 from pg_constraint
                   where conname = 'stores_business_hours_object_check' and conrelid = 'public.stores'::regclass) then
        alter table stores add constraint stores_business_hours_object_check
            check (business_hours is null or jsonb_typeof(business_hours) = 'object');
    end if;
    if not exists (select 1 from pg_constraint
                   where conname = 'stores_default_language_check' and conrelid = 'public.stores'::regclass) then
        alter table stores add constraint stores_default_language_check
            check (default_language in ('ar', 'en'));
    end if;
    if not exists (select 1 from pg_constraint
                   where conname = 'stores_timezone_check' and conrelid = 'public.stores'::regclass) then
        alter table stores add constraint stores_timezone_check
            check (timezone is null or (length(timezone) <= 64 and timezone ~ '^(UTC|[A-Za-z_]+(/[A-Za-z0-9_+-]+)+)$'));
    end if;
    if not exists (select 1 from pg_constraint
                   where conname = 'stores_owner_phone_check' and conrelid = 'public.stores'::regclass) then
        alter table stores add constraint stores_owner_phone_check
            check (owner_phone is null or owner_phone ~ '^[0-9]{6,15}$');
    end if;
end $$;

-- ---------------------------------------------------------------------
-- 2) wa_customers: one row per customer per store. Identity is a phone, a BSUID, or both (ADR-6).
--    Merging a phone row with a BSUID row is done by the V2-05 gate function.
-- ---------------------------------------------------------------------
create table if not exists wa_customers (
    id            uuid primary key default gen_random_uuid(),
    store_id      uuid not null references stores(id) on delete cascade,
    phone         text,
    bsuid         text,
    parent_bsuid  text,
    username      text,
    first_seen_at timestamptz not null default now(),
    last_seen_at  timestamptz not null default now(),
    blocked_at    timestamptz,
    constraint wa_customers_identity_check check (phone is not null or bsuid is not null),
    constraint wa_customers_phone_check check (phone is null or phone ~ '^[0-9]{6,15}$')
);
create unique index if not exists wa_customers_store_phone_uidx
    on wa_customers (store_id, phone) where phone is not null;
create unique index if not exists wa_customers_store_bsuid_uidx
    on wa_customers (store_id, bsuid) where bsuid is not null;
create index if not exists wa_customers_store_last_seen_idx
    on wa_customers (store_id, last_seen_at);

-- ---------------------------------------------------------------------
-- 3) inbound_events: the durable inbox. One row per Meta message id; the unique key is the dedupe.
--    status: pending -> processing -> done | failed | ignored. A stale "processing" row (locked_until
--    in the past) is claimed again by the reaper.
-- ---------------------------------------------------------------------
create table if not exists inbound_events (
    id           uuid primary key default gen_random_uuid(),
    store_id     uuid not null references stores(id) on delete cascade,
    wamid        text not null,
    kind         text not null default 'message',
    from_phone   text,
    from_bsuid   text,
    customer_id  uuid references wa_customers(id) on delete set null,
    event        jsonb not null,
    status       text not null default 'pending',
    attempts     integer not null default 0,
    locked_until timestamptz,
    received_at  timestamptz not null default now(),
    finished_at  timestamptz,
    error_code   text,
    constraint inbound_events_store_wamid_key unique (store_id, wamid),
    constraint inbound_events_status_check check (status in ('pending', 'processing', 'done', 'failed', 'ignored')),
    constraint inbound_events_attempts_check check (attempts >= 0),
    constraint inbound_events_identity_check check (kind <> 'message' or from_phone is not null or from_bsuid is not null),
    constraint inbound_events_event_object_check check (jsonb_typeof(event) = 'object')
);
create index if not exists inbound_events_open_idx
    on inbound_events (received_at) where status in ('pending', 'processing');
create index if not exists inbound_events_customer_idx
    on inbound_events (store_id, customer_id, received_at desc) where customer_id is not null;
create index if not exists inbound_events_received_idx
    on inbound_events (received_at);

-- ---------------------------------------------------------------------
-- 4) chat_sessions: a session is a 2 hour window of one customer (time only). counted_at is set on
--    the first successful reply, and is what the monthly cap counts.
-- ---------------------------------------------------------------------
create table if not exists chat_sessions (
    id              uuid primary key default gen_random_uuid(),
    store_id        uuid not null references stores(id) on delete cascade,
    customer_id     uuid not null references wa_customers(id) on delete cascade,
    started_at      timestamptz not null default now(),
    last_message_at timestamptz not null default now(),
    counted_at      timestamptz,
    message_count   integer not null default 0,
    tokens_total    integer not null default 0,
    cost_usd        numeric(12, 6) not null default 0,
    constraint chat_sessions_counters_check check (message_count >= 0 and tokens_total >= 0 and cost_usd >= 0)
);
create index if not exists chat_sessions_customer_idx
    on chat_sessions (store_id, customer_id, last_message_at desc);
create index if not exists chat_sessions_counted_idx
    on chat_sessions (store_id, counted_at) where counted_at is not null;

-- ---------------------------------------------------------------------
-- 5) message_log: what was said, both directions. Deleted with the customer (erasure request).
-- ---------------------------------------------------------------------
create table if not exists message_log (
    id           bigint generated always as identity primary key,
    store_id     uuid not null references stores(id) on delete cascade,
    customer_id  uuid not null references wa_customers(id) on delete cascade,
    session_id   uuid references chat_sessions(id) on delete set null,
    direction    text not null,
    wamid        text,
    content_type text not null default 'text',
    body         text,
    created_at   timestamptz not null default now(),
    constraint message_log_direction_check check (direction in ('in', 'out'))
);
create unique index if not exists message_log_wamid_uidx
    on message_log (store_id, direction, wamid) where wamid is not null;
create index if not exists message_log_customer_idx
    on message_log (store_id, customer_id, created_at desc);
create index if not exists message_log_created_idx
    on message_log (created_at);

-- ---------------------------------------------------------------------
-- 6) usage_events: one row per model call. Unique per (inbound event, kind) so a retried
--    finalize cannot count a turn twice.
-- ---------------------------------------------------------------------
create table if not exists usage_events (
    id                bigint generated always as identity primary key,
    store_id          uuid not null references stores(id) on delete cascade,
    session_id        uuid references chat_sessions(id) on delete set null,
    inbound_event_id  uuid references inbound_events(id) on delete set null,
    kind              text not null default 'reply',
    model             text,
    provider          text,
    prompt_tokens     integer,
    completion_tokens integer,
    cost_usd          numeric(12, 6),
    latency_ms        integer,
    finish_reason     text,
    error_code        text,
    created_at        timestamptz not null default now(),
    constraint usage_events_nonneg_check check (
        coalesce(prompt_tokens, 0) >= 0 and coalesce(completion_tokens, 0) >= 0
        and coalesce(cost_usd, 0) >= 0 and coalesce(latency_ms, 0) >= 0
    )
);
create unique index if not exists usage_events_inbound_kind_uidx
    on usage_events (inbound_event_id, kind) where inbound_event_id is not null;
create index if not exists usage_events_store_created_idx
    on usage_events (store_id, created_at);

-- ---------------------------------------------------------------------
-- 7) admin_alerts: deduplicated by fingerprint while open (resolved_at is null). Contains no
--    customer text: summary is written by our code from error codes only.
-- ---------------------------------------------------------------------
create table if not exists admin_alerts (
    id            bigint generated always as identity primary key,
    store_id      uuid references stores(id) on delete cascade,
    severity      text not null,
    code          text not null,
    fingerprint   text not null,
    summary       text,
    occurrences   integer not null default 1,
    first_seen_at timestamptz not null default now(),
    last_seen_at  timestamptz not null default now(),
    notified_at   timestamptz,
    resolved_at   timestamptz,
    constraint admin_alerts_severity_check check (severity in ('info', 'warning', 'error', 'critical')),
    constraint admin_alerts_occurrences_check check (occurrences >= 1)
);
create unique index if not exists admin_alerts_open_fingerprint_uidx
    on admin_alerts (fingerprint) where resolved_at is null;
create index if not exists admin_alerts_last_seen_idx
    on admin_alerts (last_seen_at desc);

-- ---------------------------------------------------------------------
-- 8) Row level security on, no policies: the service key only.
-- ---------------------------------------------------------------------
alter table wa_customers enable row level security;
alter table inbound_events enable row level security;
alter table chat_sessions enable row level security;
alter table message_log enable row level security;
alter table usage_events enable row level security;
alter table admin_alerts enable row level security;

comment on column wa_customers.phone is 'PII. Digits only. Removed with the customer profile (retention, erasure).';
comment on column inbound_events.event is 'PII. Normalized inbound event, includes the customer text. Removed by the retention sweep.';
comment on column message_log.body is 'PII. Customer or bot text. Removed by the retention sweep.';

notify pgrst, 'reload schema';
