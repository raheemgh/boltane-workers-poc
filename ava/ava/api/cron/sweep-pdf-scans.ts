// api/cron/sweep-pdf-scans.ts
//
// Sibling to activate-pending, kept as a SEPARATE job rather than folded
// into that one: activation (verified_ready -> stores) and scan-verdict
// resolution (scan_pending -> otp_sent, clean or infected) are different
// concerns triggered by different row states, and mixing their loops in
// one handler made it harder to read what "checked / activated / failed"
// would even mean.
//
// Same two trigger paths as activate-pending (see that file's header):
// scheduled() in src/cron.ts, plus the manual GET /cron/sweep-pdf-scans
// behind CRON_SECRET. All the sweep logic lives in lib/pdfScanSweep.ts
// (unit-testable without a Request); this file is auth + response
// shaping + the one shared runSweepPdfScans() both triggers call.
import type { Context } from "hono";
import { isCronAuthorizedRequest } from "../../lib/cronAuth";
import { sweepPdfScans, type SweepOutcome } from "../../lib/pdfScanSweep";
import type { Env } from "../../src/env";

export interface SweepSummary {
  checked: number;
  clean: number;
  infected: number;
  stillRunning: number;
  errors: SweepOutcome[];
}

/** Throws only if the initial scan_pending lookup fails (see sweepPdfScans). */
export async function runSweepPdfScans(): Promise<SweepSummary> {
  let outcomes: SweepOutcome[];
  try {
    outcomes = await sweepPdfScans();
  } catch (err) {
    console.error("PDF scan sweep failed:", err);
    throw err;
  }
  return {
    checked: outcomes.length,
    clean: outcomes.filter((o) => o.result === "clean").length,
    infected: outcomes.filter((o) => o.result === "infected").length,
    stillRunning: outcomes.filter((o) => o.result === "still-running").length,
    errors: outcomes.filter((o) => o.result === "error"),
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
    return c.json(await runSweepPdfScans(), 200);
  } catch {
    // already logged inside runSweepPdfScans(); nothing internal leaks
    return c.json({ error: "Sweep failed" }, 500);
  }
}
