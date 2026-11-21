-- Boltane V2 - rollback of migration 001. Safe to run more than once.
-- DESTRUCTIVE for the V2 objects only: drops the six V2 tables and the six new stores columns,
-- and the data in them. Nothing that V1 uses is touched.
-- Tables go in dependency order (children before parents). Dropping a stores column also drops
-- the check constraint that belongs to it.

drop table if exists admin_alerts;
drop table if exists usage_events;
drop table if exists message_log;
drop table if exists chat_sessions;
drop table if exists inbound_events;
drop table if exists wa_customers;

alter table stores drop column if exists features;
alter table stores drop column if exists timezone;
alter table stores drop column if exists business_hours;
alter table stores drop column if exists default_language;
alter table stores drop column if exists billing_anchor;
alter table stores drop column if exists owner_phone;

notify pgrst, 'reload schema';
