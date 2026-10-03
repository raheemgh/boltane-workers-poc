// scripts/cron-check.mjs — Stage 6 end-to-end check on a REAL local workerd
// (`wrangler dev`) with a mock Supabase behind it. Run: node scripts/cron-check.mjs
// Label the result: "passed locally".
//
// It drives wrangler's scheduled-event endpoint — the same trick as
//   curl "http://127.0.0.1:8788/cdn-cgi/handler/scheduled?cron=*/15+*+*+*+*"
// — so the REAL scheduled() export runs (not a unit-test call), reads the REAL
// wrangler.jsonc cron strings' counterparts, goes through the real
// findVerifiedReadySignups -> toStoreRow -> insertStoreRow -> updatePendingSignup
// chain, and prints the summary to the Worker log, which is what is asserted.
//
// What it does NOT prove: that Cloudflare's real scheduler fires these two
// expressions on time (only a deployed Worker shows that), nor the sed.sh verdict
// branches (clean/infected/still-running): sed.sh's base URL is hard-coded, so
// here the sweep can only reach its per-row 'error' branch (no SED_SH_API_KEY).
// Those branches are covered, unchanged, by tests/pdfScanSweep*.test.ts.
import { startMockSupabase } from "./mock-supabase.mjs";
import { startWorker } from "./_worker.mjs";

const ORIGIN = "https://boltane.github.io";
const SECRET = "local-cron-secret";
const results = [];
const check = (name, ok, detail = "") => {
  results.push(ok);
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${detail ? " - " + detail : ""}`);
};

const sb = await startMockSupabase({ port: 8799, delayMs: 10, autoCreate: false });
let worker;
let code = 1;
const seed = (row) => fetch(`${sb.url}/__seed`, { method: "POST", body: JSON.stringify(row) }).then((r) => r.json());
const rows = async () => (await fetch(`${sb.url}/__rows`)).json();
const stores = async () => (await fetch(`${sb.url}/__stores`)).json();
const byPn = async (pn) => (await rows()).find((r) => r.phone_number_id === pn);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// every "[cron] <job> {json}" line the Worker has printed so far
const cronLines = (job) =>
  worker.getLog().split("\n").filter((l) => l.includes(`[cron] ${job} `)).map((l) => JSON.parse(l.slice(l.indexOf("{"))));
const waitForLines = async (job, n) => {
  for (let i = 0; i < 40 && cronLines(job).length < n; i++) await sleep(200);
  return cronLines(job);
};
const fireCron = async (expr) =>
  fetch(`${worker.url}/cdn-cgi/handler/scheduled?cron=${expr.replaceAll(" ", "+")}`);

const complete = (tag, over = {}) => ({
  status: "verified_ready", phone_number_id: `pn-${tag}`, store_name: `Store ${tag}`,
  system_prompt: "You are a bot.", access_token: "tok", package: "low-tier", is_api_free: false,
  contact_number: `+1555000${tag}`, ...over,
});

try {
  worker = await startWorker({
    port: 8788,
    vars: { SUPABASE_URL: sb.url, SUPABASE_SERVICE_ROLE_KEY: "local-test-key", CRON_SECRET: SECRET, ALLOWED_ORIGIN: ORIGIN },
  });
  console.log(`Worker ${worker.url} | mock Supabase ${sb.url}\n`);

  await seed(complete("1"));                                   // A: activates cleanly
  await seed(complete("2"));                                   // B: its first store insert is injected to fail
  await seed(complete("3", { system_prompt: "" }));            // C: missing a required field -> never activates
  await seed({ status: "otp_sent", phone_number_id: "pn-4", contact_number: "+15550004" }); // D: must be untouched
  await seed({ status: "scan_pending", phone_number_id: "pn-5", contact_number: "+15550005", scan_id: "scan-x", pdf_storage_path: "x.pdf", pdf_uploaded: true }); // E
  await seed({ status: "draft", phone_number_id: "pn-6", contact_number: "+15550006", otp_code: null,
    conversation_history: [{ role: "user", content: "hi" }], business_context: { store_name: "Six" } }); // F: resumable
  await seed({ status: "otp_sent", phone_number_id: "pn-7", contact_number: "+15550007", otp_code: "654321",
    conversation_history: [{ role: "user", content: "secret chat" }], business_context: { store_name: "Seven" } }); // G: OTP sent
  await fetch(`${sb.url}/__fail_store?phone_number_id=pn-2`);

  console.log("== 1. scheduled(*/15) = activation, via wrangler's cdn-cgi scheduled endpoint ==");
  let res = await fireCron("*/15 * * * *");
  check("scheduled event accepted (200)", res.status === 200, `${res.status}`);
  let lines = await waitForLines("activate-pending", 1);
  const s1 = lines[0];
  check("summary logged: checked 3 / activated 1 / failed 2", s1?.checked === 3 && s1?.activated === 1 && s1?.failed === 2, JSON.stringify(s1));
  check("failures name the injected-500 row AND the missing-field row (with the real toStoreRow message)",
    s1?.failures?.length === 2 && s1.failures.some((f) => /system_prompt/.test(f.error)));
  let st = await stores();
  check("exactly one store row inserted, built by the real toStoreRow()", st.length === 1 && st[0]?.phone_number_id === "pn-1" && st[0]?.store_name === "Store 1" && st[0]?.package === "low-tier" && st[0]?.is_api_free === false, JSON.stringify(st[0]));
  check("A marked 'activated'; B (insert failed) and C (bad row) still 'verified_ready' so they retry", (await byPn("pn-1")).status === "activated" && (await byPn("pn-2")).status === "verified_ready" && (await byPn("pn-3")).status === "verified_ready");
  check("D (otp_sent) and E (scan_pending) untouched by the activation job", (await byPn("pn-4")).status === "otp_sent" && (await byPn("pn-5")).status === "scan_pending");

  console.log("\n== 2. second scheduled run: retries what failed, never re-activates what succeeded ==");
  await fireCron("*/15 * * * *");
  lines = await waitForLines("activate-pending", 2);
  const s2 = lines[1];
  check("checked 2 (B + C), activated 1 (B), failed 1 (C)", s2?.checked === 2 && s2?.activated === 1 && s2?.failed === 1, JSON.stringify(s2));
  st = await stores();
  check("2 store rows total, A NOT inserted twice", st.length === 2 && st.filter((x) => x.phone_number_id === "pn-1").length === 1, st.map((x) => x.phone_number_id).join(","));

  console.log("\n== 3. scheduled(*/20) = PDF scan sweep ==");
  res = await fireCron("*/20 * * * *");
  lines = await waitForLines("sweep-pdf-scans", 1);
  const w = lines[0];
  check("summary logged with the expected keys, checked 1", w && ["checked", "clean", "infected", "stillRunning", "errors"].every((k) => k in w) && w.checked === 1, JSON.stringify(w));
  check("no SED_SH_API_KEY -> that row reports 'error' (Missing SED_SH_API_KEY), nothing resolved", w?.errors?.[0]?.id && /Missing SED_SH_API_KEY/.test(w.errors[0].error) && w.clean === 0 && w.infected === 0);
  const e = await byPn("pn-5");
  check("E stays scan_pending with its file + scan_id intact (an erroring sweep must not destroy anything)", e.status === "scan_pending" && e.scan_id === "scan-x" && e.pdf_uploaded === true);

  console.log("\n== 4. an unrecognized cron expression fails loudly, not silently ==");
  res = await fireCron("0 3 * * *");
  await sleep(500);
  check("non-200 from the scheduled endpoint", res.status !== 200, `${res.status}`);
  check("the Worker log names the bad expression", /unrecognized cron expression "0 3 \* \* \*"/.test(worker.getLog()));

  console.log("\n== 5. manual GET /cron/* routes still work, behind CRON_SECRET ==");
  let r = await fetch(`${worker.url}/cron/activate-pending`);
  check("no secret -> 401", r.status === 401);
  r = await fetch(`${worker.url}/cron/activate-pending?secret=wrong`);
  check("wrong secret -> 401", r.status === 401);
  r = await fetch(`${worker.url}/cron/activate-pending?secret=${SECRET}`);
  let j = await r.json();
  check("?secret=<CRON_SECRET> -> 200 with the summary (C is the only candidate left, fails again)", r.status === 200 && j.checked === 1 && j.activated === 0 && j.failed === 1, JSON.stringify(j));
  r = await fetch(`${worker.url}/cron/activate-pending`, { headers: { Authorization: `Bearer ${SECRET}` } });
  check("Authorization: Bearer <CRON_SECRET> -> 200", r.status === 200);
  r = await fetch(`${worker.url}/cron/sweep-pdf-scans?secret=${SECRET}`);
  j = await r.json();
  check("sweep route -> 200 with the sweep summary shape", r.status === 200 && j.checked === 1 && Array.isArray(j.errors), JSON.stringify(j));
  r = await fetch(`${worker.url}/cron/sweep-pdf-scans?secret=${SECRET}&secret=${SECRET}`);
  check("repeated ?secret= rejected (401), same as the Express array case", r.status === 401);
  r = await fetch(`${worker.url}/cron/sweep-pdf-scans?secret=${SECRET}`, { method: "POST" });
  check("POST -> 405", r.status === 405);

  console.log("\n== 6. GET /lookup-signup on the real stack ==");
  const look = async (qs) => { const x = await fetch(`${worker.url}/lookup-signup${qs}`, { headers: { Origin: ORIGIN } }); return { status: x.status, text: await x.text(), cors: x.headers.get("access-control-allow-origin") }; };
  let a = await look("?contact_number=%2B15550006");
  j = JSON.parse(a.text);
  check("resumable draft -> found:true + the saved transcript and business_context", a.status === 200 && j.found === true && j.conversation_history?.[0]?.content === "hi" && j.business_context?.store_name === "Six", a.text);
  check("CORS header present", a.cors === ORIGIN);
  const noRow = await look("?contact_number=%2B19999999");
  const otpRow = await look("?contact_number=%2B15550007");
  check("PRIVACY: unknown number and OTP-already-sent number get byte-identical responses", noRow.status === 200 && noRow.text === otpRow.text && noRow.text === '{"found":false}', `${noRow.text} | ${otpRow.text}`);
  check("PRIVACY: nothing from the OTP-sent row leaks", !otpRow.text.includes("secret chat") && !otpRow.text.includes("Seven"));
  check("missing contact_number -> 400", (await look("")).status === 400);
  check("repeated contact_number -> 400", (await look("?contact_number=a&contact_number=b")).status === 400);

  const pass = results.filter(Boolean).length;
  console.log(`\n== SUMMARY == ${pass}/${results.length} checks passed`);
  code = pass === results.length ? 0 : 1;
} catch (e) {
  console.error("check script crashed:", e);
  if (worker) console.error("worker log tail:\n" + worker.getLog().slice(-2500));
} finally {
  if (worker) await worker.stop();
  await sb.close();
}
process.exit(code);
