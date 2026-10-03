// tests/promoCodes.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../lib/supabase", () => ({
  getClient: vi.fn(),
}));

import { validateAndApplyReferralCode } from "../lib/promoCodes";
import { getClient } from "../lib/supabase";

/**
 * Builds a fake Supabase client whose .from("promo_codes") chain
 * resolves the given select-result once, and records the args of the
 * first .update() call so tests can assert on it.
 */
function makeFakeSupabase(selectResult: { data: unknown; error: unknown }) {
  const updateArgs: unknown[] = [];
  const eqArgsForUpdate: unknown[] = [];
  let updateError: unknown = null;

  const client = {
    from: vi.fn().mockReturnValue({
      select: vi.fn().mockReturnValue({
        eq: vi.fn().mockReturnValue({
          maybeSingle: vi.fn().mockResolvedValue(selectResult),
        }),
      }),
      update: vi.fn().mockImplementation((patch: unknown) => {
        updateArgs.push(patch);
        return {
          eq: vi.fn().mockImplementation((_col: string, val: unknown) => {
            eqArgsForUpdate.push(val);
            return Promise.resolve({ error: updateError });
          }),
        };
      }),
    }),
    __setUpdateError(err: unknown) {
      updateError = err;
    },
  };

  return { client, updateArgs, eqArgsForUpdate };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("validateAndApplyReferralCode — invalid cases (existence + expiry ONLY)", () => {
  it("code doesn't exist: valid: false, no update attempted", async () => {
    const { client, updateArgs } = makeFakeSupabase({ data: null, error: null });
    vi.mocked(getClient).mockReturnValue(client as never);

    const result = await validateAndApplyReferralCode("NOPE");

    expect(result).toEqual({ code: "NOPE", valid: false });
    expect(updateArgs).toHaveLength(0);
  });

  it("expired code: valid: false, regardless of use_count", async () => {
    const { client, updateArgs } = makeFakeSupabase({
      data: {
        id: "promo-1",
        code: "OLD10",
        expires_at: "2020-01-01T00:00:00.000Z",
        free_trial_max_uses: null,
        use_count: 0,
      },
      error: null,
    });
    vi.mocked(getClient).mockReturnValue(client as never);

    const result = await validateAndApplyReferralCode("OLD10");

    expect(result).toEqual({ code: "OLD10", valid: false });
    expect(updateArgs).toHaveLength(0);
  });

  it("expired code is STILL rejected even with plenty of free_trial_max_uses room left (expiry beats tier)", async () => {
    const { client, updateArgs } = makeFakeSupabase({
      data: {
        id: "promo-1b",
        code: "OLD_BUT_ROOMY",
        expires_at: "2020-01-01T00:00:00.000Z",
        free_trial_max_uses: 1000,
        use_count: 1, // nowhere near free_trial_max_uses
      },
      error: null,
    });
    vi.mocked(getClient).mockReturnValue(client as never);

    const result = await validateAndApplyReferralCode("OLD_BUT_ROOMY");

    expect(result).toEqual({ code: "OLD_BUT_ROOMY", valid: false });
    expect(updateArgs).toHaveLength(0);
  });

  it("a code whose free_trial_max_uses is long exceeded is STILL valid (no more 'exhausted' rejection) — just tier 2", async () => {
    const { client } = makeFakeSupabase({
      data: {
        id: "promo-2",
        code: "LIMITED5",
        expires_at: null,
        free_trial_max_uses: 5,
        use_count: 500, // way past free_trial_max_uses
      },
      error: null,
    });
    vi.mocked(getClient).mockReturnValue(client as never);

    const result = await validateAndApplyReferralCode("LIMITED5");

    expect(result.valid).toBe(true);
    expect(result.tier).toBe(2);
  });
});

describe("validateAndApplyReferralCode — tier boundary (free_trial_max_uses)", () => {
  it("use_count just under free_trial_max_uses -> tier 1 (full free trial, no discount_expires_at)", async () => {
    const { client, updateArgs } = makeFakeSupabase({
      data: {
        id: "promo-3",
        code: "LAUNCH25",
        expires_at: null,
        free_trial_max_uses: 100,
        use_count: 99, // 99 < 100
      },
      error: null,
    });
    vi.mocked(getClient).mockReturnValue(client as never);

    const result = await validateAndApplyReferralCode("LAUNCH25");

    expect(result.valid).toBe(true);
    expect(result.tier).toBe(1);
    expect(result.discountExpiresAt ?? null).toBeNull();
    // Still increments use_count, same as tier 2 — it's a running
    // count, not a cap that stops the code.
    expect(updateArgs[0]).toEqual({ use_count: 100 });
  });

  it("use_count exactly at free_trial_max_uses -> tier 2 (paying from day one, discount_expires_at ~3 months out), still valid", async () => {
    const { client, updateArgs, eqArgsForUpdate } = makeFakeSupabase({
      data: {
        id: "promo-4",
        code: "LAUNCH25",
        expires_at: null,
        free_trial_max_uses: 100,
        use_count: 100, // 100 >= 100 -> tier 2
      },
      error: null,
    });
    vi.mocked(getClient).mockReturnValue(client as never);

    const before = Date.now();
    const result = await validateAndApplyReferralCode("LAUNCH25");

    expect(result.valid).toBe(true);
    expect(result.tier).toBe(2);
    expect(result.discountExpiresAt).toBeTypeOf("string");

    // ~3 months from now, generously bounded (85-95 days) to avoid
    // being brittle about exact month-length edge cases.
    const discountMs = new Date(result.discountExpiresAt!).getTime();
    const daysOut = (discountMs - before) / (1000 * 60 * 60 * 24);
    expect(daysOut).toBeGreaterThan(85);
    expect(daysOut).toBeLessThan(95);

    // Still increments use_count, same mechanism as tier 1.
    expect(updateArgs[0]).toEqual({ use_count: 101 });
    expect(eqArgsForUpdate[0]).toBe("promo-4");
  });

  it("use_count one past free_trial_max_uses -> tier 2, still valid (this is the case the old design would have rejected as 'exhausted')", async () => {
    const { client } = makeFakeSupabase({
      data: {
        id: "promo-5",
        code: "LAUNCH25",
        expires_at: null,
        free_trial_max_uses: 100,
        use_count: 101,
      },
      error: null,
    });
    vi.mocked(getClient).mockReturnValue(client as never);

    const result = await validateAndApplyReferralCode("LAUNCH25");

    expect(result.valid).toBe(true);
    expect(result.tier).toBe(2);
  });

  it("null free_trial_max_uses: unlimited tier-1 redemptions — always tier 1, regardless of use_count", async () => {
    const { client } = makeFakeSupabase({
      data: {
        id: "promo-6",
        code: "FOREVER",
        expires_at: null,
        free_trial_max_uses: null,
        use_count: 999999,
      },
      error: null,
    });
    vi.mocked(getClient).mockReturnValue(client as never);

    const result = await validateAndApplyReferralCode("FOREVER");

    expect(result.valid).toBe(true);
    expect(result.tier).toBe(1);
    expect(result.discountExpiresAt ?? null).toBeNull();
  });

  it("free_trial_max_uses = 0: every redemption is immediately tier 2", async () => {
    const { client } = makeFakeSupabase({
      data: {
        id: "promo-7",
        code: "NOTRIAL",
        expires_at: null,
        free_trial_max_uses: 0,
        use_count: 0,
      },
      error: null,
    });
    vi.mocked(getClient).mockReturnValue(client as never);

    const result = await validateAndApplyReferralCode("NOTRIAL");

    expect(result.valid).toBe(true);
    expect(result.tier).toBe(2);
  });
});

describe("validateAndApplyReferralCode — Supabase error handling", () => {
  it("select error: throws (caller decides how to fail safe)", async () => {
    const { client } = makeFakeSupabase({
      data: null,
      error: { message: "connection refused" },
    });
    vi.mocked(getClient).mockReturnValue(client as never);

    await expect(validateAndApplyReferralCode("ANY")).rejects.toThrow(/connection refused/);
  });

  it("update (use_count increment) error: throws", async () => {
    const { client } = makeFakeSupabase({
      data: {
        id: "promo-8",
        code: "LAUNCH25",
        expires_at: null,
        free_trial_max_uses: null,
        use_count: 1,
      },
      error: null,
    });
    (client as unknown as { __setUpdateError: (e: unknown) => void }).__setUpdateError({
      message: "row locked",
    });
    vi.mocked(getClient).mockReturnValue(client as never);

    await expect(validateAndApplyReferralCode("LAUNCH25")).rejects.toThrow(/row locked/);
  });
});
