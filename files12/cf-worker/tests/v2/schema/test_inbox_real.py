"""test_inbox_real - L3 test of migration 002 (the inbox functions) on the Supabase TEST project (psql and PostgREST).

Level:   L3
Stage:   V2-02B

Run from cf-worker/ (never against production), in the venv that has httpx:
    source ~/venv-tests/bin/activate
    python3 tests/v2/schema/test_inbox_real.py
Needs what test_schema_contract_real.py needs (psql, .dev.vars with SUPABASE_URL, V2_TEST_DB_URL and
V2_CONFIRM_TEST_PROJECT) and migration 001 already applied. The PostgREST part also needs SUPABASE_KEY (the TEST
project's service key) in .dev.vars and httpx; without them that part is skipped, not failed.
Order: rollback 002 -> apply -> structure and permissions -> apply again (idempotent) -> every shared scenario of
tooling/local-test-support/v2_inbox_model.py on the real functions (each in a transaction that is rolled back)
-> probes -> PostgREST through the real adapter -> rollback 002 -> apply (leaves TEST migrated). Result: inbox_real.json
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "sql" / "migrations" / "002_inbox_functions.sql"
ROLLBACK = ROOT / "sql" / "rollbacks" / "002_inbox_functions.sql"
RESULT = ROOT / "inbox_real.json"
FUNCTIONS = ("inbox_accept", "inbox_claim", "inbox_claim_stale", "inbox_finish")
SIGNATURES = [
    "inbox_accept(p_phone_number_id text, p_events jsonb)",
    "inbox_claim(p_id uuid, p_lock_seconds integer)",
    "inbox_claim_stale(p_kind text, p_limit integer, p_pending_older_than_seconds integer, p_lock_seconds integer, p_max_attempts integer)",
    "inbox_finish(p_id uuid, p_status text, p_error_code text)",
]
A = "'00000000-0000-4000-8000-0000000002a1'"
B = "'00000000-0000-4000-8000-0000000002b2'"
C = "'00000000-0000-4000-8000-0000000002c3'"
STORE_FOR = {"pn-on": "v2probe_on", "pn-off": "v2probe_off", "pn-none": "v2probe_none"}
FIXTURE = (
    "begin;\n"
    f"insert into stores (id, phone_number_id, store_name, features) values ({A}, 'v2probe_on', 'probe on', '{{}}'), "
    f"({B}, 'v2probe_off', 'probe off', '{{\"v2_pipeline\": false}}');\n"
)
ARGUMENT_NAMES = {"limit": "p_limit", "pending_older": "p_pending_older_than_seconds",
                  "lock_seconds": "p_lock_seconds", "max_attempts": "p_max_attempts"}
IN_FUNCTIONS = ", ".join(f"'{name}'" for name in FUNCTIONS)
Q_SIGNATURES = ("select p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')' from pg_proc p "
                f"where p.pronamespace = 'public'::regnamespace and p.proname in ({IN_FUNCTIONS});")
Q_DEFINITIONS = ("select p.proname || ':' || md5(pg_get_functiondef(p.oid)) from pg_proc p "
                 f"where p.pronamespace = 'public'::regnamespace and p.proname in ({IN_FUNCTIONS});")
Q_INVOKER = ("select count(*) from pg_proc p where p.pronamespace = 'public'::regnamespace "
             f"and p.proname in ({IN_FUNCTIONS}) and not p.prosecdef and p.proconfig::text like '%search_path=public%';")
Q_PRIVILEGES = ("select p.proname || ':' || has_function_privilege('anon', p.oid, 'execute') || ':' || "
                "has_function_privilege('authenticated', p.oid, 'execute') || ':' || "
                "has_function_privilege('service_role', p.oid, 'execute') from pg_proc p "
                f"where p.pronamespace = 'public'::regnamespace and p.proname in ({IN_FUNCTIONS});")
Q_GONE = f"select count(*) from pg_proc p where p.pronamespace = 'public'::regnamespace and p.proname in ({IN_FUNCTIONS});"
Q_INBOX_TABLE = "select count(*) from information_schema.tables where table_schema = 'public' and table_name = 'inbound_events';"
Q_RESIDUE = "select count(*) from stores where phone_number_id like 'v2probe%';"
EVENT = ('[{"wamid": "p1", "kind": "message", "from_phone": "963999000111", "from_bsuid": null, '
         '"status": "pending", "event": {"v": 1}}]')


def load_sibling():
    """The V2-01 L3 test, as a module: its psql runner, guard and reporting are reused as they are."""
    path = Path(__file__).with_name("test_schema_contract_real.py")
    spec = importlib.util.spec_from_file_location("schema_contract_real", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def event_id(wamid):
    return f"(select id from inbound_events where store_id = {A} and wamid = '{wamid}')"


def sql_for(index, op, args):
    """The SQL of one scenario step. Every step that answers prints 'S<index>|<answer>'."""
    tag = f"'S{index}|' || "
    if op == "accept":
        events = json.dumps(args["events"]).replace("'", "''")
        return f"select {tag}inbox_accept('{STORE_FOR[args['pn']]}', '{events}'::jsonb)::text;"
    if op == "claim":
        return f"select {tag}inbox_claim({event_id(args['wamid'])}, {args['lock']});"
    if op == "finish":
        error = "null" if args["error"] is None else f"'{args['error']}'"
        return f"select {tag}inbox_finish({event_id(args['wamid'])}, '{args['status']}', {error});"
    if op == "claim_stale":
        named = ", ".join(f"{ARGUMENT_NAMES[key]} => {value}" for key, value in args.items())
        return f"select {tag}coalesce((select string_agg(wamid, ',' order by wamid) from inbox_claim_stale({named})), '');"
    if op == "advance":  # now() stands still inside a transaction, so time passing = moving every timestamp back
        gap = f"interval '{args['seconds']} seconds'"
        return (f"update inbound_events set received_at = received_at - {gap}, locked_until = locked_until - {gap}, "
                f"finished_at = finished_at - {gap} where store_id = {A};\nselect 'S{index}|';")
    return (f"select {tag}coalesce((select string_agg(wamid || '=' || status || ':' || attempts || ':' || "
            f"coalesce(error_code, ''), ',' order by wamid) from inbound_events where store_id = {A}), '');")


def decode(vm, op, raw):
    if op == "accept":
        return vm.norm_accept(json.loads(raw))
    if op in ("claim", "finish"):
        return raw == "true"
    if op == "claim_stale":
        return sorted(item for item in raw.split(",") if item)
    if op == "advance":
        return None
    return dict(item.partition("=")[::2] for item in raw.split(",") if item)


def run_scenario(real, run, vm, name, steps):
    script = FIXTURE + "\n".join(sql_for(i, op, args) for i, (op, args, _expected) in enumerate(steps)) + "\nrollback;\n"
    out = real.run_sql(run.db_url, script)
    if out.code != 0:
        return run.check(f"scenario {name}: the SQL ran", False, out.err)
    answers = {}
    for line in out.lines:
        tag, _, value = line.partition("|")
        if tag.startswith("S") and tag[1:].isdigit():
            answers[int(tag[1:])] = value
    counter = iter(range(len(steps)))
    problems = vm.compare(steps, lambda op, args: decode(vm, op, answers.get(next(counter), "")))
    return run.check(f"scenario {name}: the real functions give what the model gives", not problems, str(problems)[:300])


def check_structure(real, run, label):
    run.check(f"{label}: the four functions with these exact signatures", run.query(Q_SIGNATURES) == sorted(SIGNATURES))
    run.check(f"{label}: all security invoker with search_path = public", run.query(Q_INVOKER) == ["4"])
    expected = sorted(f"{name}:false:false:true" for name in FUNCTIONS)
    run.check(f"{label}: anon and authenticated cannot execute, service_role can", run.query(Q_PRIVILEGES) == expected)


def probe(run, real, name, sql, state=None, lines=None):
    out = real.run_sql(run.db_url, FIXTURE + sql + "\nrollback;\n")
    ok = (out.state.startswith(state) if state else out.code == 0) and (lines is None or out.lines == lines)
    return run.check("probe: " + name, ok, f"state={out.state!r} lines={out.lines} {out.err[:200]}")


def run_probes(real, run):
    ev = EVENT.replace("'", "''")
    store_c = f"insert into stores (id, phone_number_id, store_name) values ({C}, 'v2probe_c', 'probe c');\n"
    probe(run, real, "finish with a wrong status raises 22023", "select inbox_finish(gen_random_uuid(), 'pending');", "22023")
    probe(run, real, "accept with an object instead of a list is a class 22 error",
          "select inbox_accept('v2probe_on', '{\"a\": 1}'::jsonb);", "22")
    probe(run, real, "the same wamid in two stores is two different events", store_c +
          f"select (inbox_accept('v2probe_on', '{ev}'::jsonb) -> 'results' -> 0 ->> 'inserted') || ':' || "
          f"(inbox_accept('v2probe_c', '{ev}'::jsonb) -> 'results' -> 0 ->> 'inserted');", None, ["true:true"])
    probe(run, real, "a processing row with no lock counts as expired",
          f"select inbox_accept('v2probe_on', '{ev}'::jsonb) is not null;\n"
          f"update inbound_events set status = 'processing', locked_until = null where store_id = {A};\n"
          f"select inbox_claim((select id from inbound_events where store_id = {A}), 60);", None, ["t", "t"])
    probe(run, real, "service_role (RLS on, no policy) can accept",
          f"set local role service_role;\nselect inbox_accept('v2probe_on', '{ev}'::jsonb) ->> 'routed';", None, ["true"])
    for role in ("anon", "authenticated"):
        out = real.run_sql(run.db_url, f"begin;\nset local role {role};\nselect 'ROLE-OK';\n"
                           "select inbox_claim(gen_random_uuid(), 60);\nrollback;\n")
        run.check(f"probe: {role} is refused (42501)", out.lines == ["ROLE-OK"] and out.state == "42501", f"{out.lines} {out.state}")


def rpc_error(db, name, params):
    from app.v2.core.errors import V2Error

    try:
        asyncio.run(db.rpc(name, params))
    except V2Error as err:
        return err
    return None


def smoke(real, run, values, vm):
    """The same functions over HTTPS, through the real adapter: URL, headers, answers and error mapping."""
    try:
        import httpx  # noqa: F401
    except ImportError:
        return sys.stdout.write("SKIP PostgREST part: httpx is not installed (use ~/venv-tests)\n")
    if not values.get("SUPABASE_KEY"):
        return sys.stdout.write("SKIP PostgREST part: SUPABASE_KEY (the TEST service key) is not in .dev.vars\n")
    from app.v2.adapters.supabase_rest import SupabaseRest
    from app.v2.core.errors import ErrorCode

    db = SupabaseRest(values["SUPABASE_URL"], values["SUPABASE_KEY"])
    real.run_sql(run.db_url, f"delete from stores where phone_number_id = 'v2probe_http';\n"
                 f"insert into stores (id, phone_number_id, store_name) values ({C}, 'v2probe_http', 'probe http');")
    try:
        params = {"p_phone_number_id": "v2probe_http", "p_events": [vm.msg("h1"), vm.msg("h2")]}
        first = asyncio.run(db.rpc("inbox_accept", params))
        again = asyncio.run(db.rpc("inbox_accept", params))
        run.check("http: accept stores both events", vm.norm_accept(first) == {"found": True, "routed": True,
                  "inserted": [True, True], "errors": [None, None]}, str(first)[:200])
        run.check("http: the same delivery again is all duplicates", vm.norm_accept(again)["inserted"] == [False, False])
        event = first["results"][0]["id"]
        run.check("http: claim is true once", asyncio.run(db.rpc("inbox_claim", {"p_id": event})) is True
                  and asyncio.run(db.rpc("inbox_claim", {"p_id": event})) is False)
        run.check("http: finish closes it", asyncio.run(db.rpc("inbox_finish", {"p_id": event, "p_status": "done"})) is True)
        run.check("http: claim_stale answers a list", isinstance(asyncio.run(db.rpc("inbox_claim_stale", {})), list))
        bad = rpc_error(db, "inbox_finish", {"p_id": event, "p_status": "pending"})
        run.check("http: a wrong status is DB_CONSTRAINT with SQLSTATE 22023",
                  bad is not None and bad.code is ErrorCode.DB_CONSTRAINT and bad.context.get("sqlstate") == "22023", repr(bad))
        wrong_key = rpc_error(SupabaseRest(values["SUPABASE_URL"], "not-a-key"), "inbox_claim_stale", {})
        run.check("http: a wrong key is DB_UNEXPECTED, not a retryable outage",
                  wrong_key is not None and wrong_key.code is ErrorCode.DB_UNEXPECTED, repr(wrong_key))
        missing = rpc_error(db, "inbox_does_not_exist", {})
        run.check("http: a missing function is DB_UNEXPECTED", missing is not None and missing.code is ErrorCode.DB_UNEXPECTED, repr(missing))
    finally:
        real.run_sql(run.db_url, "delete from stores where phone_number_id = 'v2probe_http';")


def main():
    real = load_sibling()
    values = real.load_settings()
    run = real.Run(real.guard(values))
    sys.path[:0] = [str(ROOT / "tooling" / "local-test-support"), str(ROOT / "src")]
    import v2_inbox_model as vm

    if not run.check("precondition: migration 001 is applied (run test_schema_contract_real.py first)",
                     run.query(Q_INBOX_TABLE) == ["1"]):
        return 1
    run.apply("clean slate", ROLLBACK)
    run.apply("first apply", MIGRATION)
    check_structure(real, run, "after apply")
    before = run.query(Q_DEFINITIONS) + run.query(Q_PRIVILEGES)
    run.apply("second apply", MIGRATION)
    run.check("second apply changed nothing (definitions, privileges)", before == run.query(Q_DEFINITIONS) + run.query(Q_PRIVILEGES) and before != [])
    for name, steps in vm.SCENARIOS.items():
        run_scenario(real, run, vm, name, steps)
    run_probes(real, run)
    smoke(real, run, values, vm)
    run.apply("rollback", ROLLBACK)
    run.check("rollback: the functions are gone and inbound_events is untouched",
              run.query(Q_GONE) == ["0"] and run.query(Q_INBOX_TABLE) == ["1"])
    run.apply("final apply", MIGRATION)
    check_structure(real, run, "final state")
    run.check("no probe rows are left in stores", run.query(Q_RESIDUE) == ["0"])
    failed = [row for row in run.rows if not row["ok"]]
    RESULT.write_text(json.dumps({"rows": run.rows}, indent=2), encoding="utf-8")
    sys.stdout.write(f"summary: passed={len(run.rows) - len(failed)} failed={len(failed)}  (wrote {RESULT.name})\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
