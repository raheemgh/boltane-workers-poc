// scripts/concurrency-test.mjs
//
// Black-box test of the double-submit guard on the REAL route
// (POST /complete-setup), adapted from the audit PoC's
// test/concurrency-test.mjs (which hit a standalone GET /lock).
// Normally run through scripts/lock-check.mjs, which starts the mock
// Supabase and `wrangler dev` for you.
//
// Usage: node scripts/concurrency-test.mjs <base-url> --mock <mock-url> [--rounds N]
//
// What changed vs the PoC, and why:
//  - The route has a SECOND guard (DB status must be 'chat_complete'),
//    so "re-acquire after finishing" is no longer a 200: the second
//    sequential call correctly gets the DB's 409 "already been
//    completed". REACQUIRE therefore checks that the 409 is NOT the
//    lock's "already being processed" one (= lock was released), and
//    adds a failure-path check (500 then a retry that works).
//  - Every check also asserts the side effect: the mock Supabase counts
//    OTP writes per contact_number. Exactly one winner must mean
//    exactly ONE OTP — that is the real bug the lock exists to prevent
//    (a second OTP overwriting the first, invalidating the code the
//    client was shown).
//  - INDEPENDENT measures a solo request first instead of hard-coding
//    the hold time (the real work's duration isn't fixed).
const args = process.argv.slice(2);
const baseUrl = (args[0] ?? "").replace(/\/$/, "");
const opt = (name, dflt) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : dflt;
};
const mockUrl = (opt("--mock", "") ?? "").replace(/\/$/, "");
const rounds = Number(opt("--rounds", 8));
if (!baseUrl || !mockUrl) {
  console.error("Usage: node scripts/concurrency-test.mjs <base-url> --mock <mock-url> [--rounds N]");
  process.exit(2);
}

const fresh = (tag) => `+9639${Date.now().toString().slice(-6)}${Math.floor(Math.random() * 1e3)}-${tag}`;
const isLock409 = (r) => r.status === 409 && /already being processed/.test(r.body?.error ?? "");
const isDb409 = (r) => r.status === 409 && /already been completed/.test(r.body?.error ?? "");
const count = (arr, pred) => arr.filter(pred).length;

async function hit(key) {
  const t0 = Date.now();
  try {
    const res = await fetch(`${baseUrl}/complete-setup`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ contact_number: key, legal_name: "Test LLC", phone_number_id: "109364823947271", access_token: "tok" }),
    });
    const body = await res.json().catch(() => null);
    return { status: res.status, body, ms: Date.now() - t0 };
  } catch (err) {
    return { status: 0, body: { error: String(err) }, ms: Date.now() - t0 };
  }
}
const stats = async (key) => (await fetch(`${mockUrl}/__stats?contact_number=${encodeURIComponent(key)}`)).json();
const failNext = (key) => fetch(`${mockUrl}/__fail_next?contact_number=${encodeURIComponent(key)}`);

const failures = [];
function record(name, ok, detail) {
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${detail ? " - " + detail : ""}`);
  if (!ok) failures.push(name);
}

console.log(`Target: ${baseUrl}   Mock Supabase: ${mockUrl}   Rounds: ${rounds}\n`);

// 1. RACE
console.log("== 1. RACE: 2 concurrent, same fresh contact_number, per round ==");
let raceOk = 0;
for (let i = 1; i <= rounds; i++) {
  const key = fresh(`race${i}`);
  const [a, b] = await Promise.all([hit(key), hit(key)]);
  const s = await stats(key);
  const ok = count([a, b], (r) => r.status === 200) === 1 && count([a, b], isLock409) === 1 && s.otp_count === 1;
  if (ok) raceOk++;
  console.log(`round ${String(i).padStart(2)} | A: ${a.status} B: ${b.status} | OTPs generated: ${s.otp_count} | ${ok ? "PASS" : "FAIL (need one 200 + one lock-409 + exactly 1 OTP)"}`);
}
record("RACE", raceOk === rounds, `${raceOk}/${rounds} rounds`);

// 2. BURST
console.log("\n== 2. BURST: 5 concurrent, same fresh contact_number ==");
let burstOk = 0;
const burstRounds = Math.max(3, Math.floor(rounds / 2));
for (let i = 1; i <= burstRounds; i++) {
  const key = fresh(`burst${i}`);
  const rs = await Promise.all(Array.from({ length: 5 }, () => hit(key)));
  const s = await stats(key);
  const ok = count(rs, (r) => r.status === 200) === 1 && count(rs, isLock409) === 4 && s.otp_count === 1;
  if (ok) burstOk++;
  console.log(`burst ${i} | statuses: ${rs.map((r) => r.status).join(",")} | OTPs: ${s.otp_count} | ${ok ? "PASS" : "FAIL"}`);
}
record("BURST", burstOk === burstRounds, `${burstOk}/${burstRounds} bursts`);

// 3. REACQUIRE (lock is released after success, after failure, after a race)
console.log("\n== 3. REACQUIRE: lock released after success / failure / a concurrent pair ==");
{
  const k1 = fresh("reacq-ok");
  const first = await hit(k1);
  const second = await hit(k1); // sequential, after the winner finished
  console.log(`sequential | 1st: ${first.status}  2nd: ${second.status} (${isDb409(second) ? "DB says already completed = lock was free" : isLock409(second) ? "LOCK STILL HELD" : "unexpected"})`);
  const okSeq = first.status === 200 && isDb409(second);

  const k2 = fresh("reacq-fail");
  await failNext(k2);
  const failed = await hit(k2); // OTP write fails -> 500
  const retry = await hit(k2); // must not be lock-409; the failed attempt never reached 'otp_sent'
  console.log(`after failure | 1st: ${failed.status}  retry: ${retry.status}`);
  const okFail = failed.status === 500 && retry.status === 200;

  const k3 = fresh("reacq-after-race");
  await Promise.all([hit(k3), hit(k3)]);
  const after = await hit(k3);
  console.log(`after a concurrent pair | next call: ${after.status} (${isDb409(after) ? "lock free" : isLock409(after) ? "LOCK STILL HELD" : "unexpected"})`);
  record("REACQUIRE", okSeq && okFail && isDb409(after));
}

// 4. INDEPENDENT
console.log("\n== 4. INDEPENDENT: different contact_numbers, concurrent ==");
{
  const solo = [];
  for (let i = 0; i < 2; i++) solo.push((await hit(fresh(`solo${i}`))).ms);
  const base = Math.min(...solo);
  console.log(`solo request time: ${solo.join("ms, ")}ms (baseline ${base}ms; serialized pair would be ~${base * 2}ms)`);
  let ok = true;
  for (let i = 1; i <= 3; i++) {
    const t0 = Date.now();
    const [a, b] = await Promise.all([hit(fresh(`indA${i}`)), hit(fresh(`indB${i}`))]);
    const wall = Date.now() - t0;
    const good = a.status === 200 && b.status === 200 && wall < base * 1.6;
    if (!good) ok = false;
    console.log(`pair ${i} | A: ${a.status} B: ${b.status} | wall ${wall}ms | ${good ? "PASS" : "FAIL"}`);
  }
  record("INDEPENDENT", ok);
}

console.log("\n== 5. TTL ==");
console.log("[N/A ] Not implemented (request-scoped lock, released in `finally`). Not counted as a pass.");

console.log("\n== SUMMARY ==");
if (failures.length === 0) {
  console.log("ALL CHECKS PASSED (1-4). TTL: N/A.");
  process.exit(0);
}
console.log(`FAILED: ${failures.join(", ")}`);
process.exit(1);
