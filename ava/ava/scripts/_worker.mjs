// scripts/_worker.mjs — shared plumbing for the scripts that need a local
// Worker: write a temporary .dev.vars, run `wrangler dev` (local workerd),
// wait until /health answers, and tear everything down afterwards (whole
// process group: `npx wrangler` spawns wrangler which spawns workerd, and
// killing only the outer one leaves workerd holding the port).
import { spawn } from "node:child_process";
import fs from "node:fs";

export async function portInUse(port) {
  try { await fetch(`http://127.0.0.1:${port}/health`, { signal: AbortSignal.timeout(1500) }); return true; } catch { return false; }
}

export async function startWorker({ port = 8788, vars = {} } = {}) {
  if (await portInUse(port)) throw new Error(`port ${port} is already serving something (a leftover wrangler dev?). Stop it first: pkill -x workerd`);
  const backup = fs.existsSync(".dev.vars") ? fs.readFileSync(".dev.vars", "utf8") : null;
  fs.writeFileSync(".dev.vars", Object.entries(vars).map(([k, v]) => `${k}=${v}`).join("\n") + "\n");

  const child = spawn("npx", ["wrangler", "dev", "--port", String(port), "--ip", "127.0.0.1"], { stdio: ["ignore", "pipe", "pipe"], detached: true });
  let log = "";
  child.stdout.on("data", (d) => (log += d));
  child.stderr.on("data", (d) => (log += d));

  const t0 = Date.now();
  let ready = false;
  while (Date.now() - t0 < 90_000) {
    try { if ((await fetch(`http://127.0.0.1:${port}/health`)).ok) { ready = true; break; } } catch {}
    await new Promise((r) => setTimeout(r, 500));
  }

  const stop = async () => {
    if (child.pid) {
      try { process.kill(-child.pid, "SIGTERM"); } catch {}
      await new Promise((r) => setTimeout(r, 500));
      try { process.kill(-child.pid, "SIGKILL"); } catch {}
      // Don't return until the port is really free — otherwise the next
      // run can find a half-dead workerd still answering.
      for (let i = 0; i < 20 && (await portInUse(port)); i++) await new Promise((r) => setTimeout(r, 250));
    }
    if (backup === null) fs.rmSync(".dev.vars", { force: true });
    else fs.writeFileSync(".dev.vars", backup);
  };
  if (!ready) {
    await stop();
    throw new Error("wrangler dev did not become ready. Last output:\n" + log.slice(-2000));
  }
  return { url: `http://127.0.0.1:${port}`, stop, getLog: () => log };
}
