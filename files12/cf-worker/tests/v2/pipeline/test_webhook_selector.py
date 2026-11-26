"""test_webhook_selector - V1's receive_message with the V2 selector: flags off is V1 unchanged, flags on stores.

Level:   L2
Stage:   V2-02B

Needs fastapi and httpx (his venv), so it is skipped where they are missing. Run from cf-worker/:
    PYTHONPATH=tooling/local-test-support:src python3 -m unittest discover -s tests/v2/pipeline
"""

import hashlib
import hmac
import json
import os
import types
import unittest

import v2_inbox_model as vm

APP_SECRET = "selector-app-secret"
PN = "100200300400"
PHONE = "963999000111"
ON = types.SimpleNamespace(V2_FLAGS="v2_pipeline", V2_ALLOWLIST=PN)
wh = None  # app.routers.webhook, imported in setUpModule
build = None


def setUpModule():
    global wh, build
    try:
        import fastapi  # noqa: F401
        import httpx  # noqa: F401
    except ImportError as exc:
        raise unittest.SkipTest("L2 needs fastapi and httpx: " + exc.name) from exc
    os.environ.update(META_VERIFY_TOKEN="selector-verify", META_APP_SECRET=APP_SECRET,
                      SUPABASE_URL="https://example.supabase.co", SUPABASE_KEY="test-key",
                      OPENROUTER_DEFAULT_KEY="test-openrouter-key")
    os.environ.pop("OPENROUTER_SOCKS5_PROXY", None)
    import workers

    if not hasattr(workers, "asgi"):  # the local stub only provides `env`
        workers.asgi = types.SimpleNamespace(fetch=None)
    if not hasattr(workers, "WorkerEntrypoint"):
        workers.WorkerEntrypoint = type("WorkerEntrypoint", (), {})
    import app.config as cfg

    cfg._settings = None
    import app.routers.webhook as webhook
    from app.v2.pipeline import build as wiring

    wh, build = webhook, wiring


def sign(raw, secret=APP_SECRET):
    return "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def meta_payload(wamid="wamid.L2", body="hello there", pn=PN):
    value = {"messaging_product": "whatsapp", "metadata": {"display_phone_number": "15550000000", "phone_number_id": pn},
             "contacts": [{"profile": {"name": "Sara"}, "wa_id": PHONE}],
             "messages": [{"from": PHONE, "id": wamid, "timestamp": "1760000000", "type": "text", "text": {"body": body}}]}
    return {"object": "whatsapp_business_account", "entry": [{"id": "0", "changes": [{"field": "messages", "value": value}]}]}


class FakeRequest:
    def __init__(self, raw, headers, scope):
        self._raw, self.headers, self.scope = raw, headers, scope

    async def body(self):
        return self._raw

    async def json(self):
        return json.loads(self._raw)


class FakeBackgroundTasks:
    def __init__(self):
        self.added = []

    def add_task(self, fn, *args):
        self.added.append((fn, args))


class SelectorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ran = []  # what V1's pipeline was asked to process

        async def fake_process_message(payload):
            self.ran.append(payload)

        self.saved = (wh._process_message, wh._v2_route_delivery, build.db_from)
        wh._process_message = fake_process_message
        self.db = vm.FakeDb(vm.InboxModel({PN: {"id": "s1", "features": {}}}))
        build.db_from = lambda env: self.db

    def tearDown(self):
        wh._process_message, wh._v2_route_delivery, build.db_from = self.saved

    async def deliver(self, body, env=None, good_signature=True):
        raw = json.dumps(body).encode("utf-8")
        headers = {"x-hub-signature-256": sign(raw if good_signature else b"other")}
        background = FakeBackgroundTasks()
        response = await wh.receive_message(FakeRequest(raw, headers, {} if env is None else {"env": env}), background)
        for fn, args in background.added:
            await fn(*args)  # Starlette runs these after the response
        return response

    async def test_selector__with_every_flag_off_v1_runs_exactly_as_before(self):
        for env in (None, types.SimpleNamespace(), types.SimpleNamespace(V2_ALLOWLIST=PN)):
            self.ran.clear()
            self.assertEqual(await self.deliver(meta_payload(), env), {"status": "received"})
            self.assertEqual(self.ran, [meta_payload()])
        self.assertEqual(self.db.calls, [])

    async def test_selector__with_the_flag_on_v2_stores_the_delivery_and_v1_does_not_run(self):
        response = await self.deliver(meta_payload(), ON)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), {"status": "accepted", "inserted": 1, "duplicates": 0})
        self.assertEqual(self.ran, [])
        self.assertEqual([r["wamid"] for r in self.db.model.rows], ["wamid.L2"])

    async def test_selector__a_number_that_is_not_allowed_goes_to_v1(self):
        self.assertEqual(await self.deliver(meta_payload(pn="100200300999"), ON), {"status": "received"})
        self.assertEqual(len(self.ran), 1)
        self.assertEqual(self.db.calls, [])

    async def test_selector__a_store_that_switched_v2_off_goes_to_v1(self):
        self.db.model.stores[PN]["features"] = {"v2_pipeline": False}
        self.assertEqual(await self.deliver(meta_payload(), ON), {"status": "received"})
        self.assertEqual(len(self.ran), 1)

    async def test_selector__a_database_outage_answers_503_so_meta_retries(self):
        from app.v2.core.errors import ErrorCode, V2Error

        self.db.fail_with = V2Error(ErrorCode.DB_UNAVAILABLE)
        response = await self.deliver(meta_payload(), ON)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body), {"status": "retry", "code": "DB_UNAVAILABLE"})
        self.assertEqual(self.ran, [])

    async def test_selector__a_failing_selector_leaves_v1_to_carry_on(self):
        async def broken(payload, env, ctx):
            raise RuntimeError("bug in V2")

        wh._v2_route_delivery = broken
        with self.assertLogs("boltane.webhook", "ERROR"):
            self.assertEqual(await self.deliver(meta_payload(), ON), {"status": "received"})
        self.assertEqual(len(self.ran), 1)

    async def test_selector__if_v2_could_not_be_loaded_v1_runs_as_before(self):
        wh._v2_route_delivery = None
        self.assertEqual(await self.deliver(meta_payload(), ON), {"status": "received"})
        self.assertEqual(len(self.ran), 1)

    async def test_selector__a_bad_signature_is_rejected_before_the_selector_is_asked(self):
        asked = []

        async def recorder(payload, env, ctx):
            asked.append(1)

        wh._v2_route_delivery = recorder
        response = await self.deliver(meta_payload(), ON, good_signature=False)
        self.assertEqual(response.status_code, 403)
        self.assertEqual((asked, self.ran, self.db.calls), ([], [], []))

    async def test_selector__it_gets_the_payload_the_env_and_the_workers_ctx(self):
        from app.worker_ctx import EnvWithCtx

        seen = []

        async def recorder(payload, env, ctx):
            seen.append((payload, env, ctx))

        class Ctx:
            def waitUntil(self, task):
                self.task = task

        wh._v2_route_delivery = recorder
        ctx, env = Ctx(), EnvWithCtx(types.SimpleNamespace(), None)
        env.worker_ctx = ctx
        await self.deliver(meta_payload(), env)
        self.assertEqual(seen, [(meta_payload(), env, ctx)])
        await ctx.task  # V1 handed its pipeline to waitUntil, as before
        self.assertEqual(len(self.ran), 1)

    async def test_selector__a_delivery_that_v2_takes_is_never_written_to_the_v1_payload_log(self):
        with self.assertLogs("boltane.webhook", "INFO") as logged:
            await self.deliver(meta_payload(body="secret customer words"), ON)
        shown = "\n".join(logged.output)
        self.assertIn("v2 accepted", shown)
        self.assertNotIn("received webhook payload", shown)
        self.assertNotIn("secret customer words", shown)


if __name__ == "__main__":
    unittest.main()
