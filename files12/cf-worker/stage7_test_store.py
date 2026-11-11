#!/usr/bin/env python3
"""
Stage 7 — manages ONE temporary row in Supabase: the test store `stage6-test-pn` (standard library only).

    python stage7_test_store.py status
    python stage7_test_store.py create
    python stage7_test_store.py delete --yes

Why it exists: the test store row is gone from the Supabase project, and test_webhook_timing.py /
test_stage6_deployed.py need it. This does what stage6-runbook.md step 3 did with a hand-typed SQL insert
(and what the final cleanup does with the delete statement).

What it can and cannot do
  * It can only ever touch rows whose phone_number_id is exactly 'stage6-test-pn'. The value is a constant in this
    file, not read from settings, and every request filters on it.
  * create  inserts the row only if it does not exist (never overwrites). The access token is the deliberately
            fake 'stage6-fake-token-phase-a': Meta rejects it (401), so no real WhatsApp message can be sent.
            ai_model is left empty (the default model chain applies), package 'pro', is_trial false, is_api_free true.
  * delete  needs --yes, refuses unless exactly one matching row exists, deletes it (its conversation_windows rows
            go with it through the foreign key's ON DELETE CASCADE) and then checks nothing is left behind.
  * status  read-only.
It never prints SUPABASE_KEY or the access token. Settings: cf-worker/.dev.vars and/or the environment
(SUPABASE_URL, SUPABASE_KEY).
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEV_VARS = HERE / ".dev.vars"
TEST_PNID = "stage6-test-pn"          # the ONLY row this script may touch
FAKE_TOKEN = "stage6-fake-token-phase-a"
SYSTEM_PROMPT = (
    "You are TGF Test, the assistant for TGF Test. You provide information about the menu, including quick "
    "sandwiches like beef and chicken burgers, grilled sandwiches, skewers, and side dishes. You assist customers "
    "with their orders and inquiries in a professional manner."
)
HIDDEN_COLUMNS = {"access_token", "api_key"}
SHOW_COLUMNS = "id,phone_number_id,store_name,ai_model,package,is_trial,is_api_free,monthly_conversation_count,conversations_remaining"


def load_settings() -> dict:
    values: dict = {}
    if DEV_VARS.exists():
        for raw in DEV_VARS.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("SUPABASE_URL", "SUPABASE_KEY"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


class Api:
    def __init__(self, url: str, key: str) -> None:
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
                        "Accept": "application/json", "User-Agent": "boltane-stage7-store/1.0"}

    def call(self, method: str, table: str, params: dict | None = None, body=None, want_rows: bool = False):
        url = f"{self.base}/{table}" + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = dict(self.headers)
        if want_rows:
            headers["Prefer"] = "return=representation"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                text = response.read().decode("utf-8", "replace")
                return json.loads(text) if text.strip() else []
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"Supabase {method} {table} -> HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}")
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"Supabase {method} {table} failed: {type(exc).__name__}: {str(exc)[:150]}")

    def rows(self) -> list:
        return self.call("GET", "stores", {"phone_number_id": f"eq.{TEST_PNID}", "select": SHOW_COLUMNS})


def show(rows: list) -> None:
    if not rows:
        print(f"no row with phone_number_id={TEST_PNID}")
    for row in rows:
        for field, value in row.items():
            if field in HIDDEN_COLUMNS:     # never print secrets, even if the server returned them
                continue
            print(f"  {field}: {value!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the temporary test store row (see the module docstring)")
    parser.add_argument("command", choices=("status", "create", "delete"))
    parser.add_argument("--yes", action="store_true", help="required for delete")
    args = parser.parse_args()

    settings = load_settings()
    if not settings.get("SUPABASE_URL") or not settings.get("SUPABASE_KEY"):
        print("SUPABASE_URL / SUPABASE_KEY not found in cf-worker/.dev.vars or the environment")
        return 2
    api = Api(settings["SUPABASE_URL"], settings["SUPABASE_KEY"])
    print(f"project: {settings['SUPABASE_URL']}   row: phone_number_id = {TEST_PNID}")

    existing = api.rows()
    if args.command == "status":
        show(existing)
        return 0

    if args.command == "create":
        if existing:
            print("the row already exists — nothing changed:")
            show(existing)
            return 0
        created = api.call("POST", "stores", None, {
            "phone_number_id": TEST_PNID, "store_name": "stage6-test", "system_prompt": SYSTEM_PROMPT,
            "access_token": FAKE_TOKEN, "package": "pro", "is_trial": False, "is_api_free": True}, want_rows=True)
        print(f"created {len(created)} row:")
        show(api.rows())
        return 0

    # delete
    if not args.yes:
        print("delete needs --yes. Current state:")
        show(existing)
        return 2
    if len(existing) != 1:
        print(f"refusing: expected exactly 1 matching row, found {len(existing)}")
        show(existing)
        return 2
    store_id = existing[0]["id"]
    deleted = api.call("DELETE", "stores", {"phone_number_id": f"eq.{TEST_PNID}"}, want_rows=True)
    print(f"deleted {len(deleted)} row")
    leftovers = api.call("GET", "conversation_windows", {"store_id": f"eq.{store_id}", "select": "customer_phone"})
    print(f"conversation_windows rows left for that store: {len(leftovers)} (expected 0)")
    print(f"stores rows left: {len(api.rows())} (expected 0)")
    return 0 if len(deleted) == 1 and not leftovers else 1


if __name__ == "__main__":
    sys.exit(main())
