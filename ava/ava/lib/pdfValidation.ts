// lib/pdfValidation.ts
//
// Cheap, synchronous-ish checks that run BEFORE the file is handed off
// for malware scanning (lib/malwareScan.ts, sed.sh): real PDF (checked
// by magic bytes, not the spoofable Content-Type header), under the
// size cap, and under the page limit. Pure functions — no network, no
// Supabase — so they're fast to test.
import { PDFDocument } from "pdf-lib";

export const MAX_PDF_PAGES = 4;
export const DEFAULT_MAX_PDF_SIZE_BYTES = 10 * 1024 * 1024; // 10MB — "unreasonable size" wasn't quantified in what I have access to; confirm and adjust via MAX_PDF_SIZE_BYTES env var.

const PDF_MAGIC_BYTES = Buffer.from("%PDF-", "ascii");

export interface PdfValidationResult {
  ok: boolean;
  reason?: string;
}

export function isPdfMagicBytes(buffer: Buffer): boolean {
  // Byte-by-byte rather than `buffer.subarray(0, 5).equals(...)`: under
  // tsconfig.workers.json (Cloudflare + Node types loaded together)
  // `Buffer.prototype.equals` loses its type declaration — a type-level
  // quirk only, runtime Buffer has it — so this keeps `tsc -p tsconfig.workers.json`
  // clean with identical behavior (tests/pdfValidation.test.ts).
  return (
    buffer.length >= PDF_MAGIC_BYTES.length &&
    PDF_MAGIC_BYTES.every((byte: number, i: number) => buffer[i] === byte)
  );
}

export async function validatePdfFile(
  buffer: Buffer,
  maxSizeBytes: number = DEFAULT_MAX_PDF_SIZE_BYTES
): Promise<PdfValidationResult> {
  if (buffer.length === 0) {
    return { ok: false, reason: "Empty file" };
  }

  if (!isPdfMagicBytes(buffer)) {
    return { ok: false, reason: "File is not a valid PDF" };
  }

  if (buffer.length > maxSizeBytes) {
    return {
      ok: false,
      reason: `File exceeds the ${Math.round(maxSizeBytes / (1024 * 1024))}MB size limit`,
    };
  }

  let pageCount: number;
  try {
    const doc = await PDFDocument.load(buffer, { ignoreEncryption: true });
    pageCount = doc.getPageCount();
  } catch {
    return { ok: false, reason: "Could not read PDF — it may be corrupted" };
  }

  if (pageCount > MAX_PDF_PAGES) {
    return {
      ok: false,
      reason: `PDF has ${pageCount} pages — the limit is ${MAX_PDF_PAGES}`,
    };
  }

  return { ok: true };
}
