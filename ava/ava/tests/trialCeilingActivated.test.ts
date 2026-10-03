// tests/trialCeilingActivated.test.ts — in-memory fake with REAL filtering,
// so this proves the double-count is gone (the count-based fake in
// trialCeiling.test.ts can't see it).
import { describe, it, expect, vi } from "vitest";

const tables: Record<string, Array<Record<string, unknown>>> = {
  // ONE customer: Ava's staging row (kept, status 'activated') + the stores copy
  pending_signups: [
    { package: "basic", is_trial: true, status: "activated" },
    { package: "basic", is_trial: true, status: "otp_sent" }, // a different, in-flight trial
    { package: "basic", is_trial: false, status: "otp_sent" }, // tier 2, never counts
  ],
  stores: [{ package: "basic", is_trial: true }],
};

vi.mock("../lib/supabase", () => ({
  getClient: () => ({
    from: (t: string) => {
      const filters: Array<[string, "eq" | "neq", unknown]> = [];
      const q: Record<string, unknown> = {
        select: () => q,
        eq: (c: string, v: unknown) => { filters.push([c, "eq", v]); return q; },
        neq: (c: string, v: unknown) => { filters.push([c, "neq", v]); return q; },
        then: (res: (v: unknown) => unknown) =>
          res({
            count: tables[t].filter((r) =>
              filters.every(([c, op, v]) => (op === "eq" ? r[c] === v : r[c] !== v))
            ).length,
            error: null,
          }),
      };
      return q;
    },
  }),
}));

import { countActiveTrialsAcrossSystem } from "../lib/trialCeiling";

describe("trial ceiling counts each customer once", () => {
  it("activated pending row + its stores copy = 1, plus one in-flight trial = 2", async () => {
    expect(await countActiveTrialsAcrossSystem()).toBe(2);
  });
});
