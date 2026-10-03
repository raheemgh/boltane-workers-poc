// tests/uploadPdf.test.ts — Stage 5. POST /upload-pdf through the REAL
// Worker app (src/index.ts), so routing + CORS + the handler + the real
// lib/pdfValidation.ts (real pdf-lib) + the real lib/pdfFlow.ts all run;
// only the network edges are faked (Supabase, sed.sh).
//
// lib/multipart.ts (busboy) had no test of its own. These cover its
// replacement (Request.formData() + the streaming size cap) AND the
// handler's whole decision tree, which had no test either.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { PDFDocument } from "pdf-lib";

vi.mock("../lib/supabase", () => ({
  findPendingSignupByContactNumber: vi.fn(),
  insertPendingSignupRow: vi.fn(),
  updatePendingSignup: vi.fn(),
  findEligiblePendingSignupForPdf: vi.fn(),
  uploadPdfToStorage: vi.fn(),
}));
vi.mock("../lib/otpHandoff", () => ({ triggerOtpHandoff: vi.fn() }));
vi.mock("../lib/malwareScan", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/malwareScan")>();
  return { ...actual, initiateMalwareScan: vi.fn() };
});

import { app } from "../src/index";
import type { Env } from "../src/env";
import { makeLockNamespace } from "./helpers/lock";
import {
  findEligiblePendingSignupForPdf,
  updatePendingSignup,
  uploadPdfToStorage,
} from "../lib/supabase";
import { MalwareScanError, initiateMalwareScan } from "../lib/malwareScan";

const ORIGIN = "https://boltane.github.io";
const PENDING = {
  id: "row-1",
  contact_number: "+15551230000",
  phone_number_id: "pn-1",
  status: "otp_sent",
};

const mFind = vi.mocked(findEligiblePendingSignupForPdf);
const mUpdate = vi.mocked(updatePendingSignup);
const mStore = vi.mocked(uploadPdfToStorage);
const mScan = vi.mocked(initiateMalwareScan);

let env: Env;
let errSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  env = { LOCK: makeLockNamespace() };
  process.env.ALLOWED_ORIGIN = ORIGIN;
  mFind.mockReset().mockResolvedValue(PENDING as never);
  mUpdate.mockReset().mockResolvedValue(undefined);
  mStore.mockReset().mockResolvedValue(undefined);
  mScan.mockReset().mockResolvedValue({ scanId: "scan-1" });
  errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => {
  delete process.env.ALLOWED_ORIGIN;
  delete process.env.MAX_PDF_SIZE_BYTES;
  delete process.env.SED_SH_API_KEY;
  errSpy.mockRestore();
});

async function makePdf(pages = 1): Promise<Buffer> {
  const doc = await PDFDocument.create();
  for (let i = 0; i < pages; i++) doc.addPage();
  return Buffer.from(await doc.save());
}

function form(parts: { phone?: string; file?: Buffer | string; fileName?: string }): FormData {
  const fd = new FormData();
  if (parts.phone !== undefined) fd.append("phone_number_id", parts.phone);
  if (parts.file !== undefined) {
    if (typeof parts.file === "string") fd.append("file", parts.file);
    else
      fd.append(
        "file",
        new File([new Uint8Array(parts.file)], parts.fileName ?? "profile.pdf", {
          type: "application/pdf",
        })
      );
  }
  return fd;
}

const post = (body?: RequestInit["body"], headers: Record<string, string> = {}) =>
  app.request(
    "/upload-pdf",
    { method: "POST", body: body ?? null, headers: { Origin: ORIGIN, ...headers } },
    env
  );

const json = async (res: Response) => (await res.json()) as { error?: string; status?: string };

describe("POST /upload-pdf — formData() parsing round trip", () => {
  it("scanning disabled: 202 'received'; the file part and the text field both come through, bytes intact", async () => {
    const pdf = await makePdf();
    const res = await post(form({ phone: "pn-1", file: pdf }));

    expect(res.status).toBe(202);
    expect((await json(res)).status).toBe("received");
    expect(res.headers.get("access-control-allow-origin")).toBe(ORIGIN);

    // the text field reached the eligibility lookup...
    expect(mFind).toHaveBeenCalledWith("pn-1");
    // ...and the exact bytes sent are the exact bytes stored
    expect(mStore).toHaveBeenCalledTimes(1);
    const [path, stored] = mStore.mock.calls[0]!;
    expect(path).toBe("row-1.pdf");
    expect(Buffer.isBuffer(stored)).toBe(true);
    expect((stored as Buffer).equals(pdf)).toBe(true);

    // pdfFlow's flags, then the skipped-scan end state
    expect(mUpdate.mock.calls.map((c) => c[1])).toEqual([
      { pdf_uploaded: true, pdf_storage_path: "row-1.pdf" },
      { status: "otp_sent", scan_id: null, pdf_scan_status: "skipped" },
    ]);
    expect(mScan).not.toHaveBeenCalled();
  });

  it("scanning enabled: hands the buffer + original filename to sed.sh, ends at scan_pending with the scanId", async () => {
    process.env.SED_SH_API_KEY = "k";
    const pdf = await makePdf();
    const res = await post(form({ phone: "pn-1", file: pdf, fileName: "my cv.pdf" }));

    expect(res.status).toBe(202);
    expect((await json(res)).status).toBe("scanning");
    expect(mScan).toHaveBeenCalledTimes(1);
    const [buf, name, type] = mScan.mock.calls[0]!;
    expect((buf as Buffer).equals(pdf)).toBe(true);
    expect(name).toBe("my cv.pdf");
    expect(type).toBe("application/pdf");
    expect(mUpdate.mock.calls.at(-1)).toEqual([
      "row-1",
      { status: "scan_pending", scan_id: "scan-1", pdf_scan_status: null },
    ]);
  });

  it("an empty-filename file part (what a browser sends when no file was chosen) is not treated as a file -> 400", async () => {
    const res = await post(form({ phone: "pn-1", file: await makePdf(), fileName: "" }));
    expect(res.status).toBe(400);
    expect((await json(res)).error).toBe("file is required");
    expect(mFind).not.toHaveBeenCalled();
  });
});

describe("POST /upload-pdf — request-shape errors (same messages as the busboy version)", () => {
  it("GET -> 405 JSON", async () => {
    const res = await app.request("/upload-pdf", { method: "GET" }, env);
    expect(res.status).toBe(405);
    expect(await res.json()).toEqual({ error: "Method not allowed" });
  });

  it("missing phone_number_id -> 400", async () => {
    const res = await post(form({ file: await makePdf() }));
    expect(res.status).toBe(400);
    expect((await json(res)).error).toBe("phone_number_id is required");
  });

  it("empty phone_number_id -> 400", async () => {
    const res = await post(form({ phone: "", file: await makePdf() }));
    expect(res.status).toBe(400);
    expect((await json(res)).error).toBe("phone_number_id is required");
  });

  it("missing file -> 400 'file is required'", async () => {
    const res = await post(form({ phone: "pn-1" }));
    expect(res.status).toBe(400);
    expect((await json(res)).error).toBe("file is required");
  });

  it("`file` sent as a plain text field (not a file part) -> 400 'file is required'", async () => {
    const res = await post(form({ phone: "pn-1", file: "%PDF-1.4 pretend" }));
    expect(res.status).toBe(400);
    expect((await json(res)).error).toBe("file is required");
  });

  it("an empty file part (browser with no file chosen) -> 400 'file is required'", async () => {
    const res = await post(form({ phone: "pn-1", file: Buffer.alloc(0) }));
    expect(res.status).toBe(400);
    expect((await json(res)).error).toBe("file is required");
  });

  it("no body at all -> 400 phone_number_id (what the empty-form busboy path answered)", async () => {
    const res = await post(null);
    expect(res.status).toBe(400);
    expect((await json(res)).error).toBe("phone_number_id is required");
  });

  it("not multipart (JSON body) -> 400 'Could not parse upload'", async () => {
    const res = await post(JSON.stringify({ phone_number_id: "pn-1" }), {
      "Content-Type": "application/json",
    });
    expect(res.status).toBe(400);
    expect((await json(res)).error).toBe("Could not parse upload");
  });

  it("multipart header with a broken body -> 400 'Could not parse upload', never a 500", async () => {
    const res = await post("this is not multipart framing at all", {
      "Content-Type": "multipart/form-data; boundary=xyz",
    });
    expect(res.status).toBe(400);
  });
});

describe("POST /upload-pdf — size cap (the streaming-vs-post-hoc difference, made explicit)", () => {
  it("file just over cap + 1MB headroom: 413, rejected before eligibility lookup or storage", async () => {
    process.env.MAX_PDF_SIZE_BYTES = "1024";
    const res = await post(form({ phone: "pn-1", file: Buffer.concat([Buffer.from("%PDF-"), Buffer.alloc(1024 + 1024 * 1024)]) }));
    expect(res.status).toBe(413);
    expect((await json(res)).error).toBe("File is too large");
    expect(mFind).not.toHaveBeenCalled();
    expect(mStore).not.toHaveBeenCalled();
  });

  it("file between the soft cap and cap + headroom: 422 with the friendly reason, not 413", async () => {
    process.env.MAX_PDF_SIZE_BYTES = "1000";
    const res = await post(form({ phone: "pn-1", file: Buffer.concat([Buffer.from("%PDF-"), Buffer.alloc(1500)]) }));
    expect(res.status).toBe(422);
    expect((await json(res)).error).toMatch(/size limit/);
    expect(mStore).not.toHaveBeenCalled();
  });

  it("declared Content-Length over the cap: 413 WITHOUT reading a single body byte", async () => {
    process.env.MAX_PDF_SIZE_BYTES = "1000";
    let pulled = 0;
    const body = new ReadableStream<Uint8Array>(
      { pull(c) { pulled++; c.enqueue(new Uint8Array(1024)); } },
      { highWaterMark: 0 } // no priming: `pulled` then counts only reads the handler asked for
    );
    const res = await app.request(
      "/upload-pdf",
      {
        method: "POST",
        headers: {
          Origin: ORIGIN,
          "Content-Type": "multipart/form-data; boundary=b",
          "Content-Length": String(50 * 1024 * 1024),
        },
        body,
        duplex: "half",
      } as RequestInit,
      env
    );
    expect(res.status).toBe(413);
    expect(pulled).toBe(0);
  });

  it("no Content-Length (chunked): 413 mid-STREAM — only ~the cap is ever read, not the whole 25MB", async () => {
    process.env.MAX_PDF_SIZE_BYTES = "1000"; // body cap ≈ 1000 + 1MB + 64KB
    const boundary = "----cap";
    const enc = new TextEncoder();
    const head = enc.encode(
      `--${boundary}\r\nContent-Disposition: form-data; name="phone_number_id"\r\n\r\npn-1\r\n` +
        `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="big.pdf"\r\n` +
        `Content-Type: application/pdf\r\n\r\n%PDF-`
    );
    const CHUNK = 64 * 1024;
    const TOTAL_CHUNKS = 400; // 25 MB if fully read
    let pulled = 0;
    const body = new ReadableStream<Uint8Array>({
      pull(c) {
        c.enqueue(pulled === 0 ? head : new Uint8Array(CHUNK).fill(65));
        pulled++;
        if (pulled > TOTAL_CHUNKS) c.close();
      },
    });
    const res = await app.request(
      "/upload-pdf",
      {
        method: "POST",
        headers: { Origin: ORIGIN, "Content-Type": `multipart/form-data; boundary=${boundary}` },
        body,
        duplex: "half",
      } as RequestInit,
      env
    );
    expect(res.status).toBe(413);
    expect((await json(res)).error).toBe("File is too large");
    // ~17 chunks make up the cap; allow generous read-ahead, but nowhere near 400
    expect(pulled).toBeLessThan(60);
    expect(res.headers.get("access-control-allow-origin")).toBe(ORIGIN);
  });

  it("an unparseable MAX_PDF_SIZE_BYTES fails CLOSED to the 10MB default (parseInt NaN used to disable the cap)", async () => {
    process.env.MAX_PDF_SIZE_BYTES = "abc";
    const res = await post(form({ phone: "pn-1", file: Buffer.concat([Buffer.from("%PDF-"), Buffer.alloc(11 * 1024 * 1024)]) }));
    expect(res.status).toBe(413);
  });
});

describe("POST /upload-pdf — decision tree after parsing (unchanged logic, now actually tested)", () => {
  it("no eligible signup -> 404, nothing stored", async () => {
    mFind.mockResolvedValue(null);
    const res = await post(form({ phone: "pn-1", file: await makePdf() }));
    expect(res.status).toBe(404);
    expect(mStore).not.toHaveBeenCalled();
  });

  it("eligibility lookup throws -> 500, generic message", async () => {
    mFind.mockRejectedValue(new Error("db down: secret detail"));
    const res = await post(form({ phone: "pn-1", file: await makePdf() }));
    expect(res.status).toBe(500);
    expect(JSON.stringify(await res.json())).not.toContain("secret");
  });

  it("not a PDF (bad magic bytes) -> 422, nothing stored or scanned", async () => {
    process.env.SED_SH_API_KEY = "k";
    const res = await post(form({ phone: "pn-1", file: Buffer.from("MZ this is an exe, not a pdf") }));
    expect(res.status).toBe(422);
    expect((await json(res)).error).toBe("File is not a valid PDF");
    expect(mScan).not.toHaveBeenCalled();
    expect(mStore).not.toHaveBeenCalled();
  });

  it("too many pages -> 422 (real pdf-lib page count runs inside the handler)", async () => {
    const res = await post(form({ phone: "pn-1", file: await makePdf(5) }));
    expect(res.status).toBe(422);
    expect((await json(res)).error).toMatch(/5 pages/);
  });

  it("sed.sh submission fails -> 502 and NOTHING is stored (no orphan file without a scanId)", async () => {
    process.env.SED_SH_API_KEY = "k";
    mScan.mockRejectedValue(new MalwareScanError("sed.sh /malware/upload failed (500): boom"));
    const res = await post(form({ phone: "pn-1", file: await makePdf() }));
    expect(res.status).toBe(502);
    expect((await json(res)).error).toBe("Malware scan submission failed");
    expect(mStore).not.toHaveBeenCalled();
  });

  it("storage upload fails -> 500 'Failed to save file', status NOT advanced", async () => {
    mStore.mockRejectedValue(new Error("Supabase Storage upload failed: x"));
    const res = await post(form({ phone: "pn-1", file: await makePdf() }));
    expect(res.status).toBe(500);
    expect((await json(res)).error).toBe("Failed to save file");
    expect(mUpdate).not.toHaveBeenCalled();
  });
});
