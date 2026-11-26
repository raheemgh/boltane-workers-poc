-- Boltane V2 - rollback of migration 002. Safe to run more than once.
-- Drops the four inbox functions only. No table, column or row is touched (the events stay in inbound_events).

drop function if exists inbox_claim_stale(text, integer, integer, integer, integer);
drop function if exists inbox_finish(uuid, text, text);
drop function if exists inbox_claim(uuid, integer);
drop function if exists inbox_accept(text, jsonb);

notify pgrst, 'reload schema';
