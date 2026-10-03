// api/complete-setup.ts
//
// POST /complete-setup — the site team's post-chat form submission.
// Meta setup (phone_number_id, access_token) and legal_name all moved
// out of Ava's conversation entirely (see api/chat.ts's header
// comment) into this dedicated endpoint. This is ALSO where OTP
// generation + the Raheem notification email now happen
// (lib/otpHandoff.ts) — that used to fire at the end of the last /chat
// turn; it fires here instead now, once phone_number_id/access_token
// are actually known.
//
// Looked up by contact_number against the pending_signups row Ava
// already created during the chat conversation. Two rejection cases,
// both surfaced with a clear error rather than a generic 500:
//   - No row at all for this contact_number -> 404. The client needs
//     to go through Ava's conversation first.
//   - A row exists but isn't at status 'chat_complete' -> 409. Either
//     the conversation isn't finished yet (still 'draft'), or this
//     form was already submitted once before (status is already
//     'otp_sent' or later) — either way, calling this endpoint again
//     right now would be wrong (re-submission would silently generate
//     a SECOND otp_code and a duplicate notification email, which is a
//     real bug, not just an edge case, so it's rejected outright
//     rather than silently re-triggering). This wasn't explicitly
//     spelled out in the spec beyond "must already exist and be past
//     the chat-completion point" — treating "already past
//     chat_complete" as equally invalid for THIS endpoint is a
//     judgment call, not a literal instruction; flagging it as such.
import type { Context } from "hono";
import type { ContentfulStatusCode } from "hono/utils/http-status";
import { readJsonBody } from "../lib/httpBody";
import { findPendingSignupByContactNumber, updatePendingSignup } from "../lib/supabase";
import { triggerOtpHandoff } from "../lib/otpHandoff";
import type { CompleteSetupRequest, CompleteSetupResponse } from "../lib/types";
import type { Env } from "../src/env";

/**
 * The final client-facing instructional message: OTP code + wait time.
 * Moved here from api/chat.ts (see that file's header comment) now
 * that OTP generation happens in this endpoint instead. Deliberately
 * takes NO pdf-related parameter — this message must read identically
 * whether or not a PDF was ever uploaded (uploading a PDF is a
 * completely separate endpoint/flow, see api/upload-pdf.ts), and
 * keeping this function's signature free of any pdf_uploaded input is
 * what makes that structurally guaranteed rather than just "currently
 * true." See tests/completeSetup.test.ts for the regression test.
 */
function buildFinalInstructionalMessage(
  raheemWhatsAppNumber: string,
  code: string
): string {
  return [
    `Almost done! One last step: send this exact code as a normal WhatsApp text message, from your own phone on this number, to ${raheemWhatsAppNumber}. We'll activate your bot within 24 hours.`,
    `Code: ${code}`,
    `شبه انتهينا! خطوة أخيرة: أرسل هذا الرمز بالضبط كرسالة واتساب عادية، من هاتفك على هذا الرقم، إلى ${raheemWhatsAppNumber}. سنقوم بتفعيل البوت الخاص بك خلال 24 ساعة.`,
    `الرمز: ${code}`,
  ].join("\n\n");
}

export interface SetupInput {
  contactNumber: string;
  legalName: string;
  phoneNumberId: string;
  accessToken: string;
  raheemWhatsAppNumber: string;
}

/** What finishSetup() produces — the Durable Object serializes this
 *  back to the Worker, which relays it as the HTTP response. */
export interface SetupResult {
  status: number;
  body: CompleteSetupResponse | { error: string };
}

// The double-submit guard used to be a module-level in-memory Set
// (inFlightSetups) — correct for one long-running process, but nothing
// on Workers guarantees two near-simultaneous requests for the same
// contact_number reach the same isolate. It now lives in the SetupLock
// Durable Object (src/setupLock.ts): one instance per contact_number,
// globally, and the REAL work below (finishSetup) runs INSIDE that
// instance, so the lock is held for the true duration of the
// read-status -> update -> OTP-handoff sequence, exactly like the old
// Set's add()/finally-delete() window (Stage 3 plan, "Option A").
export default async function handler(c: Context<{ Bindings: Env }>): Promise<Response> {
  if (c.req.method !== "POST") {
    return c.json({ error: "Method not allowed" }, 405);
  }

  const raheemWhatsAppNumber = process.env.RAHEEM_WHATSAPP_NUMBER;
  if (!raheemWhatsAppNumber) {
    return c.json({ error: "Server misconfigured: missing RAHEEM_WHATSAPP_NUMBER" }, 500);
  }

  const parsed = await readJsonBody(c.req.raw);
  if (!parsed.ok) {
    // 413 over the body cap (lib/httpBody.ts), 400 for malformed JSON.
    return parsed.tooLarge
      ? c.json({ error: "Request body too large" }, 413)
      : c.json({ error: "Invalid request" }, 400);
  }
  const body = (parsed.value ?? {}) as Partial<CompleteSetupRequest>;
  const contactNumber =
    typeof body.contact_number === "string" ? body.contact_number.trim() : "";
  const legalName = typeof body.legal_name === "string" ? body.legal_name.trim() : "";
  const phoneNumberId =
    typeof body.phone_number_id === "string" ? body.phone_number_id.trim() : "";
  const accessToken =
    typeof body.access_token === "string" ? body.access_token.trim() : "";

  // No validation beyond non-empty, per spec — legal_name in
  // particular is stored exactly as given, no format/legal-registry
  // checking of any kind.
  const missingFields: string[] = [];
  if (!contactNumber) missingFields.push("contact_number");
  if (!legalName) missingFields.push("legal_name");
  if (!phoneNumberId) missingFields.push("phone_number_id");
  if (!accessToken) missingFields.push("access_token");
  if (missingFields.length > 0) {
    return c.json({ error: `Missing required field(s): ${missingFields.join(", ")}` }, 400);
  }

  const input: SetupInput = {
    contactNumber,
    legalName,
    phoneNumberId,
    accessToken,
    raheemWhatsAppNumber,
  };

  // Same contact_number -> same Durable Object instance, anywhere on
  // Cloudflare's network. The DO answers 409 if a setup for this
  // number is already in flight; otherwise it runs finishSetup() and
  // releases the lock in its own `finally` — no separate release call.
  const stub = c.env.LOCK.get(c.env.LOCK.idFromName(contactNumber) as never);
  const lockRes = await stub.fetch(
    new Request("https://lock/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    })
  );
  const result = (await lockRes.json()) as SetupResult["body"];
  return c.json(result, lockRes.status as ContentfulStatusCode);
}

/**
 * The actual setup work. Runs inside SetupLock (src/setupLock.ts) with
 * the lock held. Returns a status + body instead of writing to a
 * response object, so it has no dependency on any HTTP framework and
 * can cross the Durable Object boundary as plain JSON.
 */
export async function finishSetup(input: SetupInput): Promise<SetupResult> {
  const { contactNumber, legalName, phoneNumberId, accessToken, raheemWhatsAppNumber } = input;

  let pending;
  try {
    pending = await findPendingSignupByContactNumber(contactNumber);
  } catch (err) {
    console.error("contact_number lookup failed:", err);
    return { status: 500, body: { error: "Failed to look up signup" } };
  }

  if (!pending) {
    return {
      status: 404,
      body: {
        error:
          "No signup found for this contact_number. Complete the onboarding conversation first.",
      },
    };
  }

  if (pending.status !== "chat_complete") {
    const detail =
      pending.status === "draft"
        ? "This signup's conversation isn't finished yet."
        : "Setup has already been completed for this contact_number.";
    return { status: 409, body: { error: detail } };
  }

  try {
    await updatePendingSignup(pending.id!, {
      legal_name: legalName,
      phone_number_id: phoneNumberId,
      access_token: accessToken,
    });

    const { code } = await triggerOtpHandoff({
      pendingId: pending.id!,
      storeName: pending.store_name!,
      phoneNumberId,
      accessToken,
    });

    const response: CompleteSetupResponse = {
      reply: buildFinalInstructionalMessage(raheemWhatsAppNumber, code),
    };
    return { status: 200, body: response };
  } catch (err) {
    console.error("complete-setup finalize failed:", err);
    return { status: 500, body: { error: "Failed to complete setup" } };
  }
}
