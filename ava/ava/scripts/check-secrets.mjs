// scripts/check-secrets.mjs — validates secrets.json BEFORE you upload it.
//   node scripts/check-secrets.mjs [path]      (default: secrets.json)
//   then:  npx wrangler secret bulk secrets.json
//
// Catches the mistakes that otherwise only show up as a broken production:
// a template placeholder uploaded as a real value, a trailing slash or path in
// ALLOWED_ORIGIN (CORS compares the origin EXACTLY — "https://x.github.io/"
// never matches, and the site just silently can't call the API), a CRON_SECRET
// short enough to guess, or a key name that is a typo of a real one.
// Reads the file only. Never prints a value (it only ever says which KEY is
// wrong), and never talks to the network.
import fs from "node:fs";

const file = process.argv[2] ?? "secrets.json";
const REQUIRED = [
  "OPENROUTER_API_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_URL",
  "CRON_SECRET", "ALLOWED_ORIGIN", "RAHEEM_WHATSAPP_NUMBER",
];
// Without these three, signups still work but Raheem gets NO email alert that a
// code is waiting (lib/notify.ts throws, the signup carries on).
const RECOMMENDED = ["RESEND_API_KEY", "RESEND_FROM_EMAIL", "RAHEEM_NOTIFY_EMAIL"];
const OPTIONAL = ["SED_SH_API_KEY", "OPENROUTER_API_BASE"];
// Set in wrangler.jsonc "vars" — uploading them as secrets too is a conflict.
const IN_VARS = ["AVA_CONVERSATION_MODEL", "DEFAULT_AUTO_AI_MODEL", "MAX_PDF_SIZE_BYTES", "SUPABASE_PDF_BUCKET"];
const DEAD = ["PORT", "OPENROUTER_SOCKS5_PROXY"];

const errors = [];
const warnings = [];
const err = (m) => errors.push(m);
const warn = (m) => warnings.push(m);

if (!fs.existsSync(file)) {
  console.error(`No ${file}. Create it:  cp secrets.example.json ${file}   then fill in the real values.`);
  process.exit(2);
}
let data;
try {
  data = JSON.parse(fs.readFileSync(file, "utf8"));
} catch (e) {
  console.error(`${file} is not valid JSON: ${e.message}`);
  process.exit(2);
}
if (data === null || typeof data !== "object" || Array.isArray(data)) {
  console.error(`${file} must be a JSON object: { "NAME": "value", ... }`);
  process.exit(2);
}

const known = new Set([...REQUIRED, ...RECOMMENDED, ...OPTIONAL]);
for (const k of Object.keys(data)) {
  if (IN_VARS.includes(k)) err(`${k}: already set in wrangler.jsonc "vars" — remove it from ${file}`);
  else if (DEAD.includes(k)) err(`${k}: no longer exists (Express/SOCKS5 are gone) — remove it`);
  else if (!known.has(k)) err(`${k}: not a variable this Worker reads — typo? (known: ${[...known].join(", ")})`);
  else if (typeof data[k] !== "string") err(`${k}: must be a JSON string`);
}

const val = (k) => (typeof data[k] === "string" ? data[k] : undefined);
const placeholder = (v) => /REPLACE_ME|CHANGEME|YOUR_|xxxx/i.test(v);

for (const k of REQUIRED) {
  const v = val(k);
  if (v === undefined || v.trim() === "") err(`${k}: missing or empty (required)`);
}
for (const k of RECOMMENDED) {
  const v = val(k);
  if (v === undefined || v.trim() === "") warn(`${k}: not set — signups work, but no email alert reaches Raheem (all three of the RESEND/NOTIFY keys are needed)`);
}
for (const k of Object.keys(data)) {
  const v = val(k);
  if (v !== undefined && v.trim() !== "" && placeholder(v)) err(`${k}: still contains a template placeholder`);
  if (v !== undefined && v !== v.trim()) err(`${k}: has leading/trailing whitespace (a pasted newline breaks auth silently)`);
}

const origin = val("ALLOWED_ORIGIN");
if (origin && !placeholder(origin)) {
  let u;
  try { u = new URL(origin); } catch { err("ALLOWED_ORIGIN: not a valid URL"); }
  if (u) {
    if (u.protocol !== "https:") err("ALLOWED_ORIGIN: must be https://");
    if (origin !== u.origin) err(`ALLOWED_ORIGIN: must be the bare origin with NO path or trailing slash (use "${u.origin}") — CORS matches it exactly`);
    if (origin.includes("*")) err("ALLOWED_ORIGIN: wildcards are not supported (one exact origin)");
  }
}
const sb = val("SUPABASE_URL");
if (sb && !placeholder(sb)) {
  try {
    const u = new URL(sb);
    if (u.protocol !== "https:") err("SUPABASE_URL: must be https://");
    if (sb.endsWith("/")) err("SUPABASE_URL: remove the trailing slash");
    if (u.pathname !== "/") err("SUPABASE_URL: must be the project root (https://<ref>.supabase.co), no path");
  } catch { err("SUPABASE_URL: not a valid URL"); }
}
const cron = val("CRON_SECRET");
if (cron && !placeholder(cron) && cron.length < 24) err("CRON_SECRET: shorter than 24 characters — generate one:  openssl rand -hex 32");
const wa = val("RAHEEM_WHATSAPP_NUMBER");
if (wa && !placeholder(wa) && !/^\+?[0-9][0-9 ]{6,}$/.test(wa)) warn("RAHEEM_WHATSAPP_NUMBER: expected digits (optionally with a leading +)");
const key = val("SUPABASE_SERVICE_ROLE_KEY");
if (key && !placeholder(key) && key.split(".").length !== 3) warn("SUPABASE_SERVICE_ROLE_KEY: doesn't look like a JWT (three dot-separated parts) — is it the service_role key, not the anon one?");
const from = val("RESEND_FROM_EMAIL");
if (from && !placeholder(from) && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(from)) warn("RESEND_FROM_EMAIL: doesn't look like an email address");
const to = val("RAHEEM_NOTIFY_EMAIL");
if (to && !placeholder(to) && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(to)) warn("RAHEEM_NOTIFY_EMAIL: doesn't look like an email address");

const present = Object.keys(data).filter((k) => known.has(k));
console.log(`${file}: ${present.length} secrets present (${present.join(", ")})`);
if (!("SED_SH_API_KEY" in data)) console.log("note: SED_SH_API_KEY not set -> PDF malware scanning will be SKIPPED (uploads are stored with pdf_scan_status='skipped').");
for (const w of warnings) console.log(`WARN  ${w}`);
for (const e of errors) console.log(`ERROR ${e}`);
if (errors.length) {
  console.log(`\n${errors.length} error(s) — do NOT upload yet.`);
  process.exit(1);
}
console.log(`\nOK. Upload with:  npx wrangler secret bulk ${file}`);
