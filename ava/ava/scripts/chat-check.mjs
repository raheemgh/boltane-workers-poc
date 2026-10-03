// scripts/chat-check.mjs — Stage 4 end-to-end check of POST /chat on a
// REAL local workerd (`wrangler dev`), with a mock OpenRouter and a mock
// Supabase behind it. Run:  node scripts/chat-check.mjs
//
// What it proves that the Vitest suite can't: the whole Worker bundle
// boots and routes /chat, `process.env` reaches the handler, the real
// fetch() path (not a stub) talks to a real HTTP server, CORS headers
// survive, and — checked on the wire, from the mock's point of view —
// no raw phone number is ever sent upstream, no `stream` flag is sent,
// and the retry rules hold through the real stack.
// What it does NOT prove: that OpenRouter accepts the request from
// Cloudflare's network, or what a real completion costs in latency
// (that's scripts/openrouter-smoke.mjs, which needs a real key and a
// network that can reach OpenRouter — i.e. NOT Syria without a VPN).
import { startMockOpenRouter } from "./mock-openrouter.mjs";
import { startMockSupabase } from "./mock-supabase.mjs";
import { startWorker } from "./_worker.mjs";

const ORIGIN = "https://boltane.github.io";
const KEY = "sk-mock-key";
const PHONE = "0999123456";
const results = [];
const check = (name, ok, detail = "") => {
  results.push(ok);
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${detail ? " - " + detail : ""}`);
};

const or = await startMockOpenRouter({ port: 8798, delayMs: 50 });
const sb = await startMockSupabase({ port: 8799, delayMs: 20, autoCreate: false });
let worker;
let code = 1;

const orReqs = async () => (await fetch(`${or.url}/__requests`)).json();
const orReset = async (times = 0, status = 429) => fetch(`${or.url}/__fail?times=${times}&status=${status}`);
const sbRow = async (cn) => (await fetch(`${sb.url}/__row?contact_number=${encodeURIComponent(cn)}`)).json();

try {
  worker = await startWorker({
    port: 8788,
    vars: {
      SUPABASE_URL: sb.url,
      SUPABASE_SERVICE_ROLE_KEY: "local-test-key",
      OPENROUTER_API_KEY: KEY,
      OPENROUTER_API_BASE: or.url,
      RAHEEM_WHATSAPP_NUMBER: "+15559998888",
      ALLOWED_ORIGIN: ORIGIN,
    },
  });
  const chat = async (body, { raw, method = "POST" } = {}) => {
    const t0 = Date.now();
    const res = await fetch(`${worker.url}/chat`, {
      method,
      headers: { "Content-Type": "application/json", Origin: ORIGIN },
      ...(method === "GET" ? {} : { body: raw ?? JSON.stringify(body) }),
    });
    const text = await res.text();
    let json = null;
    try { json = JSON.parse(text); } catch {}
    return { status: res.status, json, text, ms: Date.now() - t0, cors: res.headers.get("access-control-allow-origin") };
  };
  console.log(`Worker ${worker.url} | mock OpenRouter ${or.url} | mock Supabase ${sb.url}\n`);

  console.log("== 1. one plain turn (no contact_number) ==");
  await orReset();
  let r = await chat({ user_message: "hello", conversation_history: [] });
  let reqs = await orReqs();
  check("200 with { reply, done:false }", r.status === 200 && typeof r.json?.reply === "string" && r.json?.done === false, `status ${r.status}`);
  check("exactly 1 upstream request", reqs.length === 1, `${reqs.length}`);
  check("upstream got the bearer key", reqs[0]?.auth === `Bearer ${KEY}`);
  check("NO `stream` flag sent, json_object + max_tokens 200", !("stream" in (reqs[0]?.body ?? {})) && reqs[0]?.body?.response_format?.type === "json_object" && reqs[0]?.body?.max_tokens === 200);
  check("CORS header present on the real response", r.cors === ORIGIN);

  console.log("\n== 2. PII never leaves the Worker ==");
  await orReset();
  r = await chat({ user_message: `my whatsapp is ${PHONE}`, conversation_history: [] });
  const wire = JSON.stringify((await orReqs())[0]?.body ?? {});
  check("raw phone number absent from what was sent upstream", !wire.includes(PHONE) && wire.includes("[PHONE_1]"));
  check("real number restored in the reply the client gets", r.status === 200 && r.json?.reply?.includes(PHONE) && !r.json.reply.includes("[PHONE_"), r.json?.reply);

  console.log("\n== 3. retry through the real stack ==");
  await orReset(1, 429);
  r = await chat({ user_message: "hello", conversation_history: [] });
  reqs = await orReqs();
  check("429 once -> 200 after one retry, ~2s backoff", r.status === 200 && reqs.length === 2 && r.ms >= 1900 && r.ms < 6000, `${reqs.length} upstream calls, ${r.ms}ms`);
  await orReset(1, 400);
  r = await chat({ user_message: "hello", conversation_history: [] });
  reqs = await orReqs();
  check("400 is NOT retried: 502 straight away, 1 upstream call", r.status === 502 && reqs.length === 1 && r.ms < 1500, `${reqs.length} calls, ${r.ms}ms`);
  check("502 body leaks no upstream detail", r.json?.error === "Upstream model call failed" && !r.text.includes("mock injected") && !r.text.includes(KEY));
  await orReset(5, 500);
  r = await chat({ user_message: "hello", conversation_history: [] });
  reqs = await orReqs();
  check("persistent 500: 3 attempts total, then 502 (after ~6s of backoff)", r.status === 502 && reqs.length === 3 && r.ms >= 5800, `${reqs.length} calls, ${r.ms}ms`);
  await orReset();

  console.log("\n== 4. full completion with contact_number (Supabase draft -> chat_complete) ==");
  const CN = "+963900000001";
  r = await chat({ user_message: "DONE", conversation_history: [], business_context: { contact_number: CN } });
  reqs = await orReqs();
  const row = await sbRow(CN);
  check("200 done:true pointing to the next step", r.status === 200 && r.json?.done === true && /next step/.test(r.json?.reply ?? ""), `status ${r.status}`);
  check("2 upstream calls (reply + extraction, extraction capped at 1200)", reqs.length === 2 && reqs[1]?.body?.max_tokens === 1200, `${reqs.length}`);
  check("row finalized: status chat_complete, extracted store_name saved, NO otp_code", row?.status === "chat_complete" && row?.store_name === "Mock Candles" && !row?.otp_code, JSON.stringify({ status: row?.status, store: row?.store_name }));

  console.log("\n== 5. duplicate block (OTP already sent) ==");
  await orReset();
  const CN2 = "+963900000002";
  await fetch(`${sb.url}/__seed`, { method: "POST", body: JSON.stringify({ contact_number: CN2, status: "otp_sent", otp_code: "123456789012" }) });
  r = await chat({ user_message: "hi again", conversation_history: [], business_context: { contact_number: CN2 } });
  check("200 blocked:true and ZERO upstream calls", r.status === 200 && r.json?.blocked === true && (await orReqs()).length === 0);

  console.log("\n== 6. transport behavior ==");
  r = await chat(null, { method: "GET" });
  check("GET -> 405 JSON", r.status === 405 && r.json?.error === "Method not allowed");
  r = await chat(null, { raw: "{bad" });
  check("malformed JSON -> 400 { error: 'Invalid request' } with CORS header", r.status === 400 && r.json?.error === "Invalid request" && r.cors === ORIGIN);
  r = await chat({ conversation_history: [] });
  check("missing user_message -> 400", r.status === 400 && r.json?.error === "user_message is required");
  const pre = await fetch(`${worker.url}/chat`, { method: "OPTIONS", headers: { Origin: ORIGIN, "Access-Control-Request-Method": "POST" } });
  check("OPTIONS preflight -> 204 + allowed origin", pre.status === 204 && pre.headers.get("access-control-allow-origin") === ORIGIN);

  const failed = results.filter((x) => !x).length;
  console.log(`\n== SUMMARY == ${results.length - failed}/${results.length} checks passed`);
  code = failed === 0 ? 0 : 1;
} catch (err) {
  console.error(String(err));
} finally {
  if (worker) await worker.stop();
  await or.close();
  await sb.close();
}
process.exit(code);
