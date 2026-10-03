// tests/completeSetup.test.ts
//
// Covers POST /complete-setup: the site's post-chat form that now
// carries legal_name/phone_number_id/access_token (moved out of Ava's
// conversation — see api/chat.ts) and triggers the OTP handoff
// (lib/otpHandoff.ts) that used to fire at the end of the chat.
import { describe, it, expect, vi, beforeEach } from "vitest";
import type { Env } from "../src/env";
import { makeLockNamespace } from "./helpers/lock";
import { invokeHandler, makeReq, makeRes, type FakeReq, type FakeRes } from "./helpers/invoke";

vi.mock("../lib/supabase", () => ({
  findPendingSignupByContactNumber: vi.fn(),
  updatePendingSignup: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("../lib/otpHandoff", () => ({
  triggerOtpHandoff: vi.fn(),
}));

import realHandler from "../api/complete-setup";
import { findPendingSignupByContactNumber, updatePendingSignup } from "../lib/supabase";
import { triggerOtpHandoff } from "../lib/otpHandoff";
import type { PendingSignupRow } from "../lib/types";

// Fresh lock namespace per test so one test's lock state can't leak
// into the next; every request below goes through the REAL SetupLock
// class (which is also what makes each test prove the lock is released).
let env: Env;
const handler = (req: FakeReq, res: FakeRes) =>
  invokeHandler(realHandler, "/complete-setup", env, req, res);

const chatCompleteRow: PendingSignupRow = {
  id: "row-1",
  contact_number: "+963900000000",
  status: "chat_complete",
  store_name: "Sample Store",
  system_prompt: "You are Sami...",
  package: "low-tier",
  is_api_free: true,
};

const validBody = {
  contact_number: "+963900000000",
  legal_name: "Sample Store LLC",
  phone_number_id: "109364823947271",
  access_token: "meta-token-abc",
};

beforeEach(() => {
  vi.clearAllMocks();
  env = { LOCK: makeLockNamespace() };
  process.env.RAHEEM_WHATSAPP_NUMBER = "+15559998888";
  vi.mocked(findPendingSignupByContactNumber).mockResolvedValue(chatCompleteRow);
  vi.mocked(triggerOtpHandoff).mockResolvedValue({ code: "123456789012" });
});

describe("POST /complete-setup — missing/malformed request", () => {
  it("missing fields: 400 with a clear message, no lookup attempted", async () => {
    const req = makeReq({ contact_number: "+963900000000" }); // missing the rest
    const res = makeRes();

    await handler(req, res);

    expect(res._status).toBe(400);
    const response = res._json as { error: string };
    expect(response.error).toMatch(/legal_name/);
    expect(response.error).toMatch(/phone_number_id/);
    expect(response.error).toMatch(/access_token/);
    expect(findPendingSignupByContactNumber).not.toHaveBeenCalled();
  });

  it("empty-string legal_name (whitespace only): rejected as missing, not stored", async () => {
    const req = makeReq({ ...validBody, legal_name: "   " });
    const res = makeRes();

    await handler(req, res);

    expect(res._status).toBe(400);
    const response = res._json as { error: string };
    expect(response.error).toMatch(/legal_name/);
    expect(updatePendingSignup).not.toHaveBeenCalled();
  });

  it("any non-empty legal_name is accepted VERBATIM — no format validation beyond non-empty", async () => {
    const weirdName = "  Sample Store™ — الشركة المحدودة  ";
    const req = makeReq({ ...validBody, legal_name: weirdName });
    const res = makeRes();

    await handler(req, res);

    expect(res._status).toBe(200);
    const patch = vi.mocked(updatePendingSignup).mock.calls[0][1];
    // Only surrounding whitespace is trimmed — the content itself,
    // including special characters, is stored exactly as given.
    expect(patch.legal_name).toBe(weirdName.trim());
  });
});

describe("POST /complete-setup — no matching pending_signups row", () => {
  it("no row for this contact_number at all: 404 with a clear error, nothing written, no OTP handoff", async () => {
    vi.mocked(findPendingSignupByContactNumber).mockResolvedValue(null);

    const req = makeReq(validBody);
    const res = makeRes();

    await handler(req, res);

    expect(res._status).toBe(404);
    const response = res._json as { error: string };
    expect(response.error).toMatch(/no signup found/i);
    expect(updatePendingSignup).not.toHaveBeenCalled();
    expect(triggerOtpHandoff).not.toHaveBeenCalled();
  });
});

describe("POST /complete-setup — wrong status", () => {
  it("conversation not finished yet (status 'draft'): rejected, nothing written, no OTP handoff", async () => {
    vi.mocked(findPendingSignupByContactNumber).mockResolvedValue({
      ...chatCompleteRow,
      status: "draft",
    });

    const req = makeReq(validBody);
    const res = makeRes();

    await handler(req, res);

    expect(res._status).toBe(409);
    const response = res._json as { error: string };
    expect(response.error).toMatch(/isn't finished/i);
    expect(updatePendingSignup).not.toHaveBeenCalled();
    expect(triggerOtpHandoff).not.toHaveBeenCalled();
  });

  it("already submitted once before (status 'otp_sent'): rejected as already completed, no second OTP handoff", async () => {
    vi.mocked(findPendingSignupByContactNumber).mockResolvedValue({
      ...chatCompleteRow,
      status: "otp_sent",
      otp_code: "111122223333",
    });

    const req = makeReq(validBody);
    const res = makeRes();

    await handler(req, res);

    expect(res._status).toBe(409);
    const response = res._json as { error: string };
    expect(response.error).toMatch(/already/i);
    expect(updatePendingSignup).not.toHaveBeenCalled();
    expect(triggerOtpHandoff).not.toHaveBeenCalled();
  });
});

describe("POST /complete-setup — success path", () => {
  it("status 'chat_complete': updates legal_name/phone_number_id/access_token, then triggers the OTP handoff exactly once", async () => {
    const req = makeReq(validBody);
    const res = makeRes();

    await handler(req, res);

    expect(updatePendingSignup).toHaveBeenCalledTimes(1);
    expect(updatePendingSignup).toHaveBeenCalledWith("row-1", {
      legal_name: "Sample Store LLC",
      phone_number_id: "109364823947271",
      access_token: "meta-token-abc",
    });

    expect(triggerOtpHandoff).toHaveBeenCalledTimes(1);
    expect(triggerOtpHandoff).toHaveBeenCalledWith({
      pendingId: "row-1",
      storeName: "Sample Store",
      phoneNumberId: "109364823947271",
      accessToken: "meta-token-abc",
    });

    expect(res._status).toBe(200);
    const response = res._json as { reply: string };
    expect(response.reply).toContain("123456789012"); // the code triggerOtpHandoff returned
    expect(response.reply).toContain("+15559998888"); // RAHEEM_WHATSAPP_NUMBER
  });

  it("a second call for the same contact_number after success would now see status 'otp_sent' and be rejected (no duplicate handoff)", async () => {
    // First call succeeds.
    const req1 = makeReq(validBody);
    const res1 = makeRes();
    await handler(req1, res1);
    expect(triggerOtpHandoff).toHaveBeenCalledTimes(1);

    // Simulate the row now being past chat_complete, as it would be
    // after a real successful handoff.
    vi.mocked(findPendingSignupByContactNumber).mockResolvedValue({
      ...chatCompleteRow,
      status: "otp_sent",
      otp_code: "123456789012",
    });

    const req2 = makeReq(validBody);
    const res2 = makeRes();
    await handler(req2, res2);

    expect(res2._status).toBe(409);
    expect(triggerOtpHandoff).toHaveBeenCalledTimes(1); // still just once
  });
});

describe("POST /complete-setup — final instructional message wording (item 4 invariant)", () => {
  it("is worded identically regardless of the row's pdf_uploaded status", async () => {
    async function runWithPdfUploaded(pdfUploaded: boolean | undefined) {
      vi.mocked(findPendingSignupByContactNumber).mockResolvedValue({
        ...chatCompleteRow,
        pdf_uploaded: pdfUploaded,
      });
      vi.mocked(triggerOtpHandoff).mockResolvedValue({ code: "111122223333" });

      const req = makeReq(validBody);
      const res = makeRes();
      await handler(req, res);

      const response = res._json as { reply: string };
      return response.reply.replace(/\d{12}/g, "<CODE>");
    }

    const withPdf = await runWithPdfUploaded(true);
    const withoutPdf = await runWithPdfUploaded(false);
    const undefinedPdf = await runWithPdfUploaded(undefined);

    expect(withPdf).toBe(withoutPdf);
    expect(withPdf).toBe(undefinedPdf);
  });
});

describe("POST /complete-setup — server misconfiguration", () => {
  it("missing RAHEEM_WHATSAPP_NUMBER: 500, no lookup attempted", async () => {
    delete process.env.RAHEEM_WHATSAPP_NUMBER;

    const req = makeReq(validBody);
    const res = makeRes();

    await handler(req, res);

    expect(res._status).toBe(500);
    expect(findPendingSignupByContactNumber).not.toHaveBeenCalled();
  });
});
