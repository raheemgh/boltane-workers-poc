// scripts/smoke-deployed.mjs — smoke test for a DEPLOYED Worker (Stage 7, step 4).
//
//   node scripts/smoke-deployed.mjs https://ava-onboarding-backend.<you>.workers.dev \
//        --origin https://<you>.github.io
//   add  --cron-secret <CRON_SECRET>  --run-cron   to also run the two jobs once
//   add  --with-chat                                to also make ONE real /chat call
//
// Result label if everything passes against the real URL: "verified on Cloudflare"
// — and ONLY for what is checked here. (Everything run before deploying, vitest and
// the scripts/*-check.mjs files, is "passed locally".)
//
// DEFAULT RUN IS SAFE: it never writes a row, never sends a message, never spends
// OpenRouter credit, never touches Meta. Every request is either rejected before any
// write (401/405/400/404) or a read-only lookup for a number that can't exist:
//   - /lookup-signup for a nonsense number  -> a real Supabase READ from Cloudflare's
//     network (proves SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY are right)
//   - /upload-pdf with a made-up phone_number_id and a junk file -> real multipart
//     parsing + a Supabase READ, then 404 BEFORE anything is stored or scanned
// Opt-in, because they are NOT read-only:
//   --run-cron   runs activate-pending + sweep-pdf-scans for real, on real rows (a
//                row you set to 'verified_ready' WILL be activated, a scan_pending
//                row WILL be resolved) — exactly what the schedule would do anyway.
//   --with-chat  one POST /chat ("hi", no contact_number, so no Supabase write): a
//                real OpenRouter call from Cloudflare's network, costs a fraction of
//                a cent. This is the check that proves OpenRouter accepts Cloudflare.
//
// NOT covered, and can't be from outside: whether Cloudflare's scheduler really fires
// the two Cron Triggers (watch `npx wrangler tail`, or Dashboard -> Worker ->
// Observability / Triggers), the SetupLock Durable Object (POST /complete-setup writes
// real rows and calls Meta, so it is not smoke-tested here), and CPU-time limits under
// real traffic (watch for error 1102 in the logs).
const args = process.argv.slice(2);
const flag = (n) => args.includes(n);
const opt = (n, d) => { const i = args.indexOf(n); return i >= 0 ? args[i + 1] : d; };
const base = (args.find((a) => /^https?:\/\//.test(a)) ?? "").replace(/\/$/, "");
const origin = (opt("--origin", process.env.ORIGIN ?? "")).replace(/\/$/, "");
const cronSecret = opt("--cron-secret", process.env.CRON_SECRET ?? "");
const runCron = flag("--run-cron");
const withChat = flag("--with-chat");

if (!base || !origin) {
  console.error("Usage: node scripts/smoke-deployed.mjs <https://worker-url> --origin <https://your.github.io> [--cron-secret S --run-cron] [--with-chat]");
  process.exit(2);
}
if (runCron && !cronSecret) {
  console.error("--run-cron needs --cron-secret <CRON_SECRET> (or the CRON_SECRET env var).");
  process.exit(2);
}

const results = [];
const check = (name, ok, detail = "") => {
  results.push(ok);
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${detail ? " - " + detail : ""}`);
};
const call = async (path, init = {}) => {
  const t0 = Date.now();
  try {
    const res = await fetch(base + path, { ...init, signal: AbortSignal.timeout(60_000) });
    const text = await res.text();
    let json = null;
    try { json = JSON.parse(text); } catch {}
    return { status: res.status, text, json, ms: Date.now() - t0, h: (k) => res.headers.get(k) };
  } catch (e) {
    return { status: 0, text: String(e), json: null, ms: Date.now() - t0, h: () => null };
  }
};
const short = (t) => (t.length > 120 ? t.slice(0, 120) + "…" : t);

console.log(`Target ${base} | origin ${origin}${runCron ? " | --run-cron" : ""}${withChat ? " | --with-chat" : ""}\n`);

console.log("== 1. alive + CORS ==");
let r = await call("/health");
check("GET /health -> 200 { ok: true }", r.status === 200 && r.json?.ok === true, `${r.status} ${short(r.text)} (${r.ms}ms)`);
if (r.status === 0) {
  console.log("\nCannot reach the Worker at all — wrong URL, or not deployed yet. Stopping.");
  process.exit(1);
}
r = await call("/complete-setup", { method: "OPTIONS", headers: { Origin: origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type" } });
check("preflight from your site's origin -> 204 + Access-Control-Allow-Origin == that origin", r.status === 204 && r.h("access-control-allow-origin") === origin, `${r.status} allow-origin=${r.h("access-control-allow-origin")}`);
r = await call("/complete-setup", { method: "OPTIONS", headers: { Origin: "https://evil.example", "Access-Control-Request-Method": "POST" } });
check("preflight from another origin is NOT granted", r.h("access-control-allow-origin") !== "https://evil.example" && r.h("access-control-allow-origin") !== "*", `allow-origin=${r.h("access-control-allow-origin")}`);

console.log("\n== 2. reads from Supabase (proves SUPABASE_URL + service key, from Cloudflare's network) ==");
r = await call("/lookup-signup?contact_number=%2B000000000000", { headers: { Origin: origin } });
check("GET /lookup-signup, number that can't exist -> 200 { found: false }", r.status === 200 && r.json?.found === false, `${r.status} ${short(r.text)} (${r.ms}ms)` + (r.status === 500 ? "  <- 500 here usually means SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY is wrong" : ""));
check("…and that response carries the CORS header for your origin", r.h("access-control-allow-origin") === origin);
r = await call("/lookup-signup", { headers: { Origin: origin } });
check("GET /lookup-signup with no contact_number -> 400", r.status === 400);

console.log("\n== 3. multipart parsing on the real platform (no row can match, so nothing is stored) ==");
const fd = new FormData();
fd.append("phone_number_id", "smoke-test-no-such-id");
fd.append("file", new File([new TextEncoder().encode("%PDF-1.4 smoke junk, never stored")], "smoke.pdf", { type: "application/pdf" }));
r = await call("/upload-pdf", { method: "POST", headers: { Origin: origin }, body: fd });
check("POST /upload-pdf, unknown phone_number_id -> 404 (formData parsed + Supabase read worked, nothing stored)", r.status === 404, `${r.status} ${short(r.text)} (${r.ms}ms)` + (r.status === 500 ? "  <- 500: Supabase read failed" : /1102|exceeded resource limits/i.test(r.text) ? "  <- Cloudflare error 1102: CPU/memory limit (lower MAX_PDF_SIZE_BYTES)" : ""));
const fd2 = new FormData();
fd2.append("phone_number_id", "smoke-test-no-such-id");
r = await call("/upload-pdf", { method: "POST", headers: { Origin: origin }, body: fd2 });
check("POST /upload-pdf with no file -> 400 'file is required'", r.status === 400 && r.json?.error === "file is required", `${r.status} ${short(r.text)}`);

console.log("\n== 4. methods + auth are enforced ==");
r = await call("/chat", { headers: { Origin: origin } });
check("GET /chat -> 405", r.status === 405);
r = await call("/complete-setup", { headers: { Origin: origin } });
check("GET /complete-setup -> 405", r.status === 405);
r = await call("/cron/activate-pending");
check("GET /cron/activate-pending with no secret -> 401", r.status === 401, String(r.status));
r = await call("/cron/sweep-pdf-scans?secret=definitely-wrong");
check("GET /cron/sweep-pdf-scans with a wrong secret -> 401", r.status === 401, String(r.status));
r = await call("/chat", { method: "POST", headers: { Origin: origin, "Content-Type": "application/json" }, body: "{not json" });
check("POST /chat with malformed JSON -> 400 (not a 500)", r.status === 400, `${r.status} ${short(r.text)}`);
r = await call("/does-not-exist");
check("unknown path -> 404, not a crash", r.status === 404, String(r.status));

if (runCron) {
  console.log("\n== 5. (--run-cron) the two manual cron routes, FOR REAL ==");
  for (const job of ["activate-pending", "sweep-pdf-scans"]) {
    r = await call(`/cron/${job}`, { headers: { Authorization: `Bearer ${cronSecret}` } });
    check(`GET /cron/${job} with the real secret -> 200 + summary`, r.status === 200 && typeof r.json?.checked === "number", `${r.status} ${short(r.text)} (${r.ms}ms)`);
  }
}

if (withChat) {
  console.log("\n== 6. (--with-chat) one real OpenRouter call from Cloudflare's network ==");
  r = await call("/chat", {
    method: "POST",
    headers: { Origin: origin, "Content-Type": "application/json" },
    body: JSON.stringify({ conversation_history: [], user_message: "hi" }),
  });
  check("POST /chat 'hi' -> 200 with a non-empty reply", r.status === 200 && typeof r.json?.reply === "string" && r.json.reply.length > 0 && r.json.done === false,
    `${r.status} ${short(r.text)} (${r.ms}ms)` +
      (r.status === 502 ? "  <- 502 = the OpenRouter call failed (key/credit/blocked?)" : r.status === 500 ? "  <- 500: a secret is missing (see the error text)" : ""));
}

const pass = results.filter(Boolean).length;
console.log(`\n== SUMMARY == ${pass}/${results.length} checks passed against ${base}`);
if (pass === results.length) {
  console.log("Label: \"verified on Cloudflare\" — for exactly the checks above" + (runCron && withChat ? "." : `. Not yet run: ${[!withChat && "--with-chat (OpenRouter from Cloudflare)", !runCron && "--run-cron (the two jobs)"].filter(Boolean).join(", ")}.`));
}
process.exit(pass === results.length ? 0 : 1);
