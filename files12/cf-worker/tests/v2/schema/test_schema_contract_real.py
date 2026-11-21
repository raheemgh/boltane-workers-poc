"""test_schema_contract_real - L3 contract test of migration 001 on the Supabase TEST project (psql).

Level:   L3
Stage:   V2-01

Run from cf-worker/ (never against production):  python3 tests/v2/schema/test_schema_contract_real.py
Needs psql, SUPABASE_URL in .dev.vars, and V2_TEST_DB_URL + V2_CONFIRM_TEST_PROJECT (.dev.vars or env).
Order: rollback (clean slate) -> apply -> structure -> apply again (idempotent) -> constraint probes
-> rollback (V1 data and columns unchanged) -> apply (leaves the TEST project migrated).
Every probe runs inside a transaction that is rolled back. Result file: schema_contract_real.json
"""

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "sql" / "migrations" / "001_core_schema.sql"
ROLLBACK = ROOT / "sql" / "rollbacks" / "001_core_schema.sql"
RESULT = ROOT / "schema_contract_real.json"
TABLES = ("wa_customers", "inbound_events", "chat_sessions", "message_log", "usage_events", "admin_alerts")
COLUMNS = ("features", "timezone", "business_hours", "default_language", "billing_anchor", "owner_phone")
EXPECTED_COLUMNS = ["billing_anchor:date:YES", "business_hours:jsonb:YES", "default_language:text:NO",
                    "features:jsonb:NO", "owner_phone:text:YES", "timezone:text:YES"]
PRELUDE = "\\set ON_ERROR_STOP on\n\\set VERBOSITY verbose\nset client_min_messages to warning;\n"


def _in(names):
    return ", ".join("'" + name + "'" for name in names)


T_IN, C_IN = _in(TABLES), _in(COLUMNS)
INFO = "from information_schema.columns where table_schema = 'public'"
Q_TABLES = f"select table_name from information_schema.tables where table_schema = 'public' and table_name in ({T_IN});"
Q_NO_RLS = (f"select relname from pg_class where relkind = 'r' and relnamespace = 'public'::regnamespace "
            f"and relname in ({T_IN}) and not relrowsecurity;")
Q_POLICIES = f"select count(*) from pg_policies where schemaname = 'public' and tablename in ({T_IN});"
Q_STORE_ID = f"select table_name {INFO} and column_name = 'store_id' and table_name in ({T_IN});"
Q_TIMESTAMPS = (f"select table_name || '.' || column_name {INFO} and data_type = 'timestamp without time zone' "
                f"and (table_name in ({T_IN}) or (table_name = 'stores' and column_name in ({C_IN})));")
Q_STORE_COLS = (f"select column_name || ':' || data_type || ':' || is_nullable {INFO} "
                f"and table_name = 'stores' and column_name in ({C_IN});")
Q_V1_COLS = (f"select column_name || ':' || data_type || ':' || is_nullable {INFO} "
             f"and table_name = 'stores' and column_name not in ({C_IN});")
Q_V1_DATA = ("select count(*) || ':' || coalesce(md5(string_agg(id::text || coalesce(phone_number_id, '') "
             "|| coalesce(store_name, ''), ',' order by id)), '') from stores;")
Q_GONE = (f"select (select count(*) from information_schema.tables where table_schema = 'public' "
          f"and table_name in ({T_IN})) || ':' || (select count(*) {INFO} and table_name = 'stores' "
          f"and column_name in ({C_IN}));")
Q_RESIDUE = "select count(*) from stores where phone_number_id like 'v2probe%';"
Q_SNAPSHOT = (
    "select md5(string_agg(x, ',' order by x)) from ("
    "select 'c:' || table_name || '.' || column_name || ':' || data_type || ':' || is_nullable || ':' "
    f"|| coalesce(column_default, '') as x {INFO} and (table_name in ({T_IN}) or "
    f"(table_name = 'stores' and column_name in ({C_IN}))) "
    "union all select 'k:' || conrelid::regclass::text || '.' || conname || ':' || pg_get_constraintdef(oid) "
    f"from pg_constraint where connamespace = 'public'::regnamespace and conrelid::regclass::text in ({T_IN}, 'stores') "
    "union all select 'i:' || indexname || ':' || indexdef from pg_indexes "
    f"where schemaname = 'public' and tablename in ({T_IN})) s;"
)

A = "'00000000-0000-4000-8000-0000000000a1'"
B = "'00000000-0000-4000-8000-0000000000b2'"
C1 = "'00000000-0000-4000-8000-0000000000c1'"
E1 = "'00000000-0000-4000-8000-0000000000e1'"
S1 = "'00000000-0000-4000-8000-0000000000f1'"
EV = "'" + '{"t": 1}' + "'"
PHONE = "'963999000111'"
FIXTURE = (
    "begin;\n"
    f"insert into stores (id, phone_number_id, store_name) values ({A}, 'v2probe_a', 'probe A'), ({B}, 'v2probe_b', 'probe B');\n"
    f"insert into wa_customers (id, store_id, phone) values ({C1}, {A}, {PHONE});\n"
    f"insert into inbound_events (id, store_id, wamid, from_phone, event) values ({E1}, {A}, 'wamid.base', {PHONE}, {EV});\n"
    f"insert into chat_sessions (id, store_id, customer_id) values ({S1}, {A}, {C1});\n"
)
COUNTS = " || ':' || ".join(f"(select count(*) from {t} where store_id = {A})" for t in TABLES)

# (name, statement, SQLSTATE, text that must appear in the error: the constraint name)
REJECTED = [
    ("inbound_events: same (store, wamid) twice",
     f"insert into inbound_events (store_id, wamid, from_phone, event) values ({A}, 'wamid.base', {PHONE}, {EV});",
     "23505", "inbound_events_store_wamid_key"),
    ("inbound_events: a message with no identity",
     f"insert into inbound_events (store_id, wamid, event) values ({A}, 'w2', {EV});",
     "23514", "inbound_events_identity_check"),
    ("inbound_events: unknown status",
     f"insert into inbound_events (store_id, wamid, from_phone, event, status) values ({A}, 'w3', {PHONE}, {EV}, 'weird');",
     "23514", "inbound_events_status_check"),
    ("inbound_events: event must be a JSON object",
     f"insert into inbound_events (store_id, wamid, from_phone, event) values ({A}, 'w4', {PHONE}, '[]');",
     "23514", "inbound_events_event_object_check"),
    ("wa_customers: no phone and no bsuid",
     f"insert into wa_customers (store_id) values ({A});", "23514", "wa_customers_identity_check"),
    ("wa_customers: phone with a plus sign",
     f"insert into wa_customers (store_id, phone) values ({A}, '+963999000222');", "23514", "wa_customers_phone_check"),
    ("wa_customers: same phone twice in one store",
     f"insert into wa_customers (store_id, phone) values ({A}, {PHONE});", "23505", "wa_customers_store_phone_uidx"),
    ("chat_sessions: negative counter",
     f"insert into chat_sessions (store_id, customer_id, message_count) values ({A}, {C1}, -1);",
     "23514", "chat_sessions_counters_check"),
    ("chat_sessions: unknown store",
     f"insert into chat_sessions (store_id, customer_id) values ('00000000-0000-4000-8000-00000000dead', {C1});",
     "23503", "chat_sessions_store_id_fkey"),
    ("message_log: unknown direction",
     f"insert into message_log (store_id, customer_id, direction) values ({A}, {C1}, 'sideways');",
     "23514", "message_log_direction_check"),
    ("message_log: same (store, direction, wamid) twice",
     f"insert into message_log (store_id, customer_id, direction, wamid) values ({A}, {C1}, 'in', 'm1'), ({A}, {C1}, 'in', 'm1');",
     "23505", "message_log_wamid_uidx"),
    ("usage_events: same (inbound event, kind) twice",
     f"insert into usage_events (store_id, inbound_event_id) values ({A}, {E1}), ({A}, {E1});",
     "23505", "usage_events_inbound_kind_uidx"),
    ("usage_events: negative tokens",
     f"insert into usage_events (store_id, prompt_tokens) values ({A}, -5);", "23514", "usage_events_nonneg_check"),
    ("admin_alerts: two open alerts with one fingerprint",
     "insert into admin_alerts (severity, code, fingerprint) values ('warning', 'X', 'fp1'), ('warning', 'X', 'fp1');",
     "23505", "admin_alerts_open_fingerprint_uidx"),
    ("admin_alerts: unknown severity",
     "insert into admin_alerts (severity, code, fingerprint) values ('loud', 'X', 'fp2');",
     "23514", "admin_alerts_severity_check"),
    ("stores: default_language fr",
     f"update stores set default_language = 'fr' where id = {A};", "23514", "stores_default_language_check"),
    ("stores: features must be an object",
     f"update stores set features = '[]'::jsonb where id = {A};", "23514", "stores_features_object_check"),
    ("stores: business_hours must be an object",
     f"update stores set business_hours = '[]'::jsonb where id = {A};", "23514", "stores_business_hours_object_check"),
    ("stores: owner_phone with a plus sign",
     f"update stores set owner_phone = '+96399' where id = {A};", "23514", "stores_owner_phone_check"),
    ("stores: timezone without a region",
     f"update stores set timezone = 'Damascus' where id = {A};", "23514", "stores_timezone_check"),
]

NEW_EVENT = (f"insert into inbound_events (store_id, wamid, from_phone, event) values ({A}, 'wamid.new', {PHONE}, {EV}) "
             "on conflict (store_id, wamid) do nothing returning id")
UPSERT = (f"insert into wa_customers (store_id, phone) values ({A}, '963999000333') "
          "on conflict (store_id, phone) where phone is not null do update set last_seen_at = now();")
ALERT = "insert into admin_alerts (severity, code, fingerprint) values ('info', 'X', 'fp3');"

# (name, statements, expected output lines): these must succeed
ACCEPTED = [
    ("wa_customers: the same phone is fine in two different stores",
     f"insert into wa_customers (store_id, phone) values ({B}, {PHONE}); select count(*) from wa_customers where phone = {PHONE};",
     ["2"]),
    ("admin_alerts: a resolved fingerprint can be opened again",
     f"{ALERT} update admin_alerts set resolved_at = now() where fingerprint = 'fp3'; {ALERT} "
     "select count(*) from admin_alerts where fingerprint = 'fp3';", ["2"]),
    ("message_log: rows with no wamid never collide",
     f"insert into message_log (store_id, customer_id, direction) values ({A}, {C1}, 'in'), ({A}, {C1}, 'in'); "
     f"select count(*) from message_log where store_id = {A} and wamid is null;", ["2"]),
    ("inbox dedupe pattern: insert ... on conflict do nothing returns 1 row, then 0",
     f"with i1 as ({NEW_EVENT}) select count(*) from i1; with i2 as ({NEW_EVENT}) select count(*) from i2;", ["1", "0"]),
    ("customer upsert pattern: on conflict (store_id, phone) where phone is not null",
     f"{UPSERT} {UPSERT} select count(*) from wa_customers where phone = '963999000333';", ["1"]),
    ("deleting a store removes every V2 row of that store",
     f"insert into message_log (store_id, customer_id, session_id, direction, wamid) values ({A}, {C1}, {S1}, 'in', 'm9'); "
     f"insert into usage_events (store_id, session_id, inbound_event_id) values ({A}, {S1}, {E1}); "
     f"insert into admin_alerts (store_id, severity, code, fingerprint) values ({A}, 'info', 'X', 'fp9'); "
     f"delete from stores where id = {A}; select {COUNTS};", ["0:0:0:0:0:0"]),
]


@dataclass
class Out:
    code: int
    lines: list
    state: str  # SQLSTATE of the first error, "" if none
    err: str


def run_sql(db_url, sql):
    cmd = ["psql", db_url, "-X", "-q", "-t", "-A", "-F", "|", "-f", "-"]
    try:
        proc = subprocess.run(cmd, input=PRELUDE + sql, capture_output=True, text=True, timeout=180, check=False)
    except subprocess.TimeoutExpired:
        return Out(124, [], "", "psql timed out")
    found = re.search(r"ERROR:\s+([0-9A-Z]{5}):", proc.stderr)
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    return Out(proc.returncode, lines, found.group(1) if found else "", proc.stderr.strip()[:500])


class Run:
    def __init__(self, db_url):
        self.db_url = db_url
        self.rows = []

    def check(self, name, ok, detail=""):
        self.rows.append({"name": name, "ok": bool(ok), "detail": detail})
        tail = f"  -> {detail}" if detail and not ok else ""
        sys.stdout.write(("PASS " if ok else "FAIL ") + name + tail + "\n")
        return bool(ok)

    def query(self, sql):
        out = run_sql(self.db_url, sql)
        if out.code != 0:
            self.check("query ran: " + sql[:50], False, out.err)
        return sorted(out.lines)

    def apply(self, label, path):
        out = run_sql(self.db_url, path.read_text(encoding="utf-8"))
        return self.check(f"{label}: {path.parent.name}/{path.name} runs without error", out.code == 0, out.err)


def load_settings():
    values = {}
    dev_vars = ROOT / ".dev.vars"
    if dev_vars.is_file():
        for raw in dev_vars.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    wanted = ("SUPABASE_URL", "V2_TEST_DB_URL", "V2_CONFIRM_TEST_PROJECT")
    values.update({key: value for key, value in os.environ.items() if key in wanted})
    return values


def guard(values):
    url, db_url = values.get("SUPABASE_URL"), values.get("V2_TEST_DB_URL")
    if not url or not db_url:
        sys.exit("SUPABASE_URL (in .dev.vars) and V2_TEST_DB_URL are required; both must be the TEST project.")
    if values.get("V2_CONFIRM_TEST_PROJECT") != url:
        sys.exit("refusing to run: V2_CONFIRM_TEST_PROJECT must equal SUPABASE_URL exactly.\n"
                 "This test drops and recreates the V2 tables; it must only ever touch a test project.")
    ref = re.match(r"https://([a-z0-9]+)\.supabase\.co", url)
    if ref is None or ref.group(1) not in db_url:
        sys.exit("refusing to run: the project ref of SUPABASE_URL is not inside V2_TEST_DB_URL.")
    if shutil.which("psql") is None:
        sys.exit("psql not found. Install it: sudo apt-get update && sudo apt-get install -y postgresql-client")
    return db_url


def check_structure(run, label):
    run.check(f"{label}: the six tables exist", run.query(Q_TABLES) == sorted(TABLES))
    run.check(f"{label}: row level security is on for all six", run.query(Q_NO_RLS) == [])
    run.check(f"{label}: no policies (service key only)", run.query(Q_POLICIES) == ["0"])
    run.check(f"{label}: store_id on every table", run.query(Q_STORE_ID) == sorted(TABLES))
    run.check(f"{label}: no timestamp without time zone", run.query(Q_TIMESTAMPS) == [])
    run.check(f"{label}: stores has the six new columns, typed as specified", run.query(Q_STORE_COLS) == EXPECTED_COLUMNS)


def run_probes(run):
    for name, statement, state, fragment in REJECTED:
        out = run_sql(run.db_url, FIXTURE + statement + "\nrollback;\n")
        ok = out.state == state and fragment in out.err
        run.check("rejected: " + name, ok, f"expected {state} {fragment}; got state={out.state!r} {out.err[:200]}")
    for name, statements, expected in ACCEPTED:
        out = run_sql(run.db_url, FIXTURE + statements + "\nrollback;\n")
        run.check("accepted: " + name, out.code == 0 and out.lines == expected,
                  f"expected {expected}; got {out.lines} {out.err[:200]}")
    run.check("probes left no rows behind", run.query(Q_RESIDUE) == ["0"])


def check_rollback(run, v1_cols, v1_data):
    run.apply("rollback", ROLLBACK)
    run.check("rollback: tables and new columns are gone", run.query(Q_GONE) == ["0:0"])
    run.check("rollback: V1 stores columns unchanged", run.query(Q_V1_COLS) == v1_cols)
    run.check("rollback: V1 stores rows unchanged", run.query(Q_V1_DATA) == v1_data)


def main():
    run = Run(guard(load_settings()))
    v1_cols, v1_data = run.query(Q_V1_COLS), run.query(Q_V1_DATA)
    if not run.check("precondition: stores table exists (apply stores_table.sql first)", len(v1_cols) > 0):
        return 1
    run.apply("clean slate", ROLLBACK)
    run.apply("first apply", MIGRATION)
    check_structure(run, "after apply")
    before = run.query(Q_SNAPSHOT)
    run.apply("second apply", MIGRATION)
    run.check("second apply changed nothing (columns, constraints, indexes)", before == run.query(Q_SNAPSHOT) and before != [])
    run_probes(run)
    check_rollback(run, v1_cols, v1_data)
    run.apply("final apply", MIGRATION)
    check_structure(run, "final state")
    failed = [row for row in run.rows if not row["ok"]]
    RESULT.write_text(json.dumps({"rows": run.rows}, indent=2), encoding="utf-8")
    sys.stdout.write(f"summary: passed={len(run.rows) - len(failed)} failed={len(failed)}  (wrote {RESULT.name})\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
