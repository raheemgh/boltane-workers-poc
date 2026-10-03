// api/lookup-signup.ts
//
// GET /lookup-signup?contact_number=<number> — lets the website resume
// an abandoned Ava conversation: given a contact_number, returns the
// matching pending_signups row's conversation_history + business_context
// if a resumable (pre-OTP) draft exists, so the frontend can feed that
// straight back into POST /chat as conversation_history +
// business_context and carry on exactly where the client left off.
//
// Deliberately returns { found: false } — not a 404, and not any
// distinction between "never existed" and "exists but OTP already
// sent" — for anything that isn't a resumable pre-OTP draft. This is a
// privacy choice, not an oversight: the actual chat endpoint's
// duplicate-block (see api/chat.ts, README's "Duplicate registration"
// section) IS supposed to reveal "a signup for this number already
// exists" to the person actively trying to sign up with it again, but
// this passive lookup endpoint has no such context — anyone who can
// guess or enumerate a phone number could otherwise use it to probe
// which numbers have completed signups. Uniform found:false closes
// that off.
//
// SECURITY NOTE (see also lib/supabase.ts's findPendingSignupByContactNumber
// doc comment): contact_number is a phone number, not a secret. A
// resumable draft's conversation_history can contain whatever the
// client already typed, which may include their Meta access_token or
// OpenRouter API key if they got that far before abandoning. This
// endpoint is only as safe as "knowing someone's WhatsApp number" is
// as access control — there's no additional auth on it in this pass.
// If that's not acceptable, the fix (rate limiting, a short-lived
// resume token handed to the client instead of relying on the raw
// number, etc.) needs to be decided and added explicitly — flagging
// this rather than quietly shipping it as if it were fully safe.
import type { Context } from "hono";
import { findPendingSignupByContactNumber } from "../lib/supabase";
import type { Env } from "../src/env";

export default async function handler(c: Context<{ Bindings: Env }>): Promise<Response> {
  // CORS + OPTIONS preflight are handled by the middleware in
  // src/index.ts (they used to be applyCors(res) + a 204 here).
  if (c.req.method !== "GET") {
    return c.json({ error: "Method not allowed" }, 405);
  }

  // Express gave an ARRAY for a repeated ?contact_number=a&contact_number=b
  // (-> not a string -> "" -> 400). Hono's c.req.query() would silently
  // return the first value instead, so ask for all of them and keep the
  // old behavior: exactly one value, or it counts as missing.
  const values = c.req.queries("contact_number");
  const contactNumber = values && values.length === 1 ? values[0]!.trim() : "";

  if (!contactNumber) {
    return c.json({ error: "contact_number is required" }, 400);
  }

  let row;
  try {
    row = await findPendingSignupByContactNumber(contactNumber);
  } catch (err) {
    console.error("Lookup failed:", err);
    return c.json({ error: "Lookup failed" }, 500);
  }

  if (!row || row.otp_code) {
    // No row, or OTP already sent — either way, nothing resumable.
    return c.json({ found: false }, 200);
  }

  return c.json(
    {
      found: true,
      conversation_history: row.conversation_history ?? [],
      business_context: row.business_context ?? null,
    },
    200
  );
}
