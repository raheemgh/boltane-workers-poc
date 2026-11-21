"""test_schema_static - static checks of migration 001 and its rollback, and the three-way contract.

Level:   L1
Stage:   V2-01
"""

import json
import re
import unittest
from pathlib import Path

from app.v2.core import store_config

ROOT = Path(__file__).resolve().parents[3]
TABLES = {"wa_customers", "inbound_events", "chat_sessions", "message_log", "usage_events", "admin_alerts"}
TABLE = re.compile(r"create table if not exists (\w+)\s*\((.*?)\n\);", re.S)


def read_sql(rel):
    """SQL text with the -- comments removed."""
    return re.sub(r"--[^\n]*", "", (ROOT / rel).read_text(encoding="utf-8"))


class MigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = read_sql("sql/migrations/001_core_schema.sql")
        cls.rollback = read_sql("sql/rollbacks/001_core_schema.sql")
        cls.tables = {m.group(1): m.group(2) for m in TABLE.finditer(cls.sql)}

    def test_migration__creates_exactly_the_six_tables(self):
        self.assertEqual(set(self.tables), TABLES)

    def test_migration__every_statement_is_idempotent(self):
        for kind in ("create table", "create index", "create unique index", "add column"):
            total = len(re.findall(rf"\b{kind}\b", self.sql))
            safe = len(re.findall(rf"\b{kind} if not exists\b", self.sql))
            self.assertGreater(total, 0, kind)
            self.assertEqual(total, safe, kind)

    def test_migration__constraints_are_guarded(self):
        adds = len(re.findall(r"alter table stores add constraint", self.sql))
        guards = len(re.findall(r"pg_constraint\s+where conname = 'stores_", self.sql))
        self.assertEqual(adds, guards)

    def test_migration__is_additive_only(self):
        bad = re.findall(r"\b(drop|rename|truncate)\b|\bdelete\s+from\b|^\s*update\b", self.sql, re.M | re.I)
        self.assertEqual(bad, [])

    def test_migration__rls_and_store_id_on_every_new_table(self):
        for name, body in self.tables.items():
            self.assertIn(f"alter table {name} enable row level security;", self.sql, name)
            self.assertRegex(body, r"\bstore_id\s+uuid\b", name)
        self.assertNotRegex(self.sql, r"create policy")

    def test_migration__only_timestamptz_and_no_serial(self):
        self.assertNotRegex(self.sql, r"\btimestamp\b")
        self.assertNotRegex(self.sql, r"\b(serial|bigserial)\b")
        self.assertRegex(self.sql, r"\btimestamptz\b")

    def test_migration__every_new_stores_column_is_null_safe_for_v1(self):
        for line in re.findall(r"alter table stores add column if not exists [^;]+;", self.sql):
            self.assertTrue("not null" not in line or "default" in line, line)


class RollbackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = read_sql("sql/migrations/001_core_schema.sql")
        cls.rollback = read_sql("sql/rollbacks/001_core_schema.sql")

    def test_rollback__drops_exactly_what_the_migration_creates(self):
        created = set(re.findall(r"create table if not exists (\w+)", self.sql))
        dropped = re.findall(r"drop table if exists (\w+);", self.rollback)
        self.assertEqual(set(dropped), created)
        self.assertEqual(len(dropped), len(set(dropped)))
        added = set(re.findall(r"alter table stores add column if not exists (\w+)", self.sql))
        removed = set(re.findall(r"alter table stores drop column if exists (\w+);", self.rollback))
        self.assertEqual(removed, added)

    def test_rollback__children_go_before_parents(self):
        dropped = re.findall(r"drop table if exists (\w+);", self.rollback)
        for name, body in {m.group(1): m.group(2) for m in TABLE.finditer(self.sql)}.items():
            for parent in re.findall(r"references (\w+)\(", body):
                if parent in dropped:
                    self.assertLess(dropped.index(name), dropped.index(parent), f"{name} before {parent}")

    def test_rollback__touches_nothing_else(self):
        self.assertNotRegex(self.rollback, r"\bcascade\b|\btruncate\b|\bdelete\b|\bupdate\b")
        self.assertEqual(len(re.findall(r"\balter table\b", self.rollback)), 6)


class ContractTest(unittest.TestCase):
    """SQL, the JSON schema and core/store_config.py must say the same thing."""

    @classmethod
    def setUpClass(cls):
        cls.sql = read_sql("sql/migrations/001_core_schema.sql")
        text = (ROOT / "contracts" / "store_config.schema.json").read_text(encoding="utf-8")
        cls.schema = json.loads(text)["properties"]

    def test_contract__language_values(self):
        listed = re.search(r"default_language in \(([^)]*)\)", self.sql).group(1)
        in_sql = tuple(re.findall(r"'([^']+)'", listed))
        self.assertEqual(in_sql, store_config.DEFAULT_LANGUAGES)
        self.assertEqual(tuple(self.schema["default_language"]["enum"]), store_config.DEFAULT_LANGUAGES)

    def test_contract__patterns(self):
        phone = re.search(r"owner_phone ~ '([^']+)'", self.sql).group(1)
        zone = re.search(r"timezone ~ '([^']+)'", self.sql).group(1)
        self.assertEqual(phone, store_config.OWNER_PHONE_PATTERN)
        self.assertEqual(zone, store_config.TIMEZONE_PATTERN)
        self.assertEqual(self.schema["owner_phone"]["pattern"], store_config.OWNER_PHONE_PATTERN)
        self.assertEqual(self.schema["timezone"]["pattern"], store_config.TIMEZONE_PATTERN)
        self.assertEqual(self.schema["billing_anchor"]["pattern"], store_config.DATE_PATTERN)
        self.assertEqual(self.schema["timezone"]["maxLength"], 64)

    def test_contract__customer_phone_pattern_matches_owner_phone(self):
        in_sql = re.search(r"wa_customers_phone_check check \(phone is null or phone ~ '([^']+)'", self.sql).group(1)
        self.assertEqual(in_sql, store_config.OWNER_PHONE_PATTERN)

    def test_contract__business_hours_shape(self):
        hours = self.schema["business_hours"]
        self.assertEqual(tuple(hours["required"]), store_config.DAYS)
        self.assertEqual(tuple(hours["properties"]), store_config.DAYS)
        day = json.loads((ROOT / "contracts" / "store_config.schema.json").read_text())["$defs"]["day"]
        self.assertEqual(day["maxItems"], store_config.MAX_INTERVALS_PER_DAY)
        first, second = day["items"]["prefixItems"]
        self.assertEqual(first["pattern"], store_config.OPEN_TIME_PATTERN)
        self.assertEqual(second["pattern"], store_config.CLOSE_TIME_PATTERN)

    def test_contract__six_fields_everywhere(self):
        self.assertEqual(tuple(self.schema), store_config.FIELDS)
        for name in store_config.FIELDS:
            self.assertRegex(self.sql, rf"alter table stores add column if not exists {name}\b")


if __name__ == "__main__":
    unittest.main()
