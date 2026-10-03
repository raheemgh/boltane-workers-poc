// scripts/lock-check.mjs — one command for the Stage 3 lock check.
//
//   node scripts/lock-check.mjs                     # normal run
//   node scripts/lock-check.mjs --rounds 4          # fewer rounds (faster)
//   node scripts/lock-check.mjs --negative-control  # breaks the lock ON PURPOSE
//                                                   # and passes only if the test FAILS
//
// Starts a mock Supabase, writes a temporary .dev.vars, runs
// `wrangler dev` (local workerd), runs scripts/concurrency-test.mjs
// against it, then cleans everything up (including restoring
// src/setupLock.ts after a negative control, even on Ctrl-C).
// Result label: "passed locally" — see CHANGES.md for what that does
// and doesn't prove.
import { spawn } from "node:child_process";
import fs from "node:fs";
import { startMockSupabase } from "./mock-supabase.mjs";

const args = process.argv.slice(2);
const negative = args.includes("--negative-control");
const ri = args.indexOf("--rounds");
const rounds = ri >= 0 ? args[ri + 1] : "8";
const WORKER_PORT = 8788;
const MOCK_PORT = 8799;
const LOCK_FILE = "src/setupLock.ts";
const CHECK_LINE = "    this.locked = true;\n";
const MUTATION = "    await new Promise((r) => setTimeout(r, 5)); // NEGATIVE CONTROL: yield between check and set\n" + CHECK_LINE;

const originalLock = fs.readFileSync(LOCK_FILE, "utf8");
const devVarsBackup = fs.existsSync(".dev.vars") ? fs.readFileSync(".dev.vars", "utf8") : null;
let wrangler;
let mock;
let cleaned = false;

async function cleanup() {
  if (cleaned) return;
  cleaned = true;
  // `npx wrangler` spawns wrangler, which spawns workerd: killing only
  // the npx process leaves workerd holding the port (and answering the
  // NEXT run with stale config). Kill the whole process group.
  if (wrangler?.pid) {
    try { process.kill(-wrangler.pid, "SIGTERM"); } catch {}
    await new Promise((r) => setTimeout(r, 500));
    try { process.kill(-wrangler.pid, "SIGKILL"); } catch {}
  }
  if (mock) await mock.close();
  fs.writeFileSync(LOCK_FILE, originalLock); // always restore
  if (devVarsBackup === null) fs.rmSync(".dev.vars", { force: true });
  else fs.writeFileSync(".dev.vars", devVarsBackup);
}
for (const sig of ["SIGINT", "SIGTERM"]) process.on(sig, async () => { await cleanup(); process.exit(130); });

async function portInUse(port) {
  try { await fetch(`http://127.0.0.1:${port}/health`, { signal: AbortSignal.timeout(1500) }); return true; } catch { return false; }
}

async function waitReady(url, ms = 90_000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    try { if ((await fetch(url)).ok) return true; } catch {}
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

let exitCode = 1;
try {
  if (negative) {
    if (!originalLock.includes(CHECK_LINE)) throw new Error(`cannot apply negative control: "${CHECK_LINE.trim()}" not found in ${LOCK_FILE}`);
    fs.writeFileSync(LOCK_FILE, originalLock.replace(CHECK_LINE, MUTATION));
    console.log(`>> NEGATIVE CONTROL: ${LOCK_FILE} temporarily broken (await between check and set). The test below MUST fail.\n`);
  }

  if (await portInUse(WORKER_PORT)) {
    throw new Error(`port ${WORKER_PORT} is already serving something (a leftover wrangler dev?). Stop it first: pkill -f workerd`);
  }
  mock = await startMockSupabase({ port: MOCK_PORT, delayMs: 150 });
  fs.writeFileSync(".dev.vars", [
    `SUPABASE_URL=${mock.url}`,
    "SUPABASE_SERVICE_ROLE_KEY=local-test-key",
    "RAHEEM_WHATSAPP_NUMBER=+15559998888",
    "",
  ].join("\n"));

  wrangler = spawn("npx", ["wrangler", "dev", "--port", String(WORKER_PORT), "--ip", "127.0.0.1"], { stdio: ["ignore", "pipe", "pipe"], detached: true });
  let log = "";
  wrangler.stdout.on("data", (d) => (log += d));
  wrangler.stderr.on("data", (d) => (log += d));
  if (!(await waitReady(`http://127.0.0.1:${WORKER_PORT}/health`))) {
    console.error("wrangler dev did not become ready. Last output:\n" + log.slice(-2000));
    throw new Error("wrangler dev not ready");
  }

  const test = spawn("node", ["scripts/concurrency-test.mjs", `http://127.0.0.1:${WORKER_PORT}`, "--mock", mock.url, "--rounds", rounds], { stdio: "inherit" });
  const code = await new Promise((r) => test.on("exit", r));

  if (negative) {
    console.log(code !== 0
      ? "\n>> NEGATIVE CONTROL OK: the broken lock was caught (test failed as it should). The passing numbers from the normal run can be trusted."
      : "\n>> NEGATIVE CONTROL FAILED: the test passed against a deliberately broken lock, so it cannot be trusted.");
    exitCode = code !== 0 ? 0 : 1;
  } else {
    exitCode = code ?? 1;
  }
} catch (err) {
  console.error(String(err));
} finally {
  await cleanup();
}
process.exit(exitCode);
