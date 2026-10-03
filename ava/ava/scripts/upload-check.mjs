// scripts/upload-check.mjs — Stage 5 end-to-end check of POST /upload-pdf on a
// REAL local workerd (`wrangler dev`), with a mock Supabase behind it.
// Run:  node scripts/upload-check.mjs     (label: "passed locally")
//
// What it proves that the Vitest suite can't: the bundle (with busboy gone)
// boots under workerd; the REAL multipart bytes a client sends survive
// Request.formData() -> Buffer -> pdf-lib -> Supabase Storage intact (SHA-256
// compared at the mock); pdf-lib's PDFDocument.load()/getPageCount() actually
// run under workerd (the 5-page and corrupt-PDF rejections can only come from
// pdf-lib itself); and the size cap holds on the wire for BOTH a declared
// Content-Length and a chunked body with no Content-Length at all.
// What it does NOT prove: anything about real Cloudflare (this is wrangler dev),
// real Supabase Storage's wire format (the mock unwraps what supabase-js sends),
// or the sed.sh path (SED_SH_API_KEY is deliberately unset here: sed.sh's base URL
// is hard-coded, so scanning-enabled is covered by tests/uploadPdf.test.ts only).
import crypto from "node:crypto";
import { PDFDocument } from "pdf-lib";
import { startMockSupabase } from "./mock-supabase.mjs";
import { startWorker } from "./_worker.mjs";

const ORIGIN = "https://boltane.github.io";
const MAX = 200_000; // MAX_PDF_SIZE_BYTES for this run, so oversize cases stay small
const results = [];
const check = (name, ok, detail = "") => {
  results.push(ok);
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${detail ? " - " + detail : ""}`);
};
const sha = (b) => crypto.createHash("sha256").update(b).digest("hex");

const makePdf = async (pages = 1) => {
  const d = await PDFDocument.create();
  for (let i = 0; i < pages; i++) d.addPage();
  return Buffer.from(await d.save());
};

const sb = await startMockSupabase({ port: 8799, delayMs: 20, autoCreate: false });
let worker;
let code = 1;
const rows = async () => (await fetch(`${sb.url}/__rows`)).json();
const stored = async () => (await fetch(`${sb.url}/__storage`)).json();

try {
  worker = await startWorker({
    port: 8788,
    vars: {
      SUPABASE_URL: sb.url,
      SUPABASE_SERVICE_ROLE_KEY: "local-test-key",
      ALLOWED_ORIGIN: ORIGIN,
      MAX_PDF_SIZE_BYTES: String(MAX),
      // SED_SH_API_KEY intentionally unset -> scanning skipped (see header)
    },
  });

  const upload = async ({ phone, file, fileName = "cv.pdf", method = "POST" }) => {
    const fd = new FormData();
    if (phone !== undefined) fd.append("phone_number_id", phone);
    if (file !== undefined) fd.append("file", new File([new Uint8Array(file)], fileName, { type: "application/pdf" }));
    const t0 = Date.now();
    const res = await fetch(`${worker.url}/upload-pdf`, {
      method,
      headers: { Origin: ORIGIN },
      ...(method === "GET" ? {} : { body: fd }),
    });
    const text = await res.text();
    let json = null;
    try { json = JSON.parse(text); } catch {}
    return { status: res.status, json, text, ms: Date.now() - t0, cors: res.headers.get("access-control-allow-origin") };
  };

  // one eligible signup (status otp_sent) + one unrelated row that must never be touched
  await fetch(`${sb.url}/__seed`, { method: "POST", body: JSON.stringify({ contact_number: "+15551230000", phone_number_id: "pn-1", status: "otp_sent" }) });
  await fetch(`${sb.url}/__seed`, { method: "POST", body: JSON.stringify({ contact_number: "+15559990000", phone_number_id: "pn-other", status: "otp_sent" }) });

  console.log(`Worker ${worker.url} | mock Supabase ${sb.url} | MAX_PDF_SIZE_BYTES=${MAX}\n`);

  console.log("== 1. happy path: real multipart -> formData() -> validatePdfFile -> pdfFlow -> Supabase ==");
  const pdf = await makePdf(2);
  let r = await upload({ phone: "pn-1", file: pdf });
  check("202 { status: 'received' } (scanning disabled)", r.status === 202 && r.json?.status === "received", `${r.status} ${r.text}`);
  check("CORS header present on the real response", r.cors === ORIGIN);
  let all = await rows();
  const row = all.find((x) => x.phone_number_id === "pn-1");
  check("row got pdf_uploaded=true + pdf_storage_path", row?.pdf_uploaded === true && row?.pdf_storage_path === `${row.id}.pdf`, JSON.stringify({ u: row?.pdf_uploaded, p: row?.pdf_storage_path }));
  check("row ends at status otp_sent, pdf_scan_status 'skipped', scan_id null", row?.status === "otp_sent" && row?.pdf_scan_status === "skipped" && row?.scan_id === null);
  const objs = await stored();
  const obj = objs[`signup-pdfs/${row?.id}.pdf`];
  check("the object landed in the private bucket at <id>.pdf", Boolean(obj), Object.keys(objs).join(","));
  check("file bytes intact end to end (SHA-256 at the mock == SHA-256 sent)", obj?.sha256 === sha(pdf) && obj?.size === pdf.length, `${obj?.size}/${pdf.length}`);
  check("the unrelated signup row was not touched", all.find((x) => x.phone_number_id === "pn-other")?.pdf_uploaded === undefined);

  console.log("\n== 2. pdf-lib under workerd (only pdf-lib can produce these answers) ==");
  r = await upload({ phone: "pn-1", file: await makePdf(5) });
  check("5-page PDF -> 422 mentioning '5 pages' (getPageCount ran in workerd)", r.status === 422 && /5 pages/.test(r.json?.error ?? ""), r.text);
  r = await upload({ phone: "pn-1", file: Buffer.concat([Buffer.from("%PDF-1.4\n"), Buffer.from("this is not a real pdf body")]) });
  check("valid magic bytes + corrupt body -> 422 'Could not read PDF' (PDFDocument.load threw cleanly)", r.status === 422 && /Could not read PDF/.test(r.json?.error ?? ""), r.text);
  r = await upload({ phone: "pn-1", file: Buffer.from("MZ pretend this is an exe") });
  check("not a PDF -> 422 'File is not a valid PDF'", r.status === 422 && r.json?.error === "File is not a valid PDF", r.text);

  console.log("\n== 3. request-shape errors on the real stack ==");
  r = await upload({ phone: "pn-unknown", file: await makePdf() });
  check("unknown phone_number_id -> 404", r.status === 404);
  r = await upload({ file: await makePdf() });
  check("missing phone_number_id -> 400", r.status === 400 && r.json?.error === "phone_number_id is required", r.text);
  r = await upload({ phone: "pn-1" });
  check("missing file -> 400 'file is required'", r.status === 400 && r.json?.error === "file is required", r.text);
  r = await upload({ method: "GET" });
  check("GET -> 405 JSON", r.status === 405 && r.json?.error === "Method not allowed", r.text);
  const bad = await fetch(`${worker.url}/upload-pdf`, { method: "POST", headers: { Origin: ORIGIN, "Content-Type": "application/json" }, body: "{}" });
  check("JSON body instead of multipart -> 400, not 500", bad.status === 400, String(bad.status));

  console.log("\n== 4. size cap on the wire ==");
  const soft = Buffer.concat([Buffer.from("%PDF-"), Buffer.alloc(MAX + 500_000, 1)]);
  r = await upload({ phone: "pn-1", file: soft });
  check("between soft cap and cap+headroom -> 422 'size limit' (friendly message, not 413)", r.status === 422 && /size limit/.test(r.json?.error ?? ""), r.text);

  const big = Buffer.concat([Buffer.from("%PDF-"), Buffer.alloc(3 * 1024 * 1024, 1)]);
  r = await upload({ phone: "pn-1", file: big });
  check("3MB with declared Content-Length -> 413 'File is too large'", r.status === 413 && r.json?.error === "File is too large", `${r.status} ${r.text} (${r.ms}ms)`);

  // Chunked: a stream body has NO Content-Length, so only the streaming counter can stop it.
  const boundary = "----chunkedcap";
  const enc = new TextEncoder();
  const head = enc.encode(
    `--${boundary}\r\nContent-Disposition: form-data; name="phone_number_id"\r\n\r\npn-1\r\n` +
      `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="big.pdf"\r\nContent-Type: application/pdf\r\n\r\n%PDF-`
  );
  let sent = 0;
  const CHUNKS = 160; // 160 x 64KB = 10 MB if the Worker keeps reading
  const stream = new ReadableStream({
    async pull(c) {
      c.enqueue(sent === 0 ? head : new Uint8Array(65536).fill(66));
      sent++;
      if (sent > CHUNKS) c.close();
    },
  });
  const t0 = Date.now();
  const cr = await fetch(`${worker.url}/upload-pdf`, {
    method: "POST",
    headers: { Origin: ORIGIN, "Content-Type": `multipart/form-data; boundary=${boundary}` },
    body: stream,
    duplex: "half",
  }).catch((e) => ({ status: 0, text: async () => String(e), headers: new Headers() }));
  const crText = await cr.text();
  // NOTE: this proves the right 413 on the wire for a body with NO Content-Length. It does NOT
  // prove the Worker stopped *reading* early: over loopback the client's own socket buffers
  // can swallow the whole 10MB regardless (the client pulled all chunks above). Bounded
  // reading is proven in-process by tests/uploadPdf.test.ts (stream pulled < 60 of 400 chunks).
  check("10MB chunked body, no Content-Length -> 413 'File is too large' on the wire", cr.status === 413 && /too large/.test(crText), `${cr.status} ${crText.slice(0, 60)} (${Date.now() - t0}ms; client pulled ${sent}/${CHUNKS} chunks)`);

  // The Worker answered 413 while request-body bytes were still unread, so workerd closes
  // that TCP connection; undici's keep-alive pool may try to reuse it once (ECONNRESET).
  // That's expected transport behavior, not a Worker failure — so retry on a fresh connection.
  let healthy = false;
  for (let i = 0; i < 3 && !healthy; i++) {
    try { healthy = (await fetch(`${worker.url}/health`, { headers: { Connection: "close" } })).status === 200; } catch {}
  }
  check("Worker still healthy right after the oversize attempts (fresh connection)", healthy);

  console.log("\n== 5. nothing leaked from the rejected requests ==");
  const objsAfter = await stored();
  check("exactly ONE object in storage (only the happy-path upload)", Object.keys(objsAfter).length === 1, Object.keys(objsAfter).join(","));
  all = await rows();
  check("only the target row has pdf_uploaded", all.filter((x) => x.pdf_uploaded).length === 1);

  const pass = results.filter(Boolean).length;
  console.log(`\n== SUMMARY == ${pass}/${results.length} checks passed`);
  code = pass === results.length ? 0 : 1;
} catch (e) {
  console.error("check script crashed:", e);
  if (worker) console.error("worker log tail:\n" + worker.getLog().slice(-1500));
} finally {
  if (worker) await worker.stop();
  await sb.close();
}
process.exit(code);
