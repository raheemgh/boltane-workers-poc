// lib/notify.ts
//
// Fires one transactional email (via Resend: https://resend.com/docs/api-reference/emails/send-email)
// right after a pending_signups row gets its otp_code, so Raheem knows
// to go check WhatsApp instead of having to watch the Supabase table
// live. This is a "nice to know" side effect, not part of the
// verification mechanism itself — the code living in Supabase is the
// source of truth regardless of whether this email arrives.
//
// Deliberately non-fatal: a failed send is logged and swallowed by the
// caller (api/chat.ts), never turned into a 500 for the client, since
// the client's own turn (get the code, send it on WhatsApp) doesn't
// depend on this email at all.
export class NotifyEmailError extends Error {}

export interface SignupNotification {
  storeName: string;
  phoneNumberId: string;
  otpCode: string;
  // Best-effort (lib/meta.ts) — null if the Graph API lookup failed;
  // Raheem can still correlate on phone_number_id + code in that case.
  expectedSenderNumber: string | null;
}

export async function sendSignupNotificationEmail(
  notification: SignupNotification
): Promise<void> {
  const apiKey = process.env.RESEND_API_KEY;
  const to = process.env.RAHEEM_NOTIFY_EMAIL;
  const from = process.env.RESEND_FROM_EMAIL;

  if (!apiKey || !to || !from) {
    throw new NotifyEmailError(
      "Missing RESEND_API_KEY, RAHEEM_NOTIFY_EMAIL, or RESEND_FROM_EMAIL"
    );
  }

  const { storeName, phoneNumberId, otpCode, expectedSenderNumber } =
    notification;

  const res = await fetch("https://api.resend.com/emails", {
    method: "POST",
    signal: AbortSignal.timeout(10_000), // never hang the request on Resend
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      from,
      to,
      subject: `New Boltane signup — verify ${storeName}`,
      text: [
        `A new store finished onboarding and is waiting on manual verification.`,
        ``,
        `store_name: ${storeName}`,
        `phone_number_id: ${phoneNumberId}`,
        `code: ${otpCode}`,
        `expected sender (their own WhatsApp number): ${
          expectedSenderNumber ?? "(couldn't resolve — match on phone_number_id/code instead)"
        }`,
        ``,
        `Check WhatsApp for a text matching this code from that number, then set status = 'verified_ready' for this row in the pending_signups table (after hand-editing system_prompt first if pdf_uploaded = true).`,
      ].join("\n"),
    }),
  });

  if (!res.ok) {
    const text = await res.text();
    throw new NotifyEmailError(`Resend send failed (${res.status}): ${text}`);
  }
}
