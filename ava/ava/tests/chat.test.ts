// tests/chat.test.ts
//
// Covers api/chat.ts's current scope: business understanding + BYOK +
// optional referral_code. Meta setup (phone_number_id, access_token,
// legal_name) and OTP generation/notification moved OUT of this file
// entirely into POST /complete-setup (see tests/completeSetup.test.ts)
// — chat.ts's completion path now only reaches status: 'chat_complete',
// never generates an otp_code, and never calls lib/meta or lib/notify
// (removed from its mocks here too, since it no longer imports them).
//
// Also covers: contact_number tracking (draft creation/reuse/dedupe
// block), referral_code -> package gating, per-call + cumulative
// token budgets.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { invokeHandler, makeReq, makeRes, type FakeReq, type FakeRes } from "./helpers/invoke";

vi.mock("../lib/supabase", () => ({
  insertPendingSignupRow: vi.fn().mockResolvedValue({ id: "row-1" }),
  updatePendingSignup: vi.fn().mockResolvedValue(undefined),
  findPendingSignupByContactNumber: vi.fn().mockResolvedValue(null),
}));

vi.mock("../lib/openrouter", () => ({
  callOpenRouterJSON: vi.fn(),
}));

vi.mock("../lib/promoCodes", () => ({
  validateAndApplyReferralCode: vi.fn(),
}));

vi.mock("../lib/trialCeiling", () => ({
  isTrialCeilingReached: vi.fn().mockResolvedValue(false),
}));

import realHandler from "../api/chat";
import {
  findPendingSignupByContactNumber,
  insertPendingSignupRow,
  updatePendingSignup,
} from "../lib/supabase";
import { callOpenRouterJSON } from "../lib/openrouter";
import { validateAndApplyReferralCode } from "../lib/promoCodes";
import { isTrialCeilingReached } from "../lib/trialCeiling";
import type { ConversationMessage, PendingSignupRow } from "../lib/types";

// Transport adapter (tests/helpers/invoke.ts): runs the real Hono handler
// and exposes `_status` / `_json` exactly like the old Express fakes did,
// so every assertion below is unchanged from the Express version.
const handler = (req: FakeReq, res: FakeRes) =>
  invokeHandler(realHandler, "/chat", {}, req, res);

// No phone_number_id/access_token — no longer part of ExtractionResult
// at all (see lib/types.ts's ExtractedOnboardingData doc comment).
const baseExtraction = {
  store_name: "Sample Store",
  ai_assistant_name: "Sami",
  business_description: "Sells handmade candles online.",
  byok: false,
  api_key: null,
  ai_model: null,
  referral_code: null as string | null,
  system_prompt: "You are Sami...",
};

beforeEach(() => {
  vi.clearAllMocks();
  process.env.OPENROUTER_API_KEY = "test-key";
  process.env.RAHEEM_WHATSAPP_NUMBER = "+15559998888";
  vi.mocked(findPendingSignupByContactNumber).mockResolvedValue(null);
  vi.mocked(isTrialCeilingReached).mockResolvedValue(false);
});

describe("POST /chat — completion (no contact_number)", () => {
  it("inserts pending_signups at status 'chat_complete', generates NO otp_code, and points the client to the next step", async () => {
    vi.mocked(callOpenRouterJSON)
      .mockResolvedValueOnce({ reply: "all set", done: true }) // conversation turn
      .mockResolvedValueOnce(baseExtraction); // extraction

    const req = makeReq({
      conversation_history: [],
      user_message: "that's everything",
    });
    const res = makeRes();

    await handler(req, res);

    expect(findPendingSignupByContactNumber).not.toHaveBeenCalled();
    expect(insertPendingSignupRow).toHaveBeenCalledTimes(1);

    const insertedRow = vi.mocked(insertPendingSignupRow).mock.calls[0][0];
    expect(insertedRow.status).toBe("chat_complete");
    expect(insertedRow).not.toHaveProperty("otp_code");
    expect(insertedRow).not.toHaveProperty("phone_number_id");
    expect(insertedRow).not.toHaveProperty("access_token");
    expect(insertedRow).not.toHaveProperty("legal_name");

    // No finalize update at all in this path — the fallback insert
    // already lands the row in its final (for this file's scope)
    // shape in one write. No OTP-related update either way.
    expect(updatePendingSignup).not.toHaveBeenCalled();

    expect(res._status).toBe(200);
    const response = res._json as { reply: string; done: boolean };
    expect(response.done).toBe(true);
    expect(response.reply).not.toMatch(/\d{12}/); // definitely no OTP code in this reply
  });

  it("no referral_code given: the inserted row defaults to package 'low-tier'", async () => {
    vi.mocked(callOpenRouterJSON)
      .mockResolvedValueOnce({ reply: "all set", done: true })
      .mockResolvedValueOnce(baseExtraction);

    const req = makeReq({ conversation_history: [], user_message: "done" });
    const res = makeRes();

    await handler(req, res);

    const insertedRow = vi.mocked(insertPendingSignupRow).mock.calls[0][0];
    expect(insertedRow.package).toBe("low-tier");
    expect(insertedRow.is_trial).toBeNull();
    expect(insertedRow.referral_code).toBeNull();
    expect(validateAndApplyReferralCode).not.toHaveBeenCalled();
  });
});

describe("POST /chat — referral_code -> package gating", () => {
  it("valid referral_code (tier 2): package becomes basic, is_trial=false, referral_code + discount_expires_at stored", async () => {
    vi.mocked(validateAndApplyReferralCode).mockResolvedValue({
      code: "LAUNCH25",
      valid: true,
      tier: 2,
      discountExpiresAt: "2026-04-11T00:00:00.000Z",
    });
    vi.mocked(callOpenRouterJSON)
      .mockResolvedValueOnce({ reply: "all set", done: true })
      .mockResolvedValueOnce({ ...baseExtraction, referral_code: "LAUNCH25" });

    const req = makeReq({ conversation_history: [], user_message: "done, code LAUNCH25" });
    const res = makeRes();

    await handler(req, res);

    expect(validateAndApplyReferralCode).toHaveBeenCalledWith("LAUNCH25");
    const insertedRow = vi.mocked(insertPendingSignupRow).mock.calls[0][0];
    expect(insertedRow.package).toBe("basic");
    expect(insertedRow.is_trial).toBe(false);
    expect(insertedRow.referral_code).toBe("LAUNCH25");
    expect(insertedRow.discount_expires_at).toBe("2026-04-11T00:00:00.000Z");
  });

  it("valid referral_code (tier 1): package becomes basic, is_trial=true, no discount_expires_at", async () => {
    vi.mocked(validateAndApplyReferralCode).mockResolvedValue({
      code: "TRIALCODE",
      valid: true,
      tier: 1,
    });
    vi.mocked(callOpenRouterJSON)
      .mockResolvedValueOnce({ reply: "all set", done: true })
      .mockResolvedValueOnce({ ...baseExtraction, referral_code: "TRIALCODE" });

    const req = makeReq({ conversation_history: [], user_message: "done, code TRIALCODE" });
    const res = makeRes();

    await handler(req, res);

    const insertedRow = vi.mocked(insertPendingSignupRow).mock.calls[0][0];
    expect(insertedRow.package).toBe("basic");
    expect(insertedRow.is_trial).toBe(true);
    expect(insertedRow.referral_code).toBe("TRIALCODE");
    expect(insertedRow.discount_expires_at).toBeNull();
  });

  it("invalid referral_code: package stays low-tier, nothing stored", async () => {
    vi.mocked(validateAndApplyReferralCode).mockResolvedValue({
      code: "EXPIRED10",
      valid: false,
    });
    vi.mocked(callOpenRouterJSON)
      .mockResolvedValueOnce({ reply: "all set", done: true })
      .mockResolvedValueOnce({ ...baseExtraction, referral_code: "EXPIRED10" });

    const req = makeReq({ conversation_history: [], user_message: "done, code EXPIRED10" });
    const res = makeRes();

    await handler(req, res);

    const insertedRow = vi.mocked(insertPendingSignupRow).mock.calls[0][0];
    expect(insertedRow.package).toBe("low-tier");
    expect(insertedRow.referral_code).toBeNull();
  });

  it("referral validation throws: fails safe to invalid rather than failing the whole signup", async () => {
    vi.mocked(validateAndApplyReferralCode).mockRejectedValue(new Error("Supabase down"));
    vi.mocked(callOpenRouterJSON)
      .mockResolvedValueOnce({ reply: "all set", done: true })
      .mockResolvedValueOnce({ ...baseExtraction, referral_code: "WHATEVER" });

    const req = makeReq({ conversation_history: [], user_message: "done" });
    const res = makeRes();

    await handler(req, res);

    const insertedRow = vi.mocked(insertPendingSignupRow).mock.calls[0][0];
    expect(insertedRow.package).toBe("low-tier");
    expect(insertedRow.referral_code).toBeNull();
    expect(res._status).toBe(200);
    const response = res._json as { done: boolean };
    expect(response.done).toBe(true); // signup still completes
  });

  it("top-level referral_code takes priority over the AI-extracted one (bug fix: extraction was never meant to be the primary path)", async () => {
    vi.mocked(validateAndApplyReferralCode).mockResolvedValue({
      code: "TOPLEVEL1",
      valid: true,
      tier: 2,
      discountExpiresAt: "2026-04-11T00:00:00.000Z",
    });
    vi.mocked(callOpenRouterJSON)
      .mockResolvedValueOnce({ reply: "all set", done: true })
      // The AI extracted a DIFFERENT code — this must be ignored in
      // favor of the top-level field.
      .mockResolvedValueOnce({ ...baseExtraction, referral_code: "AI_EXTRACTED_DIFFERENT" });

    const req = makeReq({
      conversation_history: [],
      user_message: "done",
      referral_code: "TOPLEVEL1",
    });
    const res = makeRes();

    await handler(req, res);

    expect(validateAndApplyReferralCode).toHaveBeenCalledWith("TOPLEVEL1");
    expect(validateAndApplyReferralCode).not.toHaveBeenCalledWith("AI_EXTRACTED_DIFFERENT");

    const insertedRow = vi.mocked(insertPendingSignupRow).mock.calls[0][0];
    expect(insertedRow.package).toBe("basic");
    expect(insertedRow.referral_code).toBe("TOPLEVEL1");
  });

  it("falls back to the AI-extracted referral_code only when no top-level field was sent", async () => {
    vi.mocked(validateAndApplyReferralCode).mockResolvedValue({
      code: "FALLBACK1",
      valid: true,
      tier: 2,
      discountExpiresAt: "2026-04-11T00:00:00.000Z",
    });
    vi.mocked(callOpenRouterJSON)
      .mockResolvedValueOnce({ reply: "all set", done: true })
      .mockResolvedValueOnce({ ...baseExtraction, referral_code: "FALLBACK1" });

    const req = makeReq({
      conversation_history: [],
      user_message: "done, code FALLBACK1",
      // no top-level referral_code field at all
    });
    const res = makeRes();

    await handler(req, res);

    expect(validateAndApplyReferralCode).toHaveBeenCalledWith("FALLBACK1");
    const insertedRow = vi.mocked(insertPendingSignupRow).mock.calls[0][0];
    expect(insertedRow.package).toBe("basic");
  });
});

describe("POST /chat — global trial ceiling safety net (lib/trialCeiling.ts)", () => {
  it("under ceiling: a valid referral_code still grants basic normally", async () => {
    vi.mocked(isTrialCeilingReached).mockResolvedValue(false);
    vi.mocked(validateAndApplyReferralCode).mockResolvedValue({
      code: "LAUNCH25",
      valid: true,
      tier: 2,
      discountExpiresAt: "2026-04-11T00:00:00.000Z",
    });
    vi.mocked(callOpenRouterJSON)
      .mockResolvedValueOnce({ reply: "all set", done: true })
      .mockResolvedValueOnce(baseExtraction);

    const req = makeReq({
      conversation_history: [],
      user_message: "done",
      referral_code: "LAUNCH25",
    });
    const res = makeRes();

    await handler(req, res);

    expect(isTrialCeilingReached).toHaveBeenCalledTimes(1);
    expect(validateAndApplyReferralCode).toHaveBeenCalledWith("LAUNCH25");
    const insertedRow = vi.mocked(insertPendingSignupRow).mock.calls[0][0];
    expect(insertedRow.package).toBe("basic");
    expect(insertedRow.referral_code).toBe("LAUNCH25");
  });

  it("at ceiling: an otherwise-valid referral_code is treated as invalid — package low-tier, code's use_count never touched", async () => {
    vi.mocked(isTrialCeilingReached).mockResolvedValue(true);
    vi.mocked(callOpenRouterJSON)
      .mockResolvedValueOnce({ reply: "all set", done: true })
      .mockResolvedValueOnce(baseExtraction);

    const req = makeReq({
      conversation_history: [],
      user_message: "done",
      referral_code: "LAUNCH25",
    });
    const res = makeRes();

    await handler(req, res);

    expect(isTrialCeilingReached).toHaveBeenCalledTimes(1);
    // The code's own validation (and use_count increment) is skipped
    // entirely once the ceiling is already hit — no point consuming a
    // limited use for a signup that won't get 'basic' anyway.
    expect(validateAndApplyReferralCode).not.toHaveBeenCalled();

    const insertedRow = vi.mocked(insertPendingSignupRow).mock.calls[0][0];
    expect(insertedRow.package).toBe("low-tier");
    expect(insertedRow.referral_code).toBeNull();
    expect(res._status).toBe(200);
    const response = res._json as { done: boolean };
    expect(response.done).toBe(true); // signup still completes, just without the discount
  });

  it("no referral_code at all: the ceiling is never even checked", async () => {
    vi.mocked(callOpenRouterJSON)
      .mockResolvedValueOnce({ reply: "all set", done: true })
      .mockResolvedValueOnce(baseExtraction);

    const req = makeReq({ conversation_history: [], user_message: "done" });
    const res = makeRes();

    await handler(req, res);

    expect(isTrialCeilingReached).not.toHaveBeenCalled();
  });
});

describe("POST /chat — token budgets", () => {
  it("passes REPLY_MAX_OUTPUT_TOKENS to the conversation call and EXTRACTION_MAX_OUTPUT_TOKENS to the extraction call", async () => {
    vi.mocked(callOpenRouterJSON)
      .mockResolvedValueOnce({ reply: "all set", done: true })
      .mockResolvedValueOnce(baseExtraction);

    const req = makeReq({ conversation_history: [], user_message: "done" });
    const res = makeRes();

    await handler(req, res);

    const calls = vi.mocked(callOpenRouterJSON).mock.calls;
    expect(calls[0][0]).toMatchObject({ maxOutputTokens: 200 });
    expect(calls[1][0]).toMatchObject({ maxOutputTokens: 1200 });
  });

  it("cumulative budget already exhausted: no LLM call at all, responds budgetExceeded:true", async () => {
    // ~5000 assistant tokens already "spent" (chars/4 estimate) before
    // this turn even starts — comfortably over budget for a 200-token
    // reply call.
    const longHistory: ConversationMessage[] = [
      { role: "assistant", content: "x".repeat(21000) },
    ];

    const req = makeReq({
      conversation_history: longHistory,
      user_message: "hello",
    });
    const res = makeRes();

    await handler(req, res);

    expect(callOpenRouterJSON).not.toHaveBeenCalled();
    expect(res._status).toBe(200);
    const response = res._json as { done: boolean; budgetExceeded?: boolean; reply: string };
    expect(response.budgetExceeded).toBe(true);
    expect(response.done).toBe(true);
    expect(response.reply).toContain("+15559998888"); // RAHEEM_WHATSAPP_NUMBER
  });

  it("budget crosses the line only after this turn's reply: extraction is skipped, only the reply call happened", async () => {
    // Just under the reply-call threshold: budget - REPLY_MAX_OUTPUT_TOKENS
    // minus a small margin, so the reply call itself is allowed, but
    // adding the reply's own (estimated) tokens pushes cumulative usage
    // past budget before the extraction call would be allowed.
    const almostFullHistory: ConversationMessage[] = [
      { role: "assistant", content: "x".repeat(4 * (5000 - 200 - 50)) },
    ];
    // The reply itself is long enough that its estimated tokens finish
    // off the remaining budget.
    const longReplyText = "y".repeat(4 * 300);
    vi.mocked(callOpenRouterJSON).mockResolvedValueOnce({
      reply: longReplyText,
      done: true,
    });

    const req = makeReq({
      conversation_history: almostFullHistory,
      user_message: "wrap it up",
    });
    const res = makeRes();

    await handler(req, res);

    // Only the reply call happened — extraction never ran.
    expect(callOpenRouterJSON).toHaveBeenCalledTimes(1);
    expect(insertPendingSignupRow).not.toHaveBeenCalled();

    const response = res._json as { budgetExceeded?: boolean; done: boolean };
    expect(response.budgetExceeded).toBe(true);
    expect(response.done).toBe(true);
  });
});

describe("POST /chat — contact_number tracking", () => {
  it("no existing row: creates a draft, updates its conversation_history after the turn, then finalizes onto the SAME row at status 'chat_complete' (no second insert, no OTP)", async () => {
    vi.mocked(findPendingSignupByContactNumber).mockResolvedValue(null);
    vi.mocked(insertPendingSignupRow).mockResolvedValue({ id: "draft-1" } as never);
    vi.mocked(callOpenRouterJSON)
      .mockResolvedValueOnce({ reply: "all set", done: true })
      .mockResolvedValueOnce(baseExtraction);

    const req = makeReq({
      conversation_history: [],
      user_message: "I want a bot",
      business_context: { contact_number: "+963900000000", project_name: "Sample Store" },
    });
    const res = makeRes();

    await handler(req, res);

    // Exactly one insert (the fresh draft) — the done:true finalize
    // path must UPDATE that same row, not insert a second one.
    expect(insertPendingSignupRow).toHaveBeenCalledTimes(1);
    expect(vi.mocked(insertPendingSignupRow).mock.calls[0][0]).toMatchObject({
      contact_number: "+963900000000",
      status: "draft",
    });

    // 2 updates now (was 3 before OTP moved out): conversation_history
    // after the turn, then core fields + status:'chat_complete' at
    // finalize. No otp_code update — that only happens in
    // POST /complete-setup now.
    expect(updatePendingSignup).toHaveBeenCalledTimes(2);
    const calls = vi.mocked(updatePendingSignup).mock.calls;
    expect(calls.every(([id]) => id === "draft-1")).toBe(true);

    const [, historyPatch] = calls[0];
    expect(Array.isArray(historyPatch.conversation_history)).toBe(true);

    const [, finalizePatch] = calls[1];
    expect(finalizePatch.status).toBe("chat_complete");
    expect(finalizePatch.package).toBe("low-tier"); // no referral_code given
    expect(finalizePatch.contact_number).toBe("+963900000000");
    expect(finalizePatch).not.toHaveProperty("otp_code");
    expect(finalizePatch).not.toHaveProperty("phone_number_id");
    expect(finalizePatch).not.toHaveProperty("access_token");

    expect(res._status).toBe(200);
    const response = res._json as { reply: string };
    expect(response.reply).not.toMatch(/\d{12}/);
  });

  it("existing draft (otp_code not yet set): reuses/updates it instead of inserting a duplicate", async () => {
    const existing: PendingSignupRow = {
      id: "draft-2",
      contact_number: "+963900000000",
      business_context: { project_name: "Old Name" },
      conversation_history: [],
      status: "draft",
    };
    vi.mocked(findPendingSignupByContactNumber).mockResolvedValue(existing);
    vi.mocked(callOpenRouterJSON).mockResolvedValueOnce({
      reply: "tell me more",
      done: false,
    });

    const req = makeReq({
      conversation_history: [],
      user_message: "still here",
      business_context: { contact_number: "+963900000000" },
    });
    const res = makeRes();

    await handler(req, res);

    expect(insertPendingSignupRow).not.toHaveBeenCalled();
    // Free overwrite: one update to merge context/history, one more
    // after the turn's reply comes back — both against the SAME
    // existing row, never a new one.
    expect(updatePendingSignup).toHaveBeenCalledTimes(2);
    const calls = vi.mocked(updatePendingSignup).mock.calls;
    expect(calls.every(([id]) => id === "draft-2")).toBe(true);

    // business_context merge preserves the previously-known field.
    const [, mergePatch] = calls[0];
    expect(mergePatch.business_context).toMatchObject({ project_name: "Old Name" });

    expect(res._status).toBe(200);
    const response = res._json as { reply: string; done: boolean };
    expect(response.done).toBe(false);
  });

  it("existing row with otp_code already set: blocks with a support message, makes no LLM call, touches no row", async () => {
    const alreadySent: PendingSignupRow = {
      id: "row-done",
      contact_number: "+963900000000",
      status: "otp_sent",
      otp_code: "999988887777",
      otp_sent_at: "2026-01-01T00:00:00.000Z",
    };
    vi.mocked(findPendingSignupByContactNumber).mockResolvedValue(alreadySent);

    const req = makeReq({
      conversation_history: [],
      user_message: "hi again",
      business_context: { contact_number: "+963900000000" },
    });
    const res = makeRes();

    await handler(req, res);

    expect(callOpenRouterJSON).not.toHaveBeenCalled();
    expect(insertPendingSignupRow).not.toHaveBeenCalled();
    expect(updatePendingSignup).not.toHaveBeenCalled();

    expect(res._status).toBe(200);
    const response = res._json as { reply: string; done: boolean; blocked?: boolean };
    expect(response.blocked).toBe(true);
    expect(response.done).toBe(true);
    expect(response.reply).toContain("+15559998888"); // RAHEEM_WHATSAPP_NUMBER
  });
});
