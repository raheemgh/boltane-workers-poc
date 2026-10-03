// src/setupLock.ts
//
// SetupLock — replaces api/complete-setup.ts's old module-level
// `inFlightSetups` Set. One Durable Object instance per contact_number
// (env.LOCK.idFromName(contactNumber)): Cloudflare guarantees every
// request for the same name, from anywhere, reaches the SAME
// single-threaded instance, so a plain in-memory boolean here is
// exactly as safe as the Set always was — just scoped to "this one
// contact_number" instead of "this one process".
//
// Copied from the audit's PoC (ava-lock-poc/src/index.ts) with ONE
// deliberate change: the PoC held the lock across an artificial 500ms
// sleep; this holds it across the REAL work (finishSetup(): Supabase
// lookup + update + OTP handoff), which is the window the original Set
// protected (Stage 3 plan, "Option A").
//
// The check-and-set below must stay synchronous — NO `await` between
// `if (this.locked)` and `this.locked = true`. That is the entire
// correctness argument, and the negative control in
// scripts/concurrency-test.mjs exists to prove the test notices when it
// is broken.
import { finishSetup, type SetupInput } from "../api/complete-setup";

export class SetupLock {
  private locked = false;

  // No persistent storage needed: the lock is request-scoped and
  // released in `finally`. If the instance is ever evicted mid-request
  // (e.g. a deploy), the DB status check in finishSetup() (must be
  // 'chat_complete') is the second line of defence against a re-run.
  constructor(_state: unknown, _env: unknown) {}

  async fetch(request: Request): Promise<Response> {
    if (this.locked) {
      return Response.json(
        { error: "Setup for this contact_number is already being processed." },
        { status: 409 }
      );
    }
    this.locked = true;

    try {
      const input = (await request.json()) as SetupInput;
      const result = await finishSetup(input);
      return Response.json(result.body, { status: result.status });
    } finally {
      this.locked = false;
    }
  }
}
