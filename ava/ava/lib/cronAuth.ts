// lib/cronAuth.ts
//
// Shared auth check for the two MANUAL cron routes (GET /cron/activate-pending
// and GET /cron/sweep-pdf-scans). Same rules as always: Bearer header or
// ?secret=, fail closed if CRON_SECRET is unset. (scheduled() never goes
// through here — Cloudflare's own edge invokes it, there is no Request.)
//
// Uses Node's `timingSafeEqual` from "crypto" — available on Workers under
// the compatibility_date in wrangler.jsonc (confirmed in the migration
// audit's probe, and exercised for real under local workerd by
// scripts/cron-check.mjs: 401 for wrong secrets, 200 for the right one).
// Stage 7 deliberately did NOT swap it for Cloudflare's
// crypto.subtle.timingSafeEqual: that was optional, buys nothing here, and
// Stage 7 is the one stage that touches real users. If it is ever adopted,
// keep the length check below — the Workers version throws on mismatched
// lengths instead of returning false (confirmed in Stage 2).
import { timingSafeEqual } from "crypto";

function safeEqual(a: string, b: string): boolean {
  const ab = Buffer.from(a);
  const bb = Buffer.from(b);
  if (ab.length !== bb.length) return false;
  return timingSafeEqual(ab, bb);
}

/**
 * Check for a standard Web `Request` (what Hono hands the routes). Repeated
 * `?secret=a&secret=b` is REJECTED rather than "first value wins" — Express's
 * req.query gave an array for that, which the old check refused, and Hono's
 * own c.req.query() would silently return just the first value.
 */
export function isCronAuthorizedRequest(req: Request): boolean {
  const expected = process.env.CRON_SECRET;
  if (!expected) return false; // fail closed if unset — never run open

  const headerAuth = req.headers.get("authorization");
  if (headerAuth !== null && safeEqual(headerAuth, `Bearer ${expected}`)) {
    return true;
  }

  const secrets = new URL(req.url).searchParams.getAll("secret");
  return secrets.length === 1 && safeEqual(secrets[0]!, expected);
}
