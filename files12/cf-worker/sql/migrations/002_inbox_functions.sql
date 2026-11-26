-- Boltane V2 - migration 002: inbox functions (additive only).
-- Stage V2-02B. Rollback: sql/rollbacks/002_inbox_functions.sql. Needs migration 001 (inbound_events).
--
-- Rules (ADR-3, ADR-10):
--   * Only "create or replace function" and grants: running this file twice changes nothing.
--   * V1 never calls these functions. With every V2 flag false nothing here is used.
--   * security invoker, search_path = public. Supabase grants new functions to anon and authenticated by
--     default, so each function is revoked from them and granted to service_role only.
--   * The store is read by the columns needed (id, features), never "select *" (the row holds secrets).
--   * Every time is the database clock (now()), so a Worker clock can never reorder events.
--   * One bad event never costs the good ones: an event the database rejects for its data (SQLSTATE class
--     22 or 23) is reported in the result and skipped, the others are stored (ADR-10, item 6).
--
-- inbound_events.status: pending -> processing -> done | failed | ignored.

-- ---------------------------------------------------------------------
-- 1) inbox_accept: find the store, then insert each event unless (store_id, wamid) is already there.
--    p_events: [{wamid, kind, from_phone, from_bsuid, status: pending|done, event}, ...]
--    Returns {store_found, store_id, routed, results: [{wamid, inserted, id, error?}]}.
--    routed = false (nothing stored) when the store switched V2 off with features.v2_pipeline = false.
-- ---------------------------------------------------------------------
create or replace function inbox_accept(p_phone_number_id text, p_events jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
    v_store_id uuid;
    v_features jsonb;
    v_item jsonb;
    v_id uuid;
    v_results jsonb := '[]'::jsonb;
begin
    select s.id, s.features into v_store_id, v_features
      from stores s
     where s.phone_number_id = p_phone_number_id
     limit 1;

    if v_store_id is null then
        return jsonb_build_object('store_found', false, 'store_id', null::uuid, 'routed', false, 'results', v_results);
    end if;
    if (v_features -> 'v2_pipeline') = 'false'::jsonb then
        return jsonb_build_object('store_found', true, 'store_id', v_store_id, 'routed', false, 'results', v_results);
    end if;

    for v_item in select e.value from jsonb_array_elements(coalesce(p_events, '[]'::jsonb)) as e
    loop
        begin
            v_id := null;
            insert into inbound_events (store_id, wamid, kind, from_phone, from_bsuid, status, finished_at, event)
            values (v_store_id, v_item ->> 'wamid', coalesce(v_item ->> 'kind', 'message'),
                    v_item ->> 'from_phone', v_item ->> 'from_bsuid',
                    coalesce(v_item ->> 'status', 'pending'),
                    case when coalesce(v_item ->> 'status', 'pending') = 'done' then now() end,
                    v_item -> 'event')
            on conflict (store_id, wamid) do nothing
            returning id into v_id;
            v_results := v_results || jsonb_build_array(jsonb_build_object(
                'wamid', v_item ->> 'wamid', 'inserted', v_id is not null, 'id', v_id));
        exception
            when data_exception or integrity_constraint_violation then
                v_results := v_results || jsonb_build_array(jsonb_build_object(
                    'wamid', v_item ->> 'wamid', 'inserted', false, 'id', null::uuid, 'error', sqlstate));
        end;
    end loop;

    return jsonb_build_object('store_found', true, 'store_id', v_store_id, 'routed', true, 'results', v_results);
end;
$$;

-- ---------------------------------------------------------------------
-- 2) inbox_claim: pending (or processing with an expired lock) -> processing, attempts + 1.
--    True if this call got the event. The lock default (60 s) outlives ctx.waitUntil (about 30 s, ADR-8).
-- ---------------------------------------------------------------------
create or replace function inbox_claim(p_id uuid, p_lock_seconds integer default 60)
returns boolean
language plpgsql
security invoker
set search_path = public
as $$
declare
    v_count integer;
begin
    update inbound_events
       set status = 'processing',
           attempts = attempts + 1,
           locked_until = now() + make_interval(secs => greatest(p_lock_seconds, 1))
     where id = p_id
       and (status = 'pending'
            or (status = 'processing' and coalesce(locked_until, '-infinity'::timestamptz) < now()));
    get diagnostics v_count = row_count;
    return v_count = 1;
end;
$$;

-- ---------------------------------------------------------------------
-- 3) inbox_finish: processing -> done | failed | ignored. True if a row changed.
--    Any other status raises 22023 (invalid_parameter_value, a data error for the adapter).
-- ---------------------------------------------------------------------
create or replace function inbox_finish(p_id uuid, p_status text, p_error_code text default null)
returns boolean
language plpgsql
security invoker
set search_path = public
as $$
declare
    v_count integer;
begin
    if p_status is null or p_status not in ('done', 'failed', 'ignored') then
        raise exception 'inbox_finish: status must be done, failed or ignored' using errcode = '22023';
    end if;
    update inbound_events
       set status = p_status, error_code = p_error_code, finished_at = now(), locked_until = null
     where id = p_id and status = 'processing';
    get diagnostics v_count = row_count;
    return v_count = 1;
end;
$$;

-- ---------------------------------------------------------------------
-- 4) inbox_claim_stale: the reaper. Stale = pending for longer than p_pending_older_than_seconds, or
--    processing with an expired lock. A stale row already tried p_max_attempts times is failed with
--    INTERNAL_DEADLINE; the others (oldest first, at most p_limit, rows another reaper holds are skipped)
--    are claimed like inbox_claim and returned.
-- ---------------------------------------------------------------------
create or replace function inbox_claim_stale(
    p_kind text default 'message',
    p_limit integer default 10,
    p_pending_older_than_seconds integer default 45,
    p_lock_seconds integer default 60,
    p_max_attempts integer default 3)
returns setof inbound_events
language plpgsql
security invoker
set search_path = public
as $$
begin
    update inbound_events
       set status = 'failed', error_code = 'INTERNAL_DEADLINE', finished_at = now(), locked_until = null
     where kind = p_kind
       and attempts >= p_max_attempts
       and ((status = 'pending' and received_at < now() - make_interval(secs => p_pending_older_than_seconds))
            or (status = 'processing' and coalesce(locked_until, '-infinity'::timestamptz) < now()));

    return query
    with picked as (
        select e.id
          from inbound_events e
         where e.kind = p_kind
           and e.attempts < p_max_attempts
           and ((e.status = 'pending' and e.received_at < now() - make_interval(secs => p_pending_older_than_seconds))
                or (e.status = 'processing' and coalesce(e.locked_until, '-infinity'::timestamptz) < now()))
         order by e.received_at
         limit greatest(p_limit, 0)
           for update skip locked
    ), claimed as (
        update inbound_events t
           set status = 'processing',
               attempts = t.attempts + 1,
               locked_until = now() + make_interval(secs => greatest(p_lock_seconds, 1))
          from picked
         where t.id = picked.id
        returning t.*
    )
    select * from claimed;
end;
$$;

-- ---------------------------------------------------------------------
-- 5) Permissions: the service key only (the Worker). Then tell PostgREST to see the functions.
-- ---------------------------------------------------------------------
revoke all on function inbox_accept(text, jsonb) from public, anon, authenticated;
revoke all on function inbox_claim(uuid, integer) from public, anon, authenticated;
revoke all on function inbox_finish(uuid, text, text) from public, anon, authenticated;
revoke all on function inbox_claim_stale(text, integer, integer, integer, integer) from public, anon, authenticated;
grant execute on function inbox_accept(text, jsonb) to service_role;
grant execute on function inbox_claim(uuid, integer) to service_role;
grant execute on function inbox_finish(uuid, text, text) to service_role;
grant execute on function inbox_claim_stale(text, integer, integer, integer, integer) to service_role;

notify pgrst, 'reload schema';
