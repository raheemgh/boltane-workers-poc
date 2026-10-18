"""
LOCAL TEST-ONLY in-memory stand-in for the slice of Supabase/PostgREST
that supabase_client.py uses. Lives next to workers.py in
tooling/local-test-support/ — never vendored, never deployed.

Two faces over one set of tables:
  * MemoryDB.client()  -> an object shaped like supabase-py's AsyncClient
        (.table(name).select/.eq/.limit/.upsert/.update/.execute, where
        execute() is awaitable), recording every call in MemoryDB.log so
        tests can assert the EXACT queries the module builds.
  * MemoryDB's own async methods (insert_store, get_window, ...) ->
        the "independent verifier" interface that
        app/routers/debug_supabase.py scenarios use. The real runs use
        HttpDB (plain PostgREST over httpx) for the same interface.

Emulated PostgREST/Postgres behaviour (only what the module needs):
  - eq filters, limit, select("*") returning every column
  - upsert(on_conflict="a,b"): insert, or on conflict update ONLY the
    columns present in the payload (so an omitted token_count survives)
  - update(...).eq(...): updates every matching row; zero matches is
    NOT an error
  - uuid-typed columns (stores.id, conversation_windows.store_id) reject
    malformed values with code 22P02; a window for an unknown store is
    rejected with code 23503 (foreign key)
  - `timestamp` columns come back timezone-NAIVE, like the real column
  - deleting a store cascades to its conversation_windows

This describes DOCUMENTED behaviour; it cannot prove how a real
Supabase project behaves. That is what the real-project test is for.
"""

import copy
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace


class APIError(Exception):
    """Same class NAME and .code attribute as postgrest.exceptions.APIError."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_STORE_DEFAULTS = {
    "phone_number_id": None, "store_name": None, "system_prompt": None, "ai_model": None,
    "api_key": None, "access_token": None, "package": "basic", "is_api_free": True,
    "is_trial": True, "monthly_conversation_count": 0, "conversations_remaining": None,
    "last_used_model": None, "usage_reset_at": None,
}
_UUID_COLUMNS = {("stores", "id"), ("conversation_windows", "store_id")}


def _check_uuid(table: str, column: str, value) -> None:
    if (table, column) in _UUID_COLUMNS:
        try:
            uuid.UUID(str(value))
        except ValueError:
            raise APIError("22P02", f'invalid input syntax for type uuid: "{value}"') from None


def _naive_iso(value: str) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.isoformat()


class MemoryDB:
    def __init__(self) -> None:
        self.stores: list[dict] = []
        self.windows: list[dict] = []
        self.log: list[tuple] = []  # (table, operation, details)

    # ------------------------------------------------------------------
    # supabase-py-shaped face
    # ------------------------------------------------------------------
    def client(self) -> "_FakeAsyncClient":
        return _FakeAsyncClient(self)

    def _rows(self, table: str) -> list[dict]:
        return self.stores if table == "stores" else self.windows

    # ------------------------------------------------------------------
    # verifier face (same interface as debug_supabase.HttpDB)
    # ------------------------------------------------------------------
    async def insert_store(self, **fields) -> str:
        row = {"id": str(uuid.uuid4()), **copy.deepcopy(_STORE_DEFAULTS), **fields,
               "created_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()}
        self.stores.append(row)
        return row["id"]

    async def get_store(self, store_id: str) -> dict | None:
        return next((copy.deepcopy(r) for r in self.stores if r["id"] == store_id), None)

    async def insert_window(self, store_id: str, phone: str, token_count: int) -> None:
        _check_uuid("conversation_windows", "store_id", store_id)
        if not any(r["id"] == store_id for r in self.stores):
            raise APIError("23503", "violates foreign key constraint")
        self.windows.append({"store_id": store_id, "customer_phone": phone, "token_count": token_count,
                             "last_message_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()})

    async def get_window(self, store_id: str, phone: str) -> dict | None:
        return next((copy.deepcopy(w) for w in self.windows
                     if w["store_id"] == store_id and w["customer_phone"] == phone), None)

    async def count_windows(self, store_id: str, phone: str) -> int:
        return sum(1 for w in self.windows if w["store_id"] == store_id and w["customer_phone"] == phone)

    async def patch_window(self, store_id: str, phone: str, **fields) -> None:
        for w in self.windows:
            if w["store_id"] == store_id and w["customer_phone"] == phone:
                w.update(fields)

    async def delete_store(self, store_id: str) -> None:
        self.stores = [r for r in self.stores if r["id"] != store_id]
        self.windows = [w for w in self.windows if w["store_id"] != store_id]


class _FakeAsyncClient:
    def __init__(self, db: MemoryDB) -> None:
        self._db = db

    def table(self, name: str) -> "_Builder":
        assert name in ("stores", "conversation_windows"), f"unexpected table {name!r}"
        return _Builder(self._db, name)


class _Builder:
    def __init__(self, db: MemoryDB, table: str) -> None:
        self._db, self._table = db, table
        self._mode = None
        self._columns = None
        self._filters: list[tuple] = []
        self._limit = None
        self._payload = None
        self._on_conflict = None

    def select(self, columns="*"):
        self._mode, self._columns = "select", columns
        return self

    def eq(self, column, value):
        self._filters.append((column, value))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def upsert(self, payload, on_conflict=None, **_kw):
        self._mode, self._payload, self._on_conflict = "upsert", dict(payload), on_conflict
        return self

    def update(self, payload):
        self._mode, self._payload = "update", dict(payload)
        return self

    def _matching(self):
        for column, value in self._filters:
            _check_uuid(self._table, column, value)
        return [r for r in self._db._rows(self._table)
                if all(str(r.get(c)) == str(v) for c, v in self._filters)]

    async def execute(self):
        db, table = self._db, self._table
        if self._mode == "select":
            db.log.append((table, "select", {"columns": self._columns, "filters": list(self._filters), "limit": self._limit}))
            rows = self._matching()[: self._limit] if self._limit else self._matching()
            return SimpleNamespace(data=copy.deepcopy(rows))

        if self._mode == "update":
            db.log.append((table, "update", {"payload": dict(self._payload), "filters": list(self._filters)}))
            matches = self._matching()
            for row in matches:
                row.update(self._payload)
            return SimpleNamespace(data=copy.deepcopy(matches))

        if self._mode == "upsert":
            db.log.append((table, "upsert", {"payload": dict(self._payload), "on_conflict": self._on_conflict}))
            assert table == "conversation_windows", "upsert only emulated for conversation_windows"
            payload = dict(self._payload)
            _check_uuid(table, "store_id", payload["store_id"])
            if not any(r["id"] == payload["store_id"] for r in db.stores):
                raise APIError("23503", "insert or update violates foreign key constraint")
            if "last_message_at" in payload:
                payload["last_message_at"] = _naive_iso(payload["last_message_at"])
            keys = [c.strip() for c in (self._on_conflict or "").split(",") if c.strip()]
            existing = next((w for w in db.windows if all(w[k] == payload[k] for k in keys)), None)
            if existing is not None:
                existing.update(payload)  # only the columns present in the payload
                return SimpleNamespace(data=[copy.deepcopy(existing)])
            row = {"token_count": 0, **payload}
            db.windows.append(row)
            return SimpleNamespace(data=[copy.deepcopy(row)])

        raise AssertionError("execute() before select/update/upsert")
