"""test_inbox_sql_static - static checks of migration 002 and its rollback, and its contract with the Python side.

Level:   L1
Stage:   V2-02B
"""

import re
import unittest
from pathlib import Path

import v2_inbox_model as model
from app.v2.core.errors import ErrorCode
from app.v2.inbound import inbox

ROOT = Path(__file__).resolve().parents[3]
FUNCTIONS = {"inbox_accept", "inbox_claim", "inbox_finish", "inbox_claim_stale"}
DEFINITION = re.compile(r"create or replace function (\w+)\((.*?)\)\s*returns", re.S)


def read_sql(rel):
    """SQL text with the -- comments removed."""
    return re.sub(r"--[^\n]*", "", (ROOT / rel).read_text(encoding="utf-8"))


def signature(args):
    """'p_id uuid, p_lock_seconds integer default 60' -> 'uuid, integer' (what grant and revoke must repeat)."""
    return ", ".join(part.split()[1] for part in args.replace("\n", " ").split(","))


class MigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = read_sql("sql/migrations/002_inbox_functions.sql")
        cls.defs = {m.group(1): m.group(2) for m in DEFINITION.finditer(cls.sql)}

    def test_migration__defines_exactly_the_four_functions(self):
        self.assertEqual(set(self.defs), FUNCTIONS)

    def test_migration__only_create_or_replace_so_it_can_run_twice(self):
        self.assertEqual(len(re.findall(r"\bcreate\s+(?:or\s+replace\s+)?function\b", self.sql)), 4)
        self.assertEqual(len(re.findall(r"\bcreate or replace function\b", self.sql)), 4)

    def test_migration__is_additive_only(self):
        bad = re.findall(r"\b(drop|rename|truncate)\b|\balter\s+table\b|\bdelete\s+from\b", self.sql, re.I)
        self.assertEqual(bad, [])

    def test_migration__every_function_is_invoker_with_a_fixed_search_path(self):
        self.assertEqual(len(re.findall(r"\bsecurity invoker\b", self.sql)), 4)
        self.assertEqual(len(re.findall(r"set search_path = public", self.sql)), 4)
        self.assertNotRegex(self.sql, r"security definer")

    def test_migration__every_function_is_revoked_from_the_public_roles_and_granted_to_the_service_role(self):
        for name, args in self.defs.items():
            sig = f"{name}({signature(args)})"
            self.assertIn(f"revoke all on function {sig} from public, anon, authenticated;", self.sql, name)
            self.assertIn(f"grant execute on function {sig} to service_role;", self.sql, name)
        self.assertEqual(len(re.findall(r"\bgrant\b", self.sql)), 4)
        self.assertNotRegex(self.sql, r"to (public|anon|authenticated)\b")

    def test_migration__tells_postgrest_to_reload(self):
        self.assertTrue(self.sql.rstrip().endswith("notify pgrst, 'reload schema';"))

    def test_migration__reads_the_store_by_name_never_select_star(self):
        self.assertNotRegex(self.sql, r"select\s+\*\s+from\s+stores|stores\s+\w*\.\*")
        self.assertIn("select s.id, s.features into", self.sql)

    def test_migration__only_a_data_error_is_caught_per_event(self):
        self.assertIn("when data_exception or integrity_constraint_violation then", self.sql)
        self.assertNotRegex(self.sql, r"when others")

    def test_migration__only_statuses_and_error_codes_the_table_and_the_taxonomy_know(self):
        table = read_sql("sql/migrations/001_core_schema.sql")
        listed = re.search(r"inbound_events_status_check check \(status in \(([^)]*)\)", table).group(1)
        allowed = set(re.findall(r"'(\w+)'", listed))
        used = set(re.findall(r"\bstatus = '(\w+)'", self.sql))
        used |= set(re.findall(r"'(\w+)'", re.search(r"p_status not in \(([^)]*)\)", self.sql).group(1)))
        used |= set(re.findall(r"coalesce\(v_item ->> 'status', '(\w+)'\)", self.sql))
        self.assertEqual(used, {"pending", "processing", "done", "failed", "ignored"})
        self.assertLessEqual(used, allowed)
        code = re.search(r"error_code = '(\w+)'", self.sql).group(1)
        self.assertEqual(code, ErrorCode.INTERNAL_DEADLINE.value)


class RollbackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = read_sql("sql/migrations/002_inbox_functions.sql")
        cls.rollback = read_sql("sql/rollbacks/002_inbox_functions.sql")

    def test_rollback__drops_exactly_the_functions_the_migration_creates_with_the_same_signatures(self):
        created = {m.group(1): signature(m.group(2)) for m in DEFINITION.finditer(self.sql)}
        dropped = dict(re.findall(r"drop function if exists (\w+)\(([^)]*)\);", self.rollback))
        self.assertEqual(dropped, created)

    def test_rollback__touches_no_table_and_no_row(self):
        self.assertNotRegex(self.rollback, r"\bdrop table\b|\balter\b|\bdelete\b|\bupdate\b|\btruncate\b|\bcascade\b")
        self.assertTrue(self.rollback.rstrip().endswith("notify pgrst, 'reload schema';"))


class ContractTest(unittest.TestCase):
    """SQL, the reference model and inbox.py must say the same thing."""

    @classmethod
    def setUpClass(cls):
        cls.sql = read_sql("sql/migrations/002_inbox_functions.sql")

    def default(self, name):
        return re.search(rf"\b{name}\s+\w+\s+default\s+('?\w+'?)", self.sql).group(1)

    def test_contract__the_sql_defaults_equal_the_python_constants(self):
        self.assertEqual(int(self.default("p_lock_seconds")), inbox.LOCK_SECONDS)
        self.assertEqual(int(self.default("p_pending_older_than_seconds")), inbox.PENDING_OLDER_THAN)
        self.assertEqual(int(self.default("p_max_attempts")), inbox.MAX_ATTEMPTS)
        self.assertEqual(int(self.default("p_limit")), inbox.CLAIM_LIMIT)
        self.assertEqual(self.default("p_kind"), "'message'")

    def test_contract__the_model_defaults_equal_the_python_constants(self):
        import inspect

        params = inspect.signature(model.InboxModel.claim_stale).parameters
        self.assertEqual((params["limit"].default, params["pending_older"].default, params["lock_seconds"].default,
                          params["max_attempts"].default),
                         (inbox.CLAIM_LIMIT, inbox.PENDING_OLDER_THAN, inbox.LOCK_SECONDS, inbox.MAX_ATTEMPTS))

    def test_contract__the_python_side_calls_exactly_the_functions_and_parameters_the_sql_declares(self):
        source = (ROOT / "src/app/v2/inbound/inbox.py").read_text(encoding="utf-8")
        self.assertEqual(set(re.findall(r'rpc\("(\w+)"', source)), FUNCTIONS)
        declared = set(re.findall(r"\b(p_\w+)\s+(?:text|jsonb|uuid|integer)\b", self.sql))
        self.assertEqual(set(re.findall(r'"(p_\w+)"', source)), declared)

    def test_contract__the_lock_outlives_waituntil(self):
        self.assertGreater(inbox.LOCK_SECONDS, 30)  # ctx.waitUntil lives about 30 s after the response (ADR-8)


if __name__ == "__main__":
    unittest.main()
