// api/upload-pdf.ts
//
// POST /upload-pdf (multipart/form-data: `phone_number_id` field +
// `file` field). Order of steps:
//   1. Parse the upload, find an eligible pending_signups row.
//   2. PDF-only (magic bytes), size cap, page cap (lib/pdfValidation.ts).
//   3. Hand the file to sed.sh for scanning (lib/malwareScan.ts) — just
//      the submit + pre-signed PUT, both fast; NOT the verdict itself.
//   4. Store the file in our own private Supabase Storage bucket +
//      apply the existing pdf_uploaded/pdf_storage_path update
//      (lib/pdfFlow.ts, unchanged) — done now, before the verdict is
//      known, and mark status='scan_pending' with the scanId. See
//      lib/pdfScanSweep.ts's design-note comment for why this order
//      (rather than storing only after a 'clean' verdict) is what
//      sed.sh's given spec (no download endpoint) forces.
//   5. Respond 202 immediately — the verdict is resolved later, out of
//      request, by the cron sweep (scheduled() or GET /cron/sweep-pdf-scans).
//
// This function does NOT poll sed.sh at all. Nothing in this file
// waits on a scan verdict.
//
// ELIGIBILITY NOTE: OTP is fully manual (see README), so there's no
// automated "verified" checkpoint left to gate on — a client can
// upload a PDF any time their own signup row is at status IN
// ('otp_sent','verified_ready'), see findEligiblePendingSignupForPdf()
// in lib/supabase.ts. phone_number_id alone isn't a secret — flagging
// this plainly rather than implying a security property this endpoint
// doesn't actually have.
//
// STAGE 5 (Workers) — multipart parsing:
// busboy (lib/multipart.ts, deleted) is replaced by the platform's
// Request.formData(). The one real behavior difference is WHEN the
// size cap bites. busboy's `limits.fileSize` cut the upload off *while
// streaming*, so memory use stayed bounded no matter what the client
// sent. formData() buffers the whole body before returning, and a
// Worker request body is a stream that is NOT fully received before the
// handler runs (Cloudflare allows request bodies up to 100 MB on Free),
// against a 128 MB per-isolate memory limit — so a "check file.size
// after parsing" alone would still let a big upload be fully buffered
// (formData's copy + the File + arrayBuffer()'s copy) before it is
// rejected. To keep busboy's bounded-memory property the body is
// capped *during* the stream (parseUploadForm below): a cheap
// Content-Length pre-check, plus a byte-counting TransformStream for
// chunked / missing / lying Content-Length. The exact file-level check
// (file.size > cap + headroom -> 413) still runs after parsing, as
// before; validatePdfFile() still answers the friendlier 422 for files
// between the soft cap and cap + headroom.
import type { Context } from "hono";
import {
  DEFAULT_MAX_PDF_SIZE_BYTES,
  validatePdfFile,
} from "../lib/pdfValidation";
import { MalwareScanError, initiateMalwareScan } from "../lib/malwareScan";
import { findEligiblePendingSignupForPdf, updatePendingSignup } from "../lib/supabase";
import { storePdfAndOverrideStatus } from "../lib/pdfFlow";
import type { Env } from "../src/env";

// Small headroom over the soft cap so the hard 413 doesn't fire before
// validatePdfFile()'s own clearer, more informative 422 does — it just
// needs to be an upper bound, not the real limit (same idea the old
// busboy limit had).
const FILE_HEADROOM_BYTES = 1024 * 1024;
// Multipart framing (boundary lines, part headers, the phone_number_id
// field) is a few hundred bytes; this keeps the whole-body cap from ever
// rejecting a file that the exact file-level check would accept.
const BODY_OVERHEAD_BYTES = 64 * 1024;

class UploadTooLargeError extends Error {}

/**
 * MAX_PDF_SIZE_BYTES, falling back to the default when unset OR not a
 * positive number. (parseInt("abc") is NaN, and every `x > NaN`
 * comparison is false — an unparseable value used to silently disable
 * the size cap; it now fails closed to the default instead.)
 */
function resolveMaxSizeBytes(): number {
  const raw = process.env.MAX_PDF_SIZE_BYTES;
  const parsed = raw ? parseInt(raw, 10) : NaN;
  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_MAX_PDF_SIZE_BYTES;
}

/**
 * Request.formData() with a hard cap on how many body bytes are ever
 * read. Throws UploadTooLargeError when exceeded; any other throw is a
 * malformed upload (wrong content-type, broken multipart framing).
 */
async function parseUploadForm(req: Request, maxFileBytes: number): Promise<FormData> {
  const maxBodyBytes = maxFileBytes + FILE_HEADROOM_BYTES + BODY_OVERHEAD_BYTES;

  // 1. Honest clients declare their size: refuse without reading a byte.
  const declared = Number(req.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > maxBodyBytes) {
    throw new UploadTooLargeError("Declared Content-Length exceeds the upload cap");
  }

  // No body at all: same outcome busboy gave (an empty form, which the
  // caller then answers with "phone_number_id is required").
  if (!req.body) return new FormData();

  // 2. Chunked / absent / dishonest Content-Length: count as it streams
  // and abort the moment the cap is crossed (the error also cancels the
  // upstream read, so nothing past the cap is ever buffered).
  let seen = 0;
  let tooLarge = false;
  const capped = req.body.pipeThrough(
    new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, controller) {
        seen += chunk.byteLength;
        if (seen > maxBodyBytes) {
          tooLarge = true;
          controller.error(new UploadTooLargeError("Upload exceeds the size cap"));
          return;
        }
        controller.enqueue(chunk);
      },
    })
  );

  try {
    return await new Response(capped, {
      headers: { "content-type": req.headers.get("content-type") ?? "" },
    }).formData();
  } catch (err) {
    // Don't rely on the error object surviving the stream plumbing
    // intact — the flag is the source of truth.
    if (tooLarge) throw new UploadTooLargeError("Upload exceeds the size cap");
    throw err;
  }
}

export default async function handler(c: Context<{ Bindings: Env }>): Promise<Response> {
  // CORS + OPTIONS preflight are handled by the middleware in
  // src/index.ts (they used to be applyCors(res) + a 204 here).
  if (c.req.method !== "POST") {
    return c.json({ error: "Method not allowed" }, 405);
  }

  const maxSizeBytes = resolveMaxSizeBytes();

  // No SED_SH_API_KEY configured: skip malware scanning entirely rather
  // than failing the request. Temporary, deliberate state — not a
  // misconfiguration — while sed.sh isn't funded yet (see "PDF upload +
  // malware scan" in the README). Re-adding the key later resumes real
  // scanning with no other code change needed.
  const scanningEnabled = Boolean(process.env.SED_SH_API_KEY);

  // --- Parse ---
  let form: FormData;
  try {
    form = await parseUploadForm(c.req.raw, maxSizeBytes);
  } catch (err) {
    if (err instanceof UploadTooLargeError) {
      return c.json({ error: "File is too large" }, 413);
    }
    console.error("Multipart parse failed:", err);
    return c.json({ error: "Could not parse upload" }, 400);
  }

  const phoneNumberId = form.get("phone_number_id");
  if (typeof phoneNumberId !== "string" || !phoneNumberId) {
    return c.json({ error: "phone_number_id is required" }, 400);
  }

  const file = form.get("file");
  if (!(file instanceof File) || file.size === 0) {
    return c.json({ error: "file is required" }, 400);
  }
  // Exact file-level cap (see the STAGE 5 note at the top): checked on
  // file.size BEFORE arrayBuffer() so an oversized file is never copied.
  if (file.size > maxSizeBytes + FILE_HEADROOM_BYTES) {
    return c.json({ error: "File is too large" }, 413);
  }
  const fileBuffer = Buffer.from(await file.arrayBuffer());
  const fileName = file.name || null;

  // --- Eligibility ---
  let pending;
  try {
    pending = await findEligiblePendingSignupForPdf(phoneNumberId);
  } catch (err) {
    console.error("Eligibility lookup failed:", err);
    return c.json({ error: "Could not verify signup status" }, 500);
  }
  if (!pending) {
    return c.json(
      {
        error:
          "No signup found for this phone_number_id. Complete the onboarding conversation first.",
      },
      404
    );
  }

  // --- Cheap validation (PDF-only, size, pages) ---
  const validation = await validatePdfFile(fileBuffer, maxSizeBytes);
  if (!validation.ok) {
    return c.json({ error: validation.reason }, 422);
  }

  // --- Hand off to sed.sh for scanning (fast: metadata POST + a
  // direct PUT to a pre-signed URL, no verdict awaited here) — only
  // when scanning is actually enabled (see scanningEnabled above) ---
  let scanId: string | null = null;
  if (scanningEnabled) {
    try {
      const result = await initiateMalwareScan(
        fileBuffer,
        fileName || "upload.pdf",
        "application/pdf"
      );
      scanId = result.scanId;
    } catch (err) {
      const message = err instanceof MalwareScanError ? err.message : String(err);
      console.error("sed.sh malware scan submission failed:", message);
      return c.json({ error: "Malware scan submission failed" }, 502);
    }
  }

  // --- Store now. If scanning ran, verdict is unknown yet (see
  // lib/pdfScanSweep.ts) so status goes to scan_pending. If scanning
  // was skipped, there's no verdict to wait on, so this goes straight
  // to the same end-state the sweep cron would otherwise resolve it
  // to — status='otp_sent', pdf_scan_status='skipped' (distinct from
  // 'clean': a human hasn't vetted this file, sed.sh just never ran on
  // it) — rather than sitting at scan_pending forever with no scanId
  // for the sweep to ever resolve. ---
  try {
    await storePdfAndOverrideStatus(pending, fileBuffer);
    await updatePendingSignup(
      pending.id!,
      scanningEnabled
        ? { status: "scan_pending", scan_id: scanId, pdf_scan_status: null }
        : { status: "otp_sent", scan_id: null, pdf_scan_status: "skipped" }
    );
  } catch (err) {
    console.error("PDF store/status update failed:", err);
    return c.json({ error: "Failed to save file" }, 500);
  }

  return c.json(
    scanningEnabled
      ? { status: "scanning", message: "Your file is being checked, we'll be in touch." }
      : { status: "received", message: "Your file was received, we'll be in touch." },
    202
  );
}
