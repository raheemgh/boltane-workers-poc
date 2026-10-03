// scripts/openrouter-smoke.mjs — the Stage 4 "real OpenRouter" check and
// the timing the plan asks for before trusting non-streamed requests.
//
//   OPENROUTER_API_KEY=sk-or-... node scripts/openrouter-smoke.mjs
//   OPENROUTER_API_KEY=sk-or-... node scripts/openrouter-smoke.mjs --full
//   node scripts/openrouter-smoke.mjs --url https://your-worker.workers.dev
//
// Options:  --runs N (default 3)   --model openai/gpt-4o-mini (optional)
//           --full   also run ONE completion-path request (reply +
//                    extraction, the longest model call Ava makes) against
//                    a throwaway in-memory Supabase mock — nothing real is
//                    written anywhere. Local mode only.
//           --url U  hit an already-deployed Worker instead of starting
//                    `wrangler dev` (the key must be set on that Worker;
//                    plain turns only — never writes a signup).
//           --api-base U / --done-message "text"  (for self-testing with
//                    scripts/mock-openrouter.mjs)
//
// RUN THIS FROM A NETWORK THAT CAN REACH OpenRouter (GitHub Codespaces
// works; Syria without a VPN does not — that geo-block is exactly why the
// old build needed SOCKS5). Local `wrangler dev` runs on YOUR machine, so
// the numbers it prints are "this network -> OpenRouter", not "Cloudflare's
// edge -> OpenRouter". The second number only exists after a real deploy
// (Stage 7), where you re-run this with --url.
//
// What to look at:
//   - every request returns 200 with a real reply  -> the fetch() path works
//   - the ms column: is the blocking wait comfortable? (the plan's open
//     question was whether one long silent request is fine without a
//     SOCKS5 proxy in the middle; with no proxy, it is normal HTTP)
//   - the --full run: that is the worst case (up to 1200 output tokens)
//   - the key is NEVER printed by this script
import { startMockSupabase } from "./mock-supabase.mjs";
import { startWorker } from "./_worker.mjs";

const args = process.argv.slice(2);
const opt = (name, dflt) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : dflt; };
const has = (name) => args.includes(name);
const runs = Number(opt("--runs", 3));
const model = opt("--model", "");
const urlArg = opt("--url", "");
const apiBase = opt("--api-base", "");
const doneMessage = opt("--done-message", "That's everything, thanks - please finish the signup.");
const full = has("--full");

if (full && urlArg) { console.error("--full only works in local mode (it writes a signup row; only the in-memory mock is safe for that)."); process.exit(2); }
if (!urlArg && !apiBase && !process.env.OPENROUTER_API_KEY) {
  console.error("Set OPENROUTER_API_KEY (a test key with a small credit limit), or use --url for a deployed Worker.");
  process.exit(2);
}

let worker; let mock; let code = 1;
try {
  let base = urlArg.replace(/\/$/, "");
  if (!base) {
    mock = await startMockSupabase({ port: 8799, delayMs: 5, autoCreate: false });
    worker = await startWorker({
      port: 8788,
      vars: {
        SUPABASE_URL: mock.url,
        SUPABASE_SERVICE_ROLE_KEY: "local-test-key",
        OPENROUTER_API_KEY: process.env.OPENROUTER_API_KEY ?? "sk-self-test",
        ...(apiBase ? { OPENROUTER_API_BASE: apiBase } : {}),
        ...(model ? { AVA_CONVERSATION_MODEL: model } : {}),
        RAHEEM_WHATSAPP_NUMBER: "+15559998888",
      },
    });
    base = worker.url;
  }
  console.log(`Target: ${base}${model ? `  model: ${model}` : ""}\n`);

  const post = async (body) => {
    const t0 = Date.now();
    const res = await fetch(`${base}/chat`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const text = await res.text();
    let json = null; try { json = JSON.parse(text); } catch {}
    return { status: res.status, json, text, ms: Date.now() - t0 };
  };

  let ok = true;
  const times = [];
  for (let i = 1; i <= runs; i++) {
    const r = await post({ user_message: "Hi, I run a small candle shop and want a WhatsApp bot.", conversation_history: [] });
    const good = r.status === 200 && typeof r.json?.reply === "string";
    if (!good) ok = false; else times.push(r.ms);
    console.log(`plain turn ${i}: HTTP ${r.status} | ${r.ms} ms | ${good ? `done:${r.json.done} | "${r.json.reply.replace(/\s+/g, " ").slice(0, 70)}..."` : r.text.slice(0, 200)}`);
  }
  if (times.length) console.log(`  -> min ${Math.min(...times)} / max ${Math.max(...times)} ms over ${times.length} ok runs`);

  if (full) {
    console.log("\nfull completion path (reply + extraction; Supabase is an in-memory mock):");
    const history = [
      { role: "user", content: "Hi, I run a candle shop called Glow & Co and want a WhatsApp bot." },
      { role: "assistant", content: "Lovely! What would you like to call your assistant, and what should it help customers with?" },
      { role: "user", content: "Call it Nour. It answers questions about scents, prices and delivery, and takes orders." },
      { role: "assistant", content: "Great. Do you want to use your own AI key, or should we handle that for you?" },
      { role: "user", content: "You handle it. No referral code." },
    ];
    const cn = "+963900000777";
    const r = await post({ user_message: doneMessage, conversation_history: history, business_context: { contact_number: cn } });
    const row = await (await fetch(`${mock.url}/__row?contact_number=${encodeURIComponent(cn)}`)).json();
    const finished = r.status === 200 && r.json?.done === true && row?.status === "chat_complete";
    console.log(`  HTTP ${r.status} | ${r.ms} ms total (two model calls) | done:${r.json?.done} | saved row status: ${row?.status ?? "none"}`);
    if (r.status === 200 && r.json?.done !== true) {
      console.log("  The model did not signal done:true for that message, so no extraction ran. Re-run, or pass --done-message \"...\" with a clearer closing line. (Not a failure of the Worker.)");
    } else if (!finished) {
      ok = false;
      console.log(`  ${r.text.slice(0, 300)}`);
    }
  }

  console.log(`\n== ${ok ? "OK: real completions came back through POST /chat" : "PROBLEM: see the lines above"} ==`);
  code = ok ? 0 : 1;
} catch (err) {
  console.error(String(err));
} finally {
  if (worker) await worker.stop();
  if (mock) await mock.close();
}
process.exit(code);
