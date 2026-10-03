// scripts/smoke-selfcheck.mjs — proves scripts/smoke-deployed.mjs itself works,
// by pointing it at a LOCAL `wrangler dev` Worker (mock Supabase + mock OpenRouter).
// You will run smoke-deployed.mjs once, against the real URL, at the most
// stressful moment of the migration; this makes sure a bug in the script
// doesn't cost you that time. Run: node scripts/smoke-selfcheck.mjs
// Result label: "passed locally". It says nothing about real Cloudflare.
import { spawn } from "node:child_process";
import { startMockSupabase } from "./mock-supabase.mjs";
import { startMockOpenRouter } from "./mock-openrouter.mjs";
import { startWorker } from "./_worker.mjs";

const ORIGIN = "https://boltane.github.io";
const SECRET = "selfcheck-cron-secret-0123456789abcdef";
const results = [];
const check = (name, ok, detail = "") => {
  results.push(ok);
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${detail ? " - " + detail : ""}`);
};
// ASYNC spawn on purpose: the mock Supabase / OpenRouter servers live in THIS process, so a
// blocking spawnSync would freeze their event loop and every Worker->mock call would hang.
const smoke = (url, extra = []) =>
  new Promise((resolve) => {
    const child = spawn("node", ["scripts/smoke-deployed.mjs", url, ...extra], { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "", stderr = "";
    child.stdout.on("data", (d) => (stdout += d));
    child.stderr.on("data", (d) => (stderr += d));
    const timer = setTimeout(() => child.kill("SIGKILL"), 120_000);
    child.on("close", (status) => { clearTimeout(timer); resolve({ status, stdout, stderr }); });
  });

const sb = await startMockSupabase({ port: 8799, delayMs: 10, autoCreate: false });
const or = await startMockOpenRouter({ port: 8798, delayMs: 20 });
let worker;
let code = 1;
try {
  worker = await startWorker({
    port: 8788,
    vars: {
      SUPABASE_URL: sb.url, SUPABASE_SERVICE_ROLE_KEY: "k", ALLOWED_ORIGIN: ORIGIN,
      CRON_SECRET: SECRET, OPENROUTER_API_KEY: "or-key", OPENROUTER_API_BASE: or.url,
      RAHEEM_WHATSAPP_NUMBER: "+15550001111",
    },
  });

  let r = await smoke(worker.url, ["--origin", ORIGIN]);
  let m = /(\d+)\/(\d+) checks passed/.exec(r.stdout);
  check("default (read-only) run: exit 0, every check passes", r.status === 0 && m && m[1] === m[2], `${m?.[0]}\n${r.status !== 0 ? r.stdout.slice(-1500) : ""}`);
  check("…and it wrote nothing (no rows, no storage objects) and made no OpenRouter call",
    (await (await fetch(`${sb.url}/__rows`)).json()).length === 0 &&
      Object.keys(await (await fetch(`${sb.url}/__storage`)).json()).length === 0 &&
      (await (await fetch(`${or.url}/__requests`)).json()).length === 0);

  r = await smoke(worker.url, ["--origin", ORIGIN, "--cron-secret", SECRET, "--run-cron", "--with-chat"]);
  m = /(\d+)\/(\d+) checks passed/.exec(r.stdout);
  check("--run-cron --with-chat: exit 0, every check passes", r.status === 0 && m && m[1] === m[2], `${m?.[0]}\n${r.status !== 0 ? r.stdout.slice(-1500) : ""}`);
  const orReqs = await (await fetch(`${or.url}/__requests`)).json();
  check("--with-chat made exactly ONE OpenRouter call (and nothing before it did)", orReqs.length === 1, String(orReqs.length));
  check("the final label names what was verified", /verified on Cloudflare/.test(r.stdout));

  r = await smoke(worker.url, ["--origin", "https://not-the-allowed-origin.example"]);
  check("wrong --origin is CAUGHT: exit 1 and the CORS check is the one that FAILs", r.status === 1 && /\[FAIL\] preflight from your site's origin/.test(r.stdout));

  r = await smoke(worker.url, ["--origin", ORIGIN, "--cron-secret", "wrong", "--run-cron"]);
  check("--run-cron with a wrong secret is CAUGHT: exit 1", r.status === 1 && /\[FAIL\] GET \/cron\/activate-pending with the real secret/.test(r.stdout));

  r = await smoke("http://127.0.0.1:1", ["--origin", ORIGIN]);
  check("unreachable URL: exits 1 right away with a clear message (not a stack trace)", r.status === 1 && /Cannot reach the Worker/.test(r.stdout), r.stdout.slice(0, 200));

  r = await smoke(worker.url, []);
  check("missing --origin: usage message, exit 2", r.status === 2 && /Usage:/.test(r.stderr));

  const pass = results.filter(Boolean).length;
  console.log(`\n== SUMMARY == ${pass}/${results.length} checks passed`);
  code = pass === results.length ? 0 : 1;
} catch (e) {
  console.error("selfcheck crashed:", e);
  if (worker) console.error(worker.getLog().slice(-2000));
} finally {
  if (worker) await worker.stop();
  await sb.close();
  await or.close();
}
process.exit(code);
