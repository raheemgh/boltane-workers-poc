#!/usr/bin/env node
// scripts/live-e2e.mjs — live end-to-end test of the DEPLOYED Worker, using a
// clearly fake "first client" and NO Meta account.
//
//   node scripts/live-e2e.mjs seed                      # insert the fake client row
//   node scripts/live-e2e.mjs run <worker-url> [--origin https://you.github.io]
//   node scripts/live-e2e.mjs cleanup                   # delete the row + its PDF
//   node scripts/live-e2e.mjs all  <worker-url> [--origin ...]   # seed -> run -> cleanup (cleanup always runs)
//
// Needs (environment, never written to disk or printed):
//   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY     (optional: SUPABASE_PDF_BUCKET, default signup-pdfs)
//
// WHY NO META IS NEEDED: /complete-setup never validates phone_number_id or
// access_token with Meta. Its one Meta touch is a read-only, best-effort display-
// number lookup in lib/meta.ts that returns null on ANY failure (a fake token just
// gets a 4xx back). /upload-pdf never touches Meta at all. So fake values exercise
// the same code paths real ones do.
//
// SAFETY — this writes to your REAL Supabase project, so it is fenced:
//   * Everything is keyed to ONE fake identity (contact +999000000001, phone_number_id
//     zz-test-pn-000000000001, store "ZZ-TEST Cafe (delete me)"). No real client can
//     collide with it.
//   * seed/cleanup only ever delete a row whose contact_number matches AND whose
//     store_name starts with "ZZ-TEST". A same-number row with any other store name
//     makes the script REFUSE and stop, touching nothing.
//   * It never sets status 'verified_ready', so the activation cron can never copy
//     the row into the real `stores` table.
//   * Side effects to expect: one pending_signups row, one Storage object, and (if
//     RESEND_* are configured) ONE notification email about "ZZ-TEST Cafe".
//
// WHAT IT PROVES (on real Cloudflare + real Supabase) — and what it doesn't:
//   proves : the Durable Object lock under 10 truly concurrent requests; the real
//            Supabase read/write/Storage wire format from a Worker; pdf-lib and
//            formData() on Cloudflare; a near-cap upload does not hit the CPU limit
//            (Cloudflare error 1102); the privacy behavior of /lookup-signup.
//   doesn't: the real Meta display-number lookup, real WhatsApp activation (Core
//            Engine, not Ava), sed.sh scanning (SED_SH_API_KEY unset => scan skipped).
import crypto from "node:crypto";
import fs from "node:fs";
import { createClient } from "@supabase/supabase-js";
import { PDFDocument, StandardFonts } from "pdf-lib";

// ---------- the fake first client ----------
export const TEST = {
  contact: "+999000000001",
  unknownContact: "+999000000099",
  store: "ZZ-TEST Cafe (delete me)",
  marker: "ZZ-TEST",
  legalName: "ZZ-TEST Legal Name",
  phoneNumberId: "zz-test-pn-000000000001",
  accessToken: "zz-test-token-not-real",
};
const CONCURRENCY = 10;

// ---------- args / env ----------
const argv = process.argv.slice(2);
const cmd = argv[0];
const flag = (name) => {
  const i = argv.indexOf(name);
  return i >= 0 ? argv[i + 1] : undefined;
};
const origin = flag("--origin");
const workerUrl = (argv.slice(1).find((a, i, arr) => !a.startsWith("--") && arr[i - 1] !== "--origin") ?? process.env.WORKER_URL ?? "").replace(/\/$/, "");
const BUCKET = process.env.SUPABASE_PDF_BUCKET || "signup-pdfs";

const die = (msg) => { console.error(`ERROR: ${msg}`); process.exit(2); };
if (!["seed", "run", "cleanup", "all"].includes(cmd ?? "")) {
  die("usage: node scripts/live-e2e.mjs <seed|run|cleanup|all> [worker-url] [--origin https://you.github.io]");
}
if (!process.env.SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) {
  die("set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY first (see the README, 'Live end-to-end test'). Values are never printed.");
}
if (!/^https:\/\/[^/]+$/.test(process.env.SUPABASE_URL) && !/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(process.env.SUPABASE_URL)) {
  die("SUPABASE_URL must look like https://xxxx.supabase.co (no path, no trailing slash)");
}
if (["run", "all"].includes(cmd)) {
  if (!workerUrl) die("give the Worker URL: node scripts/live-e2e.mjs run https://<name>.<subdomain>.workers.dev --origin https://you.github.io");
  if (!/^https:\/\/[^/]+$/.test(workerUrl) && !/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(workerUrl)) die("worker URL must be a bare origin like https://x.workers.dev");
}

const sb = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_SERVICE_ROLE_KEY, {
  auth: { persistSession: false, autoRefreshToken: false },
});

// ---------- reporting ----------
const results = [];
let warnings = 0;
const check = (name, ok, detail = "") => {
  results.push(Boolean(ok));
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${detail ? " - " + detail : ""}`);
  return Boolean(ok);
};
const warn = (name, detail = "") => { warnings++; console.log(`[WARN] ${name}${detail ? " - " + detail : ""}`); };
const sha = (b) => crypto.createHash("sha256").update(b).digest("hex");
const mb = (n) => (n / 1048576).toFixed(2) + " MiB";

// ---------- http helpers ----------
async function http(method, path, { json, form, signal } = {}) {
  const t0 = Date.now();
  const headers = {};
  if (origin) headers.Origin = origin;
  let body;
  if (json !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(json); }
  if (form) body = form;
  try {
    const res = await fetch(workerUrl + path, { method, headers, body, signal: signal ?? AbortSignal.timeout(90_000) });
    const text = await res.text();
    let parsed = null;
    try { parsed = JSON.parse(text); } catch {}
    return { status: res.status, text, json: parsed, ms: Date.now() - t0, acao: res.headers.get("access-control-allow-origin") };
  } catch (e) {
    return { status: 0, text: String(e?.cause?.code || e?.message || e), json: null, ms: Date.now() - t0, acao: null, networkError: true };
  }
}
const uploadForm = (phone, pdf, name = "zz-test.pdf") => {
  const fd = new FormData();
  if (phone !== undefined) fd.append("phone_number_id", phone);
  if (pdf !== undefined) fd.append("file", new File([new Uint8Array(pdf)], name, { type: "application/pdf" }));
  return fd;
};

// ---------- test data ----------
function readCap() {
  // The Worker's real cap comes from wrangler.jsonc's vars (default 10 MiB in code).
  try {
    const raw = fs.readFileSync("wrangler.jsonc", "utf8").split("\n").filter((l) => !l.trim().startsWith("//")).join("\n");
    const n = parseInt(JSON.parse(raw).vars?.MAX_PDF_SIZE_BYTES, 10);
    if (Number.isFinite(n) && n > 0) return n;
  } catch {}
  return 10 * 1024 * 1024;
}
async function makePdf({ pages = 1, fillerBytes = 0 }) {
  const d = await PDFDocument.create();
  const font = await d.embedFont(StandardFonts.Helvetica);
  for (let i = 0; i < pages; i++) d.addPage().drawText(`ZZ-TEST page ${i + 1}`, { x: 50, y: 700, size: 18, font });
  // An opaque binary stream, like an embedded scan: pdf-lib parses its framing but never decodes it.
  if (fillerBytes) d.context.register(d.context.stream(crypto.randomBytes(fillerBytes)));
  return Buffer.from(await d.save({ useObjectStreams: false }));
}

// ---------- supabase helpers ----------
async function getRows() {
  const { data, error } = await sb.from("pending_signups").select("*").eq("contact_number", TEST.contact);
  if (error) throw new Error(`Supabase read failed: ${error.message}`);
  return data ?? [];
}

/** Deletes the fake row + its PDF. Refuses (returns ok:false) if a same-number row is not a ZZ-TEST row. */
async function cleanup() {
  const rows = await getRows();
  const foreign = rows.filter((r) => !String(r.store_name ?? "").startsWith(TEST.marker));
  if (foreign.length) {
    console.error(`REFUSING: a pending_signups row exists for ${TEST.contact} whose store_name does not start with "${TEST.marker}". Nothing was deleted.`);
    return { ok: false, rows: 0, objects: 0 };
  }
  let objects = 0;
  for (const r of rows) {
    const path = r.pdf_storage_path || `${r.id}.pdf`;
    const { data: removed, error } = await sb.storage.from(BUCKET).remove([path]);
    if (!error) objects += removed?.length ?? 0; // remove() of a missing path succeeds with []
  }
  const { data: gone, error } = await sb.from("pending_signups").delete().eq("contact_number", TEST.contact).like("store_name", `${TEST.marker}%`).select("id");
  if (error) { console.error(`cleanup: row delete failed: ${error.message}`); return { ok: false, rows: 0, objects }; }
  // Verify instead of assuming.
  const left = await getRows();
  let objLeft = 0;
  for (const r of rows) {
    const { data } = await sb.storage.from(BUCKET).download(r.pdf_storage_path || `${r.id}.pdf`);
    if (data) objLeft++;
  }
  return { ok: left.length === 0 && objLeft === 0, rows: gone?.length ?? 0, objects, left: left.length, objLeft };
}

async function seed() {
  const c = await cleanup(); // idempotent: clears a previous leftover first
  if (!c.ok) die("could not clear the previous test row; stopping.");
  const { data, error } = await sb.from("pending_signups")
    .insert({ contact_number: TEST.contact, store_name: TEST.store, status: "chat_complete" })
    .select("id, contact_number, store_name, status").single();
  if (error) die(`insert failed: ${error.message}`);
  console.log("Seeded the fake first client in pending_signups:");
  console.log(`  id              ${data.id}`);
  console.log(`  contact_number  ${data.contact_number}`);
  console.log(`  store_name      ${data.store_name}`);
  console.log(`  status          ${data.status}   (the state a real client is in right after the chat)`);
  console.log("  The values the test will submit as the 'form':");
  console.log(`    legal_name ${TEST.legalName} | phone_number_id ${TEST.phoneNumberId} | access_token <fake, not printed>`);
  return data;
}

// ---------- the run ----------
async function run() {
  const cap = readCap();
  console.log(`Worker ${workerUrl} | origin ${origin ?? "(none)"} | PDF cap ${mb(cap)} | bucket ${BUCKET}\n`);

  console.log("== 0. preflight ==");
  const h = await http("GET", "/health");
  check("GET /health -> 200 { ok: true }", h.status === 200 && h.json?.ok === true, `${h.status} ${h.text.slice(0, 60)} (${h.ms}ms)`);
  let rows = await getRows();
  const row0 = rows[0];
  if (!row0) { check("the fake client row exists (run `seed` first)", false); return; }
  if (row0.status !== "chat_complete" || row0.otp_code) {
    check("fake client is fresh (status chat_complete, no OTP)", false, `status=${row0.status}. Already used: run \`cleanup\` then \`seed\` (or use \`all\`).`);
    return;
  }
  check("fake client row is fresh: status chat_complete, no OTP, no PDF", !row0.otp_code && !row0.pdf_uploaded, `id ${row0.id}`);
  const rowId = row0.id;

  console.log("\n== 1. /lookup-signup before the form (draft is resumable) ==");
  let r = await http("GET", `/lookup-signup?contact_number=${encodeURIComponent(TEST.contact)}`);
  check("found:true while no OTP has been sent", r.status === 200 && r.json?.found === true, `${r.status} ${r.text.slice(0, 80)}`);

  console.log("\n== 2. cheap rejections on /complete-setup (no state change) ==");
  const body = { contact_number: TEST.contact, legal_name: TEST.legalName, phone_number_id: TEST.phoneNumberId, access_token: TEST.accessToken };
  r = await http("POST", "/complete-setup", { json: { ...body, legal_name: "" } });
  check("missing legal_name -> 400", r.status === 400 && /legal_name/.test(r.json?.error ?? ""), `${r.status} ${r.text.slice(0, 80)}`);
  r = await http("POST", "/complete-setup", { json: { ...body, contact_number: TEST.unknownContact } });
  check("unknown contact_number -> 404", r.status === 404, `${r.status} ${r.text.slice(0, 80)}`);
  rows = await getRows();
  check("the fake row is still untouched (chat_complete, no OTP)", rows[0]?.status === "chat_complete" && !rows[0]?.otp_code);

  console.log(`\n== 3. THE LOCK: ${CONCURRENCY} simultaneous /complete-setup for the same client ==`);
  const t0 = Date.now();
  const burst = await Promise.all(Array.from({ length: CONCURRENCY }, () => http("POST", "/complete-setup", { json: body })));
  const by = {};
  for (const x of burst) by[x.status] = (by[x.status] ?? 0) + 1;
  console.log(`   statuses: ${JSON.stringify(by)} in ${Date.now() - t0}ms`);
  const ok200 = burst.filter((x) => x.status === 200);
  const c409 = burst.filter((x) => x.status === 409);
  check("EXACTLY ONE 200 (a double OTP would show 2+)", ok200.length === 1, `got ${ok200.length}`);
  check(`all the others are 409 (${CONCURRENCY - 1} expected)`, c409.length === CONCURRENCY - 1, `got ${c409.length}; any 5xx: ${burst.some((x) => x.status >= 500 || x.status === 0)}`);
  const inFlight = c409.filter((x) => /being processed/.test(x.text)).length;
  console.log(`   (409 split: ${inFlight} blocked by the lock while in flight, ${c409.length - inFlight} saw 'already completed')`);
  const code = /Code: (\d{12})/.exec(ok200[0]?.json?.reply ?? "")?.[1];
  check("the 200 reply carries a 12-digit code", Boolean(code), code ? "format ok (code not printed)" : ok200[0]?.text.slice(0, 120));
  if (origin) check("CORS: the 200 carries access-control-allow-origin = your origin", ok200[0]?.acao === origin, `got ${ok200[0]?.acao}`);
  rows = await getRows();
  const row1 = rows[0] ?? {};
  check("DB: status is otp_sent and the stored OTP equals the one in the reply", row1.status === "otp_sent" && Boolean(code) && row1.otp_code === code);
  check("DB: the submitted form values were stored", row1.legal_name === TEST.legalName && row1.phone_number_id === TEST.phoneNumberId && row1.access_token === TEST.accessToken);
  r = await http("POST", "/complete-setup", { json: body });
  check("a later retry -> 409 'already completed'", r.status === 409 && /already/i.test(r.text), `${r.status}`);

  console.log("\n== 4. /lookup-signup after the OTP (privacy) ==");
  r = await http("GET", `/lookup-signup?contact_number=${encodeURIComponent(TEST.contact)}`);
  check("now exactly { found: false } - same answer as an unknown number", r.status === 200 && r.text === '{"found":false}', r.text.slice(0, 80));

  console.log("\n== 5. /upload-pdf: normal file through real Supabase Storage ==");
  const small = await makePdf({ pages: 2 });
  r = await http("POST", "/upload-pdf", { form: uploadForm(TEST.phoneNumberId, small) });
  check("2-page PDF -> 202", r.status === 202 && ["received", "scanning"].includes(r.json?.status), `${r.status} ${r.text.slice(0, 90)} (${r.ms}ms)`);
  rows = await getRows();
  const row2 = rows[0] ?? {};
  check("DB: pdf_uploaded true and pdf_storage_path = <id>.pdf", row2.pdf_uploaded === true && row2.pdf_storage_path === `${rowId}.pdf`, `${row2.pdf_storage_path}`);
  check("DB: scan state is consistent (skipped when no sed.sh key, else scan_pending)", (row2.pdf_scan_status === "skipped" && row2.status === "otp_sent") || (row2.status === "scan_pending" && Boolean(row2.scan_id)), `status=${row2.status} pdf_scan_status=${row2.pdf_scan_status}`);
  let dl = await sb.storage.from(BUCKET).download(`${rowId}.pdf`);
  const smallBack = dl.data ? Buffer.from(await dl.data.arrayBuffer()) : null;
  check("Storage: the object exists and its bytes are identical (SHA-256) to what was sent", smallBack && sha(smallBack) === sha(small), dl.error?.message ?? `${smallBack?.length}/${small.length} bytes`);

  console.log("\n== 6. /upload-pdf: near the size cap (the CPU-limit test) ==");
  const fillerBytes = Math.floor(cap * 0.95) - 8_000;
  const big = await makePdf({ pages: 4, fillerBytes });
  const bigPages = (await PDFDocument.load(big)).getPageCount();
  console.log(`   generated ${mb(big.length)} (${(big.length / cap * 100).toFixed(0)}% of the cap), ${bigPages} pages`);
  if (big.length >= cap || bigPages !== 4) { check("test PDF was built under the cap with 4 pages", false, `${big.length}/${cap}, ${bigPages} pages`); }
  else {
    r = await http("POST", "/upload-pdf", { form: uploadForm(TEST.phoneNumberId, big, "zz-test-big.pdf") });
    const limitHit = /1102|exceeded resource limits/i.test(r.text);
    check("near-cap PDF -> 202 (and no Cloudflare error 1102)", r.status === 202 && !limitHit, `${r.status} ${r.text.slice(0, 110)} (${r.ms}ms)` + (limitHit ? "  <- CPU/memory limit hit: lower MAX_PDF_SIZE_BYTES in wrangler.jsonc vars and redeploy" : r.status >= 500 ? "  <- see `wrangler tail` for the exception" : ""));
    if (r.status === 202) {
      dl = await sb.storage.from(BUCKET).download(`${rowId}.pdf`);
      const bigBack = dl.data ? Buffer.from(await dl.data.arrayBuffer()) : null;
      check("Storage: the replaced object is the big file, byte-identical (re-upload upserts)", bigBack && sha(bigBack) === sha(big), dl.error?.message ?? `${bigBack?.length}/${big.length} bytes`);
    }
  }

  console.log("\n== 7. /upload-pdf rejections on the real stack ==");
  r = await http("POST", "/upload-pdf", { form: uploadForm(TEST.phoneNumberId, await makePdf({ pages: 5 })) });
  check("5 pages -> 422 mentioning '5 pages' (pdf-lib ran on Cloudflare)", r.status === 422 && /5 pages/.test(r.json?.error ?? ""), `${r.status} ${r.text.slice(0, 90)}`);
  r = await http("POST", "/upload-pdf", { form: uploadForm(TEST.phoneNumberId, Buffer.from("MZ not a pdf at all")) });
  check("not a PDF -> 422 'File is not a valid PDF'", r.status === 422 && r.json?.error === "File is not a valid PDF", `${r.status} ${r.text.slice(0, 90)}`);
  r = await http("POST", "/upload-pdf", { form: uploadForm("zz-test-unknown-pn", small) });
  check("unknown phone_number_id -> 404", r.status === 404, `${r.status} ${r.text.slice(0, 90)}`);
  const huge = Buffer.concat([Buffer.from("%PDF-"), Buffer.alloc(cap + 1.5 * 1048576)]);
  r = await http("POST", "/upload-pdf", { form: uploadForm(TEST.phoneNumberId, huge) });
  if (r.networkError) warn(`${mb(huge.length)} body: connection was cut instead of a clean 413`, `${r.text} - expected when the server answers before reading the body; check \`wrangler tail\` shows no exception`);
  else check(`${mb(huge.length)} body (over cap + headroom) -> 413`, r.status === 413, `${r.status} ${r.text.slice(0, 90)} (${r.ms}ms)`);
  rows = await getRows();
  dl = await sb.storage.from(BUCKET).download(`${rowId}.pdf`);
  const stillBig = dl.data ? sha(Buffer.from(await dl.data.arrayBuffer())) === sha(big) : false;
  check("none of the rejected uploads changed the stored file or the row", stillBig && rows[0]?.pdf_uploaded === true);
}

// ---------- main ----------
let exitCode = 0;
try {
  if (cmd === "seed") await seed();
  else if (cmd === "cleanup") {
    const c = await cleanup();
    console.log(c.ok ? `cleanup OK: removed ${c.rows} row(s) and ${c.objects} Storage object(s); verified gone.` : "cleanup did NOT complete.");
    exitCode = c.ok ? 0 : 1;
  } else if (cmd === "run") {
    await run();
  } else if (cmd === "all") {
    await seed();
    console.log();
    try { await run(); }
    finally {
      console.log("\n== cleanup ==");
      const c = await cleanup();
      check("cleanup: fake row and PDF removed, verified gone", c.ok, `rows ${c.rows}, objects ${c.objects}${c.ok ? "" : `, left: rows ${c.left}, objects ${c.objLeft}`}`);
    }
  }
  if (["run", "all"].includes(cmd)) {
    const pass = results.filter(Boolean).length;
    console.log(`\n== SUMMARY == ${pass}/${results.length} checks passed${warnings ? `, ${warnings} warning(s)` : ""} against ${workerUrl}`);
    if (pass === results.length) console.log('Label: "verified on Cloudflare" - for exactly the checks above (no Meta, no sed.sh).');
    else console.log("At least one check FAILED. Fix or report it before cutting over. Row/PDF cleanup: `node scripts/live-e2e.mjs cleanup`.");
    exitCode = pass === results.length ? 0 : 1;
  }
} catch (e) {
  console.error("script crashed:", e?.message ?? e);
  exitCode = 1;
  if (cmd === "run") console.error("Row/PDF left behind? Run: node scripts/live-e2e.mjs cleanup");
}
process.exit(exitCode);
