// lib/otpCode.ts
//
// OTP is now fully MANUAL (see README): no hashing, no expiry, no
// attempt-tracking, no automated send. This file only generates the
// plaintext 12-digit code that gets stored on the pending_signups row
// and shown to the client to send, as a normal WhatsApp text, to
// Raheem's own number. Raheem matches it by eye and flips the row to
// 'verified_ready' himself in the Supabase table editor.
import { randomInt } from "crypto";

export const OTP_CODE_LENGTH = 12;

export function generateOtpCode(): string {
  const max = 10 ** OTP_CODE_LENGTH; // 1e12 — well under Number.MAX_SAFE_INTEGER
  const n = randomInt(0, max); // cryptographically secure, not Math.random()
  return n.toString().padStart(OTP_CODE_LENGTH, "0");
}
