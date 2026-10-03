// tests/pdfValidation.test.ts
import { describe, it, expect } from "vitest";
import { PDFDocument } from "pdf-lib";
import {
  isPdfMagicBytes,
  validatePdfFile,
  MAX_PDF_PAGES,
} from "../lib/pdfValidation";

async function makeTestPdf(pageCount: number): Promise<Buffer> {
  const doc = await PDFDocument.create();
  for (let i = 0; i < pageCount; i++) {
    doc.addPage([200, 200]);
  }
  const bytes = await doc.save();
  return Buffer.from(bytes);
}

describe("isPdfMagicBytes", () => {
  it("recognizes a real PDF's magic bytes", async () => {
    const buffer = await makeTestPdf(1);
    expect(isPdfMagicBytes(buffer)).toBe(true);
  });

  it("rejects non-PDF content", () => {
    expect(isPdfMagicBytes(Buffer.from("not a pdf at all"))).toBe(false);
  });

  it("rejects a buffer shorter than the magic bytes", () => {
    expect(isPdfMagicBytes(Buffer.from("%PD"))).toBe(false);
  });
});

describe("validatePdfFile", () => {
  it("accepts a valid small PDF within the page limit", async () => {
    const buffer = await makeTestPdf(2);
    const result = await validatePdfFile(buffer);
    expect(result.ok).toBe(true);
  });

  it("accepts exactly the page limit", async () => {
    const buffer = await makeTestPdf(MAX_PDF_PAGES);
    const result = await validatePdfFile(buffer);
    expect(result.ok).toBe(true);
  });

  it(`rejects a PDF with more than ${MAX_PDF_PAGES} pages`, async () => {
    const buffer = await makeTestPdf(MAX_PDF_PAGES + 1);
    const result = await validatePdfFile(buffer);
    expect(result.ok).toBe(false);
    expect(result.reason).toMatch(/pages/i);
  });

  it("rejects an empty buffer", async () => {
    const result = await validatePdfFile(Buffer.alloc(0));
    expect(result.ok).toBe(false);
  });

  it("rejects a non-PDF file even if small", async () => {
    const result = await validatePdfFile(Buffer.from("just some text"));
    expect(result.ok).toBe(false);
    expect(result.reason).toMatch(/not a valid pdf/i);
  });

  it("rejects a file over the size cap", async () => {
    const buffer = await makeTestPdf(1);
    const result = await validatePdfFile(buffer, 10); // tiny cap, guaranteed exceeded
    expect(result.ok).toBe(false);
    expect(result.reason).toMatch(/size limit/i);
  });

  it("rejects a corrupted PDF (valid magic bytes, broken structure)", async () => {
    const corrupted = Buffer.from(
      "%PDF-1.4\nthis is not really a pdf structure"
    );
    const result = await validatePdfFile(corrupted);
    expect(result.ok).toBe(false);
  });
});
