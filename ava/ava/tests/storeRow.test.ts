// tests/storeRow.test.ts
//
// Covers the original polarity guard ("write a quick test that inserts
// a non-BYOK row and asserts is_api_free=true, api_key=null,
// ai_model=null, plus a BYOK row asserting the opposite pattern") plus
// the referral-code package/tier gating: package defaults to 'low-tier'
// and only becomes 'basic' with a validated referral_code — a valid
// code is always tier 1 (is_trial=true, full free trial) or tier 2
// (is_trial=false, paying from day one with a 3-month discount) — see
// lib/promoCodes.ts for the tier decision itself (tested separately in
// tests/promoCodes.test.ts).
//
// buildStoreRow() no longer computes phone_number_id/access_token/
// legal_name at all — those come from POST /complete-setup instead
// (see lib/storeRow.ts's header comment). The activation round-trip
// test below simulates that two-step process explicitly: build the
// "chat" portion, then merge in what /complete-setup would add, before
// calling toStoreRow(). Run with: npm test
import { describe, it, expect } from "vitest";
import {
  buildDraftSignupRow,
  buildPendingSignupRow,
  buildStoreRow,
  toStoreRow,
} from "../lib/storeRow";
import type {
  ExtractionResult,
  PendingSignupRow,
  ReferralApplication,
  StoreRow,
} from "../lib/types";

const base: ExtractionResult = {
  store_name: "Sample Store",
  ai_assistant_name: "Sami",
  business_description: "Sells handmade candles online.",
  byok: false,
  api_key: null,
  ai_model: null,
  referral_code: null,
  system_prompt: "You are Sami, the assistant for Sample Store...",
};

// What POST /complete-setup would add on top of the chat-derived
// fields, in a real two-step flow.
const completeSetupFields = {
  phone_number_id: "1234567890",
  access_token: "meta-token-abc",
  legal_name: "Sample Store LLC",
};

const tier1Referral: ReferralApplication = {
  code: "LAUNCH25",
  valid: true,
  tier: 1,
};

const tier2Referral: ReferralApplication = {
  code: "LAUNCH25",
  valid: true,
  tier: 2,
  discountExpiresAt: "2026-04-11T00:00:00.000Z",
};

const invalidReferral: ReferralApplication = { code: "EXPIRED10", valid: false };

describe("buildStoreRow — package / is_api_free polarity guard", () => {
  it("non-BYOK row, no referral: low-tier by default, platform key, no client key/model", () => {
    const row = buildStoreRow(base, "openai/gpt-4o-mini");

    expect(row.package).toBe("low-tier");
    expect(row.is_api_free).toBe(true);
    expect(row.api_key).toBeNull();
    expect(row.ai_model).toBeNull();
    expect(row.contact_number).toBeNull(); // default when not passed
    expect(row.referral_code).toBeNull();
    expect(row.is_trial).toBeNull(); // not applicable outside package='basic'
    expect(row.discount_expires_at).toBeNull();
  });

  it("does NOT compute phone_number_id/access_token/legal_name at all — those come from POST /complete-setup now", () => {
    const row = buildStoreRow(base, "openai/gpt-4o-mini");

    expect(row).not.toHaveProperty("phone_number_id");
    expect(row).not.toHaveProperty("access_token");
    expect(row).not.toHaveProperty("legal_name");
  });

  it("BYOK row, no referral: package still low-tier — package is not conditional on BYOK", () => {
    const byokData: ExtractionResult = {
      ...base,
      byok: true,
      api_key: "sk-or-client-key",
      ai_model: "anthropic/claude-sonnet-4.5",
    };

    const row = buildStoreRow(byokData, "openai/gpt-4o-mini");

    expect(row.package).toBe("low-tier");
    expect(row.is_api_free).toBe(false);
    expect(row.api_key).toBe("sk-or-client-key");
    expect(row.ai_model).toBe("anthropic/claude-sonnet-4.5");
  });

  it('BYOK + "choose automatically": falls back to the default model id', () => {
    const byokAutoData: ExtractionResult = {
      ...base,
      byok: true,
      api_key: "sk-or-client-key",
      ai_model: null,
    };

    const row = buildStoreRow(byokAutoData, "openai/gpt-4o-mini");

    expect(row.is_api_free).toBe(false);
    expect(row.ai_model).toBe("openai/gpt-4o-mini");
  });

  it("threads a given contact_number straight through, untouched", () => {
    const row = buildStoreRow(base, "openai/gpt-4o-mini", "+963900000000");
    expect(row.contact_number).toBe("+963900000000");
  });

  it("valid referral, tier 1: package becomes basic, is_trial=true, referral_code stored, no discount_expires_at", () => {
    const row = buildStoreRow(base, "openai/gpt-4o-mini", null, tier1Referral);

    expect(row.package).toBe("basic");
    expect(row.is_trial).toBe(true);
    expect(row.referral_code).toBe("LAUNCH25");
    expect(row.discount_expires_at).toBeNull(); // nothing stacked on top of a full free trial
  });

  it("valid referral, tier 2: package becomes basic, is_trial=false, referral_code + discount_expires_at stored", () => {
    const row = buildStoreRow(base, "openai/gpt-4o-mini", null, tier2Referral);

    expect(row.package).toBe("basic");
    expect(row.is_trial).toBe(false);
    expect(row.referral_code).toBe("LAUNCH25");
    expect(row.discount_expires_at).toBe("2026-04-11T00:00:00.000Z");
  });

  it("invalid referral: package stays low-tier, nothing stored (rejected attempt isn't recorded)", () => {
    const row = buildStoreRow(base, "openai/gpt-4o-mini", null, invalidReferral);

    expect(row.package).toBe("low-tier");
    expect(row.referral_code).toBeNull();
    expect(row.is_trial).toBeNull();
    expect(row.discount_expires_at).toBeNull();
  });

  it("valid referral + BYOK: package/is_trial and is_api_free are independent axes", () => {
    const byokData: ExtractionResult = {
      ...base,
      byok: true,
      api_key: "sk-or-client-key",
      ai_model: "anthropic/claude-sonnet-4.5",
    };

    const row = buildStoreRow(byokData, "openai/gpt-4o-mini", null, tier1Referral);

    expect(row.package).toBe("basic");
    expect(row.is_trial).toBe(true);
    expect(row.is_api_free).toBe(false);
    expect(row.api_key).toBe("sk-or-client-key");
  });
});

describe("pending_signups staging → activation round-trip stays in sync with buildStoreRow", () => {
  it.each([
    { label: "non-BYOK, no referral", data: base, referral: null },
    {
      label: "BYOK, tier-1 referral",
      data: { ...base, byok: true, api_key: "sk-or-client-key", ai_model: "anthropic/claude-sonnet-4.5" },
      referral: tier1Referral,
    },
    {
      label: "tier-2 referral",
      data: base,
      referral: tier2Referral,
    },
    {
      label: "BYOK + automatic model, invalid referral",
      data: { ...base, byok: true, api_key: "sk-or-client-key", ai_model: null },
      referral: invalidReferral,
    },
  ])(
    "$label: staging row (chat_complete) + complete-setup fields → toStoreRow() equals buildStoreRow() + complete-setup fields",
    ({ data, referral }) => {
      const chatFields = buildStoreRow(data, "openai/gpt-4o-mini", "+963900000000", referral);
      const direct: StoreRow = { ...chatFields, ...completeSetupFields };

      // Simulates the real two-step flow: api/chat.ts's finalize
      // (buildPendingSignupRow) followed by api/complete-setup.ts's
      // plain field update — both applied to the same staging row
      // before activation ever reads it.
      const staged: PendingSignupRow = {
        ...buildPendingSignupRow(data, "openai/gpt-4o-mini", "+963900000000", referral),
        ...completeSetupFields,
      };
      const promoted = toStoreRow(staged);

      // Proves the activation step can never drift from the two
      // finalization steps combined: all three routes end up with
      // identical StoreRow contents.
      expect(promoted).toEqual(direct);
    }
  );

  it("staging row starts with status 'chat_complete' (not 'otp_sent' — OTP now happens in POST /complete-setup)", () => {
    const staged = buildPendingSignupRow(base, "openai/gpt-4o-mini");
    expect(staged.status).toBe("chat_complete");
  });

  it("buildPendingSignupRow defaults contact_number and referral to null/low-tier when not passed", () => {
    const staged = buildPendingSignupRow(base, "openai/gpt-4o-mini");
    expect(staged.contact_number).toBeNull();
    expect(staged.package).toBe("low-tier");
    expect(staged.is_trial).toBeNull();
  });
});

describe("buildDraftSignupRow", () => {
  it("produces a bare draft row: only contact_number/business_context/conversation_history/status, no core fields", () => {
    const history = [{ role: "user" as const, content: "Hi, I want a bot" }];
    const row = buildDraftSignupRow(
      "+963900000000",
      { project_name: "Sample Store", contact_number: "+963900000000" },
      history
    );

    expect(row).toEqual({
      contact_number: "+963900000000",
      business_context: { project_name: "Sample Store", contact_number: "+963900000000" },
      conversation_history: history,
      status: "draft",
    });
  });

  it("accepts a null business_context", () => {
    const row = buildDraftSignupRow("+963900000000", null, []);
    expect(row.business_context).toBeNull();
  });
});

describe("toStoreRow — runtime guard against a malformed/unfinalized row reaching stores", () => {
  it("throws a clear error if a required field is still missing (e.g. a draft row)", () => {
    const draft: PendingSignupRow = buildDraftSignupRow("+963900000000", null, []);
    expect(() => toStoreRow(draft)).toThrow(/missing required field/i);
  });

  it("throws if the chat portion is finalized but POST /complete-setup hasn't happened yet (still missing phone_number_id/access_token)", () => {
    const chatOnly = buildPendingSignupRow(base, "openai/gpt-4o-mini", "+963900000000");
    expect(() => toStoreRow(chatOnly)).toThrow(/phone_number_id/);
    expect(() => toStoreRow(chatOnly)).toThrow(/access_token/);
  });

  it("does not throw once BOTH finalization steps have happened (the normal case)", () => {
    const staged: PendingSignupRow = {
      ...buildPendingSignupRow(base, "openai/gpt-4o-mini", "+963900000000"),
      ...completeSetupFields,
    };
    expect(() => toStoreRow(staged)).not.toThrow();
  });

  it("legal_name is NOT in the required list — activation still succeeds even if it's null", () => {
    const staged: PendingSignupRow = {
      ...buildPendingSignupRow(base, "openai/gpt-4o-mini", "+963900000000"),
      phone_number_id: completeSetupFields.phone_number_id,
      access_token: completeSetupFields.access_token,
      // legal_name deliberately omitted
    };
    expect(() => toStoreRow(staged)).not.toThrow();
    expect(toStoreRow(staged).legal_name).toBeNull();
  });
});
