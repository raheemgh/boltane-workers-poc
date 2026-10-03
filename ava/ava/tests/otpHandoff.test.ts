// tests/otpHandoff.test.ts
//
// Unit tests for the OTP-generation + Raheem-notification logic
// extracted into its own module now that it's triggered from
// api/complete-setup.ts instead of inline at the end of api/chat.ts.
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../lib/supabase", () => ({
  updatePendingSignup: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("../lib/meta", () => ({
  resolveDisplayPhoneNumber: vi.fn().mockResolvedValue("+15550001111"),
}));

vi.mock("../lib/notify", () => ({
  sendSignupNotificationEmail: vi.fn().mockResolvedValue(undefined),
  NotifyEmailError: class NotifyEmailError extends Error {},
}));

import { triggerOtpHandoff } from "../lib/otpHandoff";
import { updatePendingSignup } from "../lib/supabase";
import { resolveDisplayPhoneNumber } from "../lib/meta";
import { sendSignupNotificationEmail, NotifyEmailError } from "../lib/notify";

beforeEach(() => {
  vi.clearAllMocks();
});

describe("triggerOtpHandoff", () => {
  it("generates a 12-digit code, persists it with status 'otp_sent', and returns the code", async () => {
    const { code } = await triggerOtpHandoff({
      pendingId: "row-1",
      storeName: "Sample Store",
      phoneNumberId: "109364823947271",
      accessToken: "meta-token-abc",
    });

    expect(code).toMatch(/^\d{12}$/);
    expect(updatePendingSignup).toHaveBeenCalledTimes(1);
    const [id, patch] = vi.mocked(updatePendingSignup).mock.calls[0];
    expect(id).toBe("row-1");
    expect(patch.otp_code).toBe(code);
    expect(patch.status).toBe("otp_sent");
    expect(patch.otp_sent_at).toBeTypeOf("string");
  });

  it("resolves a display number and sends the notification email with it", async () => {
    await triggerOtpHandoff({
      pendingId: "row-1",
      storeName: "Sample Store",
      phoneNumberId: "109364823947271",
      accessToken: "meta-token-abc",
    });

    expect(resolveDisplayPhoneNumber).toHaveBeenCalledWith(
      "109364823947271",
      "meta-token-abc"
    );
    expect(sendSignupNotificationEmail).toHaveBeenCalledTimes(1);
    const call = vi.mocked(sendSignupNotificationEmail).mock.calls[0][0];
    expect(call.storeName).toBe("Sample Store");
    expect(call.phoneNumberId).toBe("109364823947271");
    expect(call.expectedSenderNumber).toBe("+15550001111");
  });

  it("a failed notification email is non-fatal — the code is still returned, the DB write already succeeded", async () => {
    vi.mocked(sendSignupNotificationEmail).mockRejectedValue(
      new NotifyEmailError("Resend down")
    );

    const { code } = await triggerOtpHandoff({
      pendingId: "row-1",
      storeName: "Sample Store",
      phoneNumberId: "109364823947271",
      accessToken: "meta-token-abc",
    });

    expect(code).toMatch(/^\d{12}$/);
    expect(updatePendingSignup).toHaveBeenCalledTimes(1); // still happened
  });

  it("a failed pending_signups update IS fatal — throws rather than pretending a code was issued", async () => {
    vi.mocked(updatePendingSignup).mockRejectedValue(new Error("Supabase down"));

    await expect(
      triggerOtpHandoff({
        pendingId: "row-1",
        storeName: "Sample Store",
        phoneNumberId: "109364823947271",
        accessToken: "meta-token-abc",
      })
    ).rejects.toThrow(/Supabase down/);
  });
});
