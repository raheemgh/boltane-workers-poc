"""test_supabase_rest - the Supabase adapter: request shape, answers, and the error code of each kind of failure.

Level:   L1
Stage:   V2-02B
"""

import sys
import types
import unittest
from unittest import mock

from app.v2.adapters.supabase_rest import SupabaseRest, classify_failure, sqlstate_of
from app.v2.core.errors import ErrorCode, V2Error

KEY = "service-key-SECRET-123"


class HTTPError(Exception):
    pass


class Timeout(HTTPError):
    pass


class Response:
    def __init__(self, status, body=None, bad_json=False):
        self.status_code, self._body, self._bad = status, body, bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._body


def fake_httpx(outcome, seen):
    """A stand-in `httpx` whose client returns (or raises) `outcome` and records what it was asked."""
    module = types.ModuleType("httpx")
    module.HTTPError = HTTPError

    class AsyncClient:
        def __init__(self, timeout=None):
            seen["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            seen.update(url=url, headers=headers, json=json)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    module.AsyncClient = AsyncClient
    return module


class ClassifyTest(unittest.TestCase):
    def test_classify__data_errors_are_constraint_whatever_the_http_status(self):
        for status, code in ((400, "22023"), (409, "23505"), (400, "23514"), (500, "22000"), (400, "2200A")):
            self.assertIs(classify_failure(status, {"code": code}), ErrorCode.DB_CONSTRAINT, (status, code))

    def test_classify__outages_are_unavailable(self):
        for status in (500, 502, 503, 504, 408, 429):
            self.assertIs(classify_failure(status, {"message": "x"}), ErrorCode.DB_UNAVAILABLE, status)
            self.assertIs(classify_failure(status, None), ErrorCode.DB_UNAVAILABLE, status)

    def test_classify__everything_else_is_unexpected(self):
        cases = ((404, {"code": "PGRST202"}), (401, {"code": "42501"}), (400, {"code": "42883"}), (403, None),
                 (400, "text"), (400, {"code": 22023}), (400, {"code": "22"}))
        for status, body in cases:
            self.assertIs(classify_failure(status, body), ErrorCode.DB_UNEXPECTED, (status, body))

    def test_classify__sqlstate_only_from_a_five_character_code(self):
        self.assertEqual(sqlstate_of({"code": "22023"}), "22023")
        self.assertIsNone(sqlstate_of({"code": "PGRST202"}))
        self.assertIsNone(sqlstate_of([]))


class RpcTest(unittest.IsolatedAsyncioTestCase):
    async def call(self, outcome, params=None, url="https://x.supabase.co/"):
        seen = {}
        with mock.patch.dict(sys.modules, {"httpx": fake_httpx(outcome, seen)}):
            try:
                return await SupabaseRest(url, KEY).rpc("inbox_claim", params or {"p_id": "a"}), seen
            except V2Error as err:
                return err, seen

    async def test_rpc__request_shape(self):
        answer, seen = await self.call(Response(200, True), {"p_id": "abc"})
        self.assertIs(answer, True)
        self.assertEqual(seen["url"], "https://x.supabase.co/rest/v1/rpc/inbox_claim")
        self.assertEqual(seen["json"], {"p_id": "abc"})
        self.assertEqual(seen["timeout"], 5.0)
        self.assertEqual(seen["headers"]["apikey"], KEY)
        self.assertEqual(seen["headers"]["Authorization"], "Bearer " + KEY)

    async def test_rpc__answers_pass_through(self):
        for body in ({"a": 1}, [{"id": 1}], False):
            answer, _ = await self.call(Response(200, body))
            self.assertEqual(answer, body)

    async def test_rpc__no_content_and_non_json_are_none(self):
        self.assertIsNone((await self.call(Response(204, bad_json=True)))[0])
        self.assertIsNone((await self.call(Response(200, bad_json=True)))[0])

    async def test_rpc__a_refused_write_is_db_constraint_with_the_sqlstate(self):
        err, _ = await self.call(Response(400, {"code": "22023", "message": "customer text here"}))
        self.assertIs(err.code, ErrorCode.DB_CONSTRAINT)
        self.assertEqual(err.context, {"http": 400, "sqlstate": "22023"})

    async def test_rpc__outage_statuses_are_db_unavailable(self):
        err, _ = await self.call(Response(503, {"message": "down"}))
        self.assertIs(err.code, ErrorCode.DB_UNAVAILABLE)
        self.assertEqual(err.context["http"], 503)

    async def test_rpc__a_missing_function_or_a_bad_key_is_db_unexpected(self):
        for status, body in ((404, {"code": "PGRST202"}), (401, {"message": "Invalid API key"})):
            err, _ = await self.call(Response(status, body))
            self.assertIs(err.code, ErrorCode.DB_UNEXPECTED, status)

    async def test_rpc__network_failures_are_db_unavailable(self):
        err, _ = await self.call(Timeout("read timed out"))
        self.assertIs(err.code, ErrorCode.DB_UNAVAILABLE)
        self.assertEqual(err.context, {"kind": "Timeout"})
        self.assertIsInstance(err.__cause__, Timeout)

    async def test_rpc__the_key_and_the_answer_text_never_reach_the_error(self):
        err, _ = await self.call(Response(400, {"code": "22023", "message": "secret customer text"}))
        shown = str(err) + repr(err.context)
        self.assertNotIn(KEY, shown)
        self.assertNotIn("customer text", shown)


if __name__ == "__main__":
    unittest.main()
