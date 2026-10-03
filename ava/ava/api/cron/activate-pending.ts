// api/cron/activate-pending.ts
//
// For every pending_signups row Raheem has hand-set to
// 'verified_ready': insert the stores-shaped subset via toStoreRow()
// (lib/storeRow.ts) — deliberately NOT re-deriving package/is_api_free/
// api_key/ai_model, those were finalized once at signup time — then
// mark the staging row 'activated' so it's never picked up again.
//
// TWO TRIGGER PATHS, ONE IMPLEMENTATION (Stage 6):
//   - scheduled(): Cloudflare's native Cron Trigger (src/cron.ts) —
//     the primary, going-forward mechanism. There is no incoming
//     Request in that call, so there is nothing for cronAuth to check:
//     Cloudflare's own edge invokes it, not public HTTP.
//   - GET /cron/activate-pending: kept for manual/ad hoc runs ("run it
//     right now") and as an ops fallback, still protected by
//     CRON_SECRET (?secret=... or Authorization: Bearer ...).
// Both call runActivatePending() below, so the logic exists once.
import type { Context } from "hono";
import { isCronAuthorizedRequest } from "../../lib/cronAuth";
import {
  findVerifiedReadySignups,
  insertStoreRow,
  updatePendingSignup,
} from "../../lib/supabase";
import { toStoreRow } from "../../lib/storeRow";
import type { Env } from "../../src/env";

export interface ActivateSummary {
  checked: number;
  activated: number;
  failed: number;
  failures: Array<{ id: string; error: string }>;
}

/**
 * The actual activation pass. Per-row failures are collected into the
 * summary (one bad row never blocks the rows after it). Throws ONLY if
 * the initial lookup fails — the caller decides what that means (the
 * GET route answers 500, scheduled() lets the invocation fail visibly).
 */
export async function runActivatePending(): Promise<ActivateSummary> {
  let candidates;
  try {
    candidates = await findVerifiedReadySignups();
  } catch (err) {
    console.error("Activation cron: lookup failed:", err);
    throw err;
  }

  let activated = 0;
  const failures: Array<{ id: string; error: string }> = [];

  for (const pending of candidates) {
    try {
      await insertStoreRow(toStoreRow(pending));
      await updatePendingSignup(pending.id!, { status: "activated" });
      activated++;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      console.error(`Activation failed for pending_signups id ${pending.id}:`, err);
      failures.push({ id: pending.id!, error: message });
    }
  }

  return {
    checked: candidates.length,
    activated,
    failed: failures.length,
    failures,
  };
}

export default async function handler(c: Context<{ Bindings: Env }>): Promise<Response> {
  // CORS is handled by the middleware in src/index.ts.
  if (c.req.method !== "GET") {
    return c.json({ error: "Method not allowed" }, 405);
  }

  if (!isCronAuthorizedRequest(c.req.raw)) {
    return c.json({ error: "Unauthorized" }, 401);
  }

  try {
    return c.json(await runActivatePending(), 200);
  } catch {
    // already logged inside runActivatePending(); nothing internal leaks
    return c.json({ error: "Failed to look up verified_ready rows" }, 500);
  }
}
