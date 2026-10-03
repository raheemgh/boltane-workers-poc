// src/env.ts
//
// Cloudflare bindings available to the Worker (as opposed to plain
// env vars/secrets, which Ava still reads through process.env — see
// wrangler.jsonc's compatibility_date note). Grows by stage:
// Stage 3 adds LOCK; Stage 6 adds nothing (cron triggers aren't
// bindings, they are config + a scheduled() export); Stage 7 wires
// vars/secrets separately.
//
// LOCK is typed structurally (just the two methods Ava calls) rather
// than as the global DurableObjectNamespace, on purpose: api/ handlers
// and their Vitest tests are type-checked by tsconfig.json under Node's
// globals, where Cloudflare's global types don't exist (mixing the two
// type sets project-wide breaks lib/pdfValidation.ts's Buffer typing —
// a Stage 5 file — so that's not done). The real DurableObjectNamespace
// satisfies this shape, and tsconfig.workers.json proves it when
// src/index.ts passes the real binding through.
export interface LockStub {
  fetch(request: Request): Promise<Response>;
}

export interface LockNamespace {
  idFromName(name: string): unknown;
  get(id: never): LockStub;
}

// Structural stand-ins for Cloudflare's ScheduledController / ExecutionContext,
// for the same reason LockNamespace is structural (see above): this file is
// also type-checked by tsconfig.json, where Cloudflare's globals don't exist.
// The real objects satisfy these shapes, and tsconfig.workers.json proves it
// (src/workersContract.ts assigns the real default export to ExportedHandler).
export interface CronController {
  /** The cron expression that fired — wrangler.jsonc's triggers.crons entry, verbatim. */
  cron: string;
  scheduledTime: number;
}

export interface WaitUntilContext {
  waitUntil(promise: Promise<unknown>): void;
}

export interface Env {
  /** One SetupLock Durable Object per contact_number (src/setupLock.ts). */
  LOCK: LockNamespace;
}
