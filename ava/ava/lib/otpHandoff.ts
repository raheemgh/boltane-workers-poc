// lib/otpHandoff.ts
//
// The manual-OTP handoff: generate a plaintext code, persist it onto
// the pending_signups row (status -> 'otp_sent'), and fire a
// best-effort notification email to Raheem. This used to run inline at
// the end of api/chat.ts's completion turn; it now runs from
// api/complete-setup.ts instead, once phone_number_id/access_token are
// actually known (Meta setup moved out of Ava's conversation — see
// that file and README's "Meta setup / complete-setup" section).
// Factored out into its own module so both call sites (well, just
// api/complete-setup.ts now, but kept separate for testability and in
// case anything else ever needs to trigger this) share the exact same
// logic rather than risking two copies drifting apart.
import { generateOtpCode } from "./otpCode";
import { resolveDisplayPhoneNumber } from "./meta";
import { sendSignupNotificationEmail, NotifyEmailError } from "./notify";
import { updatePendingSignup } from "./supabase";

export interface OtpHandoffParams {
  pendingId: string;
  storeName: string;
  phoneNumberId: string;
  accessToken: string;
}

/**
 * Generates the code, persists it (otp_code/otp_sent_at/status), then
 * best-effort notifies Raheem. The DB write is NOT best-effort — if it
 * fails, this throws and the caller should surface a real error rather
 * than telling the client a code was issued when it wasn't. The
 * notification email IS best-effort — a failure there is logged and
 * swallowed, same reasoning as before: the row + code are already
 * saved, a missed email just means Raheem has to notice it in Supabase
 * instead of his inbox.
 */
export async function triggerOtpHandoff(
  params: OtpHandoffParams
): Promise<{ code: string }> {
  const { pendingId, storeName, phoneNumberId, accessToken } = params;

  const code = generateOtpCode();
  const otpSentAt = new Date().toISOString();
  await updatePendingSignup(pendingId, {
    otp_code: code,
    otp_sent_at: otpSentAt,
    status: "otp_sent",
  });

  // Best-effort, non-fatal: resolves a human-readable number for the
  // notification email only. Never blocks the caller's response.
  const displayNumber = await resolveDisplayPhoneNumber(phoneNumberId, accessToken);

  try {
    await sendSignupNotificationEmail({
      storeName,
      phoneNumberId,
      otpCode: code,
      expectedSenderNumber: displayNumber,
    });
  } catch (notifyErr) {
    if (notifyErr instanceof NotifyEmailError) {
      console.error("Raheem notification email failed:", notifyErr);
    } else {
      console.error("Unexpected notification error:", notifyErr);
    }
  }

  return { code };
}
