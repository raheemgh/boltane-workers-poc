// lib/pdfScanSweep.ts
//
// The actual sweep logic behind GET /cron/sweep-pdf-scans, factored
// out into a plain lib function (same pattern as api/chat.ts's
// extraction step vs. lib/storeRow.ts) so it's unit-testable without
// standing up a Request/Response.
//
// DESIGN NOTE — why the file is already stored before a verdict exists:
// sed.sh's given spec has no "download the file back" endpoint, and
// this cron runs on its own schedule with no access to the original
// request's in-memory buffer. The only way for THIS sweep to have
// bytes to act on later would be re-fetching them from somewhere — and
// there's nowhere to re-fetch them from except our own storage. So
// api/upload-pdf.ts uploads the buffer to our own private Supabase
// Storage bucket synchronously, in the same request, via the
// unmodified storePdfAndOverrideStatus() (lib/pdfFlow.ts) — before the
// verdict is known. That's a real, deliberate weakening of the old
// "never store an infected file" guarantee, forced by sed.sh's async
// verdict + no download endpoint + Vercel's request time budget all
// combining at once. This sweep exists specifically to close that
// window as fast as the cron's own cadence allows: on 'infected' it
// deletes the object immediately. If this trade-off is unacceptable,
// the alternative is holding the raw bytes somewhere else entirely
// (e.g. a queue/object store this app controls end-to-end with its own
// retrieval path) — out of scope for this pass.
//
// STATUS NOTE: both outcomes revert status to 'otp_sent', never back
// to whatever it was before scan_pending (e.g. 'verified_ready'). A
// file that changed after Raheem already reviewed a row should force
// a re-review, not silently resume wherever it left off — see
// lib/types.ts's SignupStatus doc comment for the same reasoning.
import {
  deletePdfFromStorage,
  findScanPendingSignups,
  updatePendingSignup,
} from "./supabase";
import { getMalwareScanResult, MalwareScanError } from "./malwareScan";
import type { PendingSignupRow } from "./types";

export interface SweepOutcome {
  id: string;
  result: "clean" | "infected" | "still-running" | "error";
  error?: string;
}

export async function sweepPdfScans(): Promise<SweepOutcome[]> {
  const pending = await findScanPendingSignups();
  const outcomes: SweepOutcome[] = [];

  for (const row of pending) {
    outcomes.push(await sweepOne(row));
  }

  return outcomes;
}

async function sweepOne(row: PendingSignupRow): Promise<SweepOutcome> {
  // One row failing (e.g. a storage delete error) must not abort the
  // whole sweep and starve every row after it — report it and move on;
  // the row keeps status 'scan_pending' and is retried next pass.
  try {
    return await resolveOne(row);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return { id: row.id!, result: "error", error: message };
  }
}

async function resolveOne(row: PendingSignupRow): Promise<SweepOutcome> {
  const id = row.id!;
  const scanId = row.scan_id;

  if (!scanId) {
    // Shouldn't happen (status='scan_pending' implies a scanId was
    // set alongside it in the same request) but don't let a bad row
    // wedge the whole sweep — surface it and move on.
    return { id, result: "error", error: "scan_pending row missing scan_id" };
  }

  let verdict;
  try {
    verdict = await getMalwareScanResult(scanId);
  } catch (err) {
    const message = err instanceof MalwareScanError ? err.message : String(err);
    return { id, result: "error", error: message };
  }

  if (verdict.threatStatus === "RUNNING") {
    // Leave status/scan_id untouched — picked up again next sweep.
    return { id, result: "still-running" };
  }

  // Fail closed: either signal of a threat counts as infected...
  if (verdict.status === "infected" || verdict.threatStatus === "THREATS_FOUND") {
    if (row.pdf_storage_path) {
      await deletePdfFromStorage(row.pdf_storage_path);
    }
    await updatePendingSignup(id, {
      status: "otp_sent",
      pdf_uploaded: false,
      pdf_storage_path: null,
      pdf_scan_status: "infected",
      scan_id: null,
    });
    return { id, result: "infected" };
  }

  // ...and only an explicit 'clean' is treated as clean. Anything else
  // (e.g. 'skipped', or a field the API stopped sending) used to fall
  // through to 'clean'; now the row is left as scan_pending and reported.
  if (verdict.status !== "clean") {
    return {
      id,
      result: "error",
      error: `unrecognized sed.sh verdict (status=${String(verdict.status)}, threatStatus=${String(verdict.threatStatus)}) — left as scan_pending`,
    };
  }

  // 'clean' — the file's already sitting at its final path from
  // upload time (storePdfAndOverrideStatus already set pdf_uploaded/
  // pdf_storage_path in the original request); just resolve status.
  await updatePendingSignup(id, {
    status: "otp_sent",
    pdf_scan_status: "clean",
    scan_id: null,
  });
  return { id, result: "clean" };
}
