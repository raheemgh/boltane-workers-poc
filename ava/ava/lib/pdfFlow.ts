// lib/pdfFlow.ts
//
// Orchestrates storing a PDF that's already passed the cheap checks
// (magic bytes, size, page count — lib/pdfValidation.ts). As of the
// sed.sh rewrite this now runs BEFORE the malware-scan verdict is
// known, not after — see lib/pdfScanSweep.ts's design-note comment for
// why. This file itself is unchanged by that: it just stores a buffer
// and flips pdf_uploaded/pdf_storage_path, same as always; the caller
// (api/upload-pdf.ts) decides when to call it.
//
// STATUS NOTE (OTP-to-manual rewrite): the old "override rule" moved a
// row to a dedicated 'pending_manual_review' status and cleared
// activate_at so an automated timer/cron couldn't jump ahead of
// Raheem. Both of those things are gone now — the status enum is just
// 'otp_sent' -> 'verified_ready' -> 'activated', and there's no
// activate_at column or automated timer to protect against at all;
// Raheem is the ONLY thing that ever moves a row to 'verified_ready',
// by hand, in the Supabase table editor. So the simplest option reads
// clearer here: status is left at 'otp_sent' after a PDF upload —
// pdf_uploaded=true is itself the marker Raheem sees in the table view
// telling him to hand-edit system_prompt before setting
// 'verified_ready', no separate interim status needed.
import { updatePendingSignup, uploadPdfToStorage } from "./supabase";
import type { PendingSignupRow } from "./types";

export function buildPdfStoragePath(pendingId: string): string {
  // One current PDF per signup — a re-upload (upsert: true in
  // uploadPdfToStorage) cleanly replaces rather than accumulates.
  return `${pendingId}.pdf`;
}

export async function storePdfAndOverrideStatus(
  pending: PendingSignupRow,
  buffer: Buffer
): Promise<{ storagePath: string }> {
  const storagePath = buildPdfStoragePath(pending.id!);

  await uploadPdfToStorage(storagePath, buffer);

  await updatePendingSignup(pending.id!, {
    pdf_uploaded: true,
    pdf_storage_path: storagePath,
  });

  return { storagePath };
}
