// src/cron.ts
//
// scheduled() — Cloudflare's native Cron Triggers (Stage 6). Replaces the
// external cron-job.org / GitHub Actions callers: Cloudflare's own edge
// invokes the Worker directly, with NO incoming Request, so there is no
// secret to check here (lib/cronAuth.ts only guards the manual GET routes).
//
// The two expressions below MUST equal wrangler.jsonc's triggers.crons —
// controller.cron is matched by exact string. tests/cron.test.ts reads
// wrangler.jsonc and fails if they drift, and an unrecognized expression
// throws (visible failed invocation) instead of silently doing nothing.
//
// Cadence (all times UTC): same 15-30 min window the README documented for
// the external schedulers, but reliable instead of best-effort. This Worker
// uses 2 triggers. Cloudflare's Limits page (checked 2026-10-02) says the cap
// is 5 Cron Triggers PER ACCOUNT on the Free plan (250 on Paid) — across
// every Worker in the account, not per Worker.
import { runActivatePending } from "../api/cron/activate-pending";
import { runSweepPdfScans } from "../api/cron/sweep-pdf-scans";
import type { CronController, WaitUntilContext } from "./env";

export const CRON_ACTIVATE_PENDING = "*/15 * * * *";
export const CRON_SWEEP_PDF_SCANS = "*/20 * * * *";

/**
 * Runs the job a given expression belongs to under ctx.waitUntil(), logs the
 * summary (so `wrangler tail` / Workers Logs show checked/activated/failed),
 * and re-throws a failed lookup so Cloudflare marks the invocation as failed.
 */
export function handleScheduled(controller: CronController, ctx: WaitUntilContext): void {
  switch (controller.cron) {
    case CRON_ACTIVATE_PENDING:
      ctx.waitUntil(
        runActivatePending().then((summary) => {
          console.log("[cron] activate-pending", JSON.stringify(summary));
        })
      );
      return;
    case CRON_SWEEP_PDF_SCANS:
      ctx.waitUntil(
        runSweepPdfScans().then((summary) => {
          console.log("[cron] sweep-pdf-scans", JSON.stringify(summary));
        })
      );
      return;
    default:
      throw new Error(`scheduled(): unrecognized cron expression "${controller.cron}"`);
  }
}
